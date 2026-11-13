"""#1166: família em SUBPROCESSO não pausa pelo Live — e tem de dizer isso.

O worker tem laço próprio, o pai segura o `_gen_lock` o job inteiro e o watchdog de
180 s mataria um worker parado. Em vez de prometer a pausa calada, o job recusa
explicitamente: marca `pausa_lote_sem_efeito` e diz no `progress.stage`.

Aqui o worker é um `Popen` falso (sem MLX): o que se mede é a MARCAÇÃO e o AVISO.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import app  # noqa: E402
from test_live_pausa_lote_1157 import (  # noqa: E402,F401 — fixture do caminho in-process
    _dispara, _espera, _fecha, _sessao as _sessao_1157, lote)


class _ProcMudo:
    """Worker que nunca escreve status: o job fica no estágio até o watchdog."""

    def __init__(self):
        self.pid = 999999
        self.morto = False

    def poll(self):
        return self.returncode

    @property
    def returncode(self):
        return 0 if self.morto else None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.morto = True

    def kill(self):
        self.morto = True


@pytest.fixture()
def bancada(monkeypatch, tmp_path):
    def _prepara(proc):
        monkeypatch.setitem(app._settings, "tts_marca_dagua", False)
        monkeypatch.setitem(app._settings, "live_pausa_lote", True)
        monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(app, "_unload_local_tts", lambda *a, **k: None)
        monkeypatch.setattr(app, "_schedule_job_cleanup", lambda *a, **k: None)
        monkeypatch.setattr(app, "_jobs_hist_salva", lambda *a, **k: None)
        monkeypatch.setattr(app, "_piece_dir", lambda jid: tmp_path / jid)
        monkeypatch.setattr(app, "BASE", tmp_path)
        monkeypatch.setattr(app, "_gen_lock", threading.RLock())
        monkeypatch.setattr(app, "_JOB_PARADO_S", 0.6)
        monkeypatch.setattr(app, "_JOB_TETO_S", 60.0)
        app._jobs["j"] = {"status": "queued"}
        with app._live_lock:
            app._live_sessions.clear()

    yield _prepara
    with app._live_lock:
        app._live_sessions.clear()


def _roda():
    app._run_tts_job_isolated("j", "oi", "v", None, "pt", {},
                              {"id": "kokoro", "family": "kokoro", "meta": {}})


def _sessao(**extra):
    with app._live_lock:
        app._live_sessions["s1"] = {"id": "s1", "visto": time.monotonic(),
                                    "atividade": time.monotonic(), **extra}


def test_familia_em_subprocesso_recusa_a_pausa_e_avisa_no_estagio(bancada):
    """Com Live ativo, o job de família isolada NÃO pausa — e o estágio diz isso."""
    proc = _ProcMudo()
    bancada(proc)
    _sessao()
    visto = []

    def _espia():
        while not proc.morto:
            st = (app._jobs["j"].get("progress") or {}).get("stage") or ""
            if st:
                visto.append(st)
            time.sleep(0.02)

    threading.Thread(target=_espia, daemon=True).start()
    _roda()

    assert app._jobs["j"].get("pausa_lote_sem_efeito") == app._PAUSA_LOTE_SEM_EFEITO
    assert visto, "nenhum estágio observado"
    assert any(app._PAUSA_LOTE_SEM_EFEITO in s for s in visto), visto[:3]
    assert not app._jobs["j"].get("pausado_pelo_live"), "marcou pausa onde não pausa"


def test_sem_live_nao_marca_nada(bancada):
    """Sem Live não há o que recusar: o estágio fica limpo."""
    proc = _ProcMudo()
    bancada(proc)
    _roda()
    assert app._jobs["j"].get("pausa_lote_sem_efeito") is None


def test_setting_desligado_nao_marca(bancada, monkeypatch):
    """Pausa desligada = comportamento antigo; o aviso seria ruído."""
    proc = _ProcMudo()
    bancada(proc)
    monkeypatch.setitem(app._settings, "live_pausa_lote", False)
    _sessao()
    _roda()
    assert app._jobs["j"].get("pausa_lote_sem_efeito") is None


def test_estagio_do_worker_sai_decorado(bancada, monkeypatch, tmp_path):
    """O estágio que o WORKER escreve também ganha o aviso (não só o inicial)."""
    proc = _ProcMudo()

    def _popen(*a, **k):
        # o worker falso escreve um estágio e fica vivo até o teste soltar
        st = tmp_path / "j" / "status.json"
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"progress": {"stage": "gerando trecho 1/3"}}))
        return proc

    bancada(proc)
    monkeypatch.setattr(app.subprocess, "Popen", _popen)
    _sessao()
    estagios = []

    def _espia():
        while not proc.morto:
            estagios.append((app._jobs["j"].get("progress") or {}).get("stage"))
            time.sleep(0.02)

    threading.Thread(target=_espia, daemon=True).start()
    _roda()
    assert any(s and "gerando trecho 1/3" in s and app._PAUSA_LOTE_SEM_EFEITO in s
               for s in estagios), estagios[:5]


def test_familia_in_process_continua_pausando_e_nao_leva_aviso(lote):  # noqa: F811
    """O aviso é SÓ do subprocesso: a família in-process segue pausando de verdade."""
    _sessao_1157()
    t, job = _dispara()
    assert _espera(lambda: job.get("pausado_pelo_live")), \
        "a família in-process deixou de pausar"
    assert job.get("pausa_lote_sem_efeito") is None, "marcou recusa onde a pausa vale"
    _fecha()
    t.join(20)
    assert job["status"] == "done", job.get("error")


def test_live_que_abre_no_MEIO_do_job_tambem_avisa(bancada):
    """#1179: o Live pode abrir DEPOIS de o job começar.

    A marcação da abertura não serve para ele: o caminho isolado não tem espera
    onde reavaliar, então sem a marcação no laço o aviso nunca apareceria e o job
    ficaria em silêncio — o que o #1166 veio matar. Aqui o job começa sem Live e a
    sessão abre no meio: o estágio seguinte tem de sair decorado.
    """
    proc = _ProcMudo()
    bancada(proc)
    estagios = []

    def _espia():
        while not proc.morto:
            estagios.append((app._jobs["j"].get("progress") or {}).get("stage"))
            time.sleep(0.02)

    threading.Thread(target=_espia, daemon=True).start()
    threading.Timer(0.25, _sessao).start()   # Live abre com o job já em curso
    _roda()

    assert app._jobs["j"].get("pausa_lote_sem_efeito") == app._PAUSA_LOTE_SEM_EFEITO, \
        f"o Live abriu no meio e o job ficou calado: {estagios[:6]}"
    assert any(s and app._PAUSA_LOTE_SEM_EFEITO in s for s in estagios), estagios[:6]
