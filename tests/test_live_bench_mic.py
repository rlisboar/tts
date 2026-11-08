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


def test_leitor_do_ws_morto_tambem_acorda_o_ciclo():
    """`recebe` que cai (ou o WS que fecha) acorda o ciclo igual à morte do mic."""
    class _WSQuebrado:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionResetError("ws caiu")

    c = _cliente(_WSQuebrado())
    asyncio.run(asyncio.wait_for(c.recebe(), 1.0))
    assert c.vivo is False
    assert c.erro is not None and "ws caiu" in c.erro
    assert c.morreu.is_set()


def _args_ciclo(teto: float):
    return types.SimpleNamespace(
        teto_turno_s=teto, interrompe_apos_ms=0, verbose=False,
        pausa_min_s=0.0, pausa_max_s=0.0)


def _fala_longa(i: str, ref: str) -> dict:
    return {"audio": np.zeros(16000, np.float32), "ref": ref, "id": i}


def test_recusa_de_identidade_nao_queima_o_teto(monkeypatch):
    """Portão `identidade` recusa B: o ciclo fecha em < 1 s e não é perda (#988).

    Sem o termo na espera, teto 5 s viraria 5 + 15 s à procura de um
    `turn_complete` que o portão não emite.
    """
    estado = {"n": 0}

    async def fala_pronta(c, evento, _teto):
        estado["n"] += 1
        agora = c.agora()
        c.voz_t_ini = agora
        c.voz_t_fim = agora
        c.voz = None
        evento.set()
        if estado["n"] == 1:
            c.bin.append((agora, 2400))
            c.eventos.append((agora, "turn_complete", {
                "type": "turn_complete", "transcript": "ola"}))
            c.play = np.zeros(0, np.float32)
        else:
            c.eventos.append((c.agora(), "barge_decisao", {
                "type": "barge_decisao", "tipo": "ruido", "filtro": "identidade"}))
        return True

    monkeypatch.setattr(lb, "_fala_ou_morte", fala_pronta)
    c = _cliente(_WS())
    linhas: list[dict] = []
    t0 = time.monotonic()
    asyncio.run(lb.ciclo(
        c, 0, _fala_longa("a", "ola"), _fala_longa("b", "barge de terceiro"),
        False, _args_ciclo(5.0), linhas))
    dt = time.monotonic() - t0
    assert dt < 1.0, f"recusa de identidade queimou o teto: {dt:.3f}s"
    assert len(linhas) == 2, linhas
    b = linhas[1]
    assert b["recusada_identidade"] is True
    assert b["aceito"] is False
    assert "wer_ciclo" not in b
    r = lb.resumo(
        {"linhas": linhas, "respostas": [], "sessao_s": dt, "ciclos": 1,
         "ws_vivo_no_fim": True, "erro_ws": None, "proc_ini": {}, "proc_fim": {},
         "erros": []},
        types.SimpleNamespace(sessao_min=0))
    assert r["S1"]["recusadas_identidade"] == 1
    assert r["S1"]["falas_ge_0_8s_perdidas"] == 0
    assert r["S1"]["falas"] == 1 and r["S1"]["aceitas"] == 1
    assert r["S3"]["interrupcoes"] == 0


def test_ruido_que_nao_e_identidade_nao_encerra():
    """Eco / STT vazio / stats antigo não podem soltar a espera de B."""
    c = _cliente(_WS())
    c.eventos.append((1.0, "barge_decisao", {
        "type": "barge_decisao", "tipo": "ruido", "filtro": "stt_vazio"}))
    c.eventos.append((0.2, "barge_decisao", {
        "type": "barge_decisao", "tipo": "ruido", "filtro": "identidade"}))
    assert lb._b_recusada_identidade(c, 1.0, 0) is False
    c.eventos.append((1.1, "barge_decisao", {
        "type": "barge_decisao", "tipo": "ruido", "filtro": "identidade"}))
    assert lb._b_recusada_identidade(c, 1.0, 0) is True


def test_stats_so_conta_recusa_nova():
    c = _cliente(_WS())
    c.ultimo_stats = {"motor": {"voz": {
        "recusados": 2, "ultimo": {"estado": "recusado"}}}}
    assert lb._b_recusada_identidade(c, 0.0, 2) is False
    c.ultimo_stats["motor"]["voz"]["recusados"] = 3
    assert lb._b_recusada_identidade(c, 0.0, 2) is True


def test_resumo_perda_continua_contando_sem_o_portao():
    linhas = [
        {"tipo": "interrupcao", "dur_s": 1.2, "aceito": False, "cobertura": 0.0,
         "recusada_identidade": True},
        {"tipo": "longa", "dur_s": 1.2, "aceito": False, "cobertura": 0.0},
    ]
    r = lb.resumo(
        {"linhas": linhas, "respostas": [], "sessao_s": 1, "ciclos": 1,
         "ws_vivo_no_fim": True, "erro_ws": None, "proc_ini": {}, "proc_fim": {},
         "erros": []},
        types.SimpleNamespace(sessao_min=0))
    assert r["S1"]["recusadas_identidade"] == 1
    assert r["S1"]["falas_ge_0_8s_perdidas"] == 1
    assert r["S1"]["falas"] == 1
    assert r["S3"]["interrupcoes"] == 0


def test_leitor_que_fecha_limpo_marca_morte_sem_erro():
    class _WSVazio:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    c = _cliente(_WSVazio())
    asyncio.run(asyncio.wait_for(c.recebe(), 1.0))
    assert c.vivo is False
    assert c.erro is None
    assert c.morreu.is_set()
