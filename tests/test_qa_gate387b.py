"""GATE QA #Q (`task_76728331`): integridade da referência e perfis atômicos.

O `istft` crescia o vetor a cada limpeza, o gating tirava nível, a amostra de
referência era gravada sem nível nem fade (o clone herdava o volume) e um JSON de
perfis corrompido deixava o gate de biometria INERTE sem avisar.
"""

import json
import threading

import numpy as np

import app

__all__ = ["app"]

LIMITE_DB = 3.0


def _suja(segundos: float = 2.0) -> np.ndarray:
    t = np.arange(int(segundos * 24000)) / 24000.0
    rng = np.random.default_rng(11)
    fala = 0.15 * np.sin(2 * np.pi * 200 * t) * np.hanning(t.size)
    return (fala + 0.05 * rng.standard_normal(t.size)).astype(np.float32)


def _db(a: float, b: float) -> float:
    return abs(20 * np.log10(a / b))


def test_denoise_preserva_comprimento_e_nivel_em_todas_as_forcas():
    x = _suja()
    r_in = float(np.sqrt(np.mean(x ** 2)))
    for forca in (0.3, 1.0):
        y = app._denoise_audio(x, 24000, forca)
        assert y.size == x.size, forca
        assert _db(r_in, float(np.sqrt(np.mean(y ** 2)))) <= LIMITE_DB, forca


def test_denoise_repetido_nao_incha_o_vetor():
    x = _suja(0.5)
    y = x
    for _ in range(4):
        y = app._denoise_audio(y, 24000, 0.7)
    assert y.size == x.size


def test_referencia_sai_com_fade_e_nivel():
    y = app._referencia_pronta(_suja(1.0) * 0.05, 24000)
    assert float(np.sqrt(np.mean(y ** 2))) > 0.1
    assert abs(float(y[0])) < 1e-6 and abs(float(y[-1])) < 1e-6


def test_queda_de_nivel_na_limpeza_recusa_e_nao_muda_a_referencia(monkeypatch):
    x = _suja(1.0)
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32) * 0.01)
    y, recusado = app._denoise_para_referencia(x, 24000, 1.0)
    assert recusado is True and np.allclose(y, x)


def test_perfis_corrompidos_nao_viram_gate_inerte(tmp_path, monkeypatch):
    alvo = tmp_path / ".speaker-profiles.json"
    monkeypatch.setattr(app, "SPEAKER_PATH", alvo)
    monkeypatch.setattr(app, "_speaker_embed", lambda p: [1.0, 0.0])
    app._settings["speaker_gate"] = "enforce"
    try:
        assert app._speaker_gate_ok(__import__("pathlib").Path("x.wav")) == {}   # ausente
        alvo.write_text('{"ana": {"vecs": [[0.1,')
        out = app._speaker_gate_ok(__import__("pathlib").Path("x.wav"))
        assert out.get("rejected") is True and "ilegíveis" in out.get("reason", "")
        app._settings["speaker_gate"] = "label"
        out = app._speaker_gate_ok(__import__("pathlib").Path("x.wav"))
        assert "speaker_aviso" in out and "rejected" not in out
    finally:
        app._settings["speaker_gate"] = "off"


def test_enrolls_concorrentes_preservam_todos_os_vetores(tmp_path, monkeypatch):
    alvo = tmp_path / ".speaker-profiles.json"
    monkeypatch.setattr(app, "SPEAKER_PATH", alvo)

    def enroll(vec):
        with app._speaker_lock:
            perfis = app._speaker_load()
            p = perfis.get("ana", {"vecs": [], "updated": 0})
            p["vecs"] = (p.get("vecs") or []) + [vec]
            p["updated"] = 1
            perfis["ana"] = p
            app._speaker_save(perfis)

    ts = [threading.Thread(target=enroll, args=([float(i), 0.0],)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert len(json.loads(alvo.read_text())["ana"]["vecs"]) == 8
    assert [p.name for p in tmp_path.iterdir()] == [alvo.name], "sobrou temporário"
    assert (alvo.stat().st_mode & 0o777) == 0o600


def test_perfil_gravado_e_lido_de_volta(tmp_path, monkeypatch):
    alvo = tmp_path / ".speaker-profiles.json"
    monkeypatch.setattr(app, "SPEAKER_PATH", alvo)
    app._speaker_save({"ana": {"vecs": [[0.1, 0.2]], "updated": 0}})
    dados, erro = app._speaker_load_estado()
    assert erro == "" and dados["ana"]["vecs"] == [[0.1, 0.2]]


def test_queda_no_meio_da_escrita_preserva_o_arquivo_anterior(tmp_path, monkeypatch):
    """Crash na troca não pode deixar o perfil truncado no lugar do bom."""
    import pytest

    import common

    alvo = tmp_path / ".speaker-profiles.json"
    monkeypatch.setattr(app, "SPEAKER_PATH", alvo)
    app._speaker_save({"ana": {"vecs": [[0.1, 0.2]], "updated": 0}})
    bom = alvo.read_text()

    monkeypatch.setattr(common.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("queda no replace")))
    with pytest.raises(OSError):
        app._speaker_save({"ana": {"vecs": [[9.0, 9.0]], "updated": 1}})

    assert alvo.read_text() == bom, "o arquivo bom foi destruído pela escrita parcial"
    assert [p.name for p in tmp_path.iterdir()] == [alvo.name], "sobrou temporário"
