"""Abertura do WS do Live pede o prewarm da marca (#1119, achado do gate #1115).

`_live_marca_prewarm()` é chamado em dois pontos: no `speech_start` (coberto
por `test_marca_speech_start_1115.py`) e na ABERTURA do `live_ws`, logo depois
do `ready` (o prewarm do #657). Sem célula, apagar a chamada da abertura deixava
a suíte verde: o filho subia só no 1º `speech_start`, perdendo o adiantamento.

Exerce o `live_ws` de verdade (TestClient), com `marca_dagua.prewarm` trocado
por um anotador: nenhum AudioSeal sobe.
"""
import time

import pytest
from fastapi.testclient import TestClient

import app
import marca_dagua


@pytest.fixture()
def live_limpo(monkeypatch):
    monkeypatch.setattr(app, "_live_mod", None)
    with app._live_lock:
        app._live_sessions.clear()
    yield
    with app._live_lock:
        app._live_sessions.clear()


def _cliente():
    # loopback: a auth dispensa chave
    return TestClient(app.app, raise_server_exceptions=False,
                      client=("127.0.0.1", 50000))


def _abre_e_conta(monkeypatch, marca_on: bool) -> list:
    chamadas: list = []
    monkeypatch.setattr(marca_dagua, "prewarm", lambda: chamadas.append(time.monotonic()))
    monkeypatch.setitem(app._settings, "tts_marca_dagua", marca_on)
    with _cliente().websocket_connect("/api/live/ws") as ws:
        ws.send_json({"type": "setup"})
        msg = ws.receive_json()
        assert msg["type"] == "ready", msg       # o prewarm mora DEPOIS do ready
        fim = time.monotonic() + 3.0             # a chamada vem logo após o envio
        while not chamadas and time.monotonic() < fim:
            time.sleep(0.01)
    return chamadas


def test_abertura_do_ws_pede_prewarm_da_marca(live_limpo, monkeypatch):
    chamadas = _abre_e_conta(monkeypatch, marca_on=True)
    assert len(chamadas) == 1, f"abertura do Live sem prewarm da marca: {chamadas}"


def test_abertura_do_ws_com_marca_desligada_nao_sobe_filho(live_limpo, monkeypatch):
    assert _abre_e_conta(monkeypatch, marca_on=False) == []
