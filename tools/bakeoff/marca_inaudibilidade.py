"""Inaudibilidade da marca d'água: PESQ e preparo do ABX (#522).

A marca tem de ser inaudível. Duas medidas, e a segunda é a que vale:

1. **PESQ** entre o áudio original e o MARCADO. É objetivo, mas o PESQ foi feito
   para degradação de codec: uma marca espectralmente fina pode marcar alto e não
   ser audível, então o número sozinho não decide.
2. **ABX**: o par (original, marcado) vira um par de arquivos com nomes neutros
   para alguém ouvir sem saber qual é qual. O script gera o par e a folha de
   respostas com o gabarito fora da ordem — o ABX é a medida que fecha.

Uso:
    ./venv312/bin/python tools/bakeoff/marca_inaudibilidade.py --limite 6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from pesq import pesq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alvo  # noqa: E402
import comum  # noqa: E402
import marca_robustez  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=6)
    a = ap.parse_args()
    comum.garante()
    tmp = comum.RESULTADOS / "_marca"
    tmp.mkdir(parents=True, exist_ok=True)
    marca, _ = marca_robustez._marcador()

    por = alvo._falantes()
    falantes = [f for f, ps in sorted(por.items()) if len(ps) >= 2][: a.limite]
    rng = np.random.default_rng(11)
    linhas, abx = [], []
    for f in falantes:
        x, _ = alvo.junta(por[f], 4)
        marcado = marca(x, comum.SR)
        valor = float(pesq(comum.SR, np.asarray(x, np.float64),
                          np.asarray(marcado, np.float64), "wb"))
        linhas.append({"falante": f, "pesq": round(valor, 3)})
        print(f"{f}: PESQ {valor:.3f}", flush=True)

        # par ABX com nomes neutros e gabarito fora da ordem
        original, alternativo = f"abx_{f}_A.wav", f"abx_{f}_B.wav"
        sf.write(str(tmp / original), x, comum.SR, subtype="PCM_16")
        sf.write(str(tmp / alternativo), marcado, comum.SR, subtype="PCM_16")
        if rng.random() < 0.5:
            abx.append({"falante": f, "X": original, "marcado": "A"})
        else:
            abx.append({"falante": f, "X": alternativo, "marcado": "B"})

    media = float(np.mean([l["pesq"] for l in linhas])) if linhas else float("nan")
    print(f"\nPESQ médio (original x marcado): {media:.3f} "
          f"(mínimo {min(l['pesq'] for l in linhas):.3f})", flush=True)
    print(f"pares ABX em {tmp} — folha de gabarito em resultados/abx.json", flush=True)
    (comum.RESULTADOS / "abx.json").write_text(json.dumps(
        {"nota": "X é o arquivo que o ouvinte deve dizer se é o original ou o marcado",
         "pares": abx, "pesq": linhas, "pesq_medio": round(media, 3)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
