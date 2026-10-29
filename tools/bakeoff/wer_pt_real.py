"""WER do STT com fala pt REAL — cru × RNNoise × DFN3 (R1b, decide o I2 do Live).

O R1 mediu WER em inglês; depois, em pt com vozes SINTÉTICAS do sistema — e ali
achou que o RNNoise ajuda. O PESQ/STOI, porém, disse o contrário (o RNNoise derruba
o STOI em todos os SNR). Este runner decide com fala **real**: FLEURS pt_br
(`google/fleurs`, CC BY 4.0), 40 locutores DIFERENTES, uma fala cada, transcrita
no corpus.

O corpus mora em `/tmp/tts-bakeoff/fala_pt` e é montado por `--monta` a partir dos
`.tsv` baixados do FLEURS. Nada de voz do dono.

Uso (venv de bancada, precisa do `app` para transcrever):
    ./venv312/bin/python tools/bakeoff/wer_pt_real.py --monta     # uma vez
    ./venv312/bin/python tools/bakeoff/wer_pt_real.py --limite 40 --snrs 0,5,20
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import sys
import tarfile
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import pt_ruido  # noqa: E402
import ruido_bakeoff  # noqa: E402

FLEURS = pathlib.Path("/tmp/fleurs")
DESTINO = comum.BASE / "fala_pt"
FAMILIAS = ("ventoinha", "babble")


def monta(quantos: int = 40) -> int:
    """Extrai `quantos` locutores distintos do FLEURS test. Só roda uma vez."""
    linhas = list(csv.reader(open(FLEURS / "test.tsv", encoding="utf-8"), delimiter="\t"))
    vistos, escolhidas = set(), []
    for linha in linhas:
        if len(linha) < 6 or linha[0] in vistos:
            continue
        vistos.add(linha[0])
        escolhidas.append(linha)
        if len(escolhidas) >= quantos:
            break
    alvo = {l[1] for l in escolhidas}
    DESTINO.mkdir(parents=True, exist_ok=True)
    feitos = {}
    with tarfile.open(FLEURS / "test.tar.gz") as t:
        for m in t:
            nome = pathlib.Path(m.name).name
            if nome in alvo:
                x, sr = sf.read(t.extractfile(m), dtype="float32")
                if sr != comum.SR:
                    continue
                sf.write(str(DESTINO / nome), x, comum.SR, subtype="PCM_16")
                feitos[nome] = x.size / comum.SR
    refs = {l[1]: l[3] for l in escolhidas}
    (DESTINO / "meta.json").write_text(json.dumps(
        {"fonte": "FLEURS pt_br test (CC BY 4.0)", "locutores": len(feitos),
         "refs": {k: refs[k] for k in feitos},
         "durs": {k: round(v, 2) for k, v in feitos.items()}}, ensure_ascii=False, indent=1))
    print(f"corpus: {len(feitos)} locutores distintos em {DESTINO}")
    return 0


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def main() -> int:
    sys.path.insert(0, str(comum.RAIZ_REPO))
    ap = argparse.ArgumentParser()
    ap.add_argument("--monta", action="store_true")
    ap.add_argument("--limite", type=int, default=40)
    ap.add_argument("--snrs", default="0,5,20")
    ap.add_argument("--candidatos", default="cru,rnnoise,dfnet3onnx")
    a = ap.parse_args()
    comum.garante()
    if a.monta or not (DESTINO / "meta.json").is_file():
        monta(a.limite)
    meta = json.loads((DESTINO / "meta.json").read_text())
    carga = [round(v, 2) for v in os.getloadavg()]
    print(f"carga da máquina: {carga} | locutores no corpus: {meta['locutores']}", flush=True)

    candidatos = {"cru": lambda x, sr: (np.asarray(x, np.float32), 0.0, 0.0)}
    for nome in a.candidatos.split(","):
        if nome != "cru" and nome in ruido_bakeoff.CANDIDATOS:
            candidatos[nome] = ruido_bakeoff.CANDIDATOS[nome]

    arquivos = sorted(p for p in DESTINO.glob("*.wav"))[: a.limite]
    linhas = []
    for familia in FAMILIAS:
        for snr in (float(s) for s in a.snrs.split(",")):
            for nome, fn in candidatos.items():
                wers = []
                for i, arq in enumerate(arquivos):
                    # fala INTEIRA: cortar em 8 s deixa a referência maior que o áudio e infla
                    # o WER com deleções que não têm nada a ver com o filtro
                    x = sf.read(str(arq), dtype="float32")[0]
                    ruido = pt_ruido._ruido(familia, x.size, i)
                    rx, rr = _rms(x), _rms(ruido) or 1e-9
                    sujo = (x + ruido * (rx / rr / 10 ** (snr / 20))).astype(np.float32)
                    try:
                        y, _, _ = fn(sujo, comum.SR)
                    except Exception as erro:  # noqa: BLE001
                        print(f"  {familia} {snr} {nome} {arq.name}: FALHOU {erro}", flush=True)
                        continue
                    wers.append(ruido_bakeoff._wer(meta["refs"].get(arq.name, ""),
                                                   np.asarray(y, np.float32), comum.SR))
                if wers:
                    linha = {"familia": familia, "snr": snr, "candidato": nome,
                             "wer": round(float(np.nanmean(wers)), 4),
                             "wer_mediano": round(float(np.nanmedian(wers)), 4),
                             "n": len(wers)}
                    linhas.append(linha)
                    print(f"{familia} {snr:.0f} {nome}: wer {linha['wer']:.3f} "
                          f"(mediano {linha['wer_mediano']:.3f}, n={linha['n']})", flush=True)
    print(flush=True)
    for familia in FAMILIAS:
        for snr in (float(s) for s in a.snrs.split(",")):
            dobro = [l for l in linhas if l["familia"] == familia and l["snr"] == snr]
            if dobro:
                print(f"{familia} {snr:.0f} dB: " + " | ".join(
                    f"{l['candidato']} {l['wer']:.3f}" for l in dobro), flush=True)
    (comum.RESULTADOS / "wer_pt_real.json").write_text(json.dumps(
        {"fonte": meta["fonte"], "carga": carga, "linhas": linhas}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
