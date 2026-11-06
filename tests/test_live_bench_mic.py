"""#934: o mic da bancada não pode morrer calado quando o playback esvazia.

No último quadro de um TTS o `self.play` fica vazio; o lookahead do eco era
lido DEPOIS do consumo e `np.interp` levantava "array of sample points is
empty" fora do `try` do `ws.send`. A task do mic morria, `erro_ws` ficava null
e o ciclo queimava o teto — o servidor parecia surdo.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
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


def _args(teto: float):
    return types.SimpleNamespace(teto_turno_s=teto, interrompe_apos_ms=1500)


def _fala(i: str) -> dict:
    return {"audio": np.zeros(1600, np.float32), "ref": "", "id": i}


def test_ciclo_nao_espera_o_teto_com_o_mic_morto():
    """Mic morto acorda o ciclo na hora; o teto é só do servidor lento (#941)."""
    c = _cliente(_WS())
    c._morre("mic: morreu antes")
    linhas: list[dict] = []
    t0 = time.monotonic()
    asyncio.run(lb.ciclo(c, 0, _fala("a"), _fala("b"), False, _args(0.5), linhas))
    dt = time.monotonic() - t0
    assert dt < 0.2, f"o ciclo esperou o teto com o mic morto: {dt:.3f}s"
    assert linhas and linhas[0]["timeout_fala"] is True
    assert linhas[0].get("erro_ws") == "mic: morreu antes"


def test_ciclo_sem_morte_ainda_espera_o_teto():
    """Sem erro de mic o caminho é o de antes: espera o teto e marca timeout."""
    c = _cliente(_WS())
    linhas: list[dict] = []
    t0 = time.monotonic()
    asyncio.run(lb.ciclo(c, 0, _fala("a"), _fala("b"), False, _args(0.15), linhas))
    dt = time.monotonic() - t0
    assert dt >= 0.12, dt
    assert linhas and linhas[0]["timeout_fala"] is True
    assert "erro_ws" not in linhas[0]
