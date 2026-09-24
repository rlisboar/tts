#!/usr/bin/env python3
"""Smoke de SÍNTESE REAL — o gate para subir pinos de mlx-*/transformers.

Um bump que "instala sem erro" pode quebrar a síntese em silêncio (já aconteceu:
transformers 5.6+ zerava o áudio; ver o comentário no requirements.txt). Este
script gera áudio de verdade pelo MESMO caminho do app — `backends.resolve_backend`
+ `mlx_audio.tts.utils.load_model` + `backends.generate_with_backend` — e falha se
o áudio sair mudo/curto/NaN/clipado. Com `--stt` também transcreve (mlx-whisper) e
exige texto de volta, cobrindo o lado da transcrição.

    ./.venv-mlx/bin/python smoke_sintese.py                  # modelo padrão do app
    ./.venv-mlx/bin/python smoke_sintese.py --model kokoro   # mais rápido
    ./.venv-mlx/bin/python smoke_sintese.py --stt            # + transcrição
    ./.venv-mlx/bin/python smoke_sintese.py --json           # p/ gate automatizado

Sai 0 só com tudo verde; 1 se o áudio falhar (ou a transcrição, com --stt); 2 em
erro de uso/modelo. `--out arquivo.wav` guarda o áudio para escutar.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

RAIZ = os.path.dirname(os.path.abspath(__file__))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

TEXTO_PADRAO = "Teste de síntese do TTS Studio, um dois três, gravando agora."
# critérios: RMS de fala normal fica bem acima disso; o bug do "áudio zerado"
# dava RMS ~0 e o de "mudo/silêncio" duração ~0.
MIN_RMS = 0.005
MIN_SEGUNDOS = 0.25
PICO_MAX = 1.02


def _args():
    p = argparse.ArgumentParser(description="smoke de síntese real (não mudo)")
    p.add_argument("--model", default="", help="modelo/atalho (default: o do settings.json)")
    p.add_argument("--text", default=TEXTO_PADRAO)
    p.add_argument("--stt", action="store_true", help="também transcreve o áudio gerado")
    p.add_argument("--json", action="store_true", dest="como_json")
    p.add_argument("--out", default="", help="grava o wav aqui (default: temporário)")
    p.add_argument("--min-rms", type=float, default=MIN_RMS)
    p.add_argument("--min-segundos", type=float, default=MIN_SEGUNDOS)
    return p.parse_args()


def _settings() -> dict:
    try:
        with open(os.path.join(RAIZ, "settings.json"), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001 — sem settings: segue com o default do app
        return {}


def _caminho_do_modelo(pedido: str, fallback: str) -> str:
    """Mesma resolução do app (tts_worker._resolve_path): atalho como `omnivoice`
    aponta para o dir montado/`.omnivoice-bf16`, não para um repo do HF."""
    try:
        from tts_worker import _resolve_path

        caminho = _resolve_path(pedido, _settings(), Path(RAIZ))
        if caminho and os.path.exists(caminho):
            return caminho
    except Exception as e:  # noqa: BLE001 — cai no path cru do catálogo
        print(f"[smoke] resolução do modelo falhou ({type(e).__name__}: {e}); usando {fallback}",
              file=sys.stderr)
    return fallback


def _medir(audio, sr: int) -> dict:
    import numpy as np

    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    if a.size == 0:
        return {"amostras": 0, "segundos": 0.0, "rms": 0.0, "pico": 0.0, "finito": True}
    return {
        "amostras": int(a.size),
        "segundos": round(a.size / float(sr), 3),
        "rms": round(float(np.sqrt(np.mean(a.astype(np.float64) ** 2))), 5),
        "pico": round(float(np.max(np.abs(a))), 4),
        "finito": bool(np.isfinite(a).all()),
    }


def _stt(caminho_wav: str) -> dict:
    """Transcreve com mlx-whisper (mesma lib do ASR local do app)."""
    import mlx_whisper

    modelo = os.environ.get("SMOKE_STT_MODEL", "mlx-community/whisper-large-v3-turbo")
    t0 = time.time()
    r = mlx_whisper.transcribe(caminho_wav, path_or_hf_repo=modelo, language="pt")
    return {"texto": (r.get("text") or "").strip(), "segundos": round(time.time() - t0, 1)}


def main() -> int:
    a = _args()
    from backends import generate_with_backend, resolve_backend

    pedido = (a.model or (_settings().get("model") or "").strip() or "omnivoice").strip()
    be = resolve_backend(pedido)
    caminho = _caminho_do_modelo(pedido, be["path"])
    laudo: dict = {"modelo": pedido, "familia": be["family"], "path": caminho}

    t0 = time.time()
    from mlx_audio.tts.utils import load_model

    modelo = load_model(caminho)
    sr = int(getattr(modelo, "sample_rate", 24000) or 24000)
    laudo["carga_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    audio = generate_with_backend(modelo, be["family"], a.text, language="pt")
    laudo["geracao_s"] = round(time.time() - t0, 1)
    laudo["sample_rate"] = sr
    laudo.update(_medir(audio, sr))

    destino = a.out or os.path.join(tempfile.gettempdir(), "smoke-sintese.wav")
    try:
        import numpy as np
        import soundfile as sf

        sf.write(destino, np.asarray(audio, dtype=np.float32).reshape(-1), sr)
        laudo["wav"] = destino
    except Exception as e:  # noqa: BLE001 — gravar é acessório; não invalida o smoke
        laudo["wav"] = f"(não gravei: {e})"

    falhas = []
    if not laudo["finito"]:
        falhas.append("áudio com NaN/Inf")
    if laudo["segundos"] < a.min_segundos:
        falhas.append(f"áudio curto demais ({laudo['segundos']}s < {a.min_segundos}s)")
    if laudo["rms"] < a.min_rms:
        falhas.append(f"MUDO: rms {laudo['rms']} < {a.min_rms} (bug clássico de bump)")
    if laudo["pico"] > PICO_MAX:
        falhas.append(f"clipando (pico {laudo['pico']})")

    if a.stt and not falhas:
        try:
            laudo["stt"] = _stt(destino)
            if not laudo["stt"]["texto"]:
                falhas.append("transcrição voltou vazia")
        except Exception as e:  # noqa: BLE001 — falha do STT é falha do smoke
            laudo["stt"] = {"erro": f"{type(e).__name__}: {e}"[:200]}
            falhas.append(f"STT falhou: {laudo['stt']['erro']}")

    laudo["falhas"] = falhas
    if a.como_json:
        print(json.dumps(laudo, ensure_ascii=False, indent=2))
    else:
        print(f"modelo {pedido} ({be['family']}) · sr {sr}Hz · {laudo['segundos']}s · "
              f"rms {laudo['rms']} · pico {laudo['pico']} · {laudo['geracao_s']}s de geração")
        if "stt" in laudo:
            print(f"stt  {laudo['stt'].get('texto') or laudo['stt'].get('erro')}")
        print(f"wav  {laudo['wav']}")
    for f in falhas:
        print(f"FALHA: {f}", file=sys.stderr)
    if not falhas:
        print("OK: síntese com áudio" + (" e transcrição" if a.stt else ""))
    return 1 if falhas else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)