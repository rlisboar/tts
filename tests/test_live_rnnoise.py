"""RNNoise no PCM do Live (#278) — sintético, sem microfone.

O filtro real (ffmpeg `arnndn` + `models/rnnoise/sh.rnnn`) prova duas coisas:
ruído estacionário perde energia na banda da voz, e um trecho modulado como
fala, com o mesmo ruído, não é zerado. O encaixe no handler prova que o frame
que chega em `TurnEngine.feed` já passou pelo filtro.
"""
from __future__ import annotations

import logging
import math
import threading
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


# ---------------------------------------------------------------------------
# #374 — regressões do gate #281
# ---------------------------------------------------------------------------

class _PopenEspiao(live_rnnoise.subprocess.Popen):
    """Popen que segura o `__init__` até o teste liberar.

    Existe para deixar o teste DENTRO da janela do #374/F1: o processo já
    nasceu e a linha `self._proc = proc` ainda não rodou. No código antigo a
    atribuição era fora do `_lock`, então o `fechar()` desta janela lia
    `_proc` como None, não matava nada, e o ffmpeg ficava vivo para sempre com
    o objeto fechado.
    """

    instancias: list = []
    criado = threading.Event()
    libera = threading.Event()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        _PopenEspiao.instancias.append(self)
        _PopenEspiao.criado.set()
        _PopenEspiao.libera.wait(10)

    def poll(self, *a, **kw):
        # stdin já fechado com o processo ainda não recolhido é EXATAMENTE o que o
        # `_mata` cria entre o `close()` e o `wait()`: mentir `None` aqui deixa o
        # `_abrir` tentar escrever no pipe morto de forma determinística.
        if self.stdin is not None and self.stdin.closed:
            return None
        return super().poll(*a, **kw)


def test_fechar_durante_abrir_nao_vaza_ffmpeg(monkeypatch):
    """`fechar()` no meio do `abrir()`: nenhum processo sobrevive e `ativo` é False."""
    assert live_rnnoise.MODELO_PADRAO.is_file(), "sh.rnnn tem de estar no repo"
    _PopenEspiao.instancias.clear()
    _PopenEspiao.criado.clear()
    _PopenEspiao.libera.clear()
    monkeypatch.setattr(live_rnnoise.subprocess, "Popen", _PopenEspiao)
    live_rnnoise._avisou.clear()

    ns = RnnoiseAoVivo()
    resultado: list = []
    erro: list = []

    def _alvo():
        # exceção em thread não derruba o teste: coleta para reprovar aqui.
        # Fechar no meio da abertura é caminho NORMAL (o cliente manda `Parar`),
        # então o `abrir` não pode terminar em traceback.
        try:
            resultado.append(ns.abrir())
        except BaseException as exc:      # noqa: BLE001
            erro.append(exc)

    t = threading.Thread(target=_alvo, daemon=True)
    try:
        t.start()
        assert _PopenEspiao.criado.wait(10), "o ffmpeg nem chegou a nascer"
        # janela exata do bug: processo vivo, `_proc` ainda None
        assert ns._proc is None, "o teste perdeu a janela (Popen já atribuiu)"
        ns.fechar()
    finally:
        _PopenEspiao.libera.set()
        t.join(15)

    assert erro == [], f"`abrir` estourou depois do `fechar`: {erro!r}"
    assert resultado == [False], "o `abrir` não pode se declarar bem-sucedido depois do `fechar`"
    assert len(_PopenEspiao.instancias) == 1
    proc = _PopenEspiao.instancias[0]
    try:
        proc.wait(timeout=5)
    except Exception:                    # noqa: BLE001 — morre abaixo com mensagem
        pass
    # `returncode` (não `poll`): o espião mente `None` depois que o stdin fecha
    assert proc.returncode is not None, "ffmpeg vazado: sobreviveu ao `fechar()`"
    assert ns.ativo is False
    assert ns._proc is None
    # o `abrir` não pode montar o filtro depois de fechado: com a rechecagem ele
    # mata e sai ANTES de criar as threads leitoras (é o que o gate mediu como
    # "2 threads daemon segurando o objeto")
    assert ns._leitor is None and ns._erros is None, "montou o filtro já fechado"
    # já fechado, o filtro não escreve em pipe morto: devolve o cru
    cru = b"\x01\x00" * 160
    assert ns.processa(cru) == cru


class _EspiaoFiltro:
    """Filtro falso que registra o encaixe: quantos frames e quantos `fechar()`."""

    def __init__(self):
        self.frames = 0
        self.fechou = 0

    def processa(self, pcm):
        self.frames += 1
        return b"\x22\x11" * (len(pcm) // 2)

    def fechar(self):
        self.fechou += 1


def _motor_falso(fed: list):
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

    return SimpleNamespace(TurnEngine=_Eng, Config=lambda **k: k)


def test_handler_abre_o_filtro_e_fecha_no_finally(monkeypatch):
    """O encaixe do handler: o frame passa pelo filtro E o `fechar` roda ao cair.

    Os testes acima injetam `sess["rnnoise"]` na mão e nunca passam por
    `_live_rnnoise_novo` (app.py:7167) nem pelo `finally` (app.py:7185): apagar
    a chamada, ou tirar o `fechar` do `finally`, mantinha a suíte verde (#374/F2).
    """
    import app
    from fastapi.testclient import TestClient

    fed: list[bytes] = []
    criados: list[_EspiaoFiltro] = []

    def _fabrica():
        f = _EspiaoFiltro()
        criados.append(f)
        return f

    monkeypatch.setattr(app, "_live_rnnoise_novo", _fabrica)
    monkeypatch.setattr(app, "_live_mod", None)
    monkeypatch.setattr(app, "_live_turns_mod", _motor_falso(fed))
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
            assert isinstance(sess.get("rnnoise"), _EspiaoFiltro), \
                "o handler não passou por `_live_rnnoise_novo`"
            ws.send_bytes(cru)
            for _ in range(40):
                if fed:
                    break
                time.sleep(0.05)
            assert fed == [limpo], "o frame que o motor viu não passou pelo filtro"
            assert bytes(sess["buffer"]) == limpo
        # sair do `with` fecha o ws: o `finally` do handler tem de soltar o filtro
        for _ in range(40):
            if criados[0].fechou:
                break
            time.sleep(0.05)
    finally:
        with app._live_lock:
            app._live_sessions.clear()

    assert criados[0].frames >= 1
    assert criados[0].fechou == 1, "o `finally` do handler não chamou `fechar()`"


def test_desligado_nao_cria_filtro(monkeypatch):
    """`TTS_LIVE_RNNOISE=0` desliga de verdade: nada é criado para a sessão."""
    import app

    assert app._live_rnnoise_mod is not None, "sem o módulo o teste não prova nada"
    monkeypatch.setenv("TTS_LIVE_RNNOISE", "0")
    assert app._live_rnnoise_novo() is None


def test_fechar_no_aquecimento_nao_estoura_a_escrita(monkeypatch):
    """`fechar()` depois da atribuição, com o processo ainda não recolhido.

    É a segunda janela do #374/F1: a 1ª rechecagem já passou, `_proc` está posto e
    o `_mata` já fechou o stdin — o `poll()` ainda devolve None, então o `abrir`
    tenta escrever o prime num pipe morto. O fechamento devolve `ValueError`
    ("I/O operation on closed file") e não `OSError`; sem tratá-lo, uma sessão que
    cai logo após o `ready` deixava traceback em thread.
    """
    _PopenEspiao.instancias.clear()
    _PopenEspiao.criado.clear()
    _PopenEspiao.libera.set()
    monkeypatch.setattr(live_rnnoise.subprocess, "Popen", _PopenEspiao)
    live_rnnoise._avisou.clear()

    ns = RnnoiseAoVivo()
    resultado: list = []
    erro: list = []

    def _alvo():
        try:
            resultado.append(ns.abrir())
        except BaseException as exc:      # noqa: BLE001
            erro.append(exc)

    t = threading.Thread(target=_alvo, daemon=True)
    try:
        t.start()
        for _ in range(200):              # `_proc` posto = 1ª rechecagem passada
            if ns._proc is not None or not t.is_alive():
                break
            time.sleep(0.005)
        assert ns._proc is not None, "o `abrir` não chegou a atribuir o processo"
        ns.fechar()
    finally:
        t.join(15)

    assert erro == [], f"escrita no pipe morto estourou: {erro!r}"
    assert resultado == [False]
    assert ns.ativo is False
    assert ns._proc is None
