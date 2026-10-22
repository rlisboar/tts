"""Worker TTS PERSISTENTE por sessão do Live (#152) — testes rápidos, sem modelo.

O que este arquivo fixa:
- protocolo NDJSON: o MESMO processo atende N pedidos (é o ponto da task: sem
  recarga de modelo por turno) e devolve áudio decodificável;
- isolação de crash: filho morto no meio da sessão → `_WorkerMorto` no primeiro
  pedido afetado, FALLBACK para o in-process no mesmo turno e a sessão segue;
- erro de resposta e timeout caem no mesmo fallback;
- EXCLUSIVIDADE DE METAL: `gerar()` segura o `app._gen_lock` (o mesmo da geração
  in-process e do worker de lote) enquanto o pedido está em voo;
- knob/família: só liga para família isolada e com `TTS_LIVE_WORKER` ligado.

O filho é um STUB (script python) — o smoke com modelo real é o
`tests/test_worker.py::test_worker_persistente_serve_smoke` (TTS_TEST_WORKER=1).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import app
import live_pipeline as lp

STUB = r'''
import base64, json, os, sys, time

n = 0
cam = os.environ.get("STUB_PID")
if cam:
    open(cam, "w").write(str(os.getpid()))

def envia(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for linha in sys.stdin:
    linha = linha.strip()
    if not linha:
        continue
    m = json.loads(linha)
    t = m.get("type")
    if t == "init":
        envia({"type": "ready", "proto": 1, "sr": 24000, "family": "stub"})
    elif t == "ping":
        envia({"type": "pong"})
    elif t == "close":
        break
    elif t == "synth":
        n += 1
        if os.environ.get("STUB_DORME"):
            time.sleep(float(os.environ["STUB_DORME"]))
        if os.environ.get("STUB_ERR"):
            envia({"type": "err", "id": m.get("id"), "error": "boom no filho"})
        elif os.environ.get("STUB_MUDO"):
            envia({"type": "ok", "id": m.get("id"), "sr": 24000, "n": 4800, "rms": 0.0,
                   "audio_b64": base64.b64encode(b"\x00\x00\x00\x00" * 4800).decode()})
        else:
            envia({"type": "ok", "id": m.get("id"), "sr": 24000, "n": 4800, "rms": 1.0,
                   "audio_b64": base64.b64encode(b"\x00\x00\x80\x3f" * 4800).decode()})
        if os.environ.get("STUB_MORRE_APOS") and n >= int(os.environ["STUB_MORRE_APOS"]):
            sys.stdout.flush()
            os._exit(0)          # morte abrupta: próxima leitura dá EOF
'''


@pytest.fixture
def stub(tmp_path):
    caminho = tmp_path / "stub_worker.py"
    caminho.write_text(STUB)
    return caminho


@pytest.fixture
def filho(monkeypatch, stub, tmp_path):
    """`_LiveWorker` apontando para o stub (sem venv, sem modelo)."""
    monkeypatch.setenv("STUB_PID", str(tmp_path / "pid.txt"))
    cls = lp._LiveWorker          # o nome no módulo é trocado pelo monkeypatch

    def cria(**kw):
        return cls(py=sys.executable, script=str(stub),
                   timeout_s=kw.pop("timeout_s", 5), **kw)

    return cria


@pytest.fixture
def pipeline(monkeypatch):
    """LivePipeline com `_tts_live` e sem prewarm real; `_tts_app` dublado."""
    chamadas = []

    def _tts_app_fake(texto, omni, voice_id=None, espera=None):
        chamadas.append((texto, voice_id))
        return np.ones(2400, dtype=np.float32) * 0.5

    monkeypatch.setattr(lp, "_tts_app", _tts_app_fake)
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: True)

    p = lp.LivePipeline(lambda o: None, lambda b: None, voice_id="voz-teste")
    p.chamadas_in_process = chamadas
    # `_prewarm_app` fica ligado no `__init__`; aqui o prewarm é observável
    p.prewarm_visto = {}
    p._prewarm = lambda voice_id=None, tts_in_process=True: p.prewarm_visto.update(
        voice_id=voice_id, tts=tts_in_process)
    yield p
    if p._worker is not None:      # teste que sobe e não fecha não deixa filho vivo
        p._worker.fecha()


# ---------------------------------------------------------------------------
# protocolo
# ---------------------------------------------------------------------------

def test_mesmo_processo_atende_varios_pedidos(filho, tmp_path):
    """Dois pedidos no MESMO filho (sem recarga) e áudio válido em ambos."""
    w = filho(voice_id="v")
    w.start()
    try:
        a = w.gerar("primeiro", {"num_steps": 8})
        b = w.gerar("segundo", {"num_steps": 8})
    finally:
        pid = int((tmp_path / "pid.txt").read_text())
        w.fecha()
    assert a.size == 4800 and b.size == 4800
    assert np.allclose(a, 1.0)
    assert os.getpid() != pid, "tem de ser outro processo (isolação)"


def test_close_derruba_o_filho(filho):
    w = filho(voice_id="v")
    w.start()
    proc = w._proc
    w.fecha()
    assert w._proc is None
    assert proc.poll() is not None, "filho tem de morrer com a sessão"
    w.fecha()                     # idempotente


def test_erro_de_resposta_vira_worker_morto(filho, monkeypatch):
    monkeypatch.setenv("STUB_ERR", "1")
    w = filho(voice_id="v")
    w.start()
    try:
        with pytest.raises(lp._WorkerMorto):
            w.gerar("texto", {})
    finally:
        w.fecha()


def test_timeout_vira_worker_morto(filho, monkeypatch):
    monkeypatch.setenv("STUB_DORME", "2")
    w = filho(voice_id="v", timeout_s=0.3)
    w.start()
    try:
        with pytest.raises(lp._WorkerMorto):
            w.gerar("texto", {})
    finally:
        w.fecha()


# ---------------------------------------------------------------------------
# exclusividade de Metal
# ---------------------------------------------------------------------------

def test_gerar_segura_o_gen_lock(filho, monkeypatch):
    """Enquanto o pedido está em voo, o `_gen_lock` está tomado (um Metal por vez)."""
    monkeypatch.setenv("STUB_DORME", "0.4")
    monkeypatch.setattr(app, "_use_remote_tts", lambda: False)
    w = filho(voice_id="v")
    w.start()
    livre = []

    def sonda():
        time.sleep(0.15)
        pegou = app._gen_lock.acquire(blocking=False)
        livre.append(pegou)
        if pegou:
            app._gen_lock.release()

    t = threading.Thread(target=sonda)
    t.start()
    try:
        w.gerar("segurando o metal", {})
    finally:
        t.join(5)
        w.fecha()
    assert livre == [False], "o lock tinha de estar tomado durante o pedido"
    assert app._gen_lock.acquire(blocking=False), "lock tem de ser devolvido depois"
    app._gen_lock.release()


# ---------------------------------------------------------------------------
# fallback no pipeline
# ---------------------------------------------------------------------------

def test_falha_do_worker_cai_no_in_process_e_marca_a_sessao(pipeline, filho,
                                                           monkeypatch):
    monkeypatch.setattr(lp, "_LiveWorker", filho)
    monkeypatch.setenv("STUB_ERR", "1")
    assert pipeline._worker_sobe() is True

    audio = pipeline._tts_live("primeiro turno", {})
    assert len(pipeline.chamadas_in_process) == 1      # regerado in-process
    assert pipeline._worker_indisponivel is True
    assert pipeline._worker is not None and pipeline._worker.ativo is False
    assert audio.size == 2400

    pipeline._tts_live("segundo turno", {})            # nem tenta o worker
    assert len(pipeline.chamadas_in_process) == 2


def test_morte_do_filho_nao_derruba_a_sessao(pipeline, filho, monkeypatch):
    monkeypatch.setattr(lp, "_LiveWorker", filho)
    monkeypatch.setenv("STUB_MORRE_APOS", "1")
    assert pipeline._worker_sobe() is True

    assert pipeline._tts_live("antes da morte", {}).size == 4800
    assert pipeline._worker_indisponivel is False
    audio = pipeline._tts_live("depois da morte", {})   # EOF no filho
    assert audio.size == 2400
    assert pipeline._worker_indisponivel is True
    assert pipeline.chamadas_in_process == [("depois da morte", "voz-teste")]


def test_worker_nao_sobe_quando_desabilitado(pipeline, monkeypatch):
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: False)
    assert pipeline._worker_sobe() is False
    pipeline._tts_live("in-process", {})
    assert len(pipeline.chamadas_in_process) == 1


def test_start_avisa_o_prewarm_para_nao_carregar_o_tts(pipeline, filho,
                                                       monkeypatch):
    monkeypatch.setattr(lp, "_LiveWorker", filho)
    pipeline.start()
    assert pipeline.prewarm_visto == {"voice_id": "voz-teste", "tts": False}
    assert pipeline._worker is not None and pipeline._worker.ativo


def test_start_sem_worker_mantem_o_prewarm_completo(pipeline, monkeypatch):
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: False)
    pipeline.start()
    assert pipeline.prewarm_visto == {"voice_id": "voz-teste", "tts": True}


def test_familia_nao_isolada_nao_usa_worker(monkeypatch):
    monkeypatch.setattr(lp, "_LIVE_WORKER_LIGADO", True)
    monkeypatch.setattr(app, "_current_backend",
                        lambda *a, **k: {"id": "x", "family": "omnivoice"})
    assert lp._worker_habilitado() is False
    monkeypatch.setattr(app, "_current_backend",
                        lambda *a, **k: {"id": "k", "family": "kokoro"})
    assert lp._worker_habilitado() is True
    monkeypatch.setattr(lp, "_LIVE_WORKER_LIGADO", False)
    assert lp._worker_habilitado() is False


def test_close_do_pipeline_mata_o_worker(pipeline, filho, monkeypatch):
    monkeypatch.setattr(lp, "_LiveWorker", filho)
    assert pipeline._worker_sobe() is True
    proc = pipeline._worker._proc
    pipeline.close()
    assert pipeline._worker is None
    assert proc.poll() is not None


def test_volta_ao_in_process_depois_de_fechar_a_sessao(pipeline, filho, monkeypatch):
    """Sessão que usou o worker fecha e a geração seguinte vai in-process (sem
    sobra de estado: o pai liberou o modelo no pre-warm e recarrega sozinho)."""
    monkeypatch.setattr(lp, "_LiveWorker", filho)
    assert pipeline._worker_sobe() is True
    assert pipeline._tts_live("com worker", {}).size == 4800
    pipeline.close()

    audio = pipeline._tts_live("depois de fechar", {})
    assert audio.size == 2400                       # veio do _tts_app dublado
    assert pipeline.chamadas_in_process == [("depois de fechar", "voz-teste")]


def test_log_do_filho_tem_nome_unico(filho):
    """Duas sessões simultâneas não podem se atropelar no log (lição #12/#17)."""
    a, b = filho(voice_id="a"), filho(voice_id="b")
    a.start()
    b.start()
    try:
        assert a._log.name != b._log.name
        assert Path(a._log.name).exists() and Path(b._log.name).exists()
    finally:
        a.fecha()
        b.fecha()

# ---------------------------------------------------------------------------
# #305 — o pós do Live: mesma cadeia do job, mudo do worker reprovado
# ---------------------------------------------------------------------------

def _baixo(n: int = 9600, rms: float = 0.05) -> np.ndarray:
    """Áudio no nível CRU do modelo (~0,05), como o worker entrega."""
    rng = np.random.default_rng(3)
    a = rng.standard_normal(n).astype(np.float32)
    return a * (rms / float(np.sqrt(np.mean(a ** 2))))


class _WorkerFalso:
    ativo = True

    def __init__(self, audio):
        self._audio = audio

    def gerar(self, texto, omni, espera=None):
        return self._audio.copy()

    def fecha(self):
        return None


def _rms(a) -> float:
    return float(np.sqrt(np.mean(np.asarray(a, dtype=np.float64) ** 2)))


def test_pos_do_live_iguala_worker_e_in_process(pipeline, monkeypatch):
    """Os dois caminhos passam pelo MESMO pós: mesmo nível, não 2-3 dB de diferença."""
    pipeline._worker = _WorkerFalso(_baixo())
    r_worker = _rms(pipeline._tts_live("mesmo texto", {}))

    monkeypatch.setattr(lp, "_worker_habilitado", lambda: False)
    pipeline._tts_app = lambda texto, omni, voice_id=None, espera=None: _baixo()
    r_app = _rms(pipeline._tts_live("mesmo texto", {}))

    assert abs(20 * np.log10(r_worker / r_app)) <= 1.0, (r_worker, r_app)
    # e ambos no nível do job (alvo 0,15) — sem o pós ficariam nos 0,05 crus
    for r in (r_worker, r_app):
        assert 0.10 < r < 0.22, r


def test_pos_do_live_rampeia_as_bordas(pipeline):
    """Primeiros e últimos 8 ms saem rampeados (sem clique na emenda)."""
    pipeline._worker = _WorkerFalso(np.ones(9600, dtype=np.float32) * 0.5)
    audio = pipeline._tts_live("x", {})
    assert audio[0] == pytest.approx(0.0, abs=1e-6)
    assert audio[-1] == pytest.approx(0.0, abs=1e-6)
    # o miolo continua cheio (o fade não achata o trecho)
    assert float(np.abs(audio[len(audio) // 2])) > 0.1


def test_gerar_reprova_audio_mudo_do_filho(monkeypatch):
    """`rms` abaixo do piso vira `_WorkerMorto` (o turno é regerado in-process)."""
    import base64

    w = lp._LiveWorker()
    w._ativo = True
    monkeypatch.setattr(w, "_envia", lambda obj: None)
    zeros = np.zeros(4800, dtype=np.float32)
    monkeypatch.setattr(w, "_espera", lambda tipos, timeout: {
        "type": "ok", "id": 1, "sr": 24000, "n": zeros.size, "rms": 0.0,
        "audio_b64": base64.b64encode(zeros.tobytes()).decode()})
    with pytest.raises(lp._WorkerMorto) as exc:
        w.gerar("x", {})
    assert "mudo" in str(exc.value)


def test_worker_mudo_derruba_para_o_in_process_no_turno(pipeline, filho, monkeypatch):
    """Filho devolvendo zeros: o turno sai audível e a sessão passa a in-process."""
    monkeypatch.setenv("STUB_MUDO", "1")
    pipeline._worker = filho(voice_id="v")
    pipeline._worker.start()
    pipeline._tts_app = lambda texto, omni, voice_id=None, espera=None: np.ones(
        2400, dtype=np.float32) * 0.5
    audio = pipeline._tts_live("turno", {})
    assert pipeline._worker_indisponivel is True
    assert _rms(audio) > 0.1, "aceitou mudo"
