"""O `latency` do 1º áudio diz POR QUE o `tts_gen` custou o que custou (#1135).

O custo do passo de difusão é linear no prefixo de referência e dobra com o CFG
(`evidence/1135-tts-gen-alavancas.md`). A bancada de A (prefixo de 4 s) e B (CFG off)
precisa, em cada fala, do `ref_tokens`, do `cfg` e dos `steps` usados — senão não
separa "gen mais rápido" de "voz com amostra mais curta". Só leitura.
"""

from __future__ import annotations

import types

import numpy as np

import app as app_mod
import live_pipeline as lp


def _tts_app(monkeypatch, tmp_path, conds, omni):
    monkeypatch.setattr(app_mod, "_get_model", lambda: types.SimpleNamespace(sample_rate=24000))
    monkeypatch.setattr(app_mod, "_current_backend", lambda: {"family": "omnivoice", "meta": {}})
    monkeypatch.setattr(app_mod, "_cond_for", lambda *a, **k: conds)
    monkeypatch.setattr(app_mod, "_voice_ref_text", lambda *a: None)
    monkeypatch.setattr(app_mod, "_generate_chunk", lambda *a, **k: np.zeros(8, dtype=np.float32))
    monkeypatch.setattr(app_mod, "_reamostra", lambda a, sr, alvo: a)
    (tmp_path / "voz-x.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(app_mod, "VOICES_DIR", tmp_path)
    espera: dict = {}
    lp._tts_app("Claro,", omni, voice_id="voz-x", espera=espera)
    return espera


def test_tts_app_grava_prefixo_cfg_e_passos(monkeypatch, tmp_path):
    espera = _tts_app(monkeypatch, tmp_path, np.zeros((250, 8), dtype=np.int32),
                      {"num_steps": 10, "guidance_scale": 2.0})
    assert espera["tts_ref_tokens"] == 250
    assert espera["tts_cfg"] == 2.0
    assert espera["tts_steps"] == 10


def test_cfg_desligado_continua_medido_como_zero(monkeypatch, tmp_path):
    """`guidance_scale=0` é um valor (o braço B), não ausência: não pode sumir."""
    espera = _tts_app(monkeypatch, tmp_path, np.zeros((100, 8), dtype=np.int32),
                      {"num_steps": 10, "guidance_scale": 0.0})
    assert espera["tts_cfg"] == 0.0 and espera["tts_ref_tokens"] == 100


def test_sem_dado_nao_inventa_campo(monkeypatch, tmp_path):
    """Sem forma nos conds e sem CFG/passos no perfil: nada de zero inventado."""
    espera = _tts_app(monkeypatch, tmp_path, {"cond": 1}, {})
    assert "tts_ref_tokens" not in espera
    assert "tts_cfg" not in espera and "tts_steps" not in espera


def test_latency_do_1o_audio_leva_os_parametros():
    eventos: list = []
    p = lp.LivePipeline(eventos.append, lambda pcm: None, stt=lambda pcm, l: "fala",
                        llm=lambda msgs: iter(["resposta curta. "]), prewarm=lambda: None)

    def tts(texto, omni):
        p._espera.update({"tts_gen_ms": 900, "tts_ref_tokens": 250, "tts_cfg": 2.0,
                          "tts_steps": 10})
        return np.zeros(8, dtype=np.float32)

    p._tts = tts
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    lat = next(e for e in eventos if e["type"] == "latency")
    assert (lat["tts_ref_tokens"], lat["tts_cfg"], lat["tts_steps"]) == (250, 2.0, 10)
    assert lat["tts_gen_ms"] == 900


def test_latency_sem_parametros_nao_inventa():
    eventos: list = []
    p = lp.LivePipeline(eventos.append, lambda pcm: None, stt=lambda pcm, l: "fala",
                        llm=lambda msgs: iter(["resposta. "]),
                        tts=lambda t, o: np.zeros(8, dtype=np.float32), prewarm=lambda: None)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False)
    lat = next(e for e in eventos if e["type"] == "latency")
    assert not {"tts_ref_tokens", "tts_cfg", "tts_steps"} & set(lat)
