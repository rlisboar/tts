"""GATE #215 item 6 (#213) — descoberta de modelos com cache FRIO e GETs simultâneos.

Re-derivação por fora: nada do teste do autor. A descoberta é um dublê que CONTA
chamadas e fica preso num Event (é o processo `dsh` subindo), e dois GETs disparam
em threads ao mesmo tempo. Com o lock, o 2º espera e reaproveita: 1 descoberta.
`--mutacao` troca o lock por um contexto vazio (o desenho antigo): 2+ descobertas.

Também cobre o caminho de ERRO (DshError -> 502 por request, sem cachear erro e
sem deixar o lock preso para o próximo).

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-213-dsh-models.py [--mutacao]
"""
import contextlib
import os
import sys
import threading
import time

os.environ.setdefault("TTS_LIVE_WORKER", "0")
os.environ["TTS_ROD_API_KEY"] = "chave-do-gate-215"
os.environ["TTS_CHAT_DSH_BIN"] = "/bin/echo"      # cfg estável, sem depender do dono

import app                                    # noqa: E402
import dsh_client                             # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

MUTACAO = "--mutacao" in sys.argv
cli = TestClient(app.app)
falhas = []


def ok(m):
    print(f"  ✔ {m}")


def falha(m):
    falhas.append(m)
    print(f"  ✘ {m}")


class Contador:
    def __init__(self, erro=False):
        self.chamadas = 0
        self.erro = erro
        self.solto = threading.Event()
        self.travado = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, bin=None, profile=None):
        with self._lock:
            self.chamadas += 1
        self.travado.set()                        # a descoberta COMEÇOU
        self.solto.wait(5)                        # ... e só termina quando soltarmos
        if self.erro:
            raise dsh_client.DshError("dsh fora do ar (gate #215)")
        return {"modelos": [{"id": "m1"}], "efforts": ["off"]}


def limpa_cache():
    app._dsh_models_cache.clear()
    app._chat_dsh_livres.clear()


def main():
    if MUTACAO:
        app._dsh_models_lock = contextlib.nullcontext()
    limpa_cache()

    # ---------------------------------------------------------------- 1 processo
    cont = Contador()
    original = dsh_client.descobrir_modelos
    dsh_client.descobrir_modelos = cont

    respostas = {}

    def pede(i):
        r = cli.get("/api/chat/dsh/models", headers={"x-api-key": "chave-do-gate-215"})
        respostas[i] = r

    ths = [threading.Thread(target=pede, args=(i,)) for i in range(3)]
    for t in ths:
        t.start()
    cont.travado.wait(5)                          # a 1ª descoberta está EM CURSO
    time.sleep(0.15)                              # dá tempo dos outros 2 chegarem
    cont.solto.set()
    for t in ths:
        t.join(10)

    print(f"    descobertas={cont.chamadas} respostas={len(respostas)} "
          f"status={[r.status_code for r in respostas.values()]}")
    if cont.chamadas == 1:
        ok("3 GETs simultâneos com cache frio = 1 descoberta (1 processo)")
    else:
        falha(f"{cont.chamadas} descobertas para 3 GETs simultâneos")
    if all(r.status_code == 200 for r in respostas.values()):
        ok("os 3 GETs responderam 200 (nenhum ficou preso nem falhou)")
    else:
        falha(f"status inesperados: {[r.status_code for r in respostas.values()]}")

    # caminho quente: cache válido não chama de novo
    antes = cont.chamadas
    cli.get("/api/chat/dsh/models", headers={"x-api-key": "chave-do-gate-215"})
    if cont.chamadas == antes:
        ok("cache quente reaproveita (nenhuma descoberta nova)")
    else:
        falha("cache quente disparou descoberta")

    # ------------------------------------------------------------------- erro
    limpa_cache()
    cont_erro = Contador(erro=True)
    cont_erro.solto.set()
    dsh_client.descobrir_modelos = cont_erro
    r1 = cli.get("/api/chat/dsh/models", headers={"x-api-key": "chave-do-gate-215"})
    if r1.status_code == 502:
        ok("erro do dsh vira 502 explicativo (por request)")
    else:
        falha(f"erro do dsh devolveu {r1.status_code}")
    cont_erro.erro = False
    r2 = cli.get("/api/chat/dsh/models", headers={"x-api-key": "chave-do-gate-215"})
    if r2.status_code == 200:
        ok("depois do erro o lock está LIVRE e a descoberta seguinte passa (erro não cacheia)")
    else:
        falha(f"após o erro a chamada seguinte devolveu {r2.status_code} (lock preso ou erro cacheado)")

    dsh_client.descobrir_modelos = original
    print(f"\nfalhas: {len(falhas)}")
    for f in falhas:
        print(f"  - {f}")
    print("VEREDITO:", "OK" if not falhas else "FALHOU")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())