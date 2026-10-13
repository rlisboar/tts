"""RNNoise no PCM do Live (#278) — sintético, sem microfone.

O filtro real (ffmpeg `arnndn` + `models/rnnoise/sh.rnnn`) prova duas coisas:
ruído estacionário perde energia na banda da voz, e um trecho modulado como
fala, com o mesmo ruído, não é zerado. O encaixe no handler prova que o frame
que chega em `TurnEngine.feed` já passou pelo filtro.
"""
from __future__ import annotations

import logging
import math
import time
from types import SimpleNamespace

import numpy as np

import live_rnnoise
from live_rnnoise import SR, RnnoiseAoVivo

FRAME = SR * 2 // 10          # 100 ms, o bloco do live-worklet


def _pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def _f32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0


def _banda(x: np.ndarray, lo: float, hi: float) -> float:
    w = np.hanning(len(x))
    spec = np.fft.rfft(x * w)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    m = (freqs >= lo) & (freqs <= hi)
    return float(np.sqrt(np.sum(np.abs(spec[m]) ** 2) / len(x)))


def _em_frames(ns: RnnoiseAoVivo, x: np.ndarray) -> np.ndarray:
    raw = _pcm(x)
    saida = []
    for i in range(0, len(raw), FRAME):
        pedaco = raw[i:i + FRAME]
        y = ns.processa(pedaco)
        assert len(y) == len(pedaco)
        saida.append(y)
    return _f32(b"".join(saida))


def test_ligado_respeita_o_ambiente(monkeypatch):
    monkeypatch.delenv("TTS_LIVE_RNNOISE", raising=False)
    assert live_rnnoise.ligado() is True
    monkeypatch.setenv("TTS_LIVE_RNNOISE", "0")
    assert live_rnnoise.ligado() is False


def test_sem_modelo_devolve_o_cru_e_avisa(caplog, tmp_path):
    live_rnnoise._avisou.clear()
    ns = RnnoiseAoVivo(modelo=tmp_path / "ausente.rnnn")
    cru = b"\x01\x00" * 160
    with caplog.at_level(logging.WARNING, logger="live"):
        assert ns.abrir() is False
        assert ns.processa(cru) == cru
    assert any("sem modelo" in r.message for r in caplog.records)
    ns.fechar()


def test_sem_ffmpeg_devolve_o_cru_e_avisa(caplog):
    live_rnnoise._avisou.clear()
    ns = RnnoiseAoVivo(ffmpeg="/nao/existe/ffmpeg-rnnoise")
    cru = b"\x01\x00" * 80
    with caplog.at_level(logging.WARNING, logger="live"):
        assert ns.abrir() is False
        assert ns.processa(cru) == cru
    assert any("sem ffmpeg" in r.message for r in caplog.records)
    ns.fechar()


def test_ruido_estacionario_cai_e_fala_nao_zera():
    """Tom + piso sem fala perde a banda da voz; fala modulada com o mesmo piso fica."""
    assert live_rnnoise.MODELO_PADRAO.is_file(), "sh.rnnn tem de estar no repo"
    rng = np.random.default_rng(0)
    n = SR * 2
    t = np.arange(n) / SR
    ruido = 0.08 * np.sin(2 * math.pi * 110 * t) + 0.05 * rng.standard_normal(n)
    env = np.zeros(n)
    for s in range(0, n, int(0.12 * SR)):
        e = min(n, s + int(0.08 * SR))
        env[s:e] = np.hanning(e - s)
    fala = env * (
        0.25 * np.sin(2 * math.pi * 180 * t)
        + 0.18 * np.sin(2 * math.pi * 540 * t)
        + 0.10 * np.sin(2 * math.pi * 1600 * t))
    ns = RnnoiseAoVivo()
    assert ns.abrir() is True
    try:
        y_ruido = _em_frames(ns, ruido)
        y_fala = _em_frames(ns, fala + ruido * 0.35)
        assert ns.faltas == 0, "o relógio não pode ser completado com zero"
        # ímpar não entra no filtro (quebraria o PCM16) e volta igual
        impar = b"\x00\x01\x02"
        assert ns.processa(impar) == impar
    finally:
        ns.fechar()
    assert not ns.ativo

    banda_ruido_in = _banda(ruido, 300, 3400)
    banda_ruido_out = _banda(y_ruido, 300, 3400)
    assert banda_ruido_out < banda_ruido_in * 0.5, (
        f"ventoinha/piso não caiu na banda da voz: {banda_ruido_in:.4f} -> {banda_ruido_out:.4f}")

    rms_in = float(np.sqrt(np.mean((fala + ruido * 0.35) ** 2)))
    rms_out = float(np.sqrt(np.mean(y_fala ** 2)))
    assert rms_out > rms_in * 0.4, f"a fala foi zerada: rms {rms_in:.4f} -> {rms_out:.4f}"
    assert _banda(y_fala, 300, 3400) > _banda(fala + ruido * 0.35, 300, 3400) * 0.5


def test_frame_do_live_entra_limpo_no_feed(monkeypatch):
    """O byte que o motor vê é o que saiu do filtro, não o cru do websocket."""
    import app
    from fastapi.testclient import TestClient

    fed: list[bytes] = []

    class _Eng:
        def __init__(self, config=None):
            pass

        def feed(self, pcm):
            fed.append(bytes(pcm))
            return []

        def flush(self):
            return []

        def cancel(self):
            return {}

        def set_speaking(self, ligado, nivel_dbfs=None, duracao_ms=None):
            return None

    class _Filtro:
        def processa(self, pcm):
            return b"\x22\x11" * (len(pcm) // 2)

        def fechar(self):
            return None

    monkeypatch.setattr(app, "_live_mod", None)
    monkeypatch.setattr(app, "_live_turns_mod",
                        SimpleNamespace(TurnEngine=_Eng, Config=lambda **k: k))
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 0)
    with app._live_lock:
        app._live_sessions.clear()
    cru = b"\x00\x01" * 1600
    limpo = b"\x22\x11" * 1600
    try:
        cliente = TestClient(app.app, raise_server_exceptions=False,
                             client=("127.0.0.1", 50000))
        with cliente.websocket_connect("/api/live/ws") as ws:
            ws.send_json({"type": "setup"})
            assert ws.receive_json()["type"] == "ready"
            sess = list(app._live_sessions.values())[0]
            sess["rnnoise"] = _Filtro()
            ws.send_bytes(cru)
            for _ in range(40):
                if fed and bytes(sess["buffer"]) == limpo:
                    break
                time.sleep(0.05)
            assert fed == [limpo]
            assert bytes(sess["buffer"]) == limpo
    finally:
        with app._live_lock:
            app._live_sessions.clear()


def test_tamanho_errado_cai_para_o_cru(monkeypatch):
    import app
    from fastapi.testclient import TestClient

    fed: list[bytes] = []

    class _Eng:
        def __init__(self, config=None):
            pass

        def feed(self, pcm):
            fed.append(bytes(pcm))
            return []

        def flush(self):
            return []

        def cancel(self):
            return {}

        def set_speaking(self, ligado, nivel_dbfs=None, duracao_ms=None):
            return None

    class _Ruim:
        def __init__(self):
            self.fechou = False

        def processa(self, pcm):
            return pcm[:2]

        def fechar(self):
            self.fechou = True

    monkeypatch.setattr(app, "_live_mod", None)
    monkeypatch.setattr(app, "_live_turns_mod",
                        SimpleNamespace(TurnEngine=_Eng, Config=lambda **k: k))
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 0)
    with app._live_lock:
        app._live_sessions.clear()
    cru = b"\x00\x01" * 1600
    ruim = _Ruim()
    try:
        cliente = TestClient(app.app, raise_server_exceptions=False,
                             client=("127.0.0.1", 50000))
        with cliente.websocket_connect("/api/live/ws") as ws:
            ws.send_json({"type": "setup"})
            assert ws.receive_json()["type"] == "ready"
            sess = list(app._live_sessions.values())[0]
            sess["rnnoise"] = ruim
            ws.send_bytes(cru)
            for _ in range(40):
                if fed:
                    break
                time.sleep(0.05)
            assert fed == [cru], "tamanho diferente não pode entrar no motor"
            assert bytes(sess["buffer"]) == cru
            assert sess.get("rnnoise") is None
            assert ruim.fechou is True
    finally:
        with app._live_lock:
            app._live_sessions.clear()
