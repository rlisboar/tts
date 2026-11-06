"""#877: o Live fechava o turno numa pausa de respiração dentro de uma fala longa e
a resposta à 1ª metade começava POR CIMA da 2ª (bancada E2E, #875).

CONTINUAÇÃO: fala que recomeça com o turno anterior ainda sem nenhum áudio de
resposta cancela esse turno SEM gravar history e o áudio dele vai na frente do
turno seguinte — o STT transcreve a frase inteira. Depois do 1º chunk de resposta
a fala por cima é barge (cadeia do #870), não continuação.
"""
import queue
import threading
import types

import numpy as np

import app
import live_pipeline as lp


class CanalFalso:
    """Pipeline mínimo com `ocupado` à mão; registra turnos aceitos e cancels."""

    def __init__(self):
        self._trava = threading.Lock()
        self._ocupado = False
        self._buf = bytearray()
        self.aceitos = []
        self.cancels = []

    @property
    def ocupado(self):
        with self._trava:
            return self._ocupado

    def abrir_turno(self, pcm, barge=False):
        with self._trava:
            if self._ocupado:
                return False
            self._ocupado = True
            self.aceitos.append((bytes(pcm), bool(barge)))
            return True

    def cancel(self, sem_historico=False):
        self.cancels.append(sem_historico)

    def libera(self):
        with self._trava:
            self._ocupado = False


def _sessao(pipe):
    return {"id": "t877", "buffer": bytearray(), "fila": queue.Queue(),
            "fechar": False, "turno": 0, "pipe": pipe,
            "cancelado": threading.Event(), "turno_pendente": None,
            "turno_pendente_barge": False, "pend_thread": None,
            "pendentes_descartados": 0, "prewarm_ok": True, "buffer_consumido": 0}


def _speech_start(barge=False):
    return types.SimpleNamespace(
        tipo="speech_start", t_ms=0, prob=0.9, barge_in=barge,
        to_json=lambda: {"type": "speech_start", "barge_in": barge})


A, B = b"\x01\x00" * 4000, b"\x02\x00" * 3000


def test_fala_que_recomeca_antes_da_resposta_funde_no_mesmo_turno(monkeypatch):
    monkeypatch.setattr(app, "_live_acorda_pendente", lambda s: None)
    pipe = CanalFalso()
    sess = _sessao(pipe)
    app._live_abre_turno(sess, A)                       # 1ª metade fecha na pausa
    app._live_trata_eventos(sess, [_speech_start()])    # dono volta a falar
    assert pipe.cancels == [True], "o turno da 1ª metade não foi cancelado sem history"
    pipe.libera()                                       # o cancel desenrolou
    app._live_abre_turno(sess, B)                       # 2ª metade fecha
    assert pipe.aceitos[-1][0] == A + B, "o turno não levou a frase inteira"


def test_continuacao_com_pipeline_ainda_desenrolando_vai_inteira_ao_pendente(monkeypatch):
    monkeypatch.setattr(app, "_live_acorda_pendente", lambda s: None)
    pipe = CanalFalso()
    sess = _sessao(pipe)
    app._live_abre_turno(sess, A)
    app._live_trata_eventos(sess, [_speech_start()])
    app._live_abre_turno(sess, B)                       # cancel ainda em curso
    assert sess["turno_pendente"] == A + B


def test_depois_do_primeiro_audio_da_resposta_nao_ha_continuacao(monkeypatch):
    monkeypatch.setattr(app, "_marca_pcm", lambda pcm: pcm)
    monkeypatch.setattr(app, "_live_janela_turno", lambda s, on: None)
    pipe = CanalFalso()
    sess = _sessao(pipe)
    app._live_abre_turno(sess, A)
    app._live_envia_audio(sess, b"\x00\x00" * 240)      # a resposta já soou
    app._live_trata_eventos(sess, [_speech_start()])
    assert pipe.cancels == [], "fala por cima de resposta audível é barge, não continuação"


def test_speech_start_de_barge_nao_e_continuacao():
    pipe2 = CanalFalso()
    sess2 = _sessao(pipe2)
    app._live_abre_turno(sess2, A)
    app._live_trata_eventos(sess2, [_speech_start(barge=True)])
    assert pipe2.cancels == []


def test_fora_da_janela_e_fala_nova(monkeypatch):
    monkeypatch.setattr(app, "_LIVE_CONTINUACAO_MS", 2500)
    pipe = CanalFalso()
    sess = _sessao(pipe)
    app._live_abre_turno(sess, A)
    sess["continuavel"]["t"] -= 3.0                     # LLM lento: 3 s sem áudio
    app._live_trata_eventos(sess, [_speech_start()])
    assert pipe.cancels == []


def _pipeline_real(deltas, ao_gerar=None):
    eventos = []

    def llm(msgs):
        for i, d in enumerate(deltas):
            if ao_gerar is not None and i == 1:
                ao_gerar()
            yield d

    p = lp.LivePipeline(eventos.append, lambda pcm: None,
                        stt=lambda pcm, lang: "eu queria saber",
                        llm=llm, tts=lambda c, o: np.zeros(240, np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 1600)
    return p, eventos


def test_cancel_de_continuacao_nao_grava_a_metade_no_history():
    caixa = {}
    p, ev = _pipeline_real(("Claro, ", "o que ", "você quer?"),
                           ao_gerar=lambda: caixa["p"].cancel(sem_historico=True))
    caixa["p"] = p
    p.end_of_speech(inicia_thread=False)
    assert p.history == [], f"a 1ª metade ficou no contexto do LLM: {p.history}"
    inter = [e for e in ev if e.get("type") == "interrupted"]
    assert inter and inter[-1].get("motivo") == "continuacao", inter


def test_cancel_comum_segue_gravando_history():
    """Barge/`cancel` do cliente: o que foi dito fica no contexto (sem mudança)."""
    caixa = {}
    p, ev = _pipeline_real(("Claro, ", "o que ", "você quer?"),
                           ao_gerar=lambda: caixa["p"].cancel())
    caixa["p"] = p
    p.end_of_speech(inicia_thread=False)
    assert [m["role"] for m in p.history] == ["user", "assistant"]
    inter = [e for e in ev if e.get("type") == "interrupted"]
    assert inter and "motivo" not in inter[-1], inter


def test_sem_historico_nao_gruda_no_turno_seguinte():
    """O flag é do turno CANCELADO: o turno fundido que vem depois grava normal."""
    p, _ev = _pipeline_real(("Ok.",))
    p.cancel(sem_historico=True)                        # resto do turno anterior
    p.end_of_speech(inicia_thread=False)               # turno NOVO
    assert [m["role"] for m in p.history] == ["user", "assistant"]
