"""#934: o mic da bancada não pode morrer calado quando o playback esvazia.

No último quadro de um TTS o `self.play` fica vazio; o lookahead do eco era
lido DEPOIS do consumo e `np.interp` levantava "array of sample points is
empty" fora do `try` do `ws.send`. A task do mic morria, `erro_ws` ficava null
e o ciclo queimava o teto — o servidor parecia surdo.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "tools"))

import live_bench as lb  # noqa: E402


class _WS:
    def __init__(self) -> None:
        self.enviados = 0

    async def send(self, _pcm) -> None:
        self.enviados += 1


def _cliente(ws: _WS) -> lb.Cliente:
    return lb.Cliente(ws, eco_db=-15.0, eco_atraso_ms=100, ruido_dbfs=-60.0)


def test_mic_sobrevive_playback_que_esvazia(monkeypatch):
    """Playback menor que um quadro: o mic tem de seguir mandando."""
    monkeypatch.setattr(lb, "FRAME_S", 0.01)
    ws = _WS()
    c = _cliente(ws)
    c.play = (np.sin(np.arange(100, dtype=np.float32) / 8.0) * 0.2).astype(np.float32)

    async def corre():
        tarefa = asyncio.ensure_future(c.mic())
        await asyncio.sleep(0.15)
        c.vivo = False
        await asyncio.wait_for(tarefa, 1.0)
        return tarefa

    tarefa = asyncio.run(corre())
    assert c.erro is None, c.erro
    assert not tarefa.cancelled() and tarefa.exception() is None
    assert ws.enviados >= 3, ws.enviados
    assert c.play.size == 0


def test_excecao_no_mic_vira_erro_ws(monkeypatch):
    """Exceção FORA do try do `send` também tem de aparecer em `erro`."""
    monkeypatch.setattr(lb, "FRAME_S", 0.01)
    ws = _WS()
    c = _cliente(ws)
    c.play = np.zeros(100, dtype=np.float32)

    def interp_quebrado(*_a, **_k):
        raise ValueError("interp explodiu")

    monkeypatch.setattr(lb.np, "interp", interp_quebrado)

    async def corre():
        await asyncio.wait_for(c.mic(), 1.0)

    asyncio.run(corre())
    assert c.vivo is False
    assert c.erro is not None and "mic:" in c.erro and "interp" in c.erro, c.erro
