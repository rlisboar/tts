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


def test_unload_solta_o_pool_de_metal_sob_o_lock(monkeypatch):
    """O flush do Metal tem de caber no `_gen_lock` (#800).

    Soltar o pool com uma geração já alocando disputa o Metal no pior instante: o
    modelo acabou de sair e a próxima geração está começando."""
    monkeypatch.setattr(app, "_model", None)
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: None)
    dentro = []

    def _espia(*a, **k):
        dentro.append(app._gen_lock._is_owned())

    monkeypatch.setattr(app, "_release_mlx_memory", _espia)
    app._unload_local_models(tts=True, stt=False, mt=False, ser=False)

    assert dentro == [True], f"flush fora do lock: {dentro}"


def test_unload_sem_tts_ainda_libera_o_pool(monkeypatch):
    chamou = []
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: chamou.append(1))
    app._unload_local_models(tts=False, stt=False, mt=False, ser=False)
    assert chamou, "sem TTS o pool do Metal ficou sem liberar"


def test_job_status_nao_entrega_o_dict_vivo(monkeypatch):
    """A rota tem de devolver CÓPIA (#800).

    O job é mutado pela thread que gera enquanto o FastAPI serializa: entregar o
    dict vivo dá snapshot inconsistente e, com campo trocado no meio, erro de
    iteração."""
    app._jobs["vivo"] = {"status": "running", "progress": {"current": 1}}
    try:
        r = app.job_status("vivo")
        r["status"] = "mutado"
        r["novo"] = 1
        assert app._jobs["vivo"]["status"] == "running", "devolveu o dict vivo"
        assert "novo" not in app._jobs["vivo"]
    finally:
        app._jobs.pop("vivo", None)


def test_cancelar_na_fila_libera_o_lugar_na_hora(monkeypatch):
    """Cancelar um job NA FILA não pode esperar a vez dele (#835).

    O ticket só era resolvido quando o job chegava ao topo — a fila atrás ficava
    presa por um job que já não ia tocar nada."""
    gate = app._SpeechGate()
    monkeypatch.setattr(app, "_speech_gate", gate)   # o wrapper usa o GLOBAL
    app._jobs["a"] = {"status": "queued"}
    app._jobs["b"] = {"status": "queued"}
    app._jobs["c"] = {"status": "queued"}
    try:
        ta = gate.begin("a")
        tb = gate.begin("b")
        tc = gate.begin("c")
        # "a" entrega e reserva 10 s: "b" e "c" ficam na fila
        gate.deliver(ta, duration_s=10.0)

        assert gate.depth == 2
        # cancela o "b" PELA ROTA, que é o PRÓXIMO: tem de sair agora, sem 10 s
        from fastapi.testclient import TestClient

        c = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 1))
        r = c.post("/api/tts/jobs/b/cancel")
        assert r.status_code == 200 and r.json()["lugar_liberado"] is True, r.text
        assert gate.depth == 1, "o lugar do cancelado não foi liberado"
        assert tb["done"] is True

        # e o "c" passa a ser o próximo
        assert gate._next_ticket == tc["ticket"]
    finally:
        for j in ("a", "b", "c"):
            app._jobs.pop(j, None)


def test_cancelar_quem_esta_atras_e_pulado_na_vez(monkeypatch):
    """Quem está atrás é marcado e pulado quando chega a vez, sem travar."""
    gate = app._SpeechGate()
    monkeypatch.setattr(app, "_speech_gate", gate)
    app._jobs["x"] = {"status": "queued"}
    app._jobs["y"] = {"status": "queued"}
    try:
        tx = gate.begin("x")
        ty = gate.begin("y")
        app._speech_queue_cancela("y")
        assert ty.get("cancelado") is True
        # quando o "x" entrega, o "y" (já cancelado) é pulado na chamada dele
        gate.deliver(tx, duration_s=0.0)
        t = threading.Thread(target=gate.deliver, args=(ty,), kwargs={"duration_s": 0.0})
        t.start()
        t.join(timeout=2)
        assert not t.is_alive(), "o job cancelado ficou pendurado na fila"
        assert ty["done"] is True
    finally:
        for j in ("x", "y"):
            app._jobs.pop(j, None)
