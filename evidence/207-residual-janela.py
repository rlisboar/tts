"""#207 residual: `close()` caindo DENTRO do `_LiveWorker.start()` deixa filho órfão?

A janela coberta pelo fix é a da CRIAÇÃO do worker (teste
`test_close_na_criacao_do_worker_nao_deixa_filho_orfao`, que fecha dentro do
`__init__`). A que este probe mede é a seguinte, e é mais larga: o `close()` cai
depois de `self._worker = w` e DURANTE o `w.start()`, que gasta centenas de ms
subindo o filho e esperando o `ready`.

Nessa ordem: o `close()` vê `self._worker` (chama `fecha()` sem nada para matar,
porque o processo ainda não existe), solta a referência (`self._worker = None`) e
volta; o `w.start()` termina de subir o filho; o `if self._saiu` do `start()` chama
`close()` de novo — e agora `self._worker` já é `None`, então o filho que acabou de
nascer não é fechado por ninguém. Órfão com o modelo na RAM, exatamente a classe
do #207.

Uso:  ./.venv-mlx/bin/python evidence/207-residual-janela.py
Sai 0 se o furo existe (o probe REPROVA o fix no ponto), 1 se já está coberto.
"""
import queue
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))

import app                                        # noqa: E402
import live_pipeline as lp                        # noqa: E402
from test_live_worker import STUB                 # noqa: E402
import os

STUB_PATH = Path(f"/tmp/207-stub-worker-{os.getpid()}.py")   # sufixo: paralelo não troca o stub
STUB_PATH.write_text(STUB)


def sessao(nome="residual"):
    return {"id": nome, "voice_id": None, "system": None, "history": [],
            "fila": queue.Queue()}


criados = []


class WorkerStub(lp._LiveWorker):
    """Worker REAL apontando para o stub (sem venv, sem modelo)."""

    def __init__(self, **kw):
        super().__init__(py=sys.executable, script=str(STUB_PATH),
                         timeout_s=5, **{k: v for k, v in kw.items()
                                         if k != "voice_id"},
                         voice_id=kw.get("voice_id"))
        criados.append(self)


lp._LiveWorker = WorkerStub
lp._worker_habilitado = lambda: True

pipe = app._live_pipe_novo(sessao())
pipe._prewarm = lambda **kw: None

original = WorkerStub.start


def cenario(rotulo, fechar_antes_do_spawn):
    """Roda um cenário e devolve (orfaozinho, linhas de diagnóstico).

    `fechar_antes_do_spawn=True` põe o `close()` na janela entre a atribuição
    `self._worker = w` e a criação do processo; `False` põe depois do spawn."""
    criados.clear()
    pipe = app._live_pipe_novo(sessao(rotulo))
    pipe._prewarm = lambda **kw: None

    def start_espiao(self):
        if fechar_antes_do_spawn:
            pipe.close()              # o processo ainda NÃO existe
        original(self)
        if not fechar_antes_do_spawn:
            pipe.close()              # o processo já existe

    WorkerStub.start = start_espiao
    t0 = time.perf_counter()
    pipe.start()
    dt = time.perf_counter() - t0

    w = criados[0] if criados else None
    vivo = bool(w and w._proc is not None and w._proc.poll() is None)
    orfao = bool(w and (vivo or w.ativo))
    linhas = [f"  · {rotulo}: start() {dt * 1000:.0f} ms · _saiu={pipe._saiu} · "
              f"pipe._worker={'None' if pipe._worker is None else 'setado'} · "
              f"filho vivo={vivo} · ativo={w.ativo if w else '-'}"]
    if orfao:
        linhas.append("    ✘ ÓRFÃO: o filho nasceu depois do `close()` e ninguém o fechou")
    else:
        linhas.append("    ✔ o filho foi fechado junto com a sessão")
    if w:
        w.fecha()
    return orfao, linhas


orc_a, lin_a = cenario("close ANTES do spawn", True)
orc_b, lin_b = cenario("close DEPOIS do spawn", False)
for l in lin_a + lin_b:
    print(l)

orfao = orc_a or orc_b
if orc_a:
    print("  ✘ FURO: a janela entre `self._worker = w` e o Popen deixa o filho órfão")
if not orfao:
    print("  ✔ coberto nas duas janelas")
sys.exit(0 if orfao else 1)