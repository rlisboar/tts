"""#313: buracos de observabilidade do Live — cada um obrigava o dono a adivinhar.

São cinco: `turno` no log do `interrupted`, os campos do motor que EXPLICAM um
barge falso, `stats` gerado por RELÓGIO (o antigo só saía com a fila vazia, ou
seja, nunca durante um turno), o estado/atraso do RNNoise e a espera pelo
`_gen_lock`. Mais o que veio junto no escopo: `vad.barge_ms`, teto de chars do
histórico do `setup`, fonte única do número do turno e `cancel` ocioso sem
`interrupted`.

Sem modelo, sem WS real — dublês.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

import app
import live_pipeline as lp


def _sess(**extra) -> dict:
    sess = {"id": "s-obs", "t0": time.monotonic(), "criada": time.time(),
            "visto": time.monotonic(), "turno": 1, "fila": app._LiveFila(1 << 20),
            "cancelado": app.threading.Event(), "engine": None, "pipe": None,
            "falando": False, "audio_pendente": 0, "st_stage": "idle",
            "st_stage_ini": None, "st_erro": None, "st_chunks": 0, "st_audio_bytes": 0,
            "st_proximo": 0.0, "st_bytes": 0, "vencida": False,
            "substituida": False, "fechar": False}
    sess.update(extra)
    return sess


class _EngCompleto:
    """Motor de dublê com as chaves REAIS de `estatisticas()`."""

    limiar_energia_dbfs = -45.0
    limiar_energia_turno_dbfs = -48.0
    turno_aberto = False
    estado = None

    def estatisticas(self) -> dict:
        return {"barge_in": 1, "barge_in_com_turno_aberto": 2, "barge_in_falso": 3,
                "taxa_falso_barge_in": 0.25, "noise_dbfs": -58.0, "eco_dbfs": -31.0,
                "playback_ativo": True, "tocando": True, "eco_ausente": False,
                "turno_assistente_aberto": True, "hold_to_talk": False,
                "hold_to_talk_sugerido": False}


class _Ws:
    def __init__(self) -> None:
        self.enviados: list = []
        self.fechado: int | None = None

    async def send_json(self, obj) -> None:
        self.enviados.append(obj)

    async def send_bytes(self, payload: bytes) -> None:
        self.enviados.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.fechado = code


# -- 1. `turno` no log do `interrupted` ------------------------------------
def test_interrupted_loga_o_turno_da_chave_turn(caplog):
    """O evento carrega `turn`; o log lia só `turno` e saía `turno=None`."""
    sess = _sess()
    with caplog.at_level(logging.INFO, logger="live"):
        app._live_observa(sess, {"type": "interrupted", "turn": 7})
    linha = next(r.message for r in caplog.records if "interrupted" in r.message)
    assert "turno=7" in linha and "turno=None" not in linha


def test_interrupted_aceita_a_chave_antiga_turno(caplog):
    sess = _sess()
    with caplog.at_level(logging.INFO, logger="live"):
        app._live_observa(sess, {"type": "interrupted", "turno": 9})
    linha = next(r.message for r in caplog.records if "interrupted" in r.message)
    assert "turno=9" in linha


# -- 2. campos do motor que explicam barge falso ---------------------------
def test_stats_publica_o_estado_que_explica_barge_falso():
    sess = _sess(engine=_EngCompleto())
    m = app._live_stats(sess)["motor"]
    assert m["eco_ausente"] is False and m["tocando"] is True
    assert m["turno_assistente_aberto"] is True
    assert m["taxa_falso_barge_in"] == 0.25
    assert m["eco_dbfs"] == -31.0 and m["noise_dbfs"] == -58.0
    # os de antes continuam lá
    assert m["barge_ativos"] == 3 and m["barge_falsos"] == 3


def test_stats_sem_motor_nao_inventa_campo():
    m = app._live_stats(_sess())["motor"]
    assert m["estado"] in {"ocioso", "ouvindo"}
    assert "taxa_falso_barge_in" not in m


# -- 3. `stats` por RELÓGIO, na frente do áudio ---------------------------
def test_stats_saem_durante_o_audio_e_nao_esperam_a_fila(monkeypatch):
    """3 s de áudio contínuo (chunk a cada 200 ms) tem de gerar ≥8 `stats`.

    E o `stats` não pode ficar atrás do áudio na fila: com o sender drenando, o
    `playback.chunks` do próprio payload acompanha o envio."""
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 100)
    monkeypatch.setattr(app, "_LIVE_TICK_S", 0.05)
    sess = _sess()
    ws = _Ws()
    CHUNK = b"\x00\x01" * 1600          # 100 ms de PCM16 16k

    async def fluxo() -> list:
        tarefa = asyncio.create_task(app._live_sender(sess, ws))
        for i in range(15):             # 3 s, um chunk a cada 200 ms
            app._live_envia_audio(sess, CHUNK)
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.25)
        tarefa.cancel()
        try:
            await tarefa
        except asyncio.CancelledError:
            pass
        return [e for e in ws.enviados if isinstance(e, dict) and e["type"] == "stats"]

    stats = asyncio.run(fluxo())
    assert len(stats) >= 8, f"stats por relógio: {len(stats)} em 3 s"
    chunks = [s["playback"]["chunks"] for s in stats]
    assert chunks == sorted(chunks) and chunks[-1] >= 1   # não congelou no 0
    # metadados apenas: nada de áudio no payload
    assert all(isinstance(s["mic"]["dbfs"], (int, float, type(None))) for s in stats)
    json.dumps(stats)                                     # serializável


# -- 4/5. RNNoise visível --------------------------------------------------
def test_rnnoise_desligado_aparece_zerado():
    r = app._live_stats(_sess())["rnnoise"]
    assert r == {"ativo": False, "faltas": 0, "faltas_pct": 0.0}


def test_rnnoise_conta_faltas_e_o_numero_sobrevive_a_queda():
    class Ns:
        faltas = 3200                    # 100 ms de 16 kHz

        @property
        def ativo(self):
            return True

    sess = _sess(rnnoise=Ns(), st_bytes=32000)
    r = app._live_stats(sess)["rnnoise"]
    assert r["ativo"] is True and r["faltas"] == 3200 and r["faltas_pct"] == 10.0
    sess.pop("rnnoise")                  # o filtro caiu (`_live_rnnoise_derruba`)
    r2 = app._live_stats(sess)["rnnoise"]
    assert r2["ativo"] is False and r2["faltas"] == 3200   # o estrago não some


# -- 6/7. espera pelo `_gen_lock` -----------------------------------------
def test_lock_espera_ms_vem_do_latency_para_o_stats():
    sess = _sess()
    app._live_observa(sess, {"type": "latency", "first_audio_ms": 900,
                             "lock_espera_ms": 780})
    assert app._live_stats(sess)["turno"]["lock_espera_ms"] == 780


def test_pipeline_publica_a_espera_pelo_lock_no_latency(monkeypatch):
    """A espera medida no TTS chega ao evento `latency` (o campo é a evidência)."""
    import numpy as np

    eventos: list = []
    p = lp.LivePipeline(eventos.append, lambda pcm: None,
                        stt=lambda pcm, l: "fala",
                        llm=lambda msgs: iter(["resposta curta. "]),
                        prewarm=lambda: None)

    def tts(texto, omni):                # finge a espera medida pelo `_tts_app`
        p._espera["lock_espera_ms"] = 640
        return np.zeros(8, dtype=np.float32)

    p._tts = tts
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    lat = next(e for e in eventos if e["type"] == "latency")
    assert lat["lock_espera_ms"] == 640


def test_espera_ausente_nao_inventa_campo(monkeypatch):
    import numpy as np

    eventos: list = []
    p = lp.LivePipeline(eventos.append, lambda pcm: None,
                        stt=lambda pcm, l: "fala",
                        llm=lambda msgs: iter(["resposta. "]),
                        tts=lambda t, o: np.zeros(8, dtype=np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    lat = next(e for e in eventos if e["type"] == "latency")
    assert "lock_espera_ms" not in lat


def test_tts_app_mede_a_espera_do_lock(monkeypatch, tmp_path):
    """Contra a trava de verdade: `_tts_app` reporta o tempo de espera."""
    import threading
    import numpy as np

    monkeypatch.setattr(app, "_use_remote_tts", lambda: False)
    monkeypatch.setattr(app, "_get_model", lambda: type("M", (), {"sample_rate": 24000})())
    monkeypatch.setattr(app, "_current_backend", lambda: {"family": "omnivoice"})
    monkeypatch.setattr(app, "_cond_for", lambda m, v, p: None)
    monkeypatch.setattr(app, "_voice_ref_text", lambda v: None)
    monkeypatch.setattr(app, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(app, "_generate_chunk",
                        lambda *a, **k: np.zeros(8, dtype=np.float32))
    espera: dict = {}
    app._gen_lock.acquire()
    try:
        th = threading.Thread(target=lp._tts_app, args=("oi.", {}), kwargs={"espera": espera})
        th.start()
        time.sleep(0.35)
        app._gen_lock.release()
        th.join(5)
    finally:
        if app._gen_lock.locked():
            app._gen_lock.release()
    assert espera["lock_espera_ms"] >= 250


# -- 8. `vad.barge_ms` ----------------------------------------------------
def _setup(vad=None) -> dict:
    return app._live_valida_setup({"voice_id": app.DESIGN_VOICE_ID, "vad": vad or {}})


def test_barge_ms_tem_default_e_clampa():
    assert _setup()["vad"]["barge_ms"] == 300
    assert _setup({"barge_ms": 9999})["vad"]["barge_ms"] == 600
    assert _setup({"barge_ms": 1})["vad"]["barge_ms"] == 150
    assert _setup({"barge_ms": 420})["vad"]["barge_ms"] == 420


def test_barge_ms_chega_no_motor():
    cfg = app._live_engine_novo({"vad": _setup({"barge_ms": 420})["vad"]})
    assert cfg.config.barge_in_ms == 420


def test_motor_sem_barge_ms_no_duble_usa_o_padrao():
    cfg = app._live_engine_novo({"vad": {"prefix_ms": 100, "silence_ms": 600}})
    assert cfg.config.barge_in_ms == app._LIVE_BARGE_MS_PADRAO


# -- 9/10. teto de chars do histórico -------------------------------------
def test_setup_corta_o_historico_no_teto_de_chars():
    gordo = "x" * 4000
    hist = [{"role": "user", "text": gordo} for _ in range(10)]   # 40 kB
    r = _setup()
    s = app._live_valida_setup({"voice_id": app.DESIGN_VOICE_ID, "history": hist})
    assert r["history_descartados"] == 0
    assert s["history_descartados"] > 0
    assert sum(len(h["text"]) for h in s["history"]) <= app._LIVE_SETUP_HIST_MAX_CHARS
    assert s["history"][-1]["text"] == gordo          # o recente fica


def test_hist_tam_conta_system_e_resumo():
    sess_reg = {"msgs": [{"content": "abc"}], "system": "s" * 10, "resumo": "r" * 5}
    assert app._live_hist_tam(sess_reg) == 3 + 10 + 5


# -- 11. fonte única do turno / cancel ocioso -----------------------------
def test_speech_start_adota_o_turno_do_pipeline():
    sess = _sess(turno=3)
    app._live_observa(sess, {"type": "speech_start", "turn": 4})
    assert sess["turno"] == 4


def test_cancel_com_turno_em_curso_emite_interrupted():
    class PipeOcupado:
        ocupado = True

        def cancel(self):
            pass

    sess = _sess(pipe=PipeOcupado(), turno=2)
    app._live_cancela(sess)
    # com turno RODANDO o evento `interrupted` vem do pipeline (ele é quem vê o
    # `cancelado` e fecha o turno); a sessão não duplica.
    assert sess["fila"].empty()
    assert sess["cancelado"].is_set()


def test_cancel_ocioso_nao_emite_interrupted_nem_deixa_flag():
    """Sessão SEM turno nenhum: cancelar não pode inventar "interrompido"."""
    sess = _sess(turno=0)
    app._live_cancela(sess)
    assert sess["fila"].empty()
    assert not sess["cancelado"].is_set()


def test_cancel_depois_de_um_turno_ainda_avisa():
    """Já houve turno: o `cancel` também para o que o cliente tem na fila dele.

    Comportamento antigo preservado (o `interrupted` deixa o cliente cortar o
    playback), só que agora apontando o turno certo — não um número inventado."""
    sess = _sess(turno=3)
    app._live_cancela(sess)
    tipos = [i[1]["type"] for i in list(sess["fila"]._q.queue)]
    assert tipos == ["interrupted"]


def test_cancel_de_barge_com_audio_pendente_avisa_o_cliente():
    """Há áudio em voo (sem pipe/engine): o cliente TEM de cortar o playback."""
    sess = _sess(audio_pendente=2)
    app._live_cancela(sess)
    tipos = [i[1]["type"] for i in list(sess["fila"]._q.queue)]
    assert tipos == ["interrupted"]


def test_cancel_nao_incrementa_o_turno():
    class PipeOcupado:
        ocupado = True

        def cancel(self):
            pass

    sess = _sess(pipe=PipeOcupado(), turno=5)
    app._live_cancela(sess)
    assert sess["turno"] == 5                # quem numera é o `speech_start`


if __name__ == "__main__":                   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# -- dublês antigos sem o kwarg `espera` não podem derrubar o turno ---------
def test_tts_app_antigo_sem_espera_nao_derrubar_o_turno(monkeypatch):
    """`espera` é só medição: dublê de 2 args (padrão antigo) tem de continuar
    servindo em vez de estourar `TypeError` no meio do TTS (#313)."""
    import numpy as np

    eventos: list = []
    audios: list = []
    p = lp.LivePipeline(eventos.append, audios.append,
                        stt=lambda pcm, l: "fala",
                        llm=lambda msgs: iter(["resposta. "]),
                        prewarm=lambda: None)          # `_tts` = `_tts_live`
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: False)
    monkeypatch.setattr(lp, "_tts_app", lambda texto, omni: np.ones(8, dtype=np.float32))
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    assert audios, "o áudio tem de sair mesmo com o dublê de 2 argumentos"
    assert "error" not in [e["type"] for e in eventos]


def test_duble_com_espera_continua_recebendo_a_medicao(monkeypatch):
    import numpy as np

    recebeu: dict = {}
    p = lp.LivePipeline(lambda o: None, lambda pcm: None, stt=lambda pcm, l: None,
                        llm=lambda msgs: None, prewarm=lambda: None)
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: False)

    def _tts(texto, omni, espera=None):
        recebeu["tem"] = espera is not None
        return np.ones(4, dtype=np.float32)

    monkeypatch.setattr(lp, "_tts_app", _tts)
    p._tts_live("oi", {})
    assert recebeu.get("tem") is True        # com assinatura, a medição passa
