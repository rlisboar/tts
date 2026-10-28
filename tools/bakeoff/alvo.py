"""Corpus e primeira medição do locutor-ALVO (#532).

Duas falas simultâneas: o ALVO e um CONCORRENTE, em SIR fixos. A pergunta do card
é se dá para entender só o alvo; a média é o que vaza do concorrente para a
transcrição e o WER do alvo.

Primeira camada medida aqui: **seleção por janela** com o extrator do R2 (ECAPA),
sem modelo de separação — é o piso contra o qual um TSE de verdade tem de ganhar.

Uso: python tools/bakeoff/alvo.py [--sirs 0,5,10] [--n 4]
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

VENV = Path("/tmp/tts-bakeoff/venv312/bin/python")
JANELA_S = 1.5


def _le(p: Path) -> np.ndarray:
    x, _ = sf.read(str(p), dtype="float32")
    return x if x.ndim == 1 else x.mean(axis=1)


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.float64(x) ** 2)))


def mistura(alvo: np.ndarray, conc: np.ndarray, sir_db: float) -> np.ndarray:
    n = min(alvo.size, conc.size)
    a, c = alvo[:n], conc[:n]
    c = c * (_rms(a) / (_rms(c) or 1e-9) / 10 ** (sir_db / 20))
    return (a + c).astype(np.float32)


def alternado(alvo: np.ndarray, conc: np.ndarray, sir_db: float,
             blocos_s: float = 1.5) -> np.ndarray:
    """Sobreposição PARCIAL: blocos alternando alvo e concorrente (o caso de turno
    de fala, não de conversa cruzada)."""
    n = int(blocos_s * comum.SR)
    pedacos, usar_alvo = [], True
    for i in range(0, min(alvo.size, conc.size * 3), n):
        base = alvo if usar_alvo else conc
        pedaco = base[i: i + n]
        if not pedaco.size:
            break
        if not usar_alvo:
            pedaco = pedaco * (_rms(alvo) / (_rms(conc) or 1e-9) / 10 ** (sir_db / 20))
        pedacos.append(pedaco)
        usar_alvo = not usar_alvo
    return np.concatenate(pedacos).astype(np.float32)


def _eca():
    """Extrator do R2 (ECAPA) rodando no venv de bancada."""
    codigo = """
import sys, json, numpy as np, torch
from speechbrain.inference.speaker import EncoderClassifier
enc = EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', run_opts={'device':'cpu'})
out = []
for p in sys.argv[1:]:
    import soundfile as sf
    x,_ = sf.read(p, dtype='float32')
    with torch.no_grad(): e = enc.encode_batch(torch.from_numpy(x)[None]).squeeze().numpy()
    out.append((e/np.linalg.norm(e)).tolist())
print(json.dumps(out))
"""
    def _roda(caminhos: list[Path]) -> list[np.ndarray]:
        r = subprocess.run([str(VENV), "-c", codigo, *[str(c) for c in caminhos]],
                           capture_output=True, text=True)
        return [np.array(v) for v in json.loads(r.stdout)]
    return _roda


def seleciona(mix: np.ndarray, sr: int, ref: np.ndarray, eca) -> np.ndarray:
    """Guarda só as janelas cujo escore contra o alvo passa do limiar calibrado.

    É a ideia da diarização por segmento: janelas de outro falante são descartadas
    em vez de irem ao STT."""
    tmp = comum.BASE / "resultados" / "_sel"
    tmp.mkdir(parents=True, exist_ok=True)
    passo = int(JANELA_S * sr)
    pedacos = []
    for i in range(0, mix.size - passo + 1, passo):
        p = tmp / f"j{i // passo}.wav"
        sf.write(str(p), mix[i:i + passo], sr, subtype="PCM_16")
        pedacos.append(p)
    if not pedacos:
        return mix
    pr = tmp / "ref.wav"
    sf.write(str(pr), ref, sr, subtype="PCM_16")
    embs = eca([pr, *pedacos])
    e_ref, resto = embs[0], embs[1:]
    # limiar de FAR 1% do R2 (2,43 em escore normalizado) — aqui sem AS-norm,
    # então se usa o cosseno cru: calibra-se pelo próprio par alvo/concorrente
    escores = [float(e @ e_ref) for e in resto]
    # corte RELATIVO ao melhor bloco: o cosseno absoluto do ECAPA varia com o canal
    # e com a duração, então um número fixo colhido de outro corpus erraria. A
    # margem de 0,12 vem da distância medida entre alvo e concorrente (0,88 x 0,66).
    corte = max(escores) - 0.12
    saida = [mix[i * passo:(i + 1) * passo] for i, s in enumerate(escores) if s >= corte]
    return np.concatenate(saida) if saida else mix


def _wer(ref: str, x: np.ndarray, sr: int, idioma: str) -> float:
    sys.path.insert(0, str(comum.RAIZ_REPO))
    import app

    tmp = comum.BASE / "resultados" / "_wer_alvo.wav"
    sf.write(str(tmp), x, sr, subtype="PCM_16")
    try:
        r = app._transcribe(tmp, language=idioma)
    except Exception:  # noqa: BLE001
        return float("nan")
    obtido = (r.get("text") or "").lower().split()
    alvo = ref.lower().split()
    if not alvo:
        return float("nan")
    d = list(range(len(alvo) + 1))
    for i, w in enumerate(obtido, 1):
        novo = [i]
        for j, v in enumerate(alvo, 1):
            novo.append(min(d[j] + 1, novo[-1] + 1, d[j - 1] + (w != v)))
        d = novo
    return d[-1] / len(alvo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sirs", default="0,5,10")
    ap.add_argument("--n", type=int, default=4)
    a = ap.parse_args()
    comum.garante()
    alvos = [p for p in sorted(comum.FALA.glob("pub_*.wav")) if p.with_suffix(".txt").exists()]
    conc = [p for p in reversed(alvos)]
    eca = _eca()
    linhas = []
    for i, pa in enumerate(alvos[: a.n]):
        pc = next(p for p in conc if p.stem.split("_")[1] != pa.stem.split("_")[1])
        ref = pa.with_suffix(".txt").read_text()
        xa, xc = _le(pa), _le(pc)
        for sir in (float(s) for s in a.sirs.split(",")):
            mix = mistura(xa, xc, sir)
            sel = seleciona(mix, comum.SR, xa, eca)
            linha = {"alvo": pa.name, "concorrente": pc.name, "sir": sir,
                     "modo": "sobreposto",
                     "wer_mistura": _wer(ref, mix, comum.SR, "en"),
                     "wer_selecao": _wer(ref, sel, comum.SR, "en"),
                     "reducao": round(1 - sel.size / mix.size, 3)}
            linhas.append(linha)
            alt = alternado(xa, xc, sir)
            sel2 = seleciona(alt, comum.SR, xa, eca)
            linha2 = {"alvo": pa.name, "concorrente": pc.name, "sir": sir,
                      "modo": "alternado",
                      "wer_mistura": _wer(ref, alt, comum.SR, "en"),
                      "wer_selecao": _wer(ref, sel2, comum.SR, "en"),
                      "reducao": round(1 - sel2.size / alt.size, 3)}
            linhas.append(linha2)
            print(f"SIR {sir:.0f} {pa.stem} ALTERNADO: mistura {linha2['wer_mistura']:.2f} "
                  f"-> seleção {linha2['wer_selecao']:.2f} (cortou {linha2['reducao']:.0%})",
                  flush=True)
            print(f"SIR {sir:.0f} {pa.stem}: WER mistura {linha['wer_mistura']:.2f} "
                  f"-> seleção {linha['wer_selecao']:.2f} (cortou {linha['reducao']:.0%})",
                  flush=True)
    (comum.RESULTADOS / "alvo.json").write_text(json.dumps(linhas, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
