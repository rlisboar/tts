"""GATE QA #J (`task_59ca65b5`): o Live passa pelo MESMO pós do job.

Antes o Live devolvia o chunk cru: 2-3 dB abaixo do mesmo texto no job, clique a
cada borda e áudio mudo do worker aceito como bom.

Aqui: paridade de nível entre os dois caminhos com o mesmo conteúdo, bordas
rampeadas, mudo do worker virando falha com regeração, e o custo da cadeia
medido (ela não pode abrir arquivo por chunk).
"""

import time

import numpy as np

import live_pipeline as lp

__all__ = ["lp"]


class _WorkerFalso:
    ativo = True

    def __init__(self, audio):
        self._audio = audio

    def gerar(self, texto, omni, espera=None):
        return self._audio.copy()

    def fecha(self):
        return None


def _cru(n=9600, rms=0.05):
    rng = np.random.default_rng(7)
    a = rng.standard_normal(n).astype(np.float32)
    return a * (rms / float(np.sqrt(np.mean(a ** 2))))


def _pipe(monkeypatch):
    class _Eng:
        def __init__(self, config=None):
            pass

        def feed(self, pcm):
            return []

        def flush(self):
            return []

        def cancel(self):
            return {}

        def set_speaking(self, *a, **k):
            return None

    monkeypatch.setattr(lp, "_worker_habilitado", lambda: True)
    p = lp.LivePipeline(lambda o: None, lambda b: None, voice_id="v")
    p._prewarm = lambda voice_id=None, tts_in_process=True: None
    return p


def _rms(a) -> float:
    return float(np.sqrt(np.mean(np.asarray(a, dtype=np.float64) ** 2)))


def test_paridade_de_nivel_entre_worker_e_in_process(monkeypatch):
    p = _pipe(monkeypatch)
    p._worker = _WorkerFalso(_cru())
    r_worker = _rms(p._tts_live("mesmo texto", {}))

    monkeypatch.setattr(lp, "_worker_habilitado", lambda: False)
    p._tts_app = lambda texto, omni, voice_id=None, espera=None: _cru()
    r_app = _rms(p._tts_live("mesmo texto", {}))
    dif = 20 * np.log10(r_worker / r_app)
    assert abs(dif) <= 1.0, (round(dif, 2), r_worker, r_app)
    # e ambos no nível do job (alvo 0,15), não nos 0,05 crus
    for r in (r_worker, r_app):
        assert 0.10 < r < 0.22, r


def test_bordas_rampeadas(monkeypatch):
    p = _pipe(monkeypatch)
    p._worker = _WorkerFalso(np.ones(9600, dtype=np.float32) * 0.5)
    a = p._tts_live("x", {})
    assert abs(float(a[0])) < 1e-6 and abs(float(a[-1])) < 1e-6
    assert float(np.abs(a[len(a) // 2])) > 0.1, "o fade achatou o trecho"


def test_worker_mudo_e_falha_e_regera(monkeypatch):
    p = _pipe(monkeypatch)
    p._worker = _WorkerFalso(np.zeros(9600, dtype=np.float32))
    p._tts_app = lambda texto, omni, voice_id=None, espera=None: np.ones(
        2400, dtype=np.float32) * 0.5
    a = p._tts_live("turno", {})
    assert p._worker_indisponivel is True
    assert _rms(a) > 0.1, "aceitou mudo"


def test_cadeia_nao_abre_arquivo_e_e_barata(monkeypatch):
    """O pós do Live tem de ser vetorial: nada de I/O por chunk e custo baixo."""
    import builtins

    p = _pipe(monkeypatch)
    p._worker = _WorkerFalso(_cru(2400))
    p._tts_live("aquecer", {})                 # aquece o import das libs

    original = builtins.open
    abertos = []

    def espiao(*a, **k):
        nome = str(a[0]) if a else ""
        if not nome.startswith("/dev"):
            abertos.append(nome)
        return original(*a, **k)

    monkeypatch.setattr(builtins, "open", espiao)
    t = time.perf_counter()
    p._tts_live("turno medido", {})
    decorrido = (time.perf_counter() - t) * 1000
    assert abertos == [], f"a cadeia abriu arquivo por chunk: {abertos}"
    assert decorrido < 50.0, f"pós caro: {decorrido:.1f} ms para 100 ms de áudio"


class _WorkerZerosMentindo(lp._LiveWorker):
    """Filho que devolve zeros e MIQUE no `rms`: o piso do `gerar` não acusa."""

    def __init__(self, audio, mentira):
        self._ativo = True
        self._n = 0
        self._audio = audio
        self._mentira = mentira
        self._timeout = 5.0
        self._proc = None           # `fecha()` do pai tolera None

    def _envia(self, msg):
        return None

    def fecha(self):
        self._ativo = False

    def _espera(self, tipos, timeout):
        import base64

        r = {"type": "ok", "id": self._n,
             "audio_b64": base64.b64encode(self._audio.tobytes()).decode()}
        if self._mentira is not None:
            r["rms"] = self._mentira
        return r


def _com_zeros(monkeypatch, mentira):
    import app

    monkeypatch.setattr(app, "_use_remote_tts", lambda: False)
    p = _pipe(monkeypatch)
    p._worker = _WorkerZerosMentindo(np.zeros(2400, dtype=np.float32), mentira)
    p._tts_app = lambda texto, omni, voice_id=None, espera=None: _cru(2400)
    return p


def test_worker_que_mente_o_rms_nao_atravessa_mudo(monkeypatch):
    """O piso do filho passou (rms 0,5 declarado), mas o áudio é zeros."""
    p = _com_zeros(monkeypatch, mentira=0.5)
    a = p._tts_live("turno", {})
    assert p._worker_indisponivel is True
    assert _rms(a) > 0.05, "aceitou o mudo porque o filho mentiu o rms"


def test_worker_sem_o_campo_rms_tambem_e_conferido(monkeypatch):
    """Sem `rms` no protocolo, quem decide é o áudio de verdade."""
    p = _com_zeros(monkeypatch, mentira=None)
    a = p._tts_live("turno", {})
    assert p._worker_indisponivel is True and _rms(a) > 0.05
