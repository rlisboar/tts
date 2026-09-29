"""GATE #215 item 1 (#208) — re-derivação POR FORA, com corrida REAL de threads.

O teste do autor reproduz a janela à mão (pop + re-arm manuais, uma vez). Aqui o
observador de VERDADE (`_live_descarrega_pendente`, armado por
`_live_acorda_pendente`) corre numa thread enquanto o guarda acumula da principal,
e o pipeline ABRE E FECHA sozinho — o cenário em que o re-arm existe (fechou e
reabriu outro turno no intervalo).

Invariantes cobradas no fim:
  A) nenhum byte falado na janela some: o que o guarda acumulou é o que o turno
     aberto recebe (ou segue no pendente);
  B) `pendentes_trechos` nunca DECRESCE (o re-arm antigo regravava o valor lido e
     subcontava).

`--mutacao` usa o `_live_descarrega_pendente` PRÉ-fix, verbatim do commit 4729512^
(sem lock, pop sem serialização, contadores restaurados) — é a prova de mordida.
`--janela-ms N` alarga a janela pop→re-arm (o natural é microscópico): no mundo com
fix, dorme N ms depois de soltar o lock; no mutante, dorme N ms logo após o pop.

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-208-corrida.py [--mutacao]
     [--janela-ms N] [--segundos S]
"""
import logging
import queue
import random
import sys
import threading
import time

import app

logging.getLogger("live").setLevel(logging.WARNING)

MUTACAO = "--mutacao" in sys.argv
JANELA_MS = (float(sys.argv[sys.argv.index("--janela-ms") + 1])
             if "--janela-ms" in sys.argv else 0.0)
SEGUNDOS = (float(sys.argv[sys.argv.index("--segundos") + 1])
            if "--segundos" in sys.argv else 1.2)

DIAG = {"rearmes": 0, "janela_com_bytes": 0, "bytes_na_janela": 0, "quedas": 0}


class LockJanela:
    """Embrulho do lock da sessão: dorme a janela logo APÓS soltar."""

    def __init__(self, real):
        self.real = real

    def __enter__(self):
        self.real.acquire()
        return self

    def __exit__(self, *exc):
        self.real.release()
        if JANELA_MS:
            time.sleep(JANELA_MS / 1000.0)
        return False


class PipeFalso:
    """Pipeline que abre e fecha sozinho: é o que faz o observador RE-ARMAR."""

    def __init__(self):
        self.ocupado = True
        self.cancelado = False

    def cancel(self):
        self.cancelado = True

    def end_of_speech(self):
        pass


def descarrega_prefix(sess):
    """`_live_descarrega_pendente` PRÉ-fix, verbatim (só o corpo relevante)."""
    pipe = sess.get("pipe")
    if pipe is None:
        return
    try:
        while not sess.get("fechar") and pipe.ocupado:
            time.sleep(0.02)
        pendente = sess.pop("turno_pendente", None)
        if JANELA_MS:                       # alarga a janela real (é microscópica)
            time.sleep(JANELA_MS / 1000.0)
        barge = bool(sess.pop("turno_pendente_barge", False))
        if not pendente or sess.get("fechar"):
            app._live_pendente_zera_contadores(sess)
            return
        trechos = sess.get("pendentes_trechos", 0)
        descartados_ms = sess.get("pendentes_descartados_ms", 0)
        if pipe.ocupado:                    # fechou e abriu outro no intervalo
            DIAG["rearmes"] += 1
            novo = len(sess.get("turno_pendente") or b"")
            if novo:
                DIAG["janela_com_bytes"] += 1
                DIAG["bytes_na_janela"] += novo
            sess["turno_pendente"] = pendente
            sess["turno_pendente_barge"] = barge
            sess["pendentes_trechos"] = trechos          # <- subconta
            sess["pendentes_descartados_ms"] = descartados_ms
            return
        app._live_pendente_zera_contadores(sess)
        app._live_abre_turno(sess, pendente, barge_in=barge)
    finally:
        if sess.get("turno_pendente") and not sess.get("fechar"):
            app._live_acorda_pendente(sess)


def sessao(pipe):
    return {"id": "s-208", "fila": queue.Queue(), "buffer": bytearray(),
            "buffer_consumido": 0, "pipe": pipe, "fechar": False, "turno": 0,
            "history": [], "truncado": False, "turno_pendente": None,
            "turno_pendente_barge": False}


def main():
    if MUTACAO:
        app._live_descarrega_pendente = descarrega_prefix
    elif JANELA_MS:
        reais = {}
        original = app._live_pend_lock
        app._live_pend_lock = lambda s: reais.setdefault(id(s), LockJanela(original(s)))
    if not MUTACAO:
        original_rearma = app._live_pend_rearma

        def com_diag(sess, pendente, barge):
            DIAG["rearmes"] += 1
            novo = len(sess.get("turno_pendente") or b"")
            if novo:
                DIAG["janela_com_bytes"] += 1
                DIAG["bytes_na_janela"] += novo
            return original_rearma(sess, pendente, barge)

        app._live_pend_rearma = com_diag

    pipe = PipeFalso()
    sess = sessao(pipe)
    abertos = []
    app._live_abre_turno = lambda s, pcm=b"", barge_in=False: abertos.append(bytes(pcm))

    total = bytearray()
    queda = []
    menor = 0
    ultimo_aberto = 0
    rnd = random.Random(7)
    parar = threading.Event()

    def alterna():
        """O turno do assistente fecha e outro abre: é o gatilho do re-arm."""
        while not parar.is_set():
            time.sleep(0.03)
            pipe.ocupado = not pipe.ocupado
            if not pipe.ocupado:
                app._live_acorda_pendente(sess)

    threading.Thread(target=alterna, daemon=True).start()

    fim = time.time() + SEGUNDOS
    i = 0
    while time.time() < fim:
        i += 1
        pedaco = bytes([i % 251 + 1]) * rnd.randint(1, 40)
        total += pedaco
        app._live_guarda_pendente(sess, pedaco, barge_in=(i % 7 == 0))
        if len(abertos) > ultimo_aberto:      # turno abriu: zerou os contadores (legítimo)
            ultimo_aberto = len(abertos)
            menor = 0
        vistos = sess.get("pendentes_trechos", 0)
        # Suspeita é cair para um valor POSITIVO menor SEM turno ter aberto: é a
        # assinatura do re-arm antigo, que regravava o número lido antes do pop.
        if vistos < menor:
            queda.append((i, menor, vistos))
            DIAG["quedas"] += 1
        else:
            menor = vistos
        time.sleep(rnd.uniform(0, 0.002))

    parar.set()
    pipe.ocupado = False
    limite = time.time() + 5
    while time.time() < limite:
        if not sess.get("turno_pendente") and not sess.get("pend_thread", None):
            break
        app._live_acorda_pendente(sess)
        time.sleep(0.01)

    entregue = b"".join(abertos) + bytes(sess.get("turno_pendente") or b"")
    perdidos = len(total) - len(entregue)
    print(f"mutacao={MUTACAO} janela={JANELA_MS}ms acumulado={len(total)}B "
          f"entregue={len(entregue)}B perdidos={perdidos}B "
          f"quedas_de_contador={DIAG['quedas']} rearmes={DIAG['rearmes']} "
          f"janelas_com_bytes={DIAG['janela_com_bytes']}")
    if perdidos:
        pos = next((k for k in range(min(len(total), len(entregue)) + 1)
                    if total[:k] != entregue[:k]), 0)
        print(f"  primeira divergencia no byte {pos} (bytes perdidos na janela)")
    ok = perdidos == 0 and not DIAG["quedas"]
    print("VEREDITO:", "OK" if ok else "FALHOU")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())