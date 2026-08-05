#!/usr/bin/env python3
"""Worker isolado de síntese TTS (processo filho).

Roda load+generate num processo separado do servidor FastAPI.
Se o MLX/Metal der SIGSEGV, só este processo morre — o app principal continua.

Uso:  .venv-mlx/bin/python tts_worker.py /path/to/job_config.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import unicodedata
import uuid
from pathlib import Path

# evita threads extras do BLAS atrapalhando Metal
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# diretório do projeto no path
BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from backends import generate_with_backend, resolve_backend  # noqa: E402

CHUNK_SILENCE_S = 0.25
TARGET_RMS = 0.15
PEAK_LIMIT = 0.95
DESIGN_VOICE_ID = "__design__"
OMNI_ALIASES = {"", "omnivoice", "omni", "omnivoice-bf16"}


def _write_status(path: Path, data: dict):
    """Escrita atômica do status (o pai faz poll)."""
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def _release_mlx_memory(aggressive: bool = False):
    """Devolve pool MLX/Metal ao SO — sem isto a RAM sobe a cada trecho."""
    import gc

    gc.collect()
    try:
        import mlx.core as mx
        clr = getattr(mx, "clear_cache", None)
        if clr is not None:
            clr()
    except Exception:  # noqa: BLE001
        pass
    if aggressive:
        gc.collect()
        try:
            import mlx.core as mx
            clr = getattr(mx, "clear_cache", None)
            if clr is not None:
                clr()
        except Exception:  # noqa: BLE001
            pass


def _write_wav_concat(piece_dir: Path, n: int, out_path: Path, sr: int) -> float:
    """Monta WAV final trecho a trecho (sem np.concatenate de tudo)."""
    import numpy as np
    import soundfile as sf

    total_frames = 0
    with sf.SoundFile(str(out_path), mode="w", samplerate=sr, channels=1,
                      subtype="PCM_16") as out:
        for i in range(n):
            data, _ = sf.read(str(piece_dir / f"{i}.wav"), dtype="float32")
            if getattr(data, "ndim", 1) > 1:
                data = data.mean(axis=1)
            out.write(np.asarray(data, dtype=np.float32))
            total_frames += len(data)
            del data
    return round(total_frames / sr, 1) if sr else 0.0


def _sanitize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = (text.replace("%", " por cento").replace("&", " e ")
                .replace("+", " mais ").replace("°", " graus ")
                .replace("=", " igual a ").replace("/", " ou "))
    text = re.sub(r"\s{2,}", " ", text).strip()
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def _split_text(text: str, max_chars: int = 140) -> list[str]:
    def pack(parts: list[str]) -> list[str]:
        out, cur = [], ""
        for p in parts:
            if cur and len(cur) + len(p) + 1 > max_chars:
                out.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}".strip()
        if cur:
            out.append(cur)
        return out

    def burst(c: str) -> list[str]:
        return pack(re.split(r"(?<=[,;:])\s+", c)) if len(c) > max_chars else [c]

    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]
    if not sentences:
        return []
    final = burst(sentences[0])
    for c in pack(sentences[1:]):
        final.extend(burst(c))
    final = [c for c in final if c.strip()]
    merged: list[str] = []
    for c in final:
        if merged and (len(c) < 15 or len(merged[-1]) < 15) \
                and len(merged[-1]) + len(c) + 1 <= max_chars:
            merged[-1] = f"{merged[-1]} {c}"
        else:
            merged.append(c)
    return merged


def _normalize(audio, target_rms: float = TARGET_RMS, peak_limit: float = PEAK_LIMIT):
    import numpy as np
    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0:
        return x
    rms = float(np.sqrt(np.mean(x ** 2)) or 0.0)
    if rms > 1e-8:
        x = x * (target_rms / rms)
    peak = float(np.abs(x).max() or 0.0)
    if peak > peak_limit:
        x = x * (peak_limit / peak)
    return x.astype(np.float32)


def _trim_tail_silence(audio, sr: int, limiar: float = 0.006, pad_s: float = 0.3):
    import numpy as np
    win = int(0.05 * sr)
    fim = len(audio)
    while fim > win:
        if float(np.sqrt(np.mean(audio[fim - win:fim] ** 2))) >= limiar:
            break
        fim -= win
    return audio[:min(len(audio), fim + int(pad_s * sr))]


def _fade_edges(audio, sr: int, ms: float = 8.0):
    import numpy as np
    x = np.asarray(audio, dtype=np.float32)
    n = int(sr * ms / 1000.0)
    if n <= 1 or len(x) < n * 2:
        return x
    fade = np.linspace(0, 1, n, dtype=np.float32)
    x = x.copy()
    x[:n] *= fade
    x[-n:] *= fade[::-1]
    return x


def _time_stretch(audio, speed: float, n_fft: int = 1024, hop: int = 256):
    import numpy as np
    x = np.asarray(audio, dtype=np.float32)
    if abs(speed - 1.0) < 1e-3 or x.size < n_fft * 2:
        return x
    win = np.hanning(n_fft).astype(np.float32)

    def stft(sig):
        n = 1 + (len(sig) - n_fft) // hop
        return np.stack([np.fft.rfft(sig[i * hop:i * hop + n_fft] * win) for i in range(n)], axis=1)

    def istft(D):
        frames = D.shape[1]
        out = np.zeros((frames - 1) * hop + n_fft, dtype=np.float32)
        wsum = np.zeros_like(out)
        for i in range(frames):
            seg = np.fft.irfft(D[:, i], n_fft).astype(np.float32) * win
            out[i * hop:i * hop + n_fft] += seg
            wsum[i * hop:i * hop + n_fft] += win * win
        wsum[wsum < 1e-8] = 1e-8
        return out / wsum

    D = stft(x)
    bins = D.shape[0]
    omega = 2.0 * np.pi * hop * np.arange(bins) / n_fft
    steps = np.arange(0, D.shape[1] - 1, speed)
    out = np.zeros((bins, len(steps)), dtype=np.complex64)
    phase = np.angle(D[:, 0])
    for i, stp in enumerate(steps):
        k = int(np.floor(stp))
        frac = stp - k
        mag = (1.0 - frac) * np.abs(D[:, k]) + frac * np.abs(D[:, k + 1])
        out[:, i] = mag * np.exp(1j * phase)
        dp = np.angle(D[:, k + 1]) - np.angle(D[:, k]) - omega
        dp -= 2.0 * np.pi * np.round(dp / (2.0 * np.pi))
        phase = phase + omega + dp
    y = istft(out)
    peak = float(np.abs(y).max() or 0.0)
    if peak > 0.99:
        y *= 0.99 / peak
    return y.astype(np.float32)


_NATIVE_SPEED = frozenset({
    "qwen3_tts", "qwen3_custom", "fish", "chatterbox", "kokoro",
})


def _resolve_path(model_setting: str, settings: dict, base: Path) -> str:
    be = resolve_backend(model_setting)
    if be["family"] == "omnivoice" and (
            be["is_shortcut"] or str(be["path"]).strip().lower() in OMNI_ALIASES):
        if str(settings.get("omni_precision", "bf16")).lower() == "fp32":
            return os.environ.get("TTS_ROD_OMNI_FP32", "mlx-community/OmniVoice-fp32")
        assembled = base / ".omnivoice-bf16"
        if (assembled / "model.safetensors").exists():
            return str(assembled)
        # montagem mínima (mesma lógica do app)
        from huggingface_hub import snapshot_download
        backbone_repo = os.environ.get("TTS_ROD_OMNI_BACKBONE", "mlx-community/OmniVoice-bf16")
        tok_repo = os.environ.get("TTS_ROD_OMNI_TOKENIZER", "mlx-community/OmniVoice")
        backbone = Path(snapshot_download(backbone_repo, ignore_patterns=["audio_tokenizer/*"]))
        tokrepo = Path(snapshot_download(tok_repo, allow_patterns=["audio_tokenizer/*"]))
        assembled.mkdir(exist_ok=True)
        for f in backbone.iterdir():
            if f.name == "audio_tokenizer":
                continue
            dst = assembled / f.name
            if not dst.exists():
                os.symlink(f.resolve(), dst)
        atok = assembled / "audio_tokenizer"
        if not atok.exists():
            os.symlink((tokrepo / "audio_tokenizer").resolve(), atok)
        return str(assembled)
    return be["path"]


def _voice_ref_text(voices_dir: Path, voice_id: str):
    try:
        meta = json.loads((voices_dir / f"{voice_id}.json").read_text())
        return (meta.get("ref_text") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: tts_worker.py <config.json>", file=sys.stderr)
        return 2
    cfg_path = Path(sys.argv[1])
    cfg = json.loads(cfg_path.read_text())
    status_path = Path(cfg["status_path"])
    piece_dir = Path(cfg["piece_dir"])
    outputs_dir = Path(cfg["outputs_dir"])
    voices_dir = Path(cfg["voices_dir"])
    base_dir = Path(cfg.get("base_dir") or BASE)
    piece_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    text = cfg["text"]
    voice_id = cfg.get("voice_id") or ""
    voice_path = Path(cfg["voice_path"]) if cfg.get("voice_path") else voices_dir / f"{voice_id}.wav"
    language = cfg.get("language") or "auto"
    omni = cfg.get("omni") or {}
    model_setting = cfg.get("model") or "omnivoice"
    settings = cfg.get("settings") or {}
    max_chars = int(settings.get("chunk_max_chars") or 140)

    be = resolve_backend(model_setting)
    family = be["family"]
    be_meta = be.get("meta") or {}
    label = be_meta.get("label") or be.get("id") or model_setting

    try:
        import numpy as np
        import soundfile as sf
        from mlx_audio.tts.utils import load_model

        chunks = _split_text(_sanitize_text(text), max_chars=max_chars)
        if not chunks:
            raise RuntimeError("texto vazio após limpeza")

        _write_status(status_path, {
            "status": "running", "pieces": 0, "total": len(chunks),
            "progress": {"stage": f"carregando {label}…", "backend": be.get("id"), "family": family},
        })

        path = _resolve_path(model_setting, settings, base_dir)
        model = load_model(path)
        sr = int(getattr(model, "sample_rate", 24000) or 24000)
        silence = np.zeros(int(CHUNK_SILENCE_S * sr), dtype=np.float32)

        is_design = voice_id == DESIGN_VOICE_ID
        jpath = voice_path.with_suffix(".json")
        if not is_design and jpath.exists():
            try:
                vm = json.loads(jpath.read_text())
                if vm.get("from_design"):
                    is_design = True
                    omni = {**omni, "instruct": vm.get("instruct") or omni.get("instruct"),
                            "seed": vm.get("seed", omni.get("seed"))}
            except Exception:  # noqa: BLE001
                pass

        conds = None
        ref_text = None
        ref_audio = None

        if is_design:
            pass
        elif family == "omnivoice" and voice_path.exists():
            # ref_tokens Omni no worker (sem cache entre jobs — processo morre no fim)
            from mlx_audio.tts.models.omnivoice.utils import create_voice_clone_prompt
            ref_max = float(settings.get("omni_ref_max_s") or 10.0)
            conds = create_voice_clone_prompt(
                str(voice_path), ref_text=None,
                tokenizer=model.audio_tokenizer, max_duration_s=ref_max,
            )
            ref_text = _voice_ref_text(voices_dir, voice_id)
        elif voice_path.exists():
            ref_audio = str(voice_path)
            ref_text = _voice_ref_text(voices_dir, voice_id)

        started = time.time()
        for i, chunk in enumerate(chunks):
            _write_status(status_path, {
                "status": "running", "pieces": i, "total": len(chunks),
                "progress": {"current": i + 1, "total": len(chunks),
                             "backend": be.get("id"), "family": family},
            })
            if chunk[-1] not in ".!?…":
                chunk = chunk.rstrip(" ,;:") + "."
            audio = generate_with_backend(
                model, family, chunk,
                language=language,
                ref_audio=ref_audio,
                ref_text=ref_text,
                ref_tokens=conds,
                omni=omni,
                meta=be_meta,
            )
            speed = float(omni.get("speed") or 1.0)
            if abs(speed - 1.0) > 1e-3 and family not in _NATIVE_SPEED:
                audio = _time_stretch(audio, speed)
            audio = _fade_edges(_normalize(_trim_tail_silence(audio, sr)), sr)
            if i < len(chunks) - 1:
                audio = np.concatenate([audio, silence])
            sf.write(piece_dir / f"{i}.wav", audio, sr, subtype="PCM_16")
            del audio
            _release_mlx_memory()  # sem isto o pool Metal cresce a cada trecho
            _write_status(status_path, {
                "status": "running", "pieces": i + 1, "total": len(chunks),
                "progress": {"current": i + 1, "total": len(chunks),
                             "backend": be.get("id"), "family": family},
            })

        elapsed = round(time.time() - started, 1)
        out_id = uuid.uuid4().hex[:10]
        duration = _write_wav_concat(piece_dir, len(chunks),
                                     outputs_dir / f"{out_id}.wav", sr)
        meta = {
            "id": out_id,
            "text": text,
            "voice_id": voice_id,
            "language": language,
            "backend": be.get("id"),
            "family": family,
            "num_steps": int(omni.get("num_steps") or 16),
            "guidance_scale": omni.get("guidance_scale"),
            "class_temperature": omni.get("class_temperature"),
            "instruct": omni.get("instruct") or "",
            "chunks": len(chunks),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": duration,
            "elapsed": elapsed,
            "isolated": True,
        }
        (outputs_dir / f"{out_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        # solta modelo + pool antes de sair (o SO recupera o processo, mas
        # reduz pico se o pai ainda estiver vivo e o Metal for compartilhado)
        try:
            del model, conds, ref_audio, silence
        except Exception:  # noqa: BLE001
            pass
        _release_mlx_memory(aggressive=True)
        _write_status(status_path, {
            "status": "done", "pieces": len(chunks), "total": len(chunks),
            "progress": None, "output": meta, "error": None,
        })
        return 0
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        try:
            _release_mlx_memory(aggressive=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            _write_status(status_path, {
                "status": "error", "error": err, "progress": None,
            })
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
