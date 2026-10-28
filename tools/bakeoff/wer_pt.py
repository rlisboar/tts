"""WER em pt-BR, cru × RNNoise × DFN3, sob ventoinha e babble (#520, ressalva do #529).

O R1 mediu o WER no corpus em INGLÊS. O produto é pt-BR e o gate do #529 apontou
a falta. Aqui a fala é pt (vozes do sistema, com a frase conhecida — a referência é
exata) e o ruído é o do R1, em SNR fixo.

Também é o que decide o I2: se o filtro só não atrapalha o STT, ele fica atrás do
VAD e não antes do reconhecedor.

Carga da máquina é registrada junto: com outros agentes rodando, o RTF daqui não
vale como número de produção.

Uso:
    ./.venv-mlx/bin/python tools/bakeoff/wer_pt.py --limite 6 --snrs 5,10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import pt_ruido  # noqa: E402
import ruido_bakeoff  # noqa: E402


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def _mistura(x: np.ndarray, familia: str, snr: float, semente: int) -> np.ndarray:
    r = pt_ruido._ruido(familia, x.size, semente)
    rx = _rms(x)
    rr = _rms(r) or 1e-9
    return (x + r * (rx / rr / 10 ** (snr / 20))).astype(np.float32)


def main() -> int:
    sys.path.insert(0, str(comum.RAIZ_REPO))       # `app` para transcrever
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=6)
    ap.add_argument("--snrs", default="5,10")
    ap.add_argument("--familias", default="ventoinha,babble")
    ap.add_argument("--candidatos", default="cru,rnnoise,dfnet3onnx")
    a = ap.parse_args()
    comum.garante()

    por = pt_ruido.monta(comum.BASE / "pt")
    falantes = sorted(por)[: a.limite]
    carga = [round(v, 2) for v in os.getloadavg()]
    print(f"carga da máquina: {carga}", flush=True)

    candidatos = {"cru": lambda x, sr: (np.asarray(x, np.float32), 0.0, 0.0)}
    for nome in a.candidatos.split(","):
        if nome == "cru" or nome not in ruido_bakeoff.CANDIDATOS:
            continue
        candidatos[nome] = ruido_bakeoff.CANDIDATOS[nome]

    linhas = []
    for familia in a.familias.split(","):
        for snr in (float(s) for s in a.snrs.split(",")):
            for nome, fn in candidatos.items():
                wers, rtfs = [], []
                for i, f in enumerate(falantes):
                    frase = pt_ruido.FRASES[2]
                    x = sf.read(str(por[f][2]), dtype="float32")[0]
                    sujo = _mistura(x[: 4 * comum.SR], familia, snr, i)
                    try:
                        y, dt, _ = fn(sujo, comum.SR)
                    except Exception as erro:  # noqa: BLE001
                        print(f"  {familia} {snr} {nome} {f}: FALHOU {erro}", flush=True)
                        continue
                    wers.append(ruido_bakeoff._wer(frase, np.asarray(y, np.float32), comum.SR))
                    rtfs.append(dt / (sujo.size / comum.SR))
                    print(f"{familia} {snr:.0f} {nome} {f}: wer {wers[-1]:.2f} "
                          f"rtf {rtfs[-1]:.3f}", flush=True)
                if wers:
                    linha = {"familia": familia, "snr": snr, "candidato": nome,
                             "wer": round(float(np.nanmean(wers)), 3),
                             "rtf": round(float(np.mean(rtfs)), 3), "n": len(wers)}
                    linhas.append(linha)
    print(flush=True)
    for familia in a.familias.split(","):
        for snr in (float(s) for s in a.snrs.split(",")):
            dobro = [l for l in linhas if l["familia"] == familia and l["snr"] == snr]
            if dobro:
                print(f"{familia} {snr:.0f} dB: " + " | ".join(
                    f"{l['candidato']} {l['wer']:.2f}" for l in dobro), flush=True)
    (comum.RESULTADOS / "wer_pt.json").write_text(json.dumps(
        {"carga": carga, "linhas": linhas}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
