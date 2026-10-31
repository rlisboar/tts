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

#: probabilidade mínima aceita na saída; abaixo disso o embed é repetido (máx. 4)
ALVO_DETECCAO = float(os.environ.get("TTS_MARCA_ALVO", "0.97"))

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
                # A marca é AUDÍVEL em fala baixa (#687): no par mais baixo do ABX
                # ela ficou em -63 dBFS e o dono ouviu. O que manda não é a força
                # relativa (SNR era 24 dB, igual às outras) e sim o nível absoluto.
                # Então o tanto de marca segue o nível do trecho: piso 0,6 (onde a
                # detecção ainda fica >=0,96 medido) até 1,0 em material alto.
                nivel = 20 * np.log10(max(float(np.sqrt(np.mean(x.astype(np.float64) ** 2))), 1e-9))
                fator = float(np.clip(0.6 + (nivel + 32.0) * 0.02, 0.6, 1.0))
                # A mensagem é SORTEADA a cada embed e algumas sortes grudam mal em
                # áudio baixo: medido 0,68 a 1,00 com o MESMO trecho em processos
                # diferentes (#687). Então a marca é CONFERIDA na saída e, se não
                # passou do alvo, sorteia de novo — sai sempre uma marca que o
                # detector acha, em vez de uma fraca e silenciosa.
                melhor, melhor_prob = None, -1.0
                for _tentativa in range(4):
                    with torch.no_grad():
                        y = gerador(wav, sample_rate=int(sr)).squeeze().numpy()
                    y = (x + fator * (y - x)).astype(np.float32)
                    with torch.no_grad():
                        prob, _m = detector.detect_watermark(
                            torch.from_numpy(np.ascontiguousarray(y))[None, None],
                            sample_rate=int(sr))
                    prob = float(prob.squeeze())
                    if prob > melhor_prob:
                        melhor, melhor_prob = y, prob
                    if prob >= ALVO_DETECCAO:
                        break
                sf.write(pedido["saida"], melhor, int(sr), subtype="PCM_16")
                print(json.dumps({"id": pedido.get("id"),
                                  "ms": int((time.perf_counter() - t0) * 1000)}), flush=True)
        except Exception as exc:  # noqa: BLE001 — o chamador segue sem marca
            print(json.dumps({"id": pedido.get("id"),
                              "erro": f"{type(exc).__name__}: {exc}"[:200]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
