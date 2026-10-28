"""Detector genérico de fala sintética (AASIST) — clones do app SEM marca (#522).

A marca d'água resolve o áudio que sai DESTE app. Não resolve áudio clonado por
outro sistema, nem clone antigo anterior à marca. Para isso existe o detector
genérico: ele olha o sinal e diz "isto é fala sintética?".

Modelo: **`clovaai/aasist` (NAVER), MIT** — código e pesos no mesmo repositório,
sob a mesma licença MIT (revisão em `LICENCAS.md`). Treinado na ASVspoof2019 LA;
os termos do *dataset* não são herdados para inferência.

Duas taxas medidas aqui, e as duas importam:

- **detecção** nos clones do próprio app, SEM marca (o caso que a marca não cobre);
- **falso positivo** em fala humana gravada — um detector que grita "sintético"
  em tudo não serve.

Recorte oficial (`data_utils.pad`): 64.600 amostras a 16 kHz, repetindo o sinal
quando é mais curto e cortando quando é mais longo.

Uso (venv de bancada — o modelo é torch):
    ./venv312/bin/python tools/bakeoff/clone_detect.py --limite 24
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402

RAIZ_GH = "https://raw.githubusercontent.com/clovaai/aasist/main"
PASTA = comum.BASE / "vendf" / "aasist"
ARQUIVOS = {"aasist_model.py": f"{RAIZ_GH}/models/AASIST.py",
            "AASIST.pth": f"{RAIZ_GH}/models/weights/AASIST.pth",
            "LICENSE": f"{RAIZ_GH}/LICENSE"}
N_SAMP = 64600
SR = 16000
CONFIG = {"architecture": "AASIST", "nb_samp": N_SAMP, "first_conv": 128,
          "filts": [70, [1, 32], [32, 32], [32, 64], [64, 64]],
          "gat_dims": [64, 32], "pool_ratios": [0.5, 0.7, 0.5, 0.5],
          "temperatures": [2.0, 2.0, 100.0, 100.0]}


def baixa() -> Path:
    PASTA.mkdir(parents=True, exist_ok=True)
    for nome, url in ARQUIVOS.items():
        destino = PASTA / nome
        if not destino.exists() or destino.stat().st_size < 1000:
            subprocess.run(["curl", "-sSL", "-o", str(destino), url], check=True)
    return PASTA


def carrega():
    """Modelo em eval, CPU. Devolve `(modelo, torch)`."""
    import torch

    pasta = baixa()
    sys.path.insert(0, str(pasta))
    # a classe chama-se `Model` no arquivo oficial do repositório
    from aasist_model import Model  # type: ignore[import-not-found]

    modelo = Model(CONFIG)
    estado = torch.load(pasta / "AASIST.pth", map_location="cpu", weights_only=True)
    modelo.load_state_dict(estado)
    return modelo.eval(), torch


def _recorta(x: np.ndarray) -> np.ndarray:
    """Recorte oficial: repete quando curto, corta quando longo."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if x.size >= N_SAMP:
        return x[:N_SAMP]
    repeticoes = int(np.ceil(N_SAMP / max(x.size, 1)))
    return np.tile(x, repeticoes)[:N_SAMP]


def pontua(modelo, torch, x: np.ndarray) -> float:
    """Probabilidade de ser SINTÉTICO (classe 1 do modelo)."""
    with torch.no_grad():
        saida = modelo(torch.from_numpy(_recorta(x))[None])
        if isinstance(saida, tuple):        # AASIST devolve (hidden, logits)
            saida = saida[1]
        prob = torch.softmax(saida, dim=1)[0, 1]
    return float(prob)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=24)
    ap.add_argument("--corte", type=float, default=0.5,
                    help="probabilidade a partir da qual conta como sintético")
    a = ap.parse_args()
    comum.garante()
    modelo, torch = carrega()

    clones = sorted(comum.CLONE.glob("*.wav"))[: a.limite]
    humanos = [p for p in sorted(comum.FALA.glob("pub_*.wav")) if p.with_suffix(".txt").exists()]
    humanos = humanos[: a.limite]

    def _ler(p: Path) -> np.ndarray:
        x, sr = sf.read(str(p), dtype="float32")
        x = x if x.ndim == 1 else x.mean(axis=1)
        if sr != SR:
            from scipy.signal import resample_poly

            x = resample_poly(x, SR, sr).astype(np.float32)
        return x

    res_clone = [pontua(modelo, torch, _ler(p)) for p in clones]
    res_humano = [pontua(modelo, torch, _ler(p)) for p in humanos]
    det = float(np.mean([v >= a.corte for v in res_clone])) if res_clone else float("nan")
    falso = float(np.mean([v >= a.corte for v in res_humano])) if res_humano else float("nan")
    print(f"clones SEM marca: detecção {det:.0%} em {len(res_clone)} "
          f"(mediana {np.median(res_clone):.3f})", flush=True)
    print(f"fala humana: FALSO POSITIVO {falso:.0%} em {len(res_humano)} "
          f"(mediana {np.median(res_humano):.3f})", flush=True)
    (comum.RESULTADOS / "aasist.json").write_text(json.dumps(
        {"corte": a.corte, "deteccao": det, "falso_positivo": falso,
         "clones": [round(v, 4) for v in res_clone],
         "humanos": [round(v, 4) for v in res_humano]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
