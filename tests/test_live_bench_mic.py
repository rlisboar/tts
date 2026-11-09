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


def test_resposta_de_b_por_interrupted_nao_queima_o_teto_triplo(monkeypatch):
    """Eco corta B com `interrupted` terminal e o ciclo não queima teto*3 (#942).

    O `interrupted` que cortou A cai depois de `b_fim` e antes do áudio de B;
    `motivo:"continuacao"` também não solta. Sem o filtro o ciclo acaba antes
    do corte terminal (o roteiro é cancelado e `terminal` não entra). Sem o
    conserto, `espera` fica em `turn_complete` o `teto_turno_s * 3` inteiro.
    """
    real_sleep = asyncio.sleep

    async def _sleep(s, *a, **k):
        # a cauda de 1 s do ciclo não é a espera sob teste
        if s >= 1.0:
            return None
        return await real_sleep(s, *a, **k)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    teto = 1.0
    c = _cliente(_WS())
    marcos: list[str] = []

    async def roteiro() -> None:
        while c.voz_t_fim is None:
            await real_sleep(0.005)
        a_fim = c.voz_t_fim
        # carimbo >= a_fim: `agora()` no fim do quadro pode ficar atrás do
        # relógio da fala e a espera do 1º áudio queimaria o teto
        c.bin.append((a_fim + 0.001, 2400))
        c.play = np.ones(8000, np.float32)
        while c.voz is None:
            await real_sleep(0.005)
        while c.voz is not None:
            await real_sleep(0.005)
        b_fim = c.voz_t_fim
        # corte de A DEPOIS de b_fim e ANTES do áudio de B; zera o playback
        c.eventos.append((b_fim + 0.01, "interrupted", {"motivo": "barge"}))
        c.play = np.zeros(0, np.float32)
        marcos.append("corte_a")
        await real_sleep(0.05)
        c.bin.append((b_fim + 0.02, 2400))
        marcos.append("audio_b")
        await real_sleep(0.05)
        c.eventos.append((b_fim + 0.03, "interrupted", {"motivo": "continuacao"}))
        marcos.append("continuacao")
        await real_sleep(0.25)
        marcos.append("terminal")
        c.eventos.append((b_fim + 0.04, "interrupted", {"motivo": "eco"}))
        c.play = np.zeros(0, np.float32)

    linhas: list[dict] = []
    args = types.SimpleNamespace(
        teto_turno_s=teto, interrompe_apos_ms=0,
        pausa_min_s=0.0, pausa_max_s=0.0, verbose=False,
    )

    async def corre() -> None:
        mic = asyncio.ensure_future(c.mic())
        rot = asyncio.ensure_future(roteiro())
        try:
            await lb.ciclo(c, 0, _fala("a"), _fala("b"), False, args, linhas)
        finally:
            c.vivo = False
            if not rot.done():
                rot.cancel()
            mic.cancel()
            await asyncio.gather(mic, rot, return_exceptions=True)

    t0 = time.monotonic()
    asyncio.run(corre())
    dt = time.monotonic() - t0
    assert c.erro is None, c.erro
    assert "terminal" in marcos, marcos
    assert dt < teto * 2, f"queimei o teto triplo: {dt:.3f}s (teto*3={teto * 3:.3f})"
    assert len(linhas) == 2, linhas
    assert linhas[1].get("eco_falso_corte") is True, linhas[1]


def test_turno_que_fechou_com_playback_pendente_nao_esmaga_o_teto():
    """#1021: `turn_complete` com `play` ainda cheio solta a espera na hora.

    Medido uma vez em 20 ciclos (fala 35 do #875): o turno fechou, o `play` do
    cliente tinha 12 s de eco na fila e não esvaziou — a espera só saiu nos 900 s
    do teto triplo."""
    c = _cliente(_WS())
    c.play = np.ones(48000, np.float32)        # 12 s de playback pendente
    c.eventos.append((1.0, "turn_complete", {"type": "turn_complete"}))
    assert lb._turno_b_encerrou(c, 0.9, 1.0) is True


def test_interrupted_de_continuacao_nao_fecha_e_o_terminal_fecha():
    """A contraprova do #942 segue: continuação não solta; terminal solta mesmo
    com playback pendente (o cliente já zerou a fila)."""
    c = _cliente(_WS())
    c.play = np.ones(4800, np.float32)
    c.eventos.append((1.0, "interrupted", {"motivo": "continuacao"}))
    assert lb._turno_b_encerrou(c, 0.9, 1.0) is False
    c.eventos.append((1.1, "interrupted", {"motivo": "eco"}))
    assert lb._turno_b_encerrou(c, 0.9, 1.0) is True
    # o `interrupted` que cortou A (antes do áudio de B) não conta
    c2 = _cliente(_WS())
    c2.eventos.append((0.5, "interrupted", {"motivo": "barge"}))
    assert lb._turno_b_encerrou(c2, 0.9, 1.0) is False


# ---------------------------------------------------------------------------
# #1022: decomposição do pos_llm (1º token → 1º áudio) por trecho
# ---------------------------------------------------------------------------

def test_estagios_do_pos_llm_fecham_com_os_marcos_do_latency():
    lat = {"first_token_ms": 1000, "first_clause_ms": 1600, "tts_start_ms": 1610,
           "tts_first_pcm_ms": 2900, "lock_espera_ms": 300, "marca_ms": 3100,
           "send_ms": 3120, "first_audio_ms": 3125, "t_decisao_ms": 200,
           "tts_modelo_ms": 40, "tts_cond_ms": 50, "tts_prep_ms": 15,
           "tts_gen_ms": 880}
    e = lb.estagios_pos_llm(lat, s2_ms=3400)
    assert e == {"llm_frase": 600, "fila_tts": 10, "lock": 300, "tts": 990,
                 "marca": 200, "envio": 20, "resto": 275, "vad": 200, "fio": 75,
                 "tts_modelo": 40, "tts_cond": 50, "tts_prep": 15, "tts_gen": 880,
                 "tts_fora": 5}
    # a soma dos trechos fecha com o pos_llm da bancada (s2 − 1º token), a menos
    # do intervalo send → first_audio, que é o emit (5 ms aqui). `vad`/`fio` e as
    # sub-marcas do `tts` ficam de fora: eles DECOMPÕEM, não somam.
    assert sum(v for k, v in e.items()
               if k not in ("vad", "fio", "tts_modelo", "tts_cond", "tts_prep",
                            "tts_gen", "tts_fora")) == \
        3400 - 1000 - (3125 - 3120)
    assert e["vad"] + e["fio"] == e["resto"]
    # #1022 (gate #1065): as sub-marcas FECHAM o `tts` — sem o `prep` a soma não
    # fechava e o critério "gen > 80 % do tts" lia setup como fora do worker.
    assert (e["tts_modelo"] + e["tts_cond"] + e["tts_prep"] + e["tts_gen"]
            + e["tts_fora"]) == e["tts"]


def test_tts_fora_some_se_falta_sub_marca():
    """Sem uma das quatro sub-marcas a sobra não é sobra: `tts_fora` vira None."""
    lat = {"tts_start_ms": 1600, "tts_first_pcm_ms": 2600, "tts_modelo_ms": 40,
           "tts_cond_ms": 50, "tts_gen_ms": 880}             # sem `tts_prep_ms`
    e = lb.estagios_pos_llm(lat, None)
    assert e["tts"] == 1000 and e["tts_prep"] is None and e["tts_fora"] is None


def test_marco_ausente_vira_none_e_nao_zero():
    e = lb.estagios_pos_llm({"first_token_ms": 900, "first_audio_ms": 2000}, None)
    assert set(e) == set(lb.ESTAGIOS_POS_LLM)
    assert all(v is None for v in e.values())


def test_resto_abre_em_vad_e_fio():
    """#1038: o `resto` é caixa preta de ~900 ms — `vad`/`fio` a abrem.

    `vad` é a decisão do detector; `fio` é o que sobra (transporte/fila). Sem os
    dois, cortar o `resto` é chute: 900 ms não dizem se o problema é hangover do
    VAD ou fio.
    """
    e = lb.estagios_pos_llm({"first_token_ms": 900, "first_audio_ms": 2000,
                             "t_decisao_ms": 600}, 2900)
    assert e["resto"] == 900
    assert e["vad"] == 600
    assert e["fio"] == 300


def test_resto_sem_carimbo_nao_inventa_vad_nem_fio():
    """Sem `t_decisao_ms` os dois são `None` — não rateia o `resto` no chute."""
    e = lb.estagios_pos_llm({"first_token_ms": 900, "first_audio_ms": 2000}, 2900)
    assert e["resto"] == 900 and e["vad"] is None and e["fio"] is None


def test_decisao_zero_vale_por_ausente():
    """O produtor faz `or 0`: zero é "sem carimbo", não "decisão imediata".

    Se o zero virasse medida, o `fio` sairia igual ao `resto` e a leitura diria
    "tudo é fio" mesmo quando o detector não carimbou nada.
    """
    e = lb.estagios_pos_llm({"first_token_ms": 900, "first_audio_ms": 2000,
                             "t_decisao_ms": 0}, 2900)
    assert e["vad"] is None and e["fio"] is None


def test_resumo_publica_p50_de_cada_trecho():
    base = {"tipo": "longa", "dur_s": 2.0, "s2_ms": 3000, "s2_llm_ttft_ms": 1000,
            "s2_pos_llm_ms": 2000}
    L = [dict(base, s2_estagios={"llm_frase": v, "fila_tts": 5, "lock": None, "tts": 1000,
                                 "marca": 200, "envio": 10, "resto": 100})
         for v in (400, 600, 800)]
    res = {"linhas": L, "respostas": [], "sessao_s": 1, "ciclos": 1,
           "ws_vivo_no_fim": True, "erro_ws": None, "proc_ini": {}, "proc_fim": {},
           "erros": []}
    est = lb.resumo(res, types.SimpleNamespace(sessao_min=30))["S2"]["pos_llm_estagios"]
    assert est["llm_frase"] == {"n": 3, "p50_ms": 600.0, "p95_ms": 780.0}
    assert est["lock"] == {"n": 0, "p50_ms": None, "p95_ms": None}
    assert list(est) == list(lb.ESTAGIOS_POS_LLM)


# ---------------------------------------------------------------------------
# #1022: o `latency` de A é o de A — o barge-in cancela o turno, não a medida
# ---------------------------------------------------------------------------

def test_turno_interrompido_ainda_empresta_o_proprio_latency():
    """A é SEMPRE interrompido nesta bancada; o turno de A sai `cancelado=True`.

    Descartar por `cancelado` casava o `s2_ms` de A com o `first_token_ms` de B
    (dezenas de segundos depois) e o `resto` saía negativo em ~1,4 s.
    """
    c = _cliente(_WS())
    c.eventos = [
        (10.0, "latency", {"cancelado": True, "first_token_ms": 1400,
                           "first_audio_ms": 2900}),
        (40.0, "latency", {"first_token_ms": 300, "first_audio_ms": 900}),
    ]
    e = lb.latencia_com_audio(c, 9.0)
    assert e is not None and e["first_token_ms"] == 1400   # o de A, não o de B


def test_turno_cancelado_antes_do_audio_nao_mede_s2():
    """Cancelado sem primeiro áudio: não há S2 a decompor — pula para o próximo."""
    c = _cliente(_WS())
    c.eventos = [
        (10.0, "latency", {"cancelado": True}),
        (40.0, "latency", {"first_token_ms": 300, "first_audio_ms": 900}),
    ]
    e = lb.latencia_com_audio(c, 9.0)
    assert e is not None and e["first_token_ms"] == 300


def test_sem_latency_nenhum_devolve_none():
    c = _cliente(_WS())
    assert lb.latencia_com_audio(c, 0.0) is None
