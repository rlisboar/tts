#!/usr/bin/env python3
"""Worker isolado de síntese TTS (processo filho).

Roda load+generate num processo separado do servidor FastAPI.
Se o MLX/Metal der SIGSEGV, só este processo morre — o app principal continua.

Dois modos:

  .venv-mlx/bin/python tts_worker.py /path/to/job_config.json   # um job e sai
  .venv-mlx/bin/python tts_worker.py --serve                    # PERSISTENTE

O modo `--serve` (#152) existe para o LIVE: o load do modelo custa ~5-7 s e o
job-por-processo pagava isso a CADA turno. No serve o filho carrega o modelo uma
vez por SESSÃO e atende N pedidos por NDJSON em stdin/stdout, o que mantém a
isolação de crash (a razão de o worker existir) sem a recarga por turno. O job em
lote (`/api/tts/jobs`) segue no modo antigo — lá UM processo por job é desejado.
"""

from __future__ import annotations

import base64
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
                    apply_audio_fx, fade_edges, normalize,  # noqa: E402
                    resolve_omni_source, sanitize_text, split_text,  # noqa: E402
                    time_stretch, trim_tail_silence, write_wav_concat,  # noqa: E402
                    write_json_atomic,  # noqa: E402
                    release_mlx_memory as _release_mlx_memory)  # noqa: E402

DESIGN_VOICE_ID = "__design__"
PROTOCOLO = 1          # versão do protocolo do `--serve` (o pai checa)


def _write_status(path: Path, data: dict):
    """Escrita atômica do status (o pai faz poll).

    Delegado ao `write_json_atomic` do common (tmp ÚNICO no mesmo diretório,
    sem órfão em erro, modo do destino preservado). O esquema antigo — tmp de
    nome FIXO (`path.with_suffix(".tmp")`) e sem try/except — era a mesma classe
    do bug #12: dois escritores no mesmo status se atropelavam e o
    FileNotFoundError derrubava a atualização. Hoje o status é por job, mas o
    worker é multi-processo por design, então o padrão convidava ao erro.

    No modo `--serve` NÃO há arquivo de status: o reply do protocolo é o status
    (e o nome do log do filho é único por sessão, posto pelo pai).
    """
    write_json_atomic(path, data)


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


def _carrega(cfg: dict) -> dict:
    """Carrega modelo + voz de um cfg (MESMO schema do job em lote) e devolve o
    estado reutilizável.

    O lote chama isto uma vez por job (o processo morre no fim); o `--serve` uma
    vez por SESSÃO, e todo pedido seguinte reaproveita `model`/`conds` — é
    exatamente essa diferença que troca os ~7,5 s do turno isolado por ~0,2 s
    quentes.
    """
    import numpy as np
    from mlx_audio.tts.utils import load_model

    settings = cfg.get("settings") or {}
    base_dir = Path(cfg.get("base_dir") or BASE)
    voices_dir = Path(cfg["voices_dir"])
    model_setting = cfg.get("model") or "omnivoice"
    be = resolve_backend(model_setting)
    family = be["family"]
    voice_id = cfg.get("voice_id") or ""
    voice_path = (Path(cfg["voice_path"]) if cfg.get("voice_path")
                  else voices_dir / f"{voice_id}.wav")
    omni = dict(cfg.get("omni") or {})

    path = _resolve_path(model_setting, settings, base_dir)
    model = load_model(path)
    sr = int(getattr(model, "sample_rate", 24000) or 24000)

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
        # ref_tokens Omni (sem cache entre jobs no lote; o serve mantém vivo)
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

    return {
        "model": model, "sr": sr, "be": be, "family": family,
        "be_meta": be.get("meta") or {}, "settings": settings, "omni": omni,
        "language": cfg.get("language") or "auto", "voice_id": voice_id,
        "conds": conds, "ref_text": ref_text, "ref_audio": ref_audio,
        "silence": np.zeros(int(CHUNK_SILENCE_S * sr), dtype=np.float32),
    }


def _sintetiza(est: dict, texto: str, omni: dict | None = None, fx: bool = False):
    """UM trecho: `generate` + speed.

    `fx=True` acrescenta a cadeia do LOTE (trim → normalize → EQ/ganho → fade) e
    `False` deixa o áudio igual ao `app._generate_chunk` (o caminho in-process que
    o Live usa hoje), para o turno não mudar de som quando o worker entra ou
    quando ele cai para o in-process.
    """
    o = dict(omni) if omni is not None else est["omni"]
    audio = generate_with_backend(
        est["model"], est["family"], texto,
        language=est["language"],
        ref_audio=est["ref_audio"],
        ref_text=est["ref_text"],
        ref_tokens=est["conds"],
        omni=o,
        meta=est["be_meta"],
    )
    speed = float(o.get("speed") or 1.0)
    if abs(speed - 1.0) > 1e-3 and est["family"] not in NATIVE_SPEED_FAMILIES:
        audio = time_stretch(audio, speed, est["sr"])
    if fx:
        s = est["settings"]
        audio = fade_edges(apply_audio_fx(
            normalize(trim_tail_silence(audio, est["sr"])), est["sr"],
            g_low=float(s.get("audio_eq_low_db", 0.0)),
            g_mid=float(s.get("audio_eq_mid_db", 0.0)),
            g_high=float(s.get("audio_eq_high_db", 0.0)),
            gain_db=float(s.get("audio_gain_db", 0.0)),
        ), est["sr"])
    return audio


def main() -> int:
    """Modo LOTE: um job por processo (intocado — o Live não passa por aqui)."""
    if len(sys.argv) < 2:
        print("uso: tts_worker.py <config.json> | tts_worker.py --serve", file=sys.stderr)
        return 2
    cfg_path = Path(sys.argv[1])
    cfg = json.loads(cfg_path.read_text())
    status_path = Path(cfg["status_path"])
    piece_dir = Path(cfg["piece_dir"])
    outputs_dir = Path(cfg["outputs_dir"])
    piece_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    text = cfg["text"]
    voice_id = cfg.get("voice_id") or ""
    language = cfg.get("language") or "auto"
    settings = cfg.get("settings") or {}
    max_chars = int(settings.get("chunk_max_chars") or 140)

    try:
        import soundfile as sf

        chunks = split_text(sanitize_text(text), max_chars=max_chars)
        if not chunks:
            raise RuntimeError("texto vazio após limpeza")

        be = resolve_backend(cfg.get("model") or "omnivoice")
        label = be.get("meta", {}).get("label") or be.get("id")
        _write_status(status_path, {
            "status": "running", "pieces": 0, "total": len(chunks),
            "progress": {"stage": f"carregando {label}…", "backend": be.get("id"),
                         "family": be["family"]},
        })

        est = _carrega(cfg)
        sr = est["sr"]

        started = time.time()
        for i, chunk in enumerate(chunks):
            _write_status(status_path, {
                "status": "running", "pieces": i, "total": len(chunks),
                "progress": {"current": i + 1, "total": len(chunks),
                             "backend": be.get("id"), "family": est["family"]},
            })
            if chunk[-1] not in ".!?…":
                chunk = chunk.rstrip(" ,;:") + "."
            audio = _sintetiza(est, chunk, fx=True)
            if i < len(chunks) - 1:
                audio = _concat(est, audio)
            sf.write(piece_dir / f"{i}.wav", audio, sr, subtype="PCM_16")
            del audio
            _release_mlx_memory()  # sem isto o pool Metal cresce a cada trecho
            _write_status(status_path, {
                "status": "running", "pieces": i + 1, "total": len(chunks),
                "progress": {"current": i + 1, "total": len(chunks),
                             "backend": be.get("id"), "family": est["family"]},
            })

        elapsed = round(time.time() - started, 1)
        out_id = uuid.uuid4().hex[:10]
        duration = write_wav_concat(piece_dir, len(chunks),
                                    outputs_dir / f"{out_id}.wav", sr)
        omni = est["omni"]
        meta = {
            "id": out_id,
            "text": text,
            "voice_id": voice_id,
            "language": language,
            "backend": be.get("id"),
            "family": est["family"],
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
        write_json_atomic(outputs_dir / f"{out_id}.json", meta)
        # solta modelo + pool antes de sair (o SO recupera o processo, mas
        # reduz pico se o pai ainda estiver vivo e o Metal for compartilhado)
        try:
            del est
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


def _concat(est: dict, audio):
    """Acrescenta o silêncio entre trechos (mesma ordem do lote)."""
    import numpy as np
    return np.concatenate([audio, est["silence"]])


# ---------------------------------------------------------------------------
# Modo PERSISTENTE (`--serve`) — Live
# ---------------------------------------------------------------------------


def _protocolo() -> "_Protocolo":
    """stdout do filho fica 100% protocolo.

    `os.dup(1)` guarda o stdout ORIGINAL (que é o pipe do pai) e o fd 1 passa a
    apontar para o stderr: qualquer `print()` de terceiros (common/backends/mlx)
    vai para o log do pai em vez de furar o framing NDJSON.
    """
    return _Protocolo(os.fdopen(os.dup(1), "w", buffering=1))


class _Protocolo:
    def __init__(self, out):
        self._out = out
        os.dup2(2, 1)          # print() -> stderr; o protocolo usa self._out

    def envia(self, obj: dict) -> None:
        self._out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._out.flush()


def _serve() -> int:
    """Loop persistente: NDJSON por stdin, um pedido por vez.

    -> {"type":"init","config":{...}}    (schema do job: model/voice_id/settings/dirs)
    <- {"type":"ready","proto":1,"sr":...}
    -> {"type":"synth","id":N,"text":"...","omni":{...}}
    <- {"type":"ok","id":N,"sr":...,"n":...,"rms":...,"audio_b64":"<float32 LE>"}
    <- {"type":"err","id":N,"error":"Tipo: msg"}   (erro de UM pedido não mata o worker)
    -> {"type":"ping"} / <- {"type":"pong"}
    -> {"type":"close"} -> sai 0

    Erro no `init` responde `fatal` e sai 1: o pai cai no caminho in-process.
    """
    prot = _protocolo()
    est = None
    for linha in sys.stdin:
        linha = linha.strip()
        if not linha:
            continue
        try:
            msg = json.loads(linha)
        except ValueError as exc:
            prot.envia({"type": "err", "error": f"json inválido: {exc}"})
            continue
        tipo = msg.get("type")

        if tipo == "close":
            return 0
        if tipo == "ping":
            prot.envia({"type": "pong"})
            continue
        if tipo == "init":
            try:
                t0 = time.time()
                est = _carrega(msg.get("config") or {})
                _sintetiza(est, "Ok.")      # aquece/compila os kernels já no init
                prot.envia({"type": "ready", "proto": PROTOCOLO, "sr": est["sr"],
                            "family": est["family"],
                            "load_ms": round((time.time() - t0) * 1000)})
            except Exception as exc:        # noqa: BLE001
                traceback.print_exc()
                prot.envia({"type": "fatal", "error": f"{type(exc).__name__}: {exc}"})
                return 1
            continue
        if tipo == "synth":
            pedido = msg.get("id")
            if est is None:
                prot.envia({"type": "err", "id": pedido, "error": "init não feito"})
                continue
            try:
                import numpy as np
                t0 = time.perf_counter()
                audio = _sintetiza(est, msg.get("text") or "", msg.get("omni"))
                a = np.asarray(audio, dtype=np.float32).reshape(-1)
                rms = float(np.sqrt(float((a * a).mean()))) if a.size else 0.0
                prot.envia({"type": "ok", "id": pedido, "sr": est["sr"],
                            "n": int(a.size), "rms": round(rms, 6),
                            "gen_ms": round((time.perf_counter() - t0) * 1000),
                            "audio_b64": base64.b64encode(a.tobytes()).decode("ascii")})
            except Exception as exc:        # noqa: BLE001 — um pedido ruim não derruba o worker
                traceback.print_exc()
                prot.envia({"type": "err", "id": pedido,
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        prot.envia({"type": "err", "error": f"tipo desconhecido: {tipo}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(_serve() if "--serve" in sys.argv[1:] else main())