"""#943: o estágio `stt` só conta quando o turno abriu.

speech_end com o pipeline ocupado enfileira e não é whisper. O relógio abre no
speech_start do pipeline (`buffer_bytes`, sem `t_ms`). O do motor traz `t_ms`
e chega no onset, antes de `abrir_turno`.
"""

import queue
import threading
import time
import types

import numpy as np

import app
import live_pipeline as lp


class _PipeOcupado:
    ocupado = True
    aceitos = []

    def abrir_turno(self, pcm, barge=False):
        return False


def _sessao(pipe):
    return {"id": "t943", "buffer": bytearray(), "fila": queue.Queue(),
            "fechar": False, "turno": 0, "pipe": pipe,
            "cancelado": threading.Event(), "turno_pendente": None,
            "turno_pendente_barge": False, "pend_thread": None,
            "pendentes_descartados": 0, "prewarm_ok": True, "buffer_consumido": 0,
            "st_stage": "tts", "st_stage_ini": time.monotonic()}


def _speech_end():
    pcm = b"\x01\x00" * 800

    def to_json():
        return {"type": "speech_end", "t_ms": 100, "ts": 0.1, "amostra": 1600,
                "t_decisao_ms": 12, "prob": 0.9, "rms_dbfs": -20.0, "curto": False,
                "barge_in": False, "fala_ms": 400, "barge_falso": False}

    return types.SimpleNamespace(
        tipo="speech_end", t_ms=100, prob=0.9, barge_in=False, fala_ms=400,
        curto=False, barge_falso=False, audio=pcm, detalhe="", t_decisao_ms=12,
        to_json=to_json)


def test_speech_end_com_pipeline_ocupado_nao_abre_estagio_stt(monkeypatch):
    """Mordida: o carimbo antigo em speech_end punha `stt` sem turno aberto."""
    monkeypatch.setattr(app, "_live_acorda_pendente", lambda s: None)
    pipe = _PipeOcupado()
    sess = _sessao(pipe)
    app._live_trata_eventos(sess, [_speech_end()])
    assert sess.get("st_stage") == "tts"
    assert sess.get("turno_pendente"), "a fala ocupada tinha de ir para o pendente"


def test_speech_start_do_motor_nao_entra_em_stt():
    sess = _sessao(None)
    sess["st_stage"] = "idle"
    app._live_observa(sess, {"type": "speech_start", "turn": 4, "t_ms": 10,
                             "prob": 0.9})
    assert sess["turno"] == 4
    assert sess["st_stage"] == "idle"


def test_speech_start_do_pipeline_entra_em_stt():
    """Mordida: sem este carimbo o whisper real fica invisível no relógio."""
    sess = _sessao(None)
    sess["st_stage"] = "idle"
    app._live_observa(sess, {"type": "speech_start", "turn": 4, "buffer_bytes": 3200})
    assert sess["turno"] == 4
    assert sess["st_stage"] == "stt"


def test_turno_que_abre_carimba_stt_antes_do_whisper_voltar(monkeypatch):
    """O pendente que abre de verdade também passa por este speech_start."""
    monkeypatch.setattr(app, "_live_acorda_pendente", lambda s: None)
    monkeypatch.setattr(app, "_live_hist_pos_turno", lambda s: None)
    entrou = threading.Event()

    def stt(pcm, lang):
        entrou.set()
        time.sleep(0.3)
        return "oi"

    sess = {}

    def emit(obj):
        app._live_envia_json(sess, obj)

    pipe = lp.LivePipeline(emit, lambda b: None, stt=stt,
                           llm=lambda m: iter(["ok. "]),
                           tts=lambda c, o: np.zeros(10, dtype=np.float32),
                           prewarm=lambda: None)
    sess.update(_sessao(pipe))
    sess["st_stage"] = "idle"
    app._live_abre_turno(sess, b"\x01\x00" * 800)
    assert entrou.wait(2), "o turno aberto não chegou no STT"
    assert sess.get("st_stage") == "stt"
    for _ in range(50):
        if not pipe.ocupado:
            break
        time.sleep(0.05)
    assert not pipe.ocupado
