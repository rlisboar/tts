"""Testes das funções puras do app.py (import leve — sem carregar modelos)."""

import threading
import time

import numpy as np
import pytest

import app


# ---------------------------------------------------------------------------
# Idioma / instruct
# ---------------------------------------------------------------------------

def test_omni_language():
    assert app._omni_language("auto") == "None"
    assert app._omni_language("") == "None"
    assert app._omni_language("português") == "pt"
    assert app._omni_language("Portuguese") == "pt"
    assert app._omni_language("en") == "en"


def test_sanitize_instruct_filtragem():
    ok = "male, middle-aged, low pitch"
    assert app._sanitize_instruct(ok) == ok
    # texto livre (emoção) é descartado; tags válidas ficam
    assert app._sanitize_instruct("happy, excited, female") == "female"
    assert app._sanitize_instruct("female, female, male") == "female, male"
    assert app._sanitize_instruct("") == ""


# ---------------------------------------------------------------------------
# STT: filtros anti-ruído
# ---------------------------------------------------------------------------

def _r(nsp=0.1, alp=-0.3, cr=1.2):
    return {"segments": [{"no_speech_prob": nsp, "avg_logprob": alp,
                          "compression_ratio": cr}]}


def test_stt_ok_aceita_fala_real():
    ok, motivo = app._stt_ok(_r(), "Olá, tudo bem com você?")
    assert ok, motivo


def test_stt_ok_rejeita_curto_e_blacklist():
    assert not app._stt_ok(_r(), "a")[0]              # 1 char < stt_min_chars
    ok, motivo = app._stt_ok(_r(), "obrigado")
    assert not ok and motivo == "alucinação comum"


def test_stt_ok_rejeita_metricas_ruins():
    assert not app._stt_ok(_r(nsp=0.9), "uma frase qualquer aqui")[0]
    assert not app._stt_ok(_r(alp=-2.0), "uma frase qualquer aqui")[0]
    assert not app._stt_ok(_r(cr=4.0), "uma frase qualquer aqui")[0]


# ---------------------------------------------------------------------------
# YouTube: erro retryable
# ---------------------------------------------------------------------------

def test_yt_retryable():
    assert app._yt_retryable(RuntimeError("HTTP Error 403: Forbidden"))
    assert app._yt_retryable(RuntimeError("Unable to download video data"))
    assert app._yt_retryable(RuntimeError("Sign in to confirm you're not a bot"))
    assert not app._yt_retryable(RuntimeError("Video privado"))
    assert not app._yt_retryable(RuntimeError("tres vazia"))


# ---------------------------------------------------------------------------
# Fila de falas (_SpeechGate)
# ---------------------------------------------------------------------------

@pytest.fixture()
def gate_rapido(monkeypatch):
    monkeypatch.setitem(app._settings, "speech_queue_gap_s", 0.1)
    return app._SpeechGate()


def test_gate_fifo_ordem_e_espera(gate_rapido):
    t1 = gate_rapido.begin("job-a")
    t2 = gate_rapido.begin("job-b")
    tempos = {}

    def entrega(tok, nome, dur):
        gate_rapido.deliver(tok, duration_s=dur)
        tempos[nome] = time.time()

    th1 = threading.Thread(target=entrega, args=(t1, "a", 0.0))
    th2 = threading.Thread(target=entrega, args=(t2, "b", 0.0))
    th1.start()
    time.sleep(0.05)                     # garante que "a" chega primeiro no gate
    th2.start()
    th1.join(timeout=5)
    th2.join(timeout=5)
    assert set(tempos) == {"a", "b"}
    assert tempos["a"] < tempos["b"]                    # FIFO
    assert tempos["b"] - tempos["a"] >= 0.08            # esperou folga da fala "a"


def test_gate_abort_avanca_ticket(gate_rapido):
    t1 = gate_rapido.begin("job-a")
    t2 = gate_rapido.begin("job-b")
    gate_rapido.abort(t1)                # erro: não reserva tempo de fala
    t0 = time.time()
    gate_rapido.deliver(t2, duration_s=0.0)
    assert time.time() - t0 < 0.5        # entrega imediata


def test_gate_duracao_estimada_por_texto(gate_rapido):
    t = gate_rapido.begin("job-x")
    app._jobs["job-x"] = {"text": "x" * 140, "status": "running"}
    try:
        gate_rapido.deliver(t)           # sem duration_s → estima do texto
        assert app._jobs["job-x"]["speech_duration_s"] > 0
    finally:
        app._jobs.pop("job-x", None)


# ---------------------------------------------------------------------------
# Evict de jobs
# ---------------------------------------------------------------------------

def test_anomalo_considera_speed():
    sr = 24000
    # 3s de áudio, texto de 100 chars → limiar base 100/45 ≈ 2.22s
    audio = np.full(3 * sr, 0.2, dtype=np.float32)
    assert not app._anomalo(audio, sr, "x" * 100)              # ok a speed 1
    assert app._anomalo(np.zeros(1, dtype=np.float32), sr, "x" * 100)  # inaudível
    # speed 2 encurta o áudio pela metade (1.5s < limiar 2.22): NÃO é truncamento
    curto = audio[:int(1.5 * sr)]
    assert app._anomalo(curto, sr, "x" * 100, speed=1.0)        # falso-positivo antigo
    assert not app._anomalo(curto, sr, "x" * 100, speed=2.0)    # corrigido


def test_evict_jobs_preserva_running(monkeypatch):
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_MAX", 3)
        for i in range(5):
            status = "running" if i < 2 else "done"
            app._jobs[f"job-{i}"] = {"status": status}
        app._evict_jobs()
        assert len(app._jobs) == 3
        ids = list(app._jobs)
        assert "job-0" in ids and "job-1" in ids        # running sobrevive
        assert ids[-1] == "job-4"                        # mais novo sempre fica
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


# ---------------------------------------------------------------------------
# Resolução de parâmetros
# ---------------------------------------------------------------------------

def test_resolve_omni_payload_sobrepoe_settings():
    o = app._resolve_omni({"num_steps": 24, "speed": 1.5}, family="omnivoice")
    assert o["num_steps"] == 24
    assert o["speed"] == 1.5
    o2 = app._resolve_omni({}, family="omnivoice")
    assert o2["num_steps"] == app._settings["omni_num_steps"]
    assert o2["speed"] == app._settings["speed"]
    assert o2["seed"] == app._settings["omni_seed"]


def test_resolve_duration():
    assert app._resolve_duration_s(None, 5.0) is None
    assert app._resolve_duration_s("0", 5.0) is None
    assert app._resolve_duration_s("3", 5.0) == 3.0
    assert app._resolve_duration_s(999, 5.0) == 60.0
