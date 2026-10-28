"""Gate do #443: denoise/replace com chave `use` não podem ser sem volta.

As duas rotas gravam a referência NO LUGAR. Com uma chave `use` (LAN, zip do
mic-router) a voz clonada do dono se descaracterizava sem cópia. Agora a primeira
sobrescrita guarda `<id>.orig.wav`, e o admin restaura byte a byte.
"""

import hashlib
import io
import pathlib
import zipfile

import numpy as np
import soundfile as sf

import app

pytest_plugins = ["test_api"]

from test_api import auth, chave_de_uso, client  # noqa: E402, F401
from test_backup_restore import estado  # noqa: E402, F401

__all__ = ["auth", "chave_de_uso", "client", "estado"]


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
    _voz(tmp_path, monkeypatch)
    client.post("/api/voices/v1/denoise", headers=chave_de_uso, json={"strength": 0.3})
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/voices/export", headers=auth).content))
    assert "voices/v1.orig.wav" in z.namelist()


def test_listagem_diz_quando_ha_o_que_desfazer(tmp_path, monkeypatch, client,
                                               chave_de_uso):
    """A UI só pode oferecer "desfazer" com o `.orig` presente (#443)."""
    _voz(tmp_path, monkeypatch)

    def _v1():
        r = client.get("/api/voices", headers=chave_de_uso)
        assert r.status_code == 200, r.text
        return next(v for v in r.json() if v["id"] == "v1")

    assert _v1()["tem_original"] is False
    client.post("/api/voices/v1/denoise", headers=chave_de_uso, json={"strength": 0.3})
    assert _v1()["tem_original"] is True


# --- o satélite em pacote: export -> import e backup -> restore -----------------
# O `.orig` só serve se voltar junto com a voz. As duas rotas que comem zip
# validavam o nome com `[A-Za-z0-9_-]+\.(wav|json)` e o `.` do satélite batia
# fora: o backup que o PRÓPRIO app gera voltava 400 "arquivo inesperado".


def test_import_de_zip_com_orig_entra_sem_virar_orfao(client, auth, estado):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("voices/v2.wav", b"RIFF" + b"\x00" * 64)
        z.writestr("voices/v2.json", '{"id": "v2", "name": "Bruno"}')
        z.writestr("voices/v2.orig.wav", b"RIFF" + b"\x02" * 64)
    r = client.post("/api/voices/import", headers=auth,
                    files={"zip_file": ("v.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200, r.text
    assert (estado["vozes"] / "v2.orig.wav").exists(), "o satélite foi descartado"
    corpo = r.json()
    assert corpo["orfaos"] == [], "satélite não tem json próprio: não é órfão"
    assert corpo["ignorados"] == []
    assert corpo["vozes"] == 1


def test_backup_com_orig_volta_no_restore(client, auth, estado):
    orig = estado["vozes"] / "v1.orig.wav"
    orig.write_bytes(b"RIFF" + b"\x01" * 64)
    sha = hashlib.sha256(orig.read_bytes()).hexdigest()
    r = client.get("/api/backup", headers=auth)
    assert r.status_code == 200
    pacote = r.content
    assert "voices/v1.orig.wav" in zipfile.ZipFile(io.BytesIO(pacote)).namelist()

    orig.unlink()                      # o pacote tem de trazer o satélite de volta
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    assert orig.exists(), "o restore descartou o `.orig`"
    assert hashlib.sha256(orig.read_bytes()).hexdigest() == sha
    assert r.json()["vozes"] == 1, "o `.orig` foi contado como voz nova"

