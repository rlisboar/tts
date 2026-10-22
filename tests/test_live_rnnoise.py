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


def _espera_ate(cond, teto: float = 10.0) -> bool:
    """Espera `cond` virar verdadeira, com teto generoso.

    Antes eram 40 × 0,05 s = 2 s em cada espera: com a suíte inteira rodando (e
    a máquina carregada) o encaixe do handler — que envolve abrir o WebSocket —
    estourava a janela e o teste ficava intermitente (#449)."""
    limite = time.monotonic() + teto
    while time.monotonic() < limite:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


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
            assert _espera_ate(lambda: fed), "o motor nunca viu o frame"
            assert fed == [limpo], "o frame que o motor viu não passou pelo filtro"
            assert bytes(sess["buffer"]) == limpo
        # sair do `with` fecha o ws: o `finally` do handler tem de soltar o filtro
        _espera_ate(lambda: criados[0].fechou)
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


# ---------------------------------------------------------------------------
# #375: follow-ups do gate — filtro morto tem de RECOLHER, e o desmonte não pode
# travar o event loop
# ---------------------------------------------------------------------------
def test_caiu_recolhe_o_processo_e_marca_morto():
    """`_caiu` matava nada: `_proc` ficava preenchido e o app seguia pagando
    thread + lock + `poll()` a cada frame só para devolver o cru (#375/F3)."""
    ns = RnnoiseAoVivo()
    assert ns.abrir() is True
    try:
        proc = ns._proc                       # noqa: SLF001 — o processo é o alvo
        proc.kill()
        assert proc.wait(2) is not None
        cru = _pcm(np.zeros(FRAME // 2, dtype=np.float32))
        assert ns.processa(cru) == cru        # devolve cru, como antes
        assert ns.morto is True               # mas agora AVISA que caiu
        assert ns._proc is None               # e não deixa processo pendurado
        assert ns.ativo is False
    finally:
        ns.fechar()
    assert ns.processa(cru) == cru            # fechar depois de morto não estoura


def test_filtro_morto_sai_da_sessao_no_proximo_frame():
    """O app para de chamar o filtro caído em vez de pagar o hop para sempre."""
    import app

    class NsMorto:
        morto = True
        fechados = 0

        def processa(self, pcm):
            raise AssertionError("não pode ser chamado: já caiu")

        def fechar(self):
            NsMorto.fechados += 1

    sess = {"id": "s-rn", "rnnoise": NsMorto()}
    pcm = b"\x00\x01" * 100
    assert app._live_pcm_limpo(sess, pcm) == pcm
    assert "rnnoise" not in sess              # saiu da sessão
    assert NsMorto.fechados == 1
    assert app._live_pcm_limpo(sess, pcm) == pcm   # e não quebra na sequência


def test_fechar_nao_bloqueia_o_event_loop():
    """`fechar` espera o processo; no loop isso atrasava as OUTRAS sessões (#375/F4)."""
    import asyncio

    ns = RnnoiseAoVivo()
    assert ns.abrir() is True
    try:
        assert ns.processa(_pcm(np.zeros(FRAME // 2, dtype=np.float32)))  # exercita

        async def fluxo() -> int:
            tiques = 0

            async def tique() -> None:
                nonlocal tiques
                while True:
                    tiques += 1
                    await asyncio.sleep(0.01)

            tarefa = asyncio.create_task(tique())
            await asyncio.sleep(0.02)
            antes = tiques
            await asyncio.to_thread(ns.fechar)     # o padrão do app
            durante = tiques - antes
            tarefa.cancel()
            try:
                await tarefa
            except asyncio.CancelledError:
                pass
            return durante

        durante = asyncio.run(fluxo())
        # bloquear o loop daria 0 (o sleep só roda depois); off-loop ele continua
        assert durante >= 1, f"o fechamento travou o loop ({durante} tiques)"
        assert ns.ativo is False
        ns.fechar()                                # idempotente
    finally:
        ns.fechar()


def test_queda_derruba_a_sessao_no_MESMO_frame():
    """A morte detectada pelo `processa` sai da sessão JÁ naquele frame.

    Antes o filtro era tirado no frame SEGUINTE e o frame da própria queda ainda
    pagava o hop de thread; a mudança (revisor do gate #441) não toca `morto`
    ×`ativo` nem o comportamento do filtro saudável."""
    import app

    ns = RnnoiseAoVivo()
    assert ns.abrir() is True
    try:
        cru = _pcm(np.zeros(FRAME // 2, dtype=np.float32))
        assert ns.processa(cru) == cru            # saudável: devolve filtrado/cru do pipe
        ns._proc.kill()                           # noqa: SLF001
        ns._proc.wait(2)                          # noqa: SLF001
        sess = {"id": "s-rn", "rnnoise": ns}
        assert app._live_pcm_limpo(sess, cru) == cru
        assert "rnnoise" not in sess              # saiu NESTE frame, não no próximo
        assert ns.morto is True
    finally:
        ns.fechar()


def test_handshake_falho_nao_derruba_a_sessao(caplog, tmp_path):
    """Filtro que nem abriu (sem modelo/ffmpeg) NÃO é queda: fica na sessão,
    devolve o cru e avisa uma vez. Sem isso o `morto` novo derrubaria por engano."""
    import app
    import logging

    live_rnnoise._avisou.clear()
    ns = RnnoiseAoVivo(modelo=tmp_path / "ausente.rnnn")
    with caplog.at_level(logging.WARNING, logger="live"):
        assert ns.abrir() is False
        sess = {"id": "s-rn", "rnnoise": ns}
        for _ in range(3):                        # três frames seguidos
            cru = _pcm(np.zeros(FRAME // 2, dtype=np.float32))
            assert app._live_pcm_limpo(sess, cru) == cru
            assert sess.get("rnnoise") is ns      # a sessão NÃO cai
        assert ns.morto is False
    assert sum("sem modelo" in r.message for r in caplog.records) == 1
    ns.fechar()


def test_nao_subiu_avisa(caplog, monkeypatch):
    """`Popen` falhando (ffmpeg existe, mas não executa) tem de AVISAR."""
    import subprocess

    live_rnnoise._avisou.clear()

    def _explode(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "Popen", _explode)
    ns = RnnoiseAoVivo()
    with caplog.at_level(logging.WARNING, logger="live"):
        assert ns.abrir() is False
    assert any("não subiu" in r.message and "Live segue sem o filtro" in r.message
               for r in caplog.records), [r.message for r in caplog.records]
    ns.fechar()


def test_nao_abriu_avisa(caplog, tmp_path):
    """Filho que sobe e morre na hora: `não abriu`, com o stderr no detalhe."""
    live_rnnoise._avisou.clear()
    falso = tmp_path / "ffmpeg-falso"
    falso.write_text("#!/bin/sh\necho 'arnndn: modelo invalido' >&2\nexit 1\n")
    falso.chmod(0o755)
    ns = RnnoiseAoVivo(ffmpeg=str(falso))
    with caplog.at_level(logging.WARNING, logger="live"):
        assert ns.abrir() is False
    linhas = [r.message for r in caplog.records]
    assert any("não abriu" in m and "Live segue sem o filtro" in m for m in linhas), linhas
    assert any("modelo invalido" in m for m in linhas), linhas
    ns.fechar()


def test_caiu_avisa_e_recolhe(caplog):
    """`_caiu` avisa com o stderr e recolhe o processo (estado e aviso juntos)."""
    live_rnnoise._avisou.clear()
    ns = RnnoiseAoVivo()
    assert ns.abrir() is True
    proc = ns._proc
    proc.kill()
    time.sleep(0.1)
    with caplog.at_level(logging.WARNING, logger="live"):
        ns.processa(b"\x02\x00" * 160)
    assert ns.morto is True and ns._proc is None
    assert any("rnnoise caiu" in r.message and "Live segue sem o filtro" in r.message
               for r in caplog.records), [r.message for r in caplog.records]
    ns.fechar()
