#!/usr/bin/env python3
"""Worker ISOLADO do classificador de intenção de interrupção (#507).

Roda o modelo num processo filho pelo mesmo princípio do `tts_worker`: se o
Metal estourar, morre só ele — o app e a sessão Live continuam, e o chamador cai
no `sem_decisao` da cadeia.

Modo único, NDJSON em stdin/stdout (o pai abre uma vez por sessão e manda pedidos):

    {"id": 1, "ref": "<fala do assistente>", "fala": "<fala do usuário>", "idioma": "pt"}

Resposta:

    {"id": 1, "p_cortar": 0.97, "ms": 104}

A resposta NÃO traz rótulo: quem compara com o limiar é o PAI (`live_intencao`).
Campo de rótulo aqui virou armadilha quando passou a ser recalculado lá — o do
worker podia divergir do limiar e ninguém via (#507).

A decisão é por LOGITS de UM passo: o prompt fecha em `Resposta:` e a
probabilidade sai do `softmax` de dois tokens de rótulo (` sim` / ` não`). Não
gera texto, não amostra — determinístico pelos logits.
"""

from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODELO = os.environ.get("TTS_INTENCAO_MODELO",
                        "mlx-community/Qwen2.5-3B-Instruct-4bit")

# Prompt curto e FIXO (medido: 98,6 % no corpus do gate, p95 ~107 ms). Os dois
# últimos exemplos existem porque são os casos que o modelo errava sem eles:
# backchannel COM conteúdo ("sim, sim, continua") e agradecimento.
CABECALHO = (
    "Leia o diálogo e responda: o usuário quer INTERROMPER (sim) ou está só "
    "concordando/comentando e pode deixar o assistente terminar (não)?\n"
    "Interromper = pede outra coisa, corrige, pergunta, manda parar. "
    "Não interromper = backchannel, concordância curta, riso.\n\n"
)
EXEMPLOS = (
    ("o prazo é sexta", "mas e o preço?", "sim"),
    ("o prazo é sexta", "uhum, ok", "não"),
    ("the deadline is friday", "wait, go back", "sim"),
    ("the deadline is friday", "sure, go on", "não"),
    ("vou falar do cronograma hoje", "sim, sim, continua", "não"),
    ("vou explicar a política nova", "entendi, obrigado", "não"),
    ("o relatório fica pronto amanhã", "hmm, espera", "sim"),
)


def monta_prompt(ref: str, fala: str) -> str:
    p = CABECALHO
    for r, f, y in EXEMPLOS:
        p += f"Assistente: {r}\nUsuário: {f}\n=> {y}\n"
    return p + f"Assistente: {ref}\nUsuário: {fala}\n=>"


def main() -> int:
    import mlx.core as mx
    from mlx_lm import load

    modelo, tok = load(MODELO)
    id_sim = tok.encode(" sim")[0]
    id_nao = tok.encode(" não")[0]

    def _log(msg: str) -> None:
        print(f"[intencao] {msg}", file=sys.stderr, flush=True)

    _log(f"modelo {MODELO} carregado")
    # aquece (compila o grafo): o 1º passo custa ~600 ms e não pode ser o do turno
    t = time.perf_counter()
    lg = modelo(mx.array(tok.encode(monta_prompt("oi", "oi")))[None])
    mx.eval(lg)
    _log(f"aquecido em {round((time.perf_counter() - t) * 1000)} ms")
    print(json.dumps({"tipo": "pronto"}), flush=True)

    for linha in sys.stdin:
        linha = linha.strip()
        if not linha:
            continue
        try:
            pedido = json.loads(linha)
        except Exception:  # noqa: BLE001
            continue
        if pedido.get("tipo") == "close":
            break
        t0 = time.perf_counter()
        try:
            ids = mx.array(tok.encode(monta_prompt(pedido.get("ref") or "",
                                                   pedido.get("fala") or "")))[None]
            logits = modelo(ids)
            mx.eval(logits)
            par = mx.softmax(mx.array([float(logits[0, -1, id_sim]),
                                       float(logits[0, -1, id_nao])]))
            p_cortar = float(par[0])
            ms = int((time.perf_counter() - t0) * 1000)
            print(json.dumps({"id": pedido.get("id"), "p_cortar": p_cortar,
                              "ms": ms}), flush=True)
        except Exception as exc:  # noqa: BLE001 — o pai cai no `sem_decisao`
            print(json.dumps({"id": pedido.get("id"), "erro": f"{type(exc).__name__}: {exc}"}),
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
