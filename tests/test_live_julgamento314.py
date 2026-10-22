"""#314: julgamento do turno — `_stt_ok` no Live, motivo do descarte e hold.

Três buracos da auditoria #283:

1. o Live chamava `_transcribe` direto e o filtro anti-alucinação do projeto
   (blacklist, `no_speech_prob`, `avg_logprob`, `compression_ratio`, mínimos) só
   valia na Conversa — alucinação ia reta ao LLM;
2. turno que o VAD do próprio STT recusa saía como `transcript:""`, indistinguível
   de "não ouvi";
3. o fallback "segurar pra falar" era calculado (`hold_to_talk_sugerido`) e nunca
   chegava ao cliente; `set_hold` não tinha call site.

Sem modelo e sem WS real.
"""

from __future__ import annotations

import numpy as np
import pytest

import app
import live_pipeline as lp
import live_turns


# -- 1/2. julgamento no `_stt_app` ----------------------------------------
@pytest.fixture()
def anti_ruido(monkeypatch):
    """O filtro obedece ao setting do dono (que hoje está DESLIGADO no
    settings.json): estas provas pinam ligado, que é o regime que os mede."""
    monkeypatch.setitem(app._settings, "stt_anti_ruido", True)


def _transcreve(monkeypatch, resultado: dict):
    monkeypatch.setattr(app, "_transcribe", lambda path, language=None: resultado)
    return resultado


def test_stt_ok_rejeita_alucinacao_com_motivo(monkeypatch, anti_ruido):
    """Texto da blacklist fecha como descarte `stt` (o texto vai no detalhe)."""
    _transcreve(monkeypatch, {"text": "Obrigada.", "language": "pt", "segments": []})
    julga: dict = {}
    texto = lp._stt_app(b"\x00\x01" * 400, "pt", julga)
    assert texto == "Obrigada."              # o texto segue para o `transcript`
    assert julga["motivo"] == "stt"
    assert julga["detalhe"] == "alucinação comum"


def test_stt_ok_rejeita_logprob_baixa(monkeypatch, anti_ruido):
    _transcreve(monkeypatch, {"text": "blá blá", "language": "pt",
                              "segments": [{"avg_logprob": -2.0, "no_speech_prob": 0.1,
                                            "compression_ratio": 1.0}]})
    julga: dict = {}
    lp._stt_app(b"\x00\x01" * 400, "pt", julga)
    assert julga["motivo"] == "stt" and "confiança" in julga["detalhe"]


def test_stt_ok_deixa_passar_fala_curta_boa(monkeypatch, anti_ruido):
    """"Sim." com logprob alta NÃO pode ser engolido (proteção do aceite)."""
    _transcreve(monkeypatch, {"text": "Sim.", "language": "pt",
                              "segments": [{"avg_logprob": -0.2, "no_speech_prob": 0.3,
                                            "compression_ratio": 1.1}]})
    julga: dict = {}
    assert lp._stt_app(b"\x00\x01" * 400, "pt", julga) == "Sim."
    assert "motivo" not in julga


def test_vad_do_stt_recusando_vira_motivo_vad(monkeypatch):
    """O ramo do VAD devolve texto vazio E `language` vazio: é "não ouvi"."""
    _transcreve(monkeypatch, {"text": "", "language": "", "segments": []})
    julga: dict = {}
    assert lp._stt_app(b"\x00\x01" * 400, "pt", julga) == ""
    assert julga["motivo"] == "vad"


def test_texto_vazio_com_idioma_e_motivo_stt(monkeypatch):
    """Whisper rodou (idioma presente) e não devolveu texto: não é o VAD."""
    _transcreve(monkeypatch, {"text": "  ", "language": "pt", "segments": []})
    julga: dict = {}
    assert lp._stt_app(b"\x00\x01" * 400, "pt", julga) == ""
    assert julga["motivo"] == "stt"


def test_sem_julga_o_stt_app_nao_quebra(monkeypatch):
    _transcreve(monkeypatch, {"text": "oi", "language": "pt", "segments": []})
    assert lp._stt_app(b"\x00\x01" * 400, "pt") == "oi"


# -- turno: descarte com motivo, sem LLM nem TTS --------------------------
def _pipe(eventos: list, usado: dict) -> lp.LivePipeline:
    def llm(msgs):
        usado["llm"] = usado.get("llm", 0) + 1
        return iter(["resposta. "])

    p = lp.LivePipeline(lambda o: eventos.append(o), lambda pcm: usado.setdefault(
        "audios", []).append(pcm), stt=lp._stt_app, llm=llm,
        tts=lambda t, o: np.zeros(8, dtype=np.float32), prewarm=lambda: None)
    return p


def test_turno_recusado_pelo_stt_ok_fecha_descartado_sem_llm(monkeypatch,
                                                             anti_ruido):
    _transcreve(monkeypatch, {"text": "Obrigada.", "language": "pt", "segments": []})
    eventos: list = []
    usado: dict = {}
    p = _pipe(eventos, usado)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    fim = next(e for e in eventos if e["type"] == "turn_complete")
    assert fim["descartado"] is True and fim["motivo"] == "stt"
    assert fim["transcript"] == "Obrigada."
    assert not usado.get("llm") and not usado.get("audios")
    assert "transcript_user" not in [e["type"] for e in eventos]


def test_turno_recusado_pelo_vad_fecha_com_motivo_vad(monkeypatch):
    _transcreve(monkeypatch, {"text": "", "language": "", "segments": []})
    eventos: list = []
    usado: dict = {}
    p = _pipe(eventos, usado)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    fim = next(e for e in eventos if e["type"] == "turn_complete")
    assert fim["descartado"] is True and fim["motivo"] == "vad"
    assert not usado.get("llm")


def test_fala_boa_do_stt_app_segue_para_o_llm(monkeypatch, anti_ruido):
    _transcreve(monkeypatch, {"text": "Sim.", "language": "pt",
                              "segments": [{"avg_logprob": -0.2, "no_speech_prob": 0.2,
                                            "compression_ratio": 1.0}]})
    eventos: list = []
    usado: dict = {}
    p = _pipe(eventos, usado)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    tipos = [e["type"] for e in eventos]
    assert "transcript_user" in tipos and usado.get("llm") == 1
    assert not any(e.get("descartado") for e in eventos)


def test_motivo_aparece_no_log_do_turno_descartado(caplog):
    import logging

    sess = {"id": "s-j", "st_stage": "stt", "st_stage_ini": None, "fila": app._LiveFila()}
    with caplog.at_level(logging.INFO, logger="live"):
        app._live_observa(sess, {"type": "turn_complete", "descartado": True,
                                 "motivo": "stt", "detalhe": "alucinação comum"})
    linha = next(r.message for r in caplog.records if "turno_descartado" in r.message)
    assert "motivo=stt" in linha and "alucinação" in linha
    assert sess["descartados_motivo"] == "stt"


# -- 3. hold-to-talk -----------------------------------------------------
class _EngHold:
    limiar_energia_dbfs = -45.0
    limiar_energia_turno_dbfs = -48.0
    turno_aberto = False
    estado = None

    def __init__(self) -> None:
        self.pedidos: list[bool] = []

    def set_hold(self, valor: bool):
        self.pedidos.append(bool(valor))
        return []

    def estatisticas(self) -> dict:
        return {"hold_to_talk": bool(self.pedidos and self.pedidos[-1]),
                "hold_to_talk_sugerido": True, "barge_in": 6,
                "barge_in_com_turno_aberto": 0, "barge_in_falso": 4}


def test_stats_publica_o_aviso_de_hold():
    """O fallback documentado em LIVE.md chega ao cliente pelo `stats`."""
    sess = {"id": "s-h", "t0": 1.0, "criada": 0.0, "visto": 1.0, "turno": 1,
            "engine": _EngHold(), "falando": False, "fila": app._LiveFila(),
            "st_stage": "idle"}
    m = app._live_stats(sess)["motor"]
    assert m["hold_to_talk_sugerido"] is True and m["hold_to_talk"] is False


def test_hold_to_talk_sugerido_aparece_depois_de_barges_falsos():
    """No motor REAL: 5 barge falsos (sem fala depois) ligam a sugestão."""
    eng = live_turns.TurnEngine()
    eng.feed(np.zeros(3200, dtype=np.float32))
    for _ in range(6):
        # barge-in e o turno fecha sem fala além da janela de confirmação
        eng._turno_barge = True                  # noqa: SLF001 — dublê de estado
        eng._fala_extra_frames = 0               # noqa: SLF001
        eng._estado = live_turns.Estado.FALANDO  # noqa: SLF001
        eng._abrir_turno(0, 0, barge=True)       # noqa: SLF001
        eng._fala_extra_frames = 0               # noqa: SLF001
        eng.cancel()
    st = eng.estatisticas()
    assert st["barge_in_falso"] >= 5 and st["hold_to_talk_sugerido"] is True


def test_hold_abre_turno_sem_vad():
    """`set_hold(True)` no motor real abre turno e emite o `speech_start`."""
    eng = live_turns.TurnEngine()
    eng.feed(np.zeros(3200, dtype=np.float32))   # pré-roll: o motor já viu áudio
    eventos = eng.set_hold(True)
    assert [e.tipo for e in eventos] == ["speech_start"]
    assert eventos[0].audio                  # o pré-roll vai junto
    assert eng.turno_aberto is True
    assert [e.tipo for e in eng.set_hold(False)] == ["speech_end"]


def test_hold_ligado_fica_visivel_no_stats_mesmo_sem_sugestao():
    """`hold_to_talk` NÃO pode sumir nem zerar com o modo ligado (desenho do PM).

    A faixa da UI continua na tela enquanto o modo está LIGADO, mesmo que a
    sugestão já tenha caído — senão ligar seria sem volta. Quem carrega o estado é
    `hold_to_talk`; `hold_to_talk_sugerido` pode ir a false à vontade.
    """
    eng = live_turns.TurnEngine()
    eng.feed(np.zeros(3200, dtype=np.float32))
    sess = {"id": "s-h", "t0": 1.0, "criada": 0.0, "visto": 1.0, "turno": 1,
            "engine": eng, "falando": False, "fila": app._LiveFila(), "st_stage": "idle"}
    eng.set_hold(True)
    m = app._live_stats(sess)["motor"]
    assert m["hold_to_talk"] is True            # ligado: a tela sabe
    assert "hold_to_talk" in m                  # e a chave existe (não é omitida)
    assert m["hold_to_talk_sugerido"] is False  # a sugestão pode cair
    eng.set_hold(False)
    assert app._live_stats(sess)["motor"]["hold_to_talk"] is False


def test_comando_hold_chega_ao_motor():
    """O despacho do handler manda o `on` ao motor (sem isso o recurso é morto)."""
    eng = _EngHold()
    sess = {"id": "s-h", "engine": eng, "st_stage": "idle", "st_stage_ini": None,
            "fila": app._LiveFila(), "turno": 1}
    app._live_trata_eventos(sess, eng.set_hold(True))
    assert eng.pedidos == [True]
    app._live_trata_eventos(sess, eng.set_hold(False))
    assert eng.pedidos == [True, False]


def test_hold_sem_motor_avisa_em_vez_de_sumir():
    """Sem motor o comando responde erro em vez de sumir calado."""
    esperado = app._live_erro("comando", "hold sem motor de turnos nesta sessão")
    assert esperado["code"] == "comando" and "motor" in esperado["message"]


if __name__ == "__main__":                   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_anti_ruido_desligado_o_LIVE_ainda_filtra(monkeypatch):
    """Assimetria decidida pelo PM (#314): o Live FORÇA o `_stt_ok`.

    No Live a alucinação vira turno — não há texto digitado para corrigir. Na
    Conversa o setting continua mandando e o mesmo texto passa."""
    monkeypatch.setitem(app._settings, "stt_anti_ruido", False)
    r = {"text": "Obrigada.", "language": "pt", "segments": []}
    assert app._stt_ok(r, "Obrigada.") == (True, "")          # Conversa: passa
    _transcreve(monkeypatch, r)
    julga: dict = {}
    lp._stt_app(b"\x00\x01" * 400, "pt", julga)               # Live: recusa
    assert julga.get("motivo") == "stt"


def test_anti_ruido_ligado_os_dois_filtram(anti_ruido):
    """Ligado, `forcar` é redundante: os dois lados recusam igual."""
    assert app._stt_ok({"segments": []}, "Obrigada.")[0] is False
    assert app._stt_ok({"segments": []}, "Obrigada.", forcar=True)[0] is False


def test_knob_de_processo_desliga_o_filtro_do_Live(monkeypatch):
    """`TTS_LIVE_ANTI_RUIDO=0` é a única saída e é de PROCESSO.

    O setting de ditado do dono está desligado; se o Live herdasse dele, o filtro
    nunca agiria (era o no-op do #465)."""
    _transcreve(monkeypatch, {"text": "Obrigada.", "language": "pt", "segments": []})
    julga: dict = {}
    lp._stt_app(b"\x00\x01" * 400, "pt", julga)          # knob ligado (default)
    assert julga.get("motivo") == "stt"
    monkeypatch.setattr(lp, "_LIVE_ANTI_RUIDO", False)   # env desligado
    julga2: dict = {}
    assert lp._stt_app(b"\x00\x01" * 400, "pt", julga2) == "Obrigada."
    assert "motivo" not in julga2
