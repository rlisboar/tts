"""SNR extremo (0 e 20 dB) com PESQ e STOI (#520, item 3).

O R1 mediu 5 e 10 dB com DNSMOS, timbre e WER. Aqui as pontas: 0 dB (o ruído tem
a mesma energia da fala) e 20 dB (quase limpo, onde o filtro só pode estragar).

PESQ e STOI são as métricas de inteligibilidade/preservação que faltavam. O
`cru` NÃO entra como candidato aqui: as duas comparam contra a referência limpa,
e o cru é a própria mistura — ele vira a linha de base de leitura, não uma
"melhoria".

Carga da máquina registrada junto: com outros agentes na mesma máquina o RTF daqui
não vale como número de produção.

Uso (venv de bancada):
    ./venv312/bin/python tools/bakeoff/pesq_stoi.py --limite 4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from pesq import pesq
from pystoi import stoi

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import pt_ruido  # noqa: E402
import ruido_bakeoff  # noqa: E402

SNRS = (0.0, 20.0)


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def main() -> int:
    sys.path.insert(0, str(comum.RAIZ_REPO))
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=4)
    ap.add_argument("--familias", default="ventoinha,babble")
    ap.add_argument("--candidatos", default="cru,rnnoise,dfnet3onnx")
    a = ap.parse_args()
    comum.garante()
    carga = [round(v, 2) for v in os.getloadavg()]
    print(f"carga da máquina: {carga}", flush=True)

    por = pt_ruido.monta(comum.BASE / "pt")
    falantes = sorted(por)[: a.limite]
    candidatos = {"cru": lambda x, sr: (np.asarray(x, np.float32), 0.0, 0.0)}
    for nome in a.candidatos.split(","):
        if nome != "cru" and nome in ruido_bakeoff.CANDIDATOS:
            candidatos[nome] = ruido_bakeoff.CANDIDATOS[nome]

    linhas = []
    for familia in a.familias.split(","):
        for snr in SNRS:
            for nome, fn in candidatos.items():
                ps, st, rt = [], [], []
                for i, f in enumerate(falantes):
                    limpo = sf.read(str(por[f][2]), dtype="float32")[0][: 4 * comum.SR]
                    ruido = pt_ruido._ruido(familia, limpo.size, i)
                    rx, rr = _rms(limpo), _rms(ruido) or 1e-9
                    sujo = (limpo + ruido * (rx / rr / 10 ** (snr / 20))).astype(np.float32)
                    try:
                        y, dt, _ = fn(sujo, comum.SR)
                    except Exception as erro:  # noqa: BLE001
                        print(f"  {familia} {snr} {nome} {f}: FALHOU {erro}", flush=True)
                        continue
                    y = np.asarray(y, np.float32)[: limpo.size]
                    ps.append(float(pesq(comum.SR, limpo.astype(np.float64),
                                         np.clip(y, -1, 1).astype(np.float64), "wb")))
                    st.append(float(stoi(limpo, np.clip(y, -1, 1), comum.SR, extended=False)))
                    rt.append(dt / (sujo.size / comum.SR))
                    print(f"{familia} {snr:.0f} {nome} {f}: pesq {ps[-1]:.2f} stoi "
                          f"{st[-1]:.3f} rtf {rt[-1]:.3f}", flush=True)
                if ps:
                    linhas.append({"familia": familia, "snr": snr, "candidato": nome,
                                   "pesq": round(float(np.mean(ps)), 3),
                                   "stoi": round(float(np.mean(st)), 3),
                                   "rtf": round(float(np.mean(rt)), 3), "n": len(ps)})
    print(flush=True)
    for familia in a.familias.split(","):
        for snr in SNRS:
            dobro = [l for l in linhas if l["familia"] == familia and l["snr"] == snr]
            if dobro:
                print(f"{familia} {snr:.0f} dB: " + " | ".join(
                    f"{l['candidato']} pesq {l['pesq']:.2f} stoi {l['stoi']:.2f}"
                    for l in dobro), flush=True)
    (comum.RESULTADOS / "pesq_stoi.json").write_text(json.dumps(
        {"carga": carga, "linhas": linhas}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
