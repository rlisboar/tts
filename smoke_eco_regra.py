#!/usr/bin/env python3
"""Matriz offline da classificação ECO × HUMANO do turno do Live (#301).

`_parece_eco` decide se o transcript é a PRÓPRIA fala do assistente voltando pelo
mic. O custo dos dois erros é assimétrico: falso-descarte mata a fala do humano em
silêncio (sem LLM, sem TTS, sem retry), falso-eco faz o modelo responder à própria
voz. Esta matriz mede os DOIS lados sobre texto — sem áudio, sem modelo, sem rede.

Corpora:
  - HUMANO: falas legítimas que REUSAM o vocabulário do assistente (pergunta,
    negação, correção, confirmação curta, eco-que-virou-resposta). É o corpus que
    a regra antiga furava.
  - ECO: trechos literais da referência (começo, meio, fim, frases inteiras,
    quebrados na pontuação) e variações que o Whisper produz no eco (palavra
    parecida no lugar da exata, trecho com o "?" que o assistente fez).

Critério de saída (do aceite do gate): **0 falso-descarte** nas falas humanas e
**≥92 %** do eco puro ainda descartado.

Uso:
    ./.venv-mlx/bin/python smoke_eco_regra.py            # tabela + veredito
    ./.venv-mlx/bin/python smoke_eco_regra.py -v         # + cada caso
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

import live_pipeline as lp  # noqa: E402  (depois do sys.path)

MINIMO = 0.6                     # default de `_parece_eco`
ECO_MINIMO_OK = 0.92             # aceite do gate: eco puro segue descartado

# Três referências: resposta curta, média e longa (o dsh entrega parágrafo).
REFS = {
    "curta": "Claro, eu te aviso assim que terminar.",
    "media": ("Então o dia tá bonito hoje e a gente pode aproveitar a tarde no "
              "parque. Depois eu passo na padaria e levo o pão pra casa."),
    "longa": ("Olha, o relatório ficou pronto ontem à noite e o prazo de entrega é "
              "sexta-feira. O número de páginas subiu porque entrou a parte de "
              "metas do trimestre. Se você quiser, eu mando a versão revisada por "
              "e-mail antes da reunião com o cliente."),
}

# (referência, transcript) — frases do USUÁRIO que reusam o vocabulário.
HUMANO = [
    ("media", "e a tarde?"),
    ("media", "e a gente, aproveita?"),
    ("media", "hoje não dá, tenho reunião"),
    ("media", "mas o parque tá cheio"),
    ("media", "o parque tá aberto?"),
    ("media", "não, hoje não"),
    ("media", "cade o pão?"),
    ("media", "quando a gente vai ao parque?"),
    ("longa", "Não, o prazo é outro."),
    ("longa", "e o relatório, ficou pronto?"),
    ("longa", "mas o prazo é segunda, não sexta"),
    ("longa", "quando você manda a versão revisada?"),
    ("longa", "e-mail, não WhatsApp"),
    ("longa", "o cliente confirmou?"),
    ("longa", "onde está a parte de metas?"),
    ("longa", "o relatório ficou pronto, né?"),
    ("longa", "mas por quê?"),
    ("longa", "quantas páginas?"),
    ("longa", "corrige a página três"),
    ("longa", "espera, deixa eu ver"),
    ("longa", "repete a última frase"),
    ("longa", "o prazo é outro, entendi"),
    ("curta", "sim"),
    ("curta", "OK"),
    ("curta", "tá"),
    ("curta", "não, obrigado"),
    ("curta", "avisa hoje ainda?"),
    ("curta", "vou terminar antes"),
    # matriz JÁ aceita no LIVE.md, que não pode regredir
    ("media", "Não, obrigado. Pode ser amanhã."),
]

# (referência, transcript) — o próprio TTS voltando (eco puro).
ECO = [
    # trechos literais: começo, meio, fim
    ("media", "Então o dia tá bonito hoje"),
    ("media", "hoje e a gente pode"),
    ("media", "a gente pode aproveitar"),
    ("media", "a tarde no parque"),
    ("media", "tá bonito hoje e a gente pode"),
    ("media", "aproveitar a tarde no parque"),
    ("media", "Depois eu passo na padaria"),
    ("media", "eu passo na padaria e levo o pão"),
    ("media", "levo o pão pra casa"),
    ("longa", "o relatório ficou pronto"),
    ("longa", "ficou pronto ontem à noite"),
    ("longa", "o prazo de entrega é sexta feira"),
    ("longa", "O número de páginas subiu"),
    ("longa", "subiu porque entrou a parte de metas"),
    ("longa", "entrou a parte de metas do trimestre"),
    ("longa", "a parte de metas do trimestre"),
    ("longa", "a versão revisada"),
    ("longa", "mando a versão revisada por e-mail"),
    ("longa", "antes da reunião com o cliente"),
    ("longa", "da reunião com o cliente"),
    ("curta", "eu te aviso"),
    ("curta", "assim que terminar"),
    ("curta", "Claro, eu te aviso"),
    # variações que o Whisper faz no eco: palavra parecida, e a do "?" que o
    # assistente fez
    ("longa", "o relatório ficou pronto ontem a noite"),
    ("longa", "o prazo de entrega e sexta-feita"),
    ("longa", "ola, o relatorio ficou pronto ontem a noite"),
    ("longa", "a parte de metas do trimestre!"),
    ("media", "entao o dia ta bonito hoje"),
    ("media", "a tarde no parque, depois eu passo"),
    ("curta", "claro, eu te aviso assim q terminar"),
    ("longa", "o prazo de entrega é sexta-feira?"),
]


def classifica(ref: str, transcript: str) -> bool:
    return lp._parece_eco(transcript, REFS[ref], minimo=MINIMO)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verboso", action="store_true")
    args = ap.parse_args()

    fp = [t for ref, t in HUMANO if classifica(ref, t)]          # eco que é humano
    fn = [(ref, t) for ref, t in ECO if not classifica(ref, t)]  # eco que passou
    acertos_eco = len(ECO) - len(fn)

    print(f"matriz de _parece_eco ({len(HUMANO)} humanas, {len(ECO)} ecos, "
          f"minimo={MINIMO})\n")
    for ref, t in HUMANO:
        eco = classifica(ref, t)
        if args.verboso or eco:
            print(f"  {'DESCARTA' if eco else 'segue  '} humano · {t}")
    for ref, t in fn:
        print(f"  {'DESCARTA' if not args.verboso else 'PASSOU '} eco     · {t}")

    taxa = acertos_eco / len(ECO) if ECO else 0.0
    print(f"\nfalso-descarte: {len(fp)}/{len(HUMANO)}  (aceite: 0)")
    print(f"eco descartado: {acertos_eco}/{len(ECO)} = {taxa:.1%}  "
          f"(aceite: >= {ECO_MINIMO_OK:.0%})")
    if fp:
        print("\nfalsos-descartes:")
        for t in fp:
            print(f"  · {t}")
    ok = not fp and taxa >= ECO_MINIMO_OK
    print("\nVEREDITO:", "OK" if ok else "REPROVADO")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
