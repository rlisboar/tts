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
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import auth, client  # noqa: E402, F401

# Declarados como usados: são FIXTURES consumidas pelos testes do arquivo.
# O `ruff` já cobre isso com o `noqa` (política pós-#378); o `__all__` mantém
# o `pyflakes` (que ignora o `noqa`) também verde — inerte em runtime.
__all__ = [
    "auth",
    "client",
]


@pytest.fixture
def isolado(tmp_path, monkeypatch):
    """`OUTPUTS_DIR`/histórico em tmp; `_jobs` e registries de volta no fim.

    O trava-guarda do teardown (`_ENCERRANDO`) é destravado no SETUP também: um
    `/api/shutdown` real de outro teste o deixa tomado, e aí o teardown daqui
    devolveria `{"ja_encerrando": True}` (sem as chaves) em vez de trabalhar.
    """
    if app._ENCERRANDO.locked():
        app._ENCERRANDO.release()
    saida = tmp_path / "outputs"
    saida.mkdir()
    monkeypatch.setattr(app, "OUTPUTS_DIR", saida)
    monkeypatch.setattr(app, "_JOBS_HIST_PATH", saida / ".jobs-history.json")
    antes_jobs = dict(app._jobs)
    antes_live = dict(app._live_sessions)
    antes_dsh = list(app._chat_dsh_livres)
    app._jobs.clear()
    app._live_sessions.clear()
    app._chat_dsh_livres.clear()
    yield saida
    with app._jobs_lock:
        app._jobs.clear()
        app._jobs.update(antes_jobs)
    app._live_sessions.clear()
    app._live_sessions.update(antes_live)
    app._chat_dsh_livres[:] = antes_dsh
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
    (isolado / ".job-abc" / "owner.pid").write_text(str(os.getpid()))
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


# ------------------------------------------------- rastro do autor do sinal (#423)

def test_assinatura_do_sinal_diz_sinal_e_pai(isolado, capsys):
    app._assinatura_do_sinal(15)
    saida = capsys.readouterr().err
    assert re.search(r"\[sinal\] \S+ SIGTERM", saida)
    assert "pai=" in saida and os.getppid() > 0


def test_handler_encaminha_para_o_handler_anterior(isolado, monkeypatch):
    """Chamar o anterior é o que preserva o shutdown gracioso do uvicorn; sem o
    encaminhamento o registro viraria problema."""
    chamou = []
    anteriores = app._instala_rastro_de_sinal.__wrapped__ if hasattr(
        app._instala_rastro_de_sinal, "__wrapped__") else app._instala_rastro_de_sinal
    monkeypatch.setattr(app, "_instala_rastro_de_sinal", anteriores)
    monkeypatch.setattr(app.signal, "getsignal", lambda s: (lambda n, f: chamou.append(n)))
    app._instala_rastro_de_sinal()
    app.signal.getsignal = lambda s: (lambda n, f: chamou.append(n))
    handler = __import__("signal").getsignal(__import__("signal").SIGTERM)
    handler(__import__("signal").SIGTERM, None)
    assert chamou, "o handler anterior não foi chamado"


def test_lifespan_instala_o_rastro_de_sinal(isolado, monkeypatch):
    instalou = []
    monkeypatch.setattr(app, "_instala_rastro_de_sinal",
                        lambda: instalou.append(1) or [])
    monkeypatch.setattr(app, "_encerra_tudo", lambda m="shutdown": {})
    with TestClient(app.app) as cliente:
        cliente.get("/health")
    assert instalou, "o lifespan não instalou o rastro do sinal"


# ------------------------------------- killpg só no worker do PRÓPRIO dono (#423)

def _worker_falso(isolado, nome: str, dono: int) -> subprocess.Popen:
    d = isolado / f".job-{nome}"
    d.mkdir()
    filho = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    (d / "worker.pid").write_text(str(filho.pid))
    (d / "owner.pid").write_text(str(dono))
    return filho


def test_teardown_nao_mata_worker_de_outra_instancia(isolado):
    """O diretório é compartilhado com a instância de produção: o teardown de um
    teste matava o worker do servidor vivo (job morria no meio)."""
    meu = _worker_falso(isolado, "meu", os.getpid())
    alheio = _worker_falso(isolado, "alheio", os.getppid())
    try:
        assert app._jobs_mata_workers() == 1
        for _ in range(60):
            if meu.poll() is not None:
                break
            time.sleep(0.05)
        assert meu.poll() is not None, "o worker do próprio dono devia morrer"
        assert alheio.poll() is None, "worker de OUTRA instância foi morto"
    finally:
        for p in (meu, alheio):
            if p.poll() is None:
                p.kill()


def test_matar_todos_volta_ao_comportamento_antigo(isolado):
    meu = _worker_falso(isolado, "meu2", os.getpid())
    alheio = _worker_falso(isolado, "alheio2", os.getppid())
    try:
        assert app._jobs_mata_workers(somente_proprios=False) == 2
    finally:
        for p in (meu, alheio):
            if p.poll() is None:
                p.kill()
def test_segredo_na_cmdline_do_pai_sai_mascarado(isolado, capsys):
    """O log de sinal vai para arquivo em /tmp (0644); pai com `--token` vazaria o
    token ali. A linha tem de MASCARAR o valor e continuar identificando o autor."""
    casos = [
        ("bash x.sh --token TOP-SECRET-9999", "TOP-SECRET-9999", "--token ***9999"),
        ("cloudflared tunnel --token=SEGREDO-LONGO-1 run", "SEGREDO-LONGO-1", "--token=***GO-1"),
        ("curl -H 'Authorization: Bearer abcdef1234567890'", "abcdef1234567890", "Bearer ***890"),
        ("http://127.0.0.1:7860/ws?key=CHAVE-123456&x=1", "CHAVE-123456", "key=***3456"),
    ]
    for cmd, segredo, esperado in casos:
        saida = app._mascara_cmd(cmd)
        assert segredo not in saida, f"vazou {segredo!r} em {saida!r}"
        assert esperado in saida, saida
    # não-segredo passa intacto
    assert app._mascara_cmd("uvicorn app:app --port 7860") == "uvicorn app:app --port 7860"
    assert app._mascara_cmd("") == ""


def test_assinatura_mascara_a_cmdline_do_pai(isolado, monkeypatch, capsys):
    monkeypatch.setattr(app, "_pid_cmd",
                        lambda pid: "bash /tmp/x.sh --token TOP-SECRET-9999")
    app._assinatura_do_sinal(15)
    saida = capsys.readouterr().err
    assert "TOP-SECRET-9999" not in saida and "--token ***9999" in saida


# ------------------------------------------------- rastro do autor quando ÓRFÃO (#471)

def test_assinatura_traz_pgid_sid_e_horario_estavel(isolado, capsys):
    """Um SIGTERM no app órfão não tinha como apontar autor: o `pai=1` só conta
    que o pai original sumiu. Grupo, sessão e horário são o que sobra."""
    app._assinatura_do_sinal(15)
    saida = capsys.readouterr().err
    assert "pgid=" in saida and "sid=" in saida
    assert re.search(r"\[\s*sinal\]\s+\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", saida)
    assert f"pid={os.getpid()}" in saida


def test_orfao_traz_pai_de_boot_e_o_grupo(isolado, monkeypatch, capsys):
    monkeypatch.setattr(app.os, "getppid", lambda: 1)
    app._PAI_DO_BOOT.clear()
    app._PAI_DO_BOOT.update(pid=4242, lstart="Thu Oct 22 02:36:06 2026", cmd="bash /tmp/x.sh")
    monkeypatch.setattr(app, "_processos_do_grupo", lambda pg, sd: "111:1:%d:bash /tmp/x.sh" % pg)
    app._assinatura_do_sinal(15)
    saida = capsys.readouterr().err
    assert "ÓRFÃO" in saida
    assert "pai de boot=4242" in saida and "bash /tmp/x.sh" in saida
    assert "111:1:" in saida


def test_pai_vivo_nao_polui_com_o_ramo_de_orfao(isolado, capsys):
    app._assinatura_do_sinal(15)
    assert "ÓRFÃO" not in capsys.readouterr().err


def test_pai_do_boot_e_gravado(isolado):
    app._PAI_DO_BOOT.clear()
    app._grava_pai_do_boot()
    assert app._PAI_DO_BOOT.get("pid") == os.getppid()
    assert "lstart" in app._PAI_DO_BOOT


def test_mapa_do_grupo_mascara_e_nao_lista_o_ps(isolado, monkeypatch):
    falso = ("1 0 1 /sbin/launchd\n"
             "111 1 777 bash /tmp/x.sh --token SEGREDO-123456\n"
             "999 111 777 ps -A -o pid=,ppid=,pgid=,command=\n")
    monkeypatch.setattr(app.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": falso})())
    mapa = app._processos_do_grupo(777, 777)
    assert "SEGREDO-123456" not in mapa
    assert "***3456" in mapa
    assert "ps -A" not in mapa
