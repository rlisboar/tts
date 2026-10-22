"""Ciclo de vida: teardown limpo (#358) e histórico de jobs que sobrevive ao boot.

POR QUE ESTE ARQUIVO EXISTE: o par que faz um restart doer. `os._exit(0)` no
shutdown não fechava pipeline Live, worker de job nem dsh quente (o boot seguinte
só recolhia parte), e `_jobs` volátil fazia o cliente levar 404 no meio do stream.

O que o teste prende: (a) o teardown FAZ as quatro coisas e é chamado antes do
`os._exit`; (b) todo job conhecido continua respondendo depois do restart — o que
estava em voo vira `error` com motivo.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import auth, client  # noqa: E402,F401


@pytest.fixture
def isolado(tmp_path, monkeypatch):
    """`OUTPUTS_DIR`/histórico em tmp e `_jobs` restaurado depois."""
    saida = tmp_path / "outputs"
    saida.mkdir()
    monkeypatch.setattr(app, "OUTPUTS_DIR", saida)
    monkeypatch.setattr(app, "_JOBS_HIST_PATH", saida / ".jobs-history.json")
    antes = dict(app._jobs)
    app._jobs.clear()
    yield saida
    with app._jobs_lock:
        app._jobs.clear()
        app._jobs.update(antes)
    # o trava-guarda do teardown precisa voltar destravada entre testes
    if app._ENCERRANDO.locked():
        app._ENCERRANDO.release()


def _sessao_falsa(sid="s1"):
    return {"id": sid, "cancelado": threading.Event(), "fechar": False,
            "fila": type("F", (), {"bytes": 0, "descartados": 0})(),
            "pipe": type("P", (), {"cancel": lambda self: None})()}


# ------------------------------------------------------------------- teardown

def test_teardown_fecha_live_dsh_workers_e_salva_historico(isolado, monkeypatch):
    sess = _sessao_falsa()
    app._live_sessions["s1"] = sess
    app._chat_dsh_livres.append(("k", type("C", (), {"close": lambda self: setattr(self, "f", True)})()))
    filho = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    (isolado / ".job-abc").mkdir()
    (isolado / ".job-abc" / "worker.pid").write_text(str(filho.pid))
    monkeypatch.setitem(app._jobs, "abc", {**app._JOB_BASE, "status": "running"})
    try:
        feito = app._encerra_tudo("teste")
        assert feito["live"] == 1 and feito["dsh"] == 1 and feito["workers"] == 1
        assert "s1" not in app._live_sessions and sess["cancelado"].is_set()
        assert feito["hist"] is True and app._JOBS_HIST_PATH.exists()
        for _ in range(50):
            if filho.poll() is not None:
                break
            time.sleep(0.05)
        assert filho.poll() is not None, "worker sobreviveu ao teardown"
    finally:
        if filho.poll() is None:
            filho.kill()


def test_teardown_nao_estoura_sem_nada_aberto(isolado):
    feito = app._encerra_tudo("teste")
    assert feito["live"] == 0 and feito["workers"] == 0


def test_teardown_repetido_nao_roda_duas_vezes(isolado):
    app._encerra_tudo("teste")
    assert app._encerra_tudo("teste") == {"ja_encerrando": True}


def test_shutdown_chama_o_teardown_antes_do_exit(client, auth, isolado, monkeypatch):
    ordem = []
    monkeypatch.setattr(app, "_encerra_tudo", lambda m="shutdown": ordem.append(m) or {})
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    monkeypatch.setattr(app.os, "_exit", lambda c: ordem.append(f"exit{c}"))
    assert client.post("/api/shutdown", headers=auth).status_code == 200
    for _ in range(40):
        if len(ordem) >= 2:
            break
        time.sleep(0.05)
    assert ordem == ["api", "exit0"], ordem


def test_lifespan_faz_teardown_no_encerramento(isolado, monkeypatch):
    chamadas = []
    monkeypatch.setattr(app, "_encerra_tudo", lambda m="shutdown": chamadas.append(m) or {})
    with TestClient(app.app) as cliente:
        cliente.get("/health")
    assert chamadas == ["lifespan"], chamadas


# ----------------------------------------------------------------- histórico

def test_job_em_voo_vira_error_com_motivo_depois_do_restart(isolado):
    app._JOBS_HIST_PATH.write_text(json.dumps({"jobs": [
        {"id": "a1", "status": "running", "pieces": 2, "progress": {"stage": "gerando"}},
        {"id": "d1", "status": "done", "output": {"file": "d1.wav"}, "pieces": 3},
        {"id": "e1", "status": "error", "error": "falhou antes"},
    ]}))
    app._jobs_hist_carrega()
    assert app._jobs["a1"]["status"] == "error"
    assert "reinício do servidor" in app._jobs["a1"]["error"]
    assert app._jobs["a1"]["progress"] is None      # etapa não volta
    assert app._jobs["a1"]["pieces"] == 2           # o que já saiu fica registrado
    assert app._jobs["d1"]["status"] == "done" and app._jobs["e1"]["error"] == "falhou antes"


def test_historico_salvo_e_recarregado_apos_restart(isolado):
    monkeypatch_job = {**_app_base(), "status": "running"}
    app._jobs["v1"] = monkeypatch_job
    app._jobs_hist_salva()
    app._jobs.clear()
    app._jobs_hist_carrega()                         # simula o boot seguinte
    assert "v1" in app._jobs and app._jobs["v1"]["status"] == "error"


def test_status_de_job_conhecido_nunca_404_apos_restart(client, auth, isolado):
    app._JOBS_HIST_PATH.write_text(json.dumps({"jobs": [{"id": "z9", "status": "running"}]}))
    app._jobs_hist_carrega()
    r = client.get("/api/tts/jobs/z9", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "error"


def test_historico_nao_guarda_texto_nem_chave(isolado):
    app._jobs["t1"] = {**_app_base(), "status": "done", "output": {"file": "t1.wav"}}
    app._jobs_hist_salva()
    bruto = app._JOBS_HIST_PATH.read_text()
    assert "t1.wav" in bruto and "text" not in json.loads(bruto)["jobs"][0]


def test_historico_vazio_e_ilegivel_nao_estoura(isolado):
    app._JOBS_HIST_PATH.write_text("{lixo")
    app._jobs_hist_carrega()
    assert app._jobs == {}
    app._JOBS_HIST_PATH.unlink()
    app._jobs_hist_carrega()
    assert app._jobs == {}


def test_carga_nao_duplica_job_ja_em_memoria(isolado):
    app._jobs["dup"] = {**_app_base(), "status": "done", "output": {"file": "x.wav"}}
    app._JOBS_HIST_PATH.write_text(json.dumps({"jobs": [{"id": "dup", "status": "error"}]}))
    app._jobs_hist_carrega()
    assert app._jobs["dup"]["status"] == "done"


def _app_base() -> dict:
    return dict(app._JOB_BASE)


def test_ts_do_arquivo_nao_entra_no_job(isolado):
    """`ts` é do ARQUIVO, não do registro — senão vira campo estranho no status."""
    app._jobs["x"] = {**_app_base(), "status": "done"}
    app._jobs_hist_salva()
    dados = json.loads(app._JOBS_HIST_PATH.read_text())
    assert set(dados) == {"ts", "jobs"}
    assert "ts" not in dados["jobs"][0]


def test_killpg_de_worker_morto_e_ignorado(isolado):
    d = isolado / ".job-morto"
    d.mkdir()
    (d / "worker.pid").write_text("999999")
    assert app._jobs_mata_workers() == 0
    (d / "worker.pid").write_text("nao-numero")
    assert app._jobs_mata_workers() == 0
    assert os.getpid() > 0
