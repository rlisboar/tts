"""Ciclo de vida do job TTS: watchdog e cancelamento (#800).

Sem watchdog um worker travado deixa o laço de polling girando para sempre
segurando `_gen_lock` — o app inteiro para de gerar. Sem rota de cancelar, um
pedido errado só termina quando o worker quiser.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import app  # noqa: E402


class _ProcMudo:
    """Worker que sobe e nunca escreve status: é o travamento."""

    def __init__(self):
        self.pid = 999999          # pgid cai no fallback `terminate()`
        self.returncode = None
        self.morto = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        if not self.morto:
            raise TimeoutError
        return 0

    def terminate(self):
        self.morto = True

    def kill(self):
        self.morto = True


def _prepara(monkeypatch, tmp_path, proc):
    monkeypatch.setitem(app._settings, "tts_marca_dagua", False)
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(app, "_unload_local_tts", lambda *a, **k: None)
    monkeypatch.setattr(app, "_schedule_job_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(app, "_jobs_hist_salva", lambda *a, **k: None)
    monkeypatch.setattr(app, "_piece_dir", lambda jid: tmp_path / jid)
    monkeypatch.setattr(app, "BASE", tmp_path)
    monkeypatch.setattr(app, "_gen_lock", threading.RLock())
    app._jobs["j"] = {"status": "queued"}


def _roda(job_id="j"):
    app._run_tts_job_isolated(job_id, "oi", "v", None, "pt", {},
                              {"id": "kokoro", "family": "kokoro", "meta": {}})


def test_worker_travado_morre_no_watchdog_e_libera_a_trava(monkeypatch, tmp_path):
    """Sem progresso por `_JOB_PARADO_S`, o job morre em erro — e solta a trava."""
    proc = _ProcMudo()
    _prepara(monkeypatch, tmp_path, proc)
    monkeypatch.setattr(app, "_JOB_PARADO_S", 0.4)
    monkeypatch.setattr(app, "_JOB_TETO_S", 60.0)

    _roda()

    assert app._jobs["j"]["status"] == "error"
    assert "travou" in app._jobs["j"]["error"]
    assert proc.morto, "o worker não foi morto"
    assert not app._gen_lock._is_owned(), "a trava ficou presa"


def test_cancelar_mata_o_worker_e_marca_o_job(monkeypatch, tmp_path):
    """`cancelado` no job tem de derrubar o worker na volta seguinte."""
    proc = _ProcMudo()
    _prepara(monkeypatch, tmp_path, proc)
    monkeypatch.setattr(app, "_JOB_PARADO_S", 60.0)
    monkeypatch.setattr(app, "_JOB_TETO_S", 600.0)

    def _cancela_depois():
        time.sleep(0.4)
        app._jobs["j"]["cancelado"] = True

    threading.Thread(target=_cancela_depois, daemon=True).start()
    _roda()

    assert app._jobs["j"]["status"] == "error"
    assert "cancelado" in app._jobs["j"]["error"]
    assert proc.morto


def test_rota_de_cancelar_recusa_job_terminado(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app._jobs["feito"] = {"status": "done"}
    app._jobs["roda"] = {"status": "running"}
    c = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 1))
    try:
        assert c.post("/api/tts/jobs/naoexiste/cancel").status_code == 404
        assert c.post("/api/tts/jobs/feito/cancel").status_code == 409
        r = c.post("/api/tts/jobs/roda/cancel")
        assert r.status_code == 200
        assert app._jobs["roda"]["cancelado"] is True
    finally:
        app._jobs.pop("feito", None)
        app._jobs.pop("roda", None)
