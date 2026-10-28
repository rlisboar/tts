"""VOZES PARECIDAS: onde o portão e o seletor do R4 quebram (#521/#532).

O R4 fechou com o terceiro mediano em 0,037 de cosseno contra 0,613 do dono —
timbres bem distintos, o caso fácil. Aqui se escolhem de propósito os pares de
locutores MAIS parecidos do corpus e se mede exatamente o que o PM pediu:

- **FAR dos parecidos** no ponto de operação do portão (0,35 de cosseno cru): a
  fração de pares parecidos que o portão deixa passar como se fossem o dono.
- **erro do seletor do R4**: quando alvo e concorrente são parecidos, quantas
  vezes o ECAPA prefere o bloco do CONCORRENTE ao do alvo (troca de fluxo). O
  seletor escolhe o máximo entre dois — se a diferença some, ele erra.

Como os parecidos são escolhidos: centróide de 5 amostras por locutor, cosseno
entre todos os pares de locutores distintos, os N mais altos. O conjunto de teste
é sempre de amostras que NÃO entraram no centróide.

Uso:
    /tmp/tts-bakeoff/venv312/bin/python tools/bakeoff/parecidos.py --n 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alvo  # noqa: E402
import comum  # noqa: E402

DUR_TESTE = 3.0
CORTE_GATE = 0.35          # ponto de operação recomendado no R4


def _centroide(arquivos: list[Path], eca) -> np.ndarray:
    x, _ = alvo.junta(arquivos, 12)
    p = comum.RESULTADOS / "_par" / f"cad_{arquivos[0].stem}.wav"
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), x, comum.SR, subtype="PCM_16")
    e = eca([p])[0]
    return e / (np.linalg.norm(e) or 1e-9)


def _teste(arquivo: Path, eca) -> np.ndarray:
    x, _ = alvo.junta([arquivo], DUR_TESTE)
    p = comum.RESULTADOS / "_par" / f"tst_{arquivo.stem}.wav"
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), x, comum.SR, subtype="PCM_16")
    e = eca([p])[0]
    return e / (np.linalg.norm(e) or 1e-9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="quantos pares parecidos usar")
    ap.add_argument("--corte", type=float, default=CORTE_GATE)
    a = ap.parse_args()
    comum.garante()
    por = {f: ps for f, ps in alvo._falantes().items() if len(ps) >= 3}
    falantes = sorted(por)
    eca = alvo._eca()

    # cadastro com as 5 primeiras, teste com a última (nunca a mesma amostra)
    cad = {f: _centroide(por[f][:5], eca) for f in falantes}
    tst = {f: _teste(por[f][-1], eca) for f in falantes}

    pares = []
    for i, a1 in enumerate(falantes):
        for b1 in falantes[i + 1:]:
            pares.append((float(cad[a1] @ cad[b1]), a1, b1))
    pares.sort(reverse=True)
    print(f"{len(pares)} pares de locutores diferentes; "
          f"parecido máximo {pares[0][0]:.3f}, mediano {np.median([p[0] for p in pares]):.3f}",
          flush=True)

    escolhidos = pares[: a.n]
    linhas = []
    for esc, a1, b1 in escolhidos:
        s_alvo = float(cad[a1] @ tst[a1])      # acerto: o próprio locutor
        s_imp = float(cad[a1] @ tst[b1])       # FAR: o parecido entrando como dono
        # erro do seletor: o seletor do R4 prefere o MAIOR escore entre dois blocos.
        # Aqui os dois blocos são alvo e parecido; erra se o parecido vence.
        errou = s_imp > s_alvo
        passou = s_imp >= a.corte
        linhas.append({"parecidos": round(esc, 3), "a": a1, "b": b1,
                       "escore_alvo": round(s_alvo, 3), "escore_parecido": round(s_imp, 3),
                       "passou_no_gate": passou, "seletor_errou": errou})
        print(f"parecido {esc:.3f} ({a1} x {b1}): alvo {s_alvo:.3f} x parecido "
              f"{s_imp:.3f} | gate {'PASSOU' if passou else 'barrou'} | "
              f"seletor {'ERROU' if errou else 'ok'}", flush=True)

    n = len(linhas)
    far = sum(l["passou_no_gate"] for l in linhas) / n
    erro = sum(l["seletor_errou"] for l in linhas) / n
    # linha de base: os MESMOS falantes contra um concorrente distinto (quartil de baixo)
    distintos = pares[-a.n:]
    far_dist = sum(1 for _, a1, b1 in distintos if float(cad[a1] @ tst[b1]) >= a.corte) / len(distintos)
    print(f"\npares PARECIDOS: FAR no corte {a.corte:.2f} = {far:.0%}; "
          f"erro do seletor = {erro:.0%} ({n} pares)", flush=True)
    print(f"pares DISTINTOS (controle): FAR = {far_dist:.0%}", flush=True)
    if far <= far_dist:
        print("!! sem diferença entre parecidos e distintos: a escolha de pares não pegou")

    # curva de limiar sobre TODOS os pares cruzados (780 negativos) e os 40 pares
    # do próprio locutor (positivos). O FAR mediano é baixo e engana: o que pesa
    # é a cauda dos parecidos, publicada abaixo par a par.
    curva = []
    for corte in (0.35, 0.40, 0.44, 0.48, 0.52, 0.56, 0.60):
        far_todos = float(np.mean([float(cad[x] @ tst[y]) >= corte
                                   for _, x, y in pares]))
        frr = float(np.mean([float(cad[f] @ tst[f]) < corte for f in falantes]))
        far_pares = sum(1 for l in linhas if l["escore_parecido"] >= corte) / n
        curva.append({"corte": corte, "far_todos": round(far_todos, 4),
                      "far_parecidos": round(far_pares, 3), "frr": round(frr, 3)})
        print(f"corte {corte:.2f}: FAR todos {far_todos:.1%} | FAR parecidos "
              f"{far_pares:.0%} | FRR do dono {frr:.0%}", flush=True)

    (comum.RESULTADOS / "parecidos.json").write_text(json.dumps(
        {"corte": a.corte, "n": n, "far_parecidos": far, "erro_seletor": erro,
         "far_distintos": far_dist, "curva": curva, "linhas": linhas}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
