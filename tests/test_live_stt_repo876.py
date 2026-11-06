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
    monkeypatch.setattr(app, "_use_remote_stt", lambda: False)
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

