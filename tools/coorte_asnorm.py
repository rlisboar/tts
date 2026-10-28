"""Coorte de impostores para o AS-norm do ECAPA (#526).

POR QUE EXISTE: o AS-norm não é enfeite — no bake-off #521 ele é o que leva o
ECAPA de 5,3 % para 3,5 % de EER em 1 s e de 1,6 % para 0,19 % em 3 s. Ele
normaliza o escore de um par (fala, cadastro) pela distribuição de escores dessa
MESMA fala contra uma coorte de locutores que não são o dono.

Duas consequências que este script materializa:
1. a coorte é ESPECÍFICA DO MODELO — refazer a coorte é obrigatório ao trocar de
   extrator (no Resemblyzer o AS-norm PIORA: 1,9 % → 5,0 % em 1 s);
2. ela é um ARTEFATO, não um cálculo de boot: o app não roda este script nem
   baixa corpus; ele lê o `.npy` pronto (`TTS_ASNORM_COORTE`) e cai no fallback
   sem AS-norm quando o arquivo não existe.

Protocolo (o mesmo do bake-off, para o número ser comparável):
`<corpus>/pub_<locutor>_*.wav` → até `--por-locutor` falas por locutor, recorte de
`--dur` segundos (repetindo a fala quando ela é mais curta, nunca zerofill),
embedding L2-normalizado. O `.json` ao lado guarda a proveniência e o sha256 de
cada fala usada, para o artefato ser auditável sem o corpus.

Uso (venv da bancada, é o único lugar com `speechbrain` hoje):

    BAKEOFF_DIR=/tmp/tts-bakeoff /tmp/tts-bakeoff/venv312/bin/python \
        tools/coorte_asnorm.py --modelo models/ecapa-voxceleb \
        --saida models/asnorm-coorte-ecapa
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent / "bakeoff"))
import comum  # noqa: E402

SR = comum.SR


def _le(p: Path) -> np.ndarray:
    x, _ = sf.read(str(p), dtype="float32")
    return x if x.ndim == 1 else x.mean(axis=1)


def _recorta(x: np.ndarray, dur: float) -> np.ndarray:
    """Recorte de `dur` s repetindo a fala curta (mesmo critério do bake-off)."""
    n = int(dur * SR)
    if x.size == n:
        return x
    if x.size == 0:
        return np.zeros(n, dtype="float32")
    if x.size < n:
        return np.tile(x, int(np.ceil(n / x.size)))[:n]
    return x[:n]


def _locutores(diretorio: Path) -> dict[str, list[Path]]:
    """`pub_<locutor>_<arquivo>.wav` → {locutor: [falas em ordem]}."""
    por: dict[str, list[Path]] = {}
    for wav in sorted(diretorio.glob("pub_*.wav")):
        partes = wav.stem.split("_")
        if len(partes) < 3:
            continue
        por.setdefault(partes[1], []).append(wav)
    return por


def _emb(enc, x: np.ndarray) -> np.ndarray:
    import torch

    with torch.no_grad():
        e = enc.encode_batch(torch.from_numpy(x)[None]).squeeze().numpy()
    return e / (np.linalg.norm(e) or 1e-9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(comum.FALA), help="diretório dos wavs públicos")
    ap.add_argument("--modelo", default="models/ecapa-voxceleb", help="diretório do extrator")
    ap.add_argument("--saida", default="models/asnorm-coorte-ecapa", help="prefixo do artefato")
    ap.add_argument("--por-locutor", type=int, default=2)
    ap.add_argument("--dur", type=float, default=3.0)
    ap.add_argument("--locutores", type=int, default=999, help="teto de locutores")
    ap.add_argument("--excluir", default="", help="locutores separados por vírgula")
    a = ap.parse_args()

    corpus = Path(a.corpus)
    if not corpus.is_dir():
        print(f"corpus não encontrado: {corpus} (rode tools/bakeoff/corpus.py publico)")
        return 2

    from speechbrain.inference.speaker import EncoderClassifier

    enc = EncoderClassifier.from_hparams(source=a.modelo, savedir=a.modelo,
                                         run_opts={"device": "cpu"})
    excluir = {x.strip() for x in a.excluir.split(",") if x.strip()}
    por = {k: v for k, v in _locutores(corpus).items() if k not in excluir}

    vetores, usadas = [], []
    for locutor in sorted(por)[: a.locutores]:
        for wav in por[locutor][: a.por_locutor]:
            vetores.append(_emb(enc, _recorta(_le(wav), a.dur)).astype("float32"))
            usadas.append({"locutor": locutor, "arquivo": wav.name,
                           "sha256": comum.sha256(wav)})
    if not vetores:
        print("nenhuma fala utilizável no corpus")
        return 2

    m = np.stack(vetores)
    saida = Path(a.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    np.save(saida.with_suffix(".npy"), m)
    manifesto = {
        "modelo": a.modelo,
        "revisao_modelo": (Path(a.modelo) / "FONTE.txt").read_text().strip()
        if (Path(a.modelo) / "FONTE.txt").exists() else "",
        "dimensao": int(m.shape[1]),
        "vetores": int(m.shape[0]),
        "locutores": len({u["locutor"] for u in usadas}),
        "dur_s": a.dur,
        "por_locutor": a.por_locutor,
        "corpus": str(corpus),
        "corpus_manifesto": "tools/bakeoff/corpus.py (sha256 por arquivo)",
        "fala_usada": usadas,
        "protocolo": "recorte repetindo fala curta, embedding L2-normalizado, "
                     "normalização por top-k da coorte (k<=50) no escore",
        "aviso": "coorte ESPECÍFICA do modelo: trocar o extrator exige refazer",
        "gera": "tools/coorte_asnorm.py",
    }
    saida.with_suffix(".json").write_text(json.dumps(manifesto, indent=1, ensure_ascii=False))
    print(f"{m.shape[0]} vetores de {manifesto['locutores']} locutores → {saida.with_suffix('.npy')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
