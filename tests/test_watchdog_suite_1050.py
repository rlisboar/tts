"""#1050: a suíte não arma o prazo C do watchdog do /health.

O `faulthandler.dump_traceback_later` despeja de uma thread C sem a GIL. Se o
laço que rearma só atrasou (CPU disputada) e o principal segue em Python, o
despejo lê frame liberado e falha em `dump_frame`; o rearme seguinte espera essa
thread segurando a GIL e o processo congela com o `pytest-timeout` mudo.
Reproduzido em 2026-11-09: prazo de 2 ms + laço de `np.mean` → SIGSEGV
(rc=139) fora do pytest e 98,7 % de CPU parado sob pytest, com `sample`
mostrando `faulthandler_thread → dump_frame` e `cancel_dump_traceback_later`
preso em `PyThread_acquire_lock_timed`.
"""

import faulthandler
import os
import time

from fastapi.testclient import TestClient

import app


def test_sessao_nasce_sem_prazo_c_e_com_log_isolado():
    assert app._SAUDE_TRAVADO_S == 0, app._SAUDE_TRAVADO_S
    arq = os.path.realpath(app._SAUDE_WATCHDOG_ARQ)
    assert arq.startswith(os.path.realpath(os.environ["TTS_STATE_DIR"])), arq


def test_health_e_laco_nao_armam_o_despejo_c(monkeypatch):
    armados = []
    monkeypatch.setattr(faulthandler, "dump_traceback_later",
                        lambda *a, **k: armados.append((a, k)))
    with TestClient(app.app) as cliente:
        assert cliente.get("/health").status_code == 200
    # o laço `health-watchdog` passa a cada 0,05–0,25 s
    time.sleep(0.6)
    app._saude_rearma()
    assert armados == [], armados
