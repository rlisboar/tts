"""GATE QA #L (`task_e076e406`): julgamento do turno no Live.

O Live chamava `_transcribe` direto: o filtro anti-alucinação do projeto (blacklist,
`no_speech_prob`, `logprob`, `compression_ratio`) só valia na Conversa. Agora o
turno julgado pelo STT fecha com `motivo` e não gasta LLM.
"""

import numpy as np
import pytest

import live_pipeline as lp
import live_turns as lt
from test_live_pipeline import Fala

__all__ = ["lp", "lt"]


def _pipeline(fala, texto, *, language="pt"):
    """Pipeline com o `_stt_app` REAL (para exercitar o `_stt_ok`)."""
    usou = {"llm": 0}

    def llm(msgs):
        usou["llm"] += 1
        yield "certo."

    p = lp.LivePipeline(fala.json, fala.audio, llm=llm, tts=lambda c, o, **k: np.zeros(240, np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 1600)
    p._transcricao = (texto, language)
    return p, usou


@pytest.fixture(autouse=True)
def _stt_falso(monkeypatch):
    import app

    def _transcreve(_path, language=None, allow_remote=True):
        texto, lang = p_atual["p"]._transcricao
        return {"text": texto, "language": lang,
                "segments": [{"no_speech_prob": 0.05, "avg_logprob": -0.2,
                              "compression_ratio": 1.1, "start": 0.0, "end": 1.0}]}

    monkeypatch.setattr(app, "_transcribe", _transcreve)
    # o arquivo do dono tem `stt_anti_ruido: false`: sem fixar, o `_stt_ok` é no-op
    monkeypatch.setitem(app._settings, "stt_anti_ruido", True)
    monkeypatch.setattr(app, "_vad_tem_fala", lambda *a, **k: True)
    return _transcreve


p_atual = {}


def _roda(fala, texto, **kw):
    p, usou = _pipeline(fala, texto, **kw)
    p_atual["p"] = p
    p.end_of_speech(inicia_thread=False)
    return p, usou


def test_alucinacao_da_blacklist_descarta_e_nao_chama_llm():
    fala = Fala()
    _p, usou = _roda(fala, "Amara.org")
    fim = [e for e in [e for _, e in fala.eventos] if e["type"] == "turn_complete"][-1]
    assert fim["descartado"] is True and fim["motivo"] == "stt"
    assert usou["llm"] == 0


def test_compressao_alta_tambem_descarta(monkeypatch):
    import app

    original = app._stt_ok

    def _ok(r, texto, *a, **k):          # repassa o `forcar` (novo no #4xx)
        segs = [{"no_speech_prob": 0.05, "avg_logprob": -0.2,
                 "compression_ratio": 9.9}]
        return original({**r, "segments": segs}, texto, *a, **k)

    monkeypatch.setattr(app, "_stt_ok", _ok)
    fala = Fala()
    p, usou = _pipeline(fala, "bom dia bom dia bom dia bom dia bom dia bom dia bom dia")
    p_atual["p"] = p
    p.end_of_speech(inicia_thread=False)
    fim = [e for e in [e for _, e in fala.eventos] if e["type"] == "turn_complete"][-1]
    assert fim["descartado"] is True and "repetitivo" in fim["detalhe"]
    assert usou["llm"] == 0


def test_fala_curta_com_confianca_passa():
    """"Sim." é fala legítima: não pode ser engolida."""
    fala = Fala()
    _p, usou = _roda(fala, "Sim.")
    textos = [e for e in [e for _, e in fala.eventos] if e["type"] == "transcript_user"]
    assert textos and textos[-1]["text"] == "Sim."
    assert usou["llm"] == 1
    fim = [e for e in [e for _, e in fala.eventos] if e["type"] == "turn_complete"][-1]
    assert not fim.get("descartado")


def test_recusa_do_vad_sai_com_motivo_proprio():
    fala = Fala()
    p, usou = _pipeline(fala, "")
    p_atual["p"] = p
    p._transcricao = ("", "")          # formato do ramo de recusa do `_transcribe`
    p.end_of_speech(inicia_thread=False)
    fim = [e for e in [e for _, e in fala.eventos] if e["type"] == "turn_complete"][-1]
    assert fim["descartado"] is True and fim["motivo"] == "vad"
    assert usou["llm"] == 0


def test_hold_to_talk_abre_turno_sem_vad_e_aparece_no_stats():
    eng = lt.TurnEngine(config=lt.Config(prefix_ms=100, silence_ms=400,
                                         sugestao_apos_barge_in=3,
                                         taxa_falso_barge_in_limite=0.5))
    # três barges falsos: sugestão de hold aparece
    eng._stat["barge_in"] = 3
    eng._stat["barge_in_falso"] = 3          # 100 % de barge falso, acima do limite
    assert eng.estatisticas()["hold_to_talk_sugerido"] is True

    eventos = eng.set_hold(True)
    assert eng.estatisticas()["hold_to_talk"] is True
    assert any(getattr(e, "tipo", None) == "speech_start" for e in eventos)
    fechou = eng.set_hold(False)
    assert any(getattr(e, "tipo", None) == "speech_end" for e in fechou)
