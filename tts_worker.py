#!/usr/bin/env python3
"""Worker isolado de síntese TTS (processo filho).

Roda load+generate num processo separado do servidor FastAPI.
Se o MLX/Metal der SIGSEGV, só este processo morre — o app principal continua.

Uso:  .venv-mlx/bin/python tts_worker.py /path/to/job_config.json
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
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
from common import (CHUNK_SILENCE_S, NATIVE_SPEED_FAMILIES, OMNI_ALIASES,  # noqa: E402
                    fade_edges, normalize, resolve_omni_source,  # noqa: E402
                    sanitize_text, split_text, time_stretch,  # noqa: E402
                    trim_tail_silence, write_wav_concat,  # noqa: E402
                    release_mlx_memory as _release_mlx_memory)  # noqa: E402

DESIGN_VOICE_ID = "__design__"


def _write_status(path: Path, data: dict):
    """Escrita atômica do status (o pai faz poll)."""
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def _resolve_path(model_setting: str, settings: dict, base: Path) -> str:
    be = resolve_backend(model_setting)
    if be["family"] == "omnivoice" and (
            be["is_shortcut"] or str(be["path"]).strip().lower() in OMNI_ALIASES):
        return resolve_omni_source(settings, base)
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

        chunks = split_text(sanitize_text(text), max_chars=max_chars)
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
            if abs(speed - 1.0) > 1e-3 and family not in NATIVE_SPEED_FAMILIES:
                audio = time_stretch(audio, speed)
            audio = fade_edges(normalize(trim_tail_silence(audio, sr)), sr)
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
        duration = write_wav_concat(piece_dir, len(chunks),
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
