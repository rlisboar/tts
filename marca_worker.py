#!/usr/bin/env python3
"""Worker ISOLADO do AudioSeal (marca d'água anti-clone, #657).

Áudio gerado sai marcado; o detector é a outra ponta. O modelo é torch em CPU e
tem de ficar FORA do processo do app: o app já convive com MLX no Metal e o
`first_audio_ms` não pode pagar setup de torch a cada trecho.

Modo único, NDJSON em stdin/stdout:

    {"id": 1, "op": "marca", "wav": "<entrada>", "saida": "<saída>"}
    {"id": 2, "op": "detecta", "wav": "<entrada>"}

Resposta:

    {"id": 1, "ms": 41}                    (marca: escreve o wav de saída)
    {"id": 2, "prob": 0.999, "ms": 12}     (detecta: probabilidade da marca)

16 kHz mono dos dois lados — o app só chama com a taxa dele.
"""

from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

RAIZ = os.path.dirname(os.path.abspath(__file__))


def _log(msg: str) -> None:
    print(f"[marca] {msg}", file=sys.stderr, flush=True)


def main() -> int:
    import numpy as np
    import soundfile as sf
    import torch
    from audioseal import AudioSeal

    pasta = os.environ.get("TTS_MARCA_MODELO_DIR") or os.path.join(
        RAIZ, "models", "audioseal")
    gerador_p = os.path.join(pasta, "gerador.pt")
    detector_p = os.path.join(pasta, "detector.pt")
    t = time.perf_counter()
    # `nbits=16` explícito: sem a carta do pacote o config vem do próprio
    # checkpoint (`xp.cfg`), e não há download nenhum aqui
    gerador = AudioSeal.load_generator(gerador_p, nbits=16).eval()
    detector = AudioSeal.load_detector(detector_p, nbits=16).eval()
    _log(f"carregado em {round((time.perf_counter() - t) * 1000)} ms")
    t = time.perf_counter()
    with torch.no_grad():                    # aquece o grafo do gerador
        gerador(torch.zeros(1, 1, 16000), sample_rate=16000)
    _log(f"aquecido em {round((time.perf_counter() - t) * 1000)} ms")
    print(json.dumps({"tipo": "pronto"}), flush=True)

    for linha in sys.stdin:
        linha = linha.strip()
        if not linha:
            continue
        try:
            pedido = json.loads(linha)
        except Exception:  # noqa: BLE001
            continue
        if pedido.get("tipo") == "close":
            break
        t0 = time.perf_counter()
        try:
            x, sr = sf.read(pedido["wav"], dtype="float32")
            if x.ndim > 1:
                x = x.mean(axis=1)
            wav = torch.from_numpy(np.ascontiguousarray(x))[None, None]
            if pedido.get("op") == "detecta":
                with torch.no_grad():
                    prob, _msg = detector.detect_watermark(wav, sample_rate=int(sr))
                print(json.dumps({"id": pedido.get("id"), "prob": float(prob.squeeze()),
                                  "ms": int((time.perf_counter() - t0) * 1000)}), flush=True)
            else:
                with torch.no_grad():
                    y = gerador(wav, sample_rate=int(sr)).squeeze().numpy()
                sf.write(pedido["saida"], y, int(sr), subtype="PCM_16")
                print(json.dumps({"id": pedido.get("id"),
                                  "ms": int((time.perf_counter() - t0) * 1000)}), flush=True)
        except Exception as exc:  # noqa: BLE001 — o chamador segue sem marca
            print(json.dumps({"id": pedido.get("id"),
                              "erro": f"{type(exc).__name__}: {exc}"[:200]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
