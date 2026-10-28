#!/usr/bin/env python3
"""Worker ISOLADO do separador de locutores (VOZ-I6, #534).

Roda o MossFormer2 num processo filho pelo mesmo princípio do `intencao_worker` e
do `tts_worker`: o modelo tem 670 MB e mexe em Metal (MPS); se estourar, morre só
ele — o app, a sessão e o STT continuam, e o chamador segue com o áudio original.

Modo único, NDJSON em stdin/stdout (o pai abre uma vez e manda pedidos):

    {"id": 1, "wav": "<entrada 16k mono>", "dir": "<saída>"}

Resposta:

    {"id": 1, "fluxos": ["<dir/a.wav>", "<dir/b.wav>"], "ms": 812}

Os dois fluxos saem SEM ordem: quem diz qual é o do dono é o ECAPA, no pai — o
worker não conhece perfil nenhum. Não há seleção aqui de propósito: o extrator já
existe no app e o worker ficaria com duas cópias dele.

O código do modelo é o VENDORIZADO (`tools/separa_vend`) e os pesos vêm de
`models/mossformer2-ss-16k` — nada de Hub (`tools/bakeoff/separa.py`).
"""

from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

RAIZ = os.path.dirname(os.path.abspath(__file__))


def _log(msg: str) -> None:
    print(f"[separa] {msg}", file=sys.stderr, flush=True)


def main() -> int:
    sys.path.insert(0, os.path.join(RAIZ, "tools", "bakeoff"))
    import numpy as np
    import soundfile as sf

    import separa  # tools/bakeoff/separa.py — o mesmo caminho medido no R4

    t = time.perf_counter()
    modelo = separa.carrega()
    _log(f"modelo carregado em {round((time.perf_counter() - t) * 1000)} ms")
    # aquece: o 1º bloco paga a compilação do grafo no MPS (~1 s)
    t = time.perf_counter()
    separa.separa(modelo, np.zeros(separa.comum.SR, dtype=np.float32))
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
            if int(sr) != separa.comum.SR:      # o app entrega 16k; blinda o resto
                import scipy.signal as ss

                x = ss.resample_poly(x, separa.comum.SR, int(sr)).astype(np.float32)
            fluxos = separa.separa(modelo, x)
            saida = []
            for i, f in enumerate(fluxos):
                caminho = os.path.join(pedido["dir"], f"fluxo{i}.wav")
                sf.write(caminho, f, separa.comum.SR, subtype="PCM_16")
                saida.append(caminho)
            print(json.dumps({"id": pedido.get("id"), "fluxos": saida,
                              "ms": int((time.perf_counter() - t0) * 1000)}), flush=True)
        except Exception as exc:  # noqa: BLE001 — o pai segue com o áudio original
            print(json.dumps({"id": pedido.get("id"),
                              "erro": f"{type(exc).__name__}: {exc}"[:200]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
