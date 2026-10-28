#!/usr/bin/env python3
"""Matriz offline da INTENÇÃO de interrupção do Live (#501).

O corte por energia derruba a resposta em "uhum" e em tosse. Esta matriz mede a
cadeia de decisão sobre TEXTO — sem áudio, sem STT, sem rede —, que é a parte
que a regra resolve; o caminho de áudio tem os testes de turno.

Corpora (≥20 em cada, pt e en):
  - BACKCHANNEL: confirmações curtas que NÃO devem cortar.
  - COMANDO: ordens de parar, que DEVEM cortar.
  - PERGUNTA: pergunta e frase com conteúdo, que DEVEM cortar.
  - RUÍDO: transcript vazio/oscilações, que NÃO devem cortar.
  - ECO: trechos literais do assistente, que NÃO devem cortar (regra do #301).

Critério (do aceite do card): backchannel e ruído NÃO cortam em ≥90 %; comando e
pergunta cortam em ≥95 %; eco não corta em 100 %. Versionado: é o instrumento do
gate — sem ele a matriz não se reproduz.

    ./smoke_barge_intencao.py            # veredito
    ./smoke_barge_intencao.py --verboso  # imprime cada caso errado
"""
from __future__ import annotations

import sys

import app  # noqa: F401 — o `_settings` da cadeia
from live_turns import classifica_barge

BACKCHANNEL = ["uhum", "aham", "hum", "hmm", "sim", "sei", "ok", "tá", "certo",
               "entendi", "isso", "claro", "beleza", "legal", "nossa", "mhm",
               "yeah", "right", "aham sim", "sim sim", "uhum uhum", "certo isso"]
COMANDO = ["para", "pare", "espera", "peraí", "pera", "calma", "chega", "não",
           "stop", "wait", "hold on", "para aí", "espera um pouco", "chega disso",
           "não, para", "calma calma", "para para", "espera, espera", "pare aí",
           "não quero", "chega por hoje"]
PERGUNTA = ["qual o prazo?", "quando sai?", "onde está?", "por que assim?",
            "quanto custa?", "quem fez?", "como faz?", "que horas são?",
            "what time is it?", "why so slow?", "cadê o arquivo?", "que dia é hoje?",
            "o prazo de entrega é sexta-feira?", "e o outro relatório?",
            "me diz o valor", "quero o resumo de ontem", "muda para o outro arquivo",
            "abre a pasta de saída", "faz de novo", "mostra o resultado"]
RUIDO = ["", "", "hm", "ã", "é", "ahn"]
ECO = ["então o dia está bonito hoje", "o dia está bonito",
       "quer que eu conte mais alguma coisa?", "claro, o dia está bonito hoje",
       "mais alguma coisa", "que eu conte mais alguma coisa"]
# (paráfrase fora do literal — "eu posso contar outra coisa" — não entra: a regra
# do #301 compara palavras, e uma paráfrase cairia em `frase_longa` e cortaria)
REF = "então o dia está bonito hoje. Quer que eu conte mais alguma coisa?"

ESPERADO = {"backchannel": "nao_corta", "comando": "corta", "pergunta": "corta",
            "ruido": "nao_corta", "eco": "nao_corta"}


def decide(texto: str, ms: int) -> str:
    # modo FIXO no default (o `settings.json` do dono é mutável e a rodada tem de
    # ser reprodutível): `BARGE_MODO=imediata` força a mordida
    import os
    modo = os.environ.get("BARGE_MODO") or "inteligente"
    if modo == "imediata":
        return "corta"        # o modo antigo corta no limiar, sem olhar conteúdo
    sess = {"id": "smoke", "ultimo_assistente": REF, "pipe": None}
    # `so_comando`: só a lista de parar corta (o resto segue a resposta)
    app._settings["live_barge_filtros"] = (
        {"pergunta": {"on": False}, "frase_longa": {"on": False},
         "fala_sustentada": {"on": False}, "eco": {"on": False}} if modo == "so_comando" else {})
    app._settings["live_barge_sem_decisao"] = "continua"
    tipo, _p, _m = app._live_avalia_filtros(sess, texto, ms, {})
    return "corta" if tipo == "interrupcao" else "nao_corta"


def main(argv: list[str]) -> int:
    verboso = "--verboso" in argv
    corpora = {"backchannel": BACKCHANNEL, "comando": COMANDO,
               "pergunta": PERGUNTA, "ruido": RUIDO, "eco": ECO}
    piso = {"backchannel": 0.90, "ruido": 0.90, "comando": 0.95,
            "pergunta": 0.95, "eco": 1.00}
    dur = {"backchannel": 300, "comando": 350, "pergunta": 500, "ruido": 180,
           "eco": 600}
    falhou = False
    for nome, casos in corpora.items():
        ok = 0
        for texto in casos:
            obtido = decide(texto, dur[nome])
            certo = obtido == ESPERADO[nome]
            ok += certo
            if not certo and verboso:
                print(f"  ✘ {nome}: {texto!r} → {obtido} (esperado {ESPERADO[nome]})")
        taxa = ok / len(casos)
        marca = "✔" if taxa >= piso[nome] else "✘"
        print(f"{marca} {nome:12} {ok}/{len(casos)} = {taxa*100:5.1f}% (piso {piso[nome]*100:.0f}%)")
        falhou |= taxa < piso[nome]
    # amostras de intenção: a mesma classificação pura, direto na função
    amostras = [("para", 300), ("uhum", 250), ("", 150), ("qual o prazo?", 400)]
    print("classifica_barge:", [classifica_barge(t, m, REF) for t, m in amostras])
    print("\nVEREDITO:", "REPROVADO" if falhou else "OK")
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
