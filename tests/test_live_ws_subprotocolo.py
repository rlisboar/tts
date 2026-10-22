"""Subprotocolo `tts-live.v1` do WS do Live (#478).

O subprotocolo é NEGOCIADO, nunca exigido: sem o header o handshake é o de
sempre (é o caminho de `client/` e dos smokes), com o header certo o servidor
responde o mesmo valor e, com versão desconhecida, a sessão abre SEM subprotocolo
e o motivo aparece no `stats` — recusar o handshake deixaria o cliente antigo sem
saber por quê.
"""
import pathlib

import pytest
from fastapi.testclient import TestClient

import app


def _abertura_do_ws() -> str:
    """Trecho de `live_ws` até depois do `accept` (onde a negociação mora)."""
    txt = pathlib.Path("app.py").read_text(encoding="utf-8")
    return txt.split("async def live_ws", 1)[1].split("_live_sweep", 1)[0]


# sessão mínima só para `_live_stats` não estourar
_SESS = {"t0": 0.0, "criada": 0.0, "visto": 0.0, "id": "s-sub", "turno": 0,
         "cancelado": None, "fila": None, "subprotocolo_recusado": "x"}


class _Ws:
    def __init__(self, cru: str | None):
        self.headers = {} if cru is None else {"sec-websocket-protocol": cru}


def test_sem_header_nao_negocia_e_mantem_o_comportamento():
    assert app._live_negocia_subprotocolo(_Ws(None)) == (None, None)
    assert app._live_negocia_subprotocolo(_Ws("")) == (None, None)


def test_versao_conhecida_e_negociada():
    assert app._live_negocia_subprotocolo(_Ws("tts-live.v1")) == ("tts-live.v1", None)


def test_versao_conhecida_entre_varias_e_negociada():
    """O header aceita lista; negociar a nossa é o que o navegador espera."""
    aceito, motivo = app._live_negocia_subprotocolo(_Ws("outra.v2, tts-live.v1"))
    assert aceito == "tts-live.v1" and motivo is None


def test_versao_desconhecida_abre_sem_subprotocolo_e_diz_o_motivo():
    aceito, motivo = app._live_negocia_subprotocolo(_Ws("tts-live.v9"))
    assert aceito is None
    assert "tts-live.v9" in motivo and "conhecida" in motivo


def test_aceite_usa_a_negociacao_e_o_motivo_vai_para_o_stats():
    trecho = _abertura_do_ws()
    assert "_live_negocia_subprotocolo(ws)" in trecho
    assert "await ws.accept(subprotocol=_sub)" in trecho
    # o motivo não pode morrer no log: `stats` é o canal do painel
    assert "subprotocolo" in app._live_stats(_SESS)


def test_a_ui_oferece_o_subprotocolo():
    html = pathlib.Path("static/index.html").read_text(encoding="utf-8")
    assert 'new WebSocket(lxWsUrl() + q, "tts-live.v1")' in html


def test_o_cliente_de_terminal_fica_sem_o_subprotocolo_como_legado():
    """Decisão registrada: `client/` não oferece; o caminho sem header é a prova."""
    fonte = pathlib.Path("client/mic_router.py").read_text(encoding="utf-8")
    assert "tts-live.v1" not in fonte


@pytest.fixture()
def live_limpo(monkeypatch):
    monkeypatch.setattr(app, "_live_mod", None)
    with app._live_lock:
        app._live_sessions.clear()
    yield
    with app._live_lock:
        app._live_sessions.clear()


def _cliente():
    # loopback: a auth dispensa chave, e o teste mede só o handshake
    return TestClient(app.app, raise_server_exceptions=False,
                      client=("127.0.0.1", 50000))


def test_handshake_real_negocia_o_subprotocolo(live_limpo):
    """Ponta a ponta: o valor negociado volta no `Sec-WebSocket-Protocol`."""
    with _cliente().websocket_connect(
            "/api/live/ws", subprotocols=["tts-live.v1"]) as ws:
        ws.send_json({"type": "setup"})
        assert ws.receive_json()["type"] in ("ready", "error")
        assert ws.accepted_subprotocol == "tts-live.v1"


def test_handshake_real_sem_header_continua_como_hoje(live_limpo):
    with _cliente().websocket_connect("/api/live/ws") as ws:
        ws.send_json({"type": "setup"})
        assert ws.receive_json()["type"] in ("ready", "error")
        assert ws.accepted_subprotocol is None      # nada negociado, sem erro


@pytest.mark.parametrize("cabecalho", [None, "tts-live.v1", "tts-live.v9"])
def test_handshake_de_verdade_negocia_ou_recusa_sem_quebrar(cabecalho):
    """Ponta a ponta no handshake ASGI: o `subprotocol` escolhido pelo `accept`
    é o que sai no `Sec-WebSocket-Protocol` da resposta."""
    import asyncio

    enviados = []

    async def enviar(msg):
        enviados.append(msg)

    class Ws:
        def __init__(self):
            self.headers = {} if cabecalho is None else {
                "sec-websocket-protocol": cabecalho}

        async def accept(self, subprotocol=None):
            enviados.append({"type": "websocket.accept", "subprotocol": subprotocol})

    ws = Ws()
    aceito, _ = app._live_negocia_subprotocolo(ws)
    asyncio.run(ws.accept(subprotocol=aceito))
    assert enviados[0]["subprotocol"] == (cabecalho if cabecalho != "tts-live.v9" else None)
