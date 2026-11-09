"""#876: o Live transcreve com o SEU Whisper (`live_stt_whisper_repo`, vazio = turbo).

O dono pôs o large-v3 no STT de arquivo; no Live ele custa 1,5–2,4× a latência do
turbo sem ganho medido de WER (evidence/876-stt-live.md). As células provam que o
turno e as passadas de barge pegam o repo do Live e que o arquivo segue no dele."""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

import app
import live_pipeline

LARGE = "mlx-community/whisper-large-v3-mlx"


@pytest.fixture
def repos(monkeypatch):
    """Whisper falso que anota o repo pedido (nada de modelo real)."""
    pedidos: list[str] = []

    def transcribe(audio, path_or_hf_repo=None, **kw):
        pedidos.append(path_or_hf_repo)
        return {"text": "liga a luz da sala por favor", "language": "pt",
                "segments": [{"no_speech_prob": 0.01, "avg_logprob": -0.1,
                              "compression_ratio": 1.1}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=transcribe))
    monkeypatch.setitem(app._settings, "stt_whisper_repo", LARGE)
    monkeypatch.setitem(app._settings, "live_stt_whisper_repo", "")
    monkeypatch.setattr(app, "_vad_tem_fala", lambda p, minimo_s=0.3: True)
    monkeypatch.setitem(app._settings, "stt_local_engine", "whisper")
    monkeypatch.setattr(app, "_live_voz_ligado", lambda: None)
    return pedidos


def _pcm(s: float = 1.0) -> bytes:
    t = np.arange(int(16000 * s)) / 16000
    return (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()


def test_turno_do_live_usa_o_whisper_do_live(repos):
    assert live_pipeline._stt_app(_pcm(), "pt", {}) != ""
    assert repos == [app.LIVE_WHISPER_PADRAO], repos


def test_setting_do_live_e_respeitado(repos, monkeypatch):
    monkeypatch.setitem(app._settings, "live_stt_whisper_repo", "mlx-community/whisper-small-mlx")
    live_pipeline._stt_app(_pcm(), "pt", {})
    assert repos == ["mlx-community/whisper-small-mlx"]


def test_stt_de_arquivo_segue_no_repo_do_dono(repos, tmp_path):
    import wave

    f = tmp_path / "a.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(_pcm())
    app._transcribe(f, language="pt")
    assert repos == [LARGE]


def test_marca_do_live_nao_vaza_da_thread(repos):
    with app._stt_do_live():
        assert app._whisper_repo() == app.LIVE_WHISPER_PADRAO
    assert app._whisper_repo() == LARGE


@pytest.mark.parametrize("prazo", [0.0, 5.0])
def test_passada_de_comando_do_barge_usa_o_whisper_do_live(repos, prazo):
    """O passo `cru` vai direto no Whisper (sem `_stt_app`); com prazo roda num
    pool — a marca tem de valer na thread do pool também."""
    sess = {"id": "s", "pipe": types.SimpleNamespace(language="pt", _stt=None)}
    app._live_transcreve_trecho(sess, _pcm(), cru=True, prazo_s=prazo)
    assert repos == [app.LIVE_WHISPER_PADRAO], repos


def test_setting_valida_e_e_de_admin():
    assert "live_stt_whisper_repo" in app._SETTINGS_ADMIN
    assert app._SETTINGS_DEFAULTS["live_stt_whisper_repo"] == ""


def test_prewarm_do_live_aquece_o_whisper_do_live(repos):
    """O prewarm compilava o repo de ARQUIVO. No mini isso é o large-v3, e o
    1º turno do Live pagava a carga do turbo de novo — o prewarm não servia."""
    live_pipeline._stt_aquece(app)
    assert repos == [app.LIVE_WHISPER_PADRAO], repos



# ---------------------------------------------------------------------------
# #876 C1: MOTOR do Live (`live_stt_engine`) e release do MLX fora do caminho
# ---------------------------------------------------------------------------
@pytest.fixture
def parakeet(monkeypatch, repos):
    """Parakeet falso que anota as chamadas (nada de modelo real)."""
    chamadas: list[str] = []

    def cru(caminho):
        chamadas.append(str(caminho))
        return types.SimpleNamespace(text="liga a luz da sala por favor", sentences=[])

    monkeypatch.setattr(app, "_parakeet_cru", cru)
    monkeypatch.setitem(app._settings, "live_stt_engine", "")
    monkeypatch.setitem(app._settings, "live_stt_release_adiado", False)
    return chamadas


@pytest.mark.parametrize("local", ["whisper", "parakeet"])
def test_motor_do_live_vazio_segue_o_stt_local_engine(monkeypatch, local):
    """Default (vazio) = nada muda: o Live segue o `stt_local_engine` de sempre."""
    monkeypatch.setitem(app._settings, "stt_local_engine", local)
    monkeypatch.setitem(app._settings, "live_stt_engine", "")
    assert app._stt_motor(live=True) == local
    assert app._SETTINGS_DEFAULTS["live_stt_engine"] == ""
    assert app._SETTINGS_DEFAULTS["live_stt_release_adiado"] is False


def test_turno_do_live_com_parakeet_e_o_arquivo_segue_no_whisper(parakeet, repos, monkeypatch,
                                                                 tmp_path):
    import wave

    monkeypatch.setitem(app._settings, "live_stt_engine", "parakeet")
    assert live_pipeline._stt_app(_pcm(), "pt", {}) == "liga a luz da sala por favor"
    assert len(parakeet) == 1 and repos == [], (parakeet, repos)
    f = tmp_path / "a.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(_pcm())
    app._transcribe(f, language="pt")
    assert len(parakeet) == 1, "o STT de arquivo foi para o Parakeet do Live"
    assert repos == [LARGE], repos


@pytest.mark.parametrize("prazo", [0.0, 5.0])
def test_passada_de_comando_usa_o_motor_do_live(parakeet, repos, monkeypatch, prazo):
    """Um modelo só residente: a passada `cru` do barge vai no MESMO motor do turno."""
    monkeypatch.setitem(app._settings, "live_stt_engine", "parakeet")
    sess = {"id": "s", "pipe": types.SimpleNamespace(language="pt", _stt=None)}
    assert app._live_transcreve_trecho(sess, _pcm(), cru=True, prazo_s=prazo) != ""
    assert len(parakeet) == 1 and repos == [], (parakeet, repos)


def test_prewarm_aquece_o_parakeet_quando_e_o_motor_do_live(parakeet, repos, monkeypatch):
    monkeypatch.setitem(app._settings, "live_stt_engine", "parakeet")
    live_pipeline._stt_aquece(app)
    assert len(parakeet) == 1 and repos == [], (parakeet, repos)


def _conta_release(monkeypatch) -> list:
    soltos: list = []
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: soltos.append(1))
    app._stt_release_pendente.clear()
    return soltos


def _espera(cond, s: float = 2.0) -> bool:
    import time
    fim = time.monotonic() + s
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_release_adiado_sai_do_stt_e_volta_depois(parakeet, monkeypatch):
    """#876 C1: com a chave, o STT do Live NÃO solta o MLX dentro do estágio; o
    `_stt_solta_adiado` solta em segundo plano. Sem a chave, solta dentro (antes)."""
    soltos = _conta_release(monkeypatch)
    live_pipeline._stt_app(_pcm(), "pt", {})
    assert soltos == [1], "sem a chave o release segue dentro do STT"
    soltos.clear()
    monkeypatch.setitem(app._settings, "live_stt_release_adiado", True)
    live_pipeline._stt_app(_pcm(), "pt", {})
    assert soltos == [] and app._stt_release_pendente.is_set()
    assert app._stt_solta_adiado() is True
    assert _espera(lambda: soltos == [1]), "o release adiado não aconteceu"
    assert not app._stt_release_pendente.is_set()
    assert app._stt_solta_adiado() is False, "nada pendente, nada a fazer"


def test_release_adiado_fora_do_live_nao_adia(parakeet, monkeypatch, tmp_path):
    import wave

    soltos = _conta_release(monkeypatch)
    monkeypatch.setitem(app._settings, "live_stt_release_adiado", True)
    f = tmp_path / "a.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(_pcm())
    app._transcribe(f, language="pt")
    assert soltos == [1] and not app._stt_release_pendente.is_set()


def test_release_adiado_nao_cruza_com_a_geracao_do_tts(parakeet, monkeypatch):
    """TTS gerando (`_gen_lock` ocupado): o release fica pendente (#803)."""
    import threading

    soltos = _conta_release(monkeypatch)
    app._stt_release_pendente.set()
    ocupado, solta = threading.Event(), threading.Event()

    def gera():
        with app._gen_lock:
            ocupado.set()
            solta.wait(2)
    th = threading.Thread(target=gera)
    th.start()
    ocupado.wait(2)
    try:
        app._stt_solta_adiado()
        import time
        time.sleep(0.2)
        assert soltos == [] and app._stt_release_pendente.is_set()
        # e não PRENDE o STT seguinte esperando o TTS (o `_stt_lock` fica livre)
        assert app._stt_lock.acquire(timeout=0.5), "o release adiado segurou o _stt_lock"
        app._stt_lock.release()
    finally:
        solta.set()
        th.join(2)
    app._stt_solta_adiado()
    assert _espera(lambda: soltos == [1])


def test_turno_do_pipeline_solta_o_release_depois_do_stt(monkeypatch):
    """O elo: o turno com o STT de produção chama o `_stt_solta_adiado`."""
    chamou = []
    monkeypatch.setattr(app, "_stt_solta_adiado", lambda: chamou.append(1) or False)
    monkeypatch.setattr(live_pipeline, "_stt_julga", lambda pcm, lang, julga=None: "bom dia")
    eventos = []
    p = live_pipeline.LivePipeline(eventos.append, lambda b: None, stt=live_pipeline._stt_app,
                                   llm=lambda m: iter(["ok. "]),
                                   tts=lambda c, o: np.zeros(240, dtype=np.float32),
                                   prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 1600)
    p.end_of_speech(inicia_thread=False)
    assert chamou == [1], [e.get("type") for e in eventos]
