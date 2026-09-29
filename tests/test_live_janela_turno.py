"""#167: o app marca o TURNO do assistente no motor (janela de barge colada ao turno).

A janela do motor era dimensionada pela FILA de chunks (`audio_pendente`), então
fechava 900 ms depois do último envio e o onset num VÃO de geração virava turno
novo em vez de interrupção. Estes testes fixam o call site: abre com o 1º áudio do
turno e fecha no evento terminal. Sem modelo e sem WS.
"""

from __future__ import annotations

import queue

import app


class _EngFake:
    def __init__(self) -> None:
        self.chamadas: list[bool] = []

    def set_turno_aberto(self, aberto: bool) -> None:
        self.chamadas.append(bool(aberto))


def _sess(engine=None):
    eng = engine if engine is not None else _EngFake()
    sess = {"id": "s1", "fila": queue.Queue(), "engine": eng, "falando": False,
            "audio_pendente": 0, "st_chunks": 0, "st_audio_bytes": 0}
    return sess, eng


def test_audio_do_turno_abre_a_janela_no_motor():
    sess, eng = _sess()
    app._live_envia_audio(sess, b"\x00\x00" * 10)
    assert eng.chamadas == [True]
    assert sess["audio_pendente"] == 1


def test_evento_terminal_fecha_a_janela():
    sess, eng = _sess()
    app._live_envia_json(sess, {"type": "turn_complete", "turn": 1})
    app._live_envia_json(sess, {"type": "interrupted", "turn": 2})
    assert eng.chamadas == [False, False]


def test_evento_nao_terminal_nao_mexe_na_janela():
    sess, eng = _sess()
    app._live_envia_json(sess, {"type": "stats"})
    app._live_envia_json(sess, {"type": "speech_start", "turn": 1})
    assert eng.chamadas == []


def test_audio_vazio_nao_abre_a_janela():
    sess, eng = _sess()
    app._live_envia_audio(sess, b"")
    assert eng.chamadas == []
    assert sess["audio_pendente"] == 0


def test_motor_sem_a_api_nova_nao_quebra():
    """Motor antigo (sem `set_turno_aberto`) tem de seguir funcionando."""

    class _Velho:
        pass

    sess, _ = _sess(engine=_Velho())
    app._live_envia_audio(sess, b"\x00\x00")
    app._live_envia_json(sess, {"type": "turn_complete"})
    assert sess["audio_pendente"] == 1


def test_erro_no_motor_nao_derruba_o_envio():
    class _Explode:
        def set_turno_aberto(self, aberto):
            raise RuntimeError("boom")

    sess, _ = _sess(engine=_Explode())
    app._live_envia_audio(sess, b"\x00\x00")
    app._live_envia_json(sess, {"type": "interrupted"})
    assert sess["audio_pendente"] == 1
    assert not sess["fila"].empty()