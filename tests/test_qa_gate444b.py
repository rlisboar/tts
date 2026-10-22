"""GATE do #364 (`task_985c5f71`): shutdown sem órfão e jobs sobrevivendo ao restart.

O par que faz o restart doer: `os._exit(0)` no shutdown largava pipeline Live,
worker de job e dsh quente, e `_jobs` volátil fazia o cliente levar 404 no meio do
stream. Aqui o órfão é medido com processo DE VERDADE e `ps`, não com dublê.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import app
from test_ciclo_vida import isolado  # noqa: F401
from test_api import auth, client  # noqa: F401

__all__ = ["auth", "client", "isolado"]


def _vivo(pid: int) -> bool:
    saida = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(saida) and "Z" not in saida


def _espia_filho(tmp: Path) -> subprocess.Popen:
    filho = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    d = tmp / ".job-voo"
    d.mkdir()
    (d / "worker.pid").write_text(str(filho.pid))
    (d / "owner.pid").write_text(str(os.getpid()))
    return filho


def test_shutdown_nao_deixa_orfao_e_salva_historico(isolado):  # noqa: F811
    filho = _espia_filho(isolado)
    sess = {"id": "s1", "cancelado": threading.Event(), "fechar": False,
            "fila": type("F", (), {"bytes": 0, "descartados": 0})(),
            "pipe": type("P", (), {"cancel": lambda self: None})()}
    app._live_sessions["s1"] = sess
    monkeypatch_job = {**app._JOB_BASE, "status": "running"}
    app._jobs["voo"] = monkeypatch_job
    try:
        assert _vivo(filho.pid), "o filho nem subiu"
        feito = app._encerra_tudo("gate")
        assert feito["workers"] == 1 and feito["live"] == 1
        for _ in range(60):
            if not _vivo(filho.pid):
                break
            time.sleep(0.05)
        assert not _vivo(filho.pid), "worker ficou órfão (ps ainda o mostra)"
        assert app._JOBS_HIST_PATH.exists(), "histórico não foi salvo"
        # `_jobs` continua conhecido no processo: nada de 404 no meio do stream
        assert "voo" in app._jobs
    finally:
        if filho.poll() is None:
            filho.kill()


def test_job_em_voo_vira_error_com_motivo_depois_do_restart(isolado):  # noqa: F811
    app._jobs["voo"] = {**app._JOB_BASE, "status": "running"}
    app._encerra_tudo("gate")
    app._jobs.clear()
    app._jobs_hist_carrega()                     # o boot seguinte
    assert app._jobs["voo"]["status"] == "error"
    assert "rein" in app._jobs["voo"]["error"].lower(), app._jobs["voo"]["error"]


def test_status_de_job_conhecido_nao_da_404_depois_do_restart(client, auth, isolado):  # noqa: F811
    app._jobs["voo"] = {**app._JOB_BASE, "status": "running"}
    app._encerra_tudo("gate")
    app._jobs.clear()
    app._jobs_hist_carrega()
    r = client.get("/api/tts/jobs/voo", headers=auth)
    assert r.status_code == 200, r.status_code
    assert r.json()["status"] == "error"


def test_historico_recente_aparece_na_listagem(client, auth, isolado):  # noqa: F811
    app._jobs["voo"] = {**app._JOB_BASE, "status": "running"}
    app._encerra_tudo("gate")
    app._jobs.clear()
    app._jobs_hist_carrega()
    r = client.get("/api/tts/jobs/voo", headers=auth)      # conhecido: some do 404
    assert r.status_code == 200
    hist = app._JOBS_HIST_PATH.read_text()
    assert '"voo"' in hist, "o job não entrou no histórico em disco"
    r = client.get("/api/outputs", headers=auth)           # listagem não 404a
    assert r.status_code == 200


def test_shutdown_chama_teardown_antes_do_exit(client, auth, isolado, monkeypatch):  # noqa: F811
    """Mordida do órfão: com `os._exit` antes do teardown nada é fechado."""
    ordem = []
    monkeypatch.setattr(app, "_encerra_tudo", lambda m="x": ordem.append(m) or {})
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    monkeypatch.setattr(app.os, "_exit", lambda c: ordem.append("exit"))
    assert client.post("/api/shutdown", headers=auth).status_code == 200
    for _ in range(40):
        if len(ordem) >= 2:
            break
        time.sleep(0.05)
    assert ordem[:2] == ["api", "exit"], ordem
