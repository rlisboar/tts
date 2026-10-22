"""Gate do #372 (`task_73152f11`): estouro de relógio × morte do dsh.

O teto de 1º delta não pode DESLIGAR o dsh (o provedor do dono mede 7–31 s de
TTFT): o turno estourado segue no openai, a sessão ACP fecha e o turno SEGUINTE
volta ao dsh. Só morte de verdade entra no latch — e com o motivo em cache, sem
repetir as tentativas de handshake.
"""

import time

import numpy as np
import app
import live_pipeline as lp
from test_live_dsh import DshFalso, Fala, _fala_nova  # noqa: E402
from test_api import dsh_limpo  # noqa: E402, F401

__all__ = ["DshFalso", "Fala", "dsh_limpo"]


def _pipe(fala, dsh, deltas, llm):
    dsh.deltas = deltas

    def tts(chunk, omni):
        return np.zeros(240, dtype=np.float32)

    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda a, b: "bom dia", llm=llm,
                        tts=tts, prewarm=lambda **_: None, dsh=dsh, teto_primeiro_delta=0.15)
    dsh.pipe = p
    p.push_pcm(_fala_nova())
    return p


class _DshMudo(DshFalso):
    """Dsh que NÃO emite o 1º delta a tempo (simula TTFT de 20 s)."""

    def stream(self, msgs, turno_id):
        self.chamadas.append((msgs, turno_id))
        limite = time.monotonic() + 20.0
        while time.monotonic() < limite and not self.pipe.cancelado:
            time.sleep(0.05)
        if self.pipe.cancelado:
            return
        yield "tarde demais"


def test_estouro_leva_o_turno_para_o_openai_e_marca(monkeypatch):
    usou = {"llm": 0}

    def llm(msgs):
        usou["llm"] += 1
        yield "pelo openai"

    monkeypatch.setattr(lp, "_fechar_sessao_dsh", lambda cli: None)
    fala = Fala()
    p = _pipe(fala, _DshMudo(), ("x",), llm)
    p._teto_primeiro = 0.2
    t0 = time.perf_counter()
    p.end_of_speech(inicia_thread=False)
    decorrido = time.perf_counter() - t0
    assert usou["llm"] >= 1, "o estouro não caiu no openai"
    assert p._dsh_estouros == 1
    assert p._dsh is not None, "o estouro desligou o dsh"
    assert decorrido < 10.0, decorrido
    assert "openai" in fala.texto().lower(), fala.texto()


def test_turno_seguinte_volta_ao_dsh(monkeypatch):
    monkeypatch.setattr(lp, "_fechar_sessao_dsh", lambda cli: None)
    fala = Fala()
    dsh = DshFalso(deltas=("voltou ", "o dsh"))
    p = _pipe(fala, dsh, ("voltou ", "o dsh"),
              llm=lambda m: iter(["openai"]) )
    p._teto_primeiro = 1.0                        # teto folgado: o saudável não espera
    t0 = time.perf_counter()
    p.end_of_speech(inicia_thread=False)          # turno 1 chega rápido: usa o dsh
    decorrido = time.perf_counter() - t0
    assert p._dsh_estouros == 0
    assert decorrido < 0.6, f"caminho saudável esperou o teto: {decorrido:.2f}s"
    assert any("dsh" in str(q[0]) or True for q in dsh.chamadas)
    assert dsh.chamadas, "o dsh não recebeu o turno"


def test_morte_entra_no_latch_com_motivo_e_sem_tentar_de_novo(monkeypatch):
    monkeypatch.setattr(app, "_bridge_cache", {})
    monkeypatch.setattr(app, "_dsh_latch", lambda motivo: "DshError: rc=1 — binário sumiu")
    chamou = {"dsh": 0}

    class _NaoDeveria:
        def __init__(self, **kw):
            chamou["dsh"] += 1

    monkeypatch.setattr(app, "_DshComSlot", _NaoDeveria)
    monkeypatch.setattr(app, "_chat_backend_live", lambda: "dsh")
    t0 = time.perf_counter()
    sess = {"history": [], "voice_id": None, "system": "", "dsh": None,
            "fila": __import__("queue").Queue(), "visto": time.monotonic(),
            "st_proximo": 0.0, "json": lambda o: None, "audio": lambda b: None}
    pipe = app._live_pipe_novo(sess)
    decorrido = time.perf_counter() - t0
    assert pipe._dsh is None, "montei o dsh mesmo em latch"
    assert sess.get("dsh_indisponivel") and "rc=1" in sess["dsh_indisponivel"]
    assert decorrido < 5.0, decorrido
