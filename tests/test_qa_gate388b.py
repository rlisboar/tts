"""GATE QA #S (`task_ddf2b67b`): os sete itens de higiene de áudio."""

import io
import json
import resource

import numpy as np
from fastapi import HTTPException

import app
import backends
import common
from client.mic_router import Router

__all__ = ["app", "backends", "common", "Router"]


def _tom(segundos: float, sr: int = 8000, nivel: float = 0.1) -> np.ndarray:
    t = np.arange(int(segundos * sr)) / sr
    return (nivel * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


class _Up:
    filename = "longo.wav"
    content_type = "audio/wav"

    def __init__(self, dados: bytes):
        self.file = io.BytesIO(dados)


def test_vinte_minutos_passam_e_acima_do_teto_da_413(tmp_path, monkeypatch):
    import soundfile as sf

    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path)   # não sujar o outputs/ do dono
    curto = tmp_path / "vinte.wav"
    sf.write(str(curto), _tom(20 * 60), 8000, subtype="PCM_16")
    # o espião tem de gravar o conteúdo do PRÓPRIO upload (dois arquivos no teste)
    monkeypatch.setattr(app, "_write_upload_limited",
                        lambda up, destino: destino.write_bytes(up.file.read()))
    antes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wav = app._save_audio_upload(_Up(curto.read_bytes()))
    assert wav.exists()
    depois = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    cresceu_mb = (depois - antes) / (1024 * 1024)
    # em macOS ru_maxrss vem em bytes; 20 min de 16 kHz float32 = ~77 MB, o teto
    # é 200 MB — o teste dá folga para o resto da suíte já ter subido o pico
    assert cresceu_mb < 200, f"cresceu {cresceu_mb:.0f} MB"

    longo = tmp_path / "longo.wav"
    sf.write(str(longo), _tom(1860), 8000, subtype="PCM_16")     # 31 min
    with __import__("pytest").raises(HTTPException) as exc:
        app._save_audio_upload(_Up(longo.read_bytes()))
    assert exc.value.status_code == 413


def test_costura_das_janelas_nao_duplica_nem_come_palavra(monkeypatch):
    """A sobreposição de 1 s transcreve a palavra da emenda: ela não pode repetir."""
    import sys
    import types

    # cada janela termina/começa com a MESMA palavra ("emenda") no meio do texto
    falas = iter(["primeira parte emenda", "emenda segunda parte", "emenda fim"])
    vistos = []

    def _transcreve(audio, **kw):
        vistos.append(len(audio))
        return {"text": next(falas), "language": "pt", "segments": []}

    monkeypatch.setitem(sys.modules, "mlx_whisper",
                        types.SimpleNamespace(transcribe=_transcreve))
    audio = np.zeros(16000 * 65, dtype=np.float32)
    r = app._transcreve_em_janelas(audio, None)
    assert r["text"].lower().split().count("emenda") == 2, r["text"]
    assert "primeira" in r["text"] and "fim" in r["text"], r["text"]


def test_24_7_vira_palavra_falada():
    assert common.sanitize_text("aberto 24/7") == "aberto vinte e quatro por sete."


def test_cache_de_voz_remota_podado(monkeypatch):
    monkeypatch.setattr(app, "_REMOTE_VOICE_CACHE_MAX", 3)
    app._remote_voice_cache.clear()
    for i in range(10):
        app._remote_voice_cache_guarda(f"v{i}", ("o", f"v{i}", i))
    assert len(app._remote_voice_cache) == 3
    app._remote_voice_cache.clear()


def test_speed_nao_e_no_op_no_fallback_do_qwen():
    vistos = {}

    class _M:
        def generate(self, **kw):
            vistos.update(kw)
            return [np.zeros(64, dtype=np.float32)]

    backends.generate_with_backend(_M(), "qwen3_custom", "oi", omni={"speed": 1.5})
    assert vistos.get("speed") == 1.5


def test_mic_sem_clip_duro_e_sem_degrau():
    r = Router()
    r.gain = 2.0
    e = np.full((128, 1), 0.8, dtype=np.float32)
    s = np.zeros((128, 2), dtype=np.float32)
    r._callback(e, s, 128, None, None)
    pico = float(np.abs(s[:, 0]).max())
    assert 0.95 < pico < 1.0, pico
    r.passthrough = False
    niveis = []
    for _ in range(20):
        e = np.full((128, 1), 0.5, dtype=np.float32)
        s = np.zeros((128, 2), dtype=np.float32)
        r._callback(e, s, 128, None, None)
        niveis.append(float(s[:, 0].max()))
        if niveis[-1] < 0.01:
            break
    assert niveis[0] > 0 and niveis[-1] < 0.01 and len(niveis) >= 3


def test_24_7_e_sn_falados_certo():
    assert "vinte e quatro por sete" in common.sanitize_text("aberto 24/7")
    assert "sim ou não" in common.sanitize_text("S/N")


def test_presets_distintos():
    instructs = [p["instruct"] for p in app.OMNI_PRESETS.values()]
    assert len(set(instructs)) == len(instructs)


def test_semente_ruim_e_trocada_e_registrada(tmp_path, monkeypatch):
    import soundfile as sf

    monkeypatch.setattr(app, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(app, "_trim_tail_silence", lambda a, sr, **k: a)
    n = {"i": 0}

    class _R:
        def __init__(self, a):
            self.audio = a

    class _M:
        def generate(self, **kw):
            n["i"] += 1
            if n["i"] == 1:
                return [_R(np.zeros(8000, dtype=np.float32))]
            return [_R(_tom(4.0, sr=24000, nivel=0.2).astype(np.float32))]

    app._materialize_preset(_M(), 24000, "vd-locutora")
    meta = json.loads((tmp_path / "vd-locutora.json").read_text())
    assert n["i"] == 2 and meta["tentativas"] == 1 and "seed" in meta
    wav, _ = sf.read(str(tmp_path / "vd-locutora.wav"), dtype="float32")
    assert float(np.sqrt(np.mean(wav ** 2))) > 0.05
