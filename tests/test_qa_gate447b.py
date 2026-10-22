"""Gate do #443: denoise/replace com chave `use` não podem ser sem volta.

As duas rotas gravam a referência NO LUGAR. Com uma chave `use` (LAN, zip do
mic-router) a voz clonada do dono se descaracterizava sem cópia. Agora a primeira
sobrescrita guarda `<id>.orig.wav`, e o admin restaura byte a byte.
"""

import hashlib
import io
import pathlib

import numpy as np
import soundfile as sf

import app

pytest_plugins = ["test_api"]

from test_api import auth, chave_de_uso, client  # noqa: E402, F401

__all__ = ["auth", "chave_de_uso", "client"]


def _voz(tmp_path, monkeypatch, nome="v1") -> pathlib.Path:
    vd = tmp_path / "voices"
    vd.mkdir(exist_ok=True)
    monkeypatch.setattr(app, "VOICES_DIR", vd)
    p = vd / f"{nome}.wav"
    t = np.arange(24000) / 24000.0
    sf.write(str(p), (0.2 * np.sin(2 * np.pi * 180 * t)).astype(np.float32), 24000,
             subtype="PCM_16")
    (vd / f"{nome}.json").write_text('{"id": "%s", "name": "Teste"}' % nome)
    return p


def _hash(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_denoise_com_chave_use_cria_orig_uma_vez(tmp_path, monkeypatch, client, chave_de_uso):
    wav = _voz(tmp_path, monkeypatch)
    original = _hash(wav)
    r = client.post("/api/voices/v1/denoise", headers=chave_de_uso, json={"strength": 0.4})
    assert r.status_code == 200, r.text
    orig = tmp_path / "voices" / "v1.orig.wav"
    assert orig.exists() and _hash(orig) == original

    assert _hash(orig) == original, "o `.orig` não pode mudar na 2ª sobrescrita"
    assert client.post("/api/voices/v1/denoise", headers=chave_de_uso,
                       json={"strength": 0.9}).status_code == 200
    assert _hash(orig) == original


def test_replace_tambem_guarda_o_original(tmp_path, monkeypatch, client, chave_de_uso):
    wav = _voz(tmp_path, monkeypatch)
    original = _hash(wav)
    buf = io.BytesIO()
    sf.write(buf, (np.zeros(24000, dtype=np.float32) + 0.1), 24000, format="WAV")
    r = client.post("/api/voices/v1/replace", headers=chave_de_uso,
                    files={"audio": ("a.wav", buf.getvalue(), "audio/wav")})
    assert r.status_code == 200, r.text
    assert _hash(tmp_path / "voices" / "v1.orig.wav") == original


def test_restore_devolve_byte_identico_e_exige_admin(tmp_path, monkeypatch, client,
                                                     auth, chave_de_uso):
    wav = _voz(tmp_path, monkeypatch)
    original = _hash(wav)
    client.post("/api/voices/v1/denoise", headers=chave_de_uso, json={"strength": 0.8})
    assert _hash(wav) != original

    assert client.post("/api/voices/v1/restore-original",
                       headers=chave_de_uso).status_code == 403
    r = client.post("/api/voices/v1/restore-original", headers=auth)
    assert r.status_code == 200, r.text
    assert _hash(wav) == original, "restaurou diferente do original"
    assert (tmp_path / "voices" / "v1.orig.wav").exists(), "o `.orig` some ao restaurar"


def test_restore_sem_original_da_404(tmp_path, monkeypatch, client, auth):
    _voz(tmp_path, monkeypatch)
    assert client.post("/api/voices/v1/restore-original", headers=auth).status_code == 404


def test_delecao_leva_o_satelite(tmp_path, monkeypatch, client, auth, chave_de_uso):
    _voz(tmp_path, monkeypatch)
    client.post("/api/voices/v1/denoise", headers=chave_de_uso, json={"strength": 0.3})
    assert (tmp_path / "voices" / "v1.orig.wav").exists()
    assert client.delete("/api/voices/v1", headers=auth).status_code == 200
    assert not (tmp_path / "voices" / "v1.orig.wav").exists()


def test_export_leva_o_orig(tmp_path, monkeypatch, client, auth, chave_de_uso):
    import zipfile

    _voz(tmp_path, monkeypatch)
    client.post("/api/voices/v1/denoise", headers=chave_de_uso, json={"strength": 0.3})
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/voices/export", headers=auth).content))
    assert "voices/v1.orig.wav" in z.namelist()
