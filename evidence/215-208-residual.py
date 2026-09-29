"""GATE #215 item 1 (#208) — RESIDUAL: o observador PERDEDOR zera os contadores.

O fix fechou o re-arm (merge + lock) e o roteiro do PM cobria exatamente isso:
"os contadores não podem voltar a valores antigos NO RE-ARM". Este script olha o
outro caminho de `_live_descarrega_pendente` — o `if not pendente:` — que NÃO foi
tocado e continua zerando `pendentes_trechos`/`pendentes_descartados_ms`.

Dois observadores acordados pelo mesmo fechamento é o caso NORMAL (está na
docstring: "com vários observadores acordados pelo mesmo fechamento, só o primeiro
processa"): o perdedor faz `pop` e recebe None; o vencedor re-arma. O perdedor zera
os contadores DEPOIS de o vencedor ter contado os trechos e ANTES de ele re-armar →
o pendente que volta a existir fica com `trechos = 0` e o próximo evento
`turno_pendente` SUB-CONTA (mesma família do #208, caminho diferente).

Controle: sem o perdedor, os mesmos passos mantêm `trechos = 1` (é o esperado).

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-208-residual.py
"""
import logging
import queue
import sys
import threading
import time

import app

logging.getLogger("live").setLevel(logging.WARNING)

A = b"\x01" * 10
B = b"\x02" * 10
falhas = []


class PipePortao:
    """`ocupado` responde False na 1ª leitura de cada thread (sai do laço) e True
    nas seguintes (o `pop` já aconteceu → o caminho é o RE-ARM). O portão garante
    que os dois observadores cheguem juntos ao laço."""

    def __init__(self):
        self.portao = threading.Event()
        self._vistos = set()
        self._l = threading.Lock()

    @property
    def ocupado(self):
        tid = threading.get_ident()
        with self._l:
            primeiro = tid not in self._vistos
            self._vistos.add(tid)
        if primeiro:
            self.portao.wait(5)
            return False
        return True

    def cancel(self):
        pass

    def end_of_speech(self):
        pass


def sessao(pipe):
    return {"id": "s-res", "fila": queue.Queue(), "buffer": bytearray(),
            "buffer_consumido": 0, "pipe": pipe, "fechar": False, "turno": 0,
            "history": [], "truncado": False, "turno_pendente": None,
            "turno_pendente_barge": False, "pendentes_trechos": 0,
            "pendentes_descartados_ms": 0}


def eventos():
    """Captura os eventos `turno_pendente` que o guarda emite."""
    vistos = []
    original = app._live_envia_json
    app._live_envia_json = lambda s, o: (vistos.append(o)
                                         if o.get("type") == "turno_pendente" else None)
    return vistos, original


def cenario(com_perdedor: bool):
    pipe = PipePortao()
    sess = sessao(pipe)
    vistos, original = eventos()
    lento = app._live_pend_rearma

    def rearma_lento(s, p, b):     # dá tempo do perdedor passar pelo `pop`
        time.sleep(0.4)
        return lento(s, p, b)

    app._live_pend_rearma = rearma_lento
    try:
        app._live_guarda_pendente(sess, A, False)     # arma O1 (preso no portão)
        if com_perdedor:
            threading.Thread(target=app._live_descarrega_pendente,
                             args=(sess,), daemon=True).start()
        time.sleep(0.25)                              # os dois no portão
        pipe.portao.set()                             # solta: um faz pop, o outro None
        time.sleep(1.5)
        # segunda fala: o evento tem de dizer 2 trechos (A já estava lá)
        app._live_guarda_pendente(sess, B, False)
        time.sleep(0.3)
        return sess, vistos
    finally:
        app._live_pend_rearma = lento
        app._live_envia_json = original


def main():
    for rotulo, com_perdedor in (("controle (1 observador)", False),
                                 ("com perdedor", True)):
        sess, vistos = cenario(com_perdedor)
        estado = {"turno_pendente": len(sess.get("turno_pendente") or b""),
                  "trechos": sess.get("pendentes_trechos"),
                  "descartados_ms": sess.get("pendentes_descartados_ms"),
                  "eventos_trechos": [e.get("trechos") for e in vistos]}
        print(f"  · {rotulo}: {estado}")
        if com_perdedor:
            if estado["turno_pendente"] == len(A) + len(B):
                print("  ✔ o conteúdo sobreviveu (o fix do re-arm segura)")
            else:
                falhas.append("o conteúdo se perdeu — é o #208 de novo")
                print("  ✘ conteúdo perdido")
            if estado["trechos"] == 0:
                print("  ✘ `pendentes_trechos` zerado com pendente VIVO (subconta)")
                falhas.append("contadores zerados pelo observador perdedor")
            else:
                print("  ✔ contadores preservados")
            if estado["eventos_trechos"][-1] != 2:
                print(f"  ✘ evento diz trechos={estado['eventos_trechos'][-1]} "
                      f"(2 trechos acumulados)")
                falhas.append("evento `turno_pendente` subconta os trechos")
            else:
                print("  ✔ evento conta os 2 trechos")
        else:
            if estado["trechos"] == 2 and estado["eventos_trechos"] == [1, 2]:
                print("  ✔ controle: sem perdedor, os contadores ficam corretos")
            else:
                print(f"  ✘ controle inesperado: {estado}")
                falhas.append("controle não bateu")

    print(f"\nfalhas: {len(falhas)}")
    for f in falhas:
        print(f"  - {f}")
    print("VEREDITO:", "OK" if not falhas else "RESIDUAL CONFIRMADO")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())