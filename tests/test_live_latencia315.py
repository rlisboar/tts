"""#315: latência auditável do Live — decisão medida, gate de prewarm, perfil.

O alvo do épico é "fim-de-fala → 1º áudio ≤ 1,5 s" e ele não era auditável: o
`latency` contava de outro zero (o início da thread) e a decisão do VAD ficava só
na sessão. Aqui entram `decisao_ms`/`decisao_para_audio_ms` no evento; o gate do
prewarm segura a primeira fala 2 s em vez de pagar o modelo frio; e o 2º chunk sai
no perfil rápido (o 3º em diante volta ao setting).

Sem modelo e sem WS real (dublês), exceto onde o comentário diz o contrário.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

import app
import live_pipeline as lp


# -- 1. a decisão entra na conta ------------------------------------------
def _sess(**extra) -> dict:
    sess = {"id": "s-lat", "t0": time.monotonic(), "criada": time.time(),
            "visto": time.monotonic(), "turno": 1, "fila": app._LiveFila(),
            "cancelado": app.threading.Event(), "engine": None, "pipe": None,
            "st_stage": "stt", "st_stage_ini": None, "st_erro": None,
            "t_decisao_ms": 480, "t_decisao": None, "t_abre_turno": None}
    sess.update(extra)
    return sess


def test_latency_publica_a_decisao_e_a_soma_com_o_primeiro_audio():
    sess = _sess()
    sess["t_decisao"] = time.monotonic() - 0.100      # decisão 100 ms atrás
    sess["t_abre_turno"] = time.monotonic()           # ...e o turno abre agora
    ev = {"type": "latency", "first_audio_ms": 900, "stt_ms": 200}
    app._live_observa(sess, ev)
    assert ev["decisao_ms"] == 480                    # latência do detector
    assert 980 <= ev["decisao_para_audio_ms"] <= 1100  # 900 + os ~100 ms de espera
    assert ev["decisao_para_audio_ms"] >= ev["first_audio_ms"]
    assert sess["t_decisao"] is None and sess["t_abre_turno"] is None


def test_latency_sem_decisao_nao_inventa_os_campos():
    ev = {"type": "latency", "first_audio_ms": 900}
    app._live_observa(_sess(), ev)
    assert "decisao_ms" not in ev and "decisao_para_audio_ms" not in ev


def test_latency_sem_primeiro_audio_ainda_publica_a_decisao():
    """Turno interrompido antes do áudio: `decisao_ms` vale, a soma não."""
    sess = _sess(t_decisao=time.monotonic(), t_abre_turno=time.monotonic())
    ev = {"type": "latency", "first_audio_ms": None}
    app._live_observa(sess, ev)
    assert ev["decisao_ms"] == 480 and "decisao_para_audio_ms" not in ev


def test_speech_end_carimba_a_decisao():
    """O carimbo do relógio tem de nascer no evento que ABRE o turno."""
    class Ev:
        tipo, t_ms, t_decisao_ms = "speech_end", 0, 512
        prob, rms_dbfs, fala_ms, curto, barge_falso, detalhe = 0.9, -20.0, 700, False, False, ""
        audio, barge_in = b"", False

        def to_json(self):
            return {"type": self.tipo}

    sess = _sess(pipe=None)
    app._live_trata_eventos(sess, [Ev()])
    assert isinstance(sess["t_decisao"], float) and sess["t_decisao_ms"] == 512
    # o stub (sem pipe) fecha o turno e o `abre` segue nulo: a soma só sai com áudio
    assert sess["t_abre_turno"] is None


# -- 2. gate do prewarm ---------------------------------------------------
class _PipePrewarm:
    """Pipeline de dublê: o que o gate consulta é `ocupado` e o registro do turno."""

    ocupado = False

    def __init__(self) -> None:
        self.turnos: list[bytes] = []

    def push_pcm(self, pcm, substituir=False):
        self.turnos.append(pcm)

    def end_of_speech(self, **kw):
        return True


def test_fala_antes_do_prewarm_fica_no_pendente(monkeypatch):
    monkeypatch.setattr(app, "_LIVE_ESPERA_PREWARM_S", 2.0)
    pipe = _PipePrewarm()
    sess = _sess(pipe=pipe)
    app._live_abre_turno(sess, b"\x00\x01" * 400)
    assert pipe.turnos == []                      # o turno NÃO abriu ainda
    assert sess["turno_pendente"]                  # a fala está guardada
    assert sess["prewarm_ate"] is not None


def test_prewarm_concluido_solta_o_turno_pendente(monkeypatch):
    monkeypatch.setattr(app, "_LIVE_ESPERA_PREWARM_S", 2.0)
    pipe = _PipePrewarm()
    sess = _sess(pipe=pipe, prewarm_ate=time.monotonic() + 2.0)
    sess["turno_pendente"] = b"\x00\x01" * 400
    app._live_acorda_pendente(sess)
    time.sleep(0.05)
    sess["prewarm_ok"] = True                     # o prewarm terminou
    app._live_acorda_pendente(sess)
    _espera(lambda: bool(pipe.turnos), 2.0)
    assert pipe.turnos and sess.get("turno_pendente") is None


def test_prewarm_estourado_avisa_e_abre_assim_mesmo(monkeypatch):
    monkeypatch.setattr(app, "_LIVE_ESPERA_PREWARM_S", 0.05)
    pipe = _PipePrewarm()
    sess = _sess(pipe=pipe)
    sess["turno_pendente"] = b"\x00\x01" * 400
    sess["prewarm_ate"] = time.monotonic() + 0.05
    app._live_acorda_pendente(sess)
    _espera(lambda: bool(pipe.turnos), 3.0)
    tipos = [i[1].get("code") for i in list(sess["fila"]._q.queue)
             if isinstance(i[1], dict)]
    assert "prewarm_em_curso" in tipos            # o cliente VÊ o motivo da demora
    assert pipe.turnos                            # abriu o turno com o modelo frio
    assert sess["prewarm_ate"] is None            # não espera de novo


def test_espera_zero_restaura_o_comportamento_antigo(monkeypatch):
    monkeypatch.setattr(app, "_LIVE_ESPERA_PREWARM_S", 0.0)
    pipe = _PipePrewarm()
    sess = _sess(pipe=pipe)
    app._live_abre_turno(sess, b"\x00\x01" * 400)
    assert pipe.turnos                             # abriu direto
    assert not sess.get("turno_pendente")


def test_prewarm_ok_desliga_o_gate(monkeypatch):
    monkeypatch.setattr(app, "_LIVE_ESPERA_PREWARM_S", 2.0)
    pipe = _PipePrewarm()
    sess = _sess(pipe=pipe, prewarm_ok=True)
    app._live_abre_turno(sess, b"\x00\x01" * 400)
    assert pipe.turnos and not sess.get("turno_pendente")


def _espera(cond, limite: float) -> None:
    fim = time.monotonic() + limite
    while time.monotonic() < fim and not cond():
        time.sleep(0.02)


# -- 3. perfil do 2º chunk ------------------------------------------------
def _perfis(monkeypatch, n_chunks: int) -> list[dict]:
    usados: list[dict] = []
    monkeypatch.setitem(app._settings, "live_num_steps", 20)
    # o teto do perfil só aparece no `min()` se o setting global da família for
    # MAIOR: com o default do código (16) o 2º chunk sairia 16 e a prova mediria
    # o outro caminho. Fixar aqui deixa a asserção independente do settings.json
    # do dono (em máquina limpa/CI o arquivo não existe).
    monkeypatch.setitem(app._settings, "omni_num_steps", 64)
    p = lp.LivePipeline(lambda o: None, lambda pcm: None,
                        stt=lambda pcm, l: "fala",
                        llm=lambda msgs: iter(["um. ", "dois. ", "tres. ", "quatro. "]),
                        tts=lambda t, o: usados.append(o) or np.zeros(8, dtype=np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    return usados


def test_perfil_rapido_no_primeiro_e_no_segundo_chunk(monkeypatch):
    usados = _perfis(monkeypatch, 4)
    assert len(usados) >= 3
    assert usados[0]["num_steps"] <= lp._LIVE_MAX_STEPS      # 1º: teto 12
    assert usados[1]["num_steps"] == 20                      # 2º: `live_num_steps`
    assert usados[2]["num_steps"] == app._settings["omni_num_steps"]   # 3º: setting


def test_live_num_steps_absurdo_e_limitado(monkeypatch):
    monkeypatch.setitem(app._settings, "live_num_steps", 1)
    assert lp._num_steps_live() == 4
    monkeypatch.setitem(app._settings, "live_num_steps", "x")
    assert lp._num_steps_live() == lp._LIVE_NUM_STEPS_PADRAO


# -- 4. teto do histórico no setup (entrou no #313, provado lá) -----------
def test_setup_corta_historico_absurdamente_grande():
    gordo = "x" * 4000
    s = app._live_valida_setup({"voice_id": app.DESIGN_VOICE_ID,
                                "history": [{"role": "user", "text": gordo}] * 50})
    assert s["history_descartados"] > 0
    assert sum(len(h["text"]) for h in s["history"]) <= app._LIVE_SETUP_HIST_MAX_CHARS


if __name__ == "__main__":                    # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
