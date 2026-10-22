"""Backup/restore do ESTADO (#377) — round-trip, recusa por versão/sha e rollback."""

import copy
import hashlib
import io
import json
import zipfile
import pytest
from fastapi.testclient import TestClient

import app

pytest_plugins = ["test_api"]


@pytest.fixture
def estado(tmp_path, monkeypatch):
    """Isola os quatro blocos de valor num diretório temporário."""
    vozes = tmp_path / "voices"
    vozes.mkdir()
    saida = tmp_path / "outputs"
    saida.mkdir()
    settings = tmp_path / "settings.json"
    chaves = tmp_path / "apikeys.json"
    perfis = tmp_path / "speaker-profiles.json"
    monkeypatch.setattr(app, "VOICES_DIR", vozes)
    monkeypatch.setattr(app, "OUTPUTS_DIR", saida)
    monkeypatch.setattr(app, "SETTINGS_PATH", settings)
    monkeypatch.setattr(app, "APIKEYS_PATH", chaves)
    monkeypatch.setattr(app, "SPEAKER_PATH", perfis)
    settings.write_text(json.dumps({"language": "pt", "speed": 1.0}))
    chaves.write_text(json.dumps({"enabled": True, "keys": [
        {"id": "k1", "secret": "s3cr3t-de-teste", "role": "admin", "name": "t"}]}))
    perfis.write_text(json.dumps({"ana": {"vecs": [[0.1, 0.2]]}}))
    (vozes / "v1.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    (vozes / "v1.json").write_text(json.dumps({"id": "v1", "name": "Ana"}))
    # o restore recarrega `_apikeys` no processo: sem devolver o estado global no
    # fim, os testes seguintes herdavam as chaves do pacote de teste
    antes_apikeys = copy.deepcopy(app._apikeys)
    yield {"base": tmp_path, "vozes": vozes, "settings": settings}
    app._apikeys.clear()
    app._apikeys.update(antes_apikeys)


def _hash(estado) -> dict:
    alvos = [estado["settings"], estado["base"] / "apikeys.json",
             estado["base"] / "speaker-profiles.json",
             estado["vozes"] / "v1.wav", estado["vozes"] / "v1.json"]
    return {str(p.name): hashlib.sha256(p.read_bytes()).hexdigest() for p in alvos}


def _reembala(zip_bytes: bytes, *, schema=None, corromper=None) -> bytes:
    """Reescreve o pacote mudando o schema ou um byte de um arquivo (sha quebra)."""
    origem = zipfile.ZipFile(io.BytesIO(zip_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as novo:
        for info in origem.infolist():
            dados = origem.read(info.filename)
            if info.filename == "manifest.json" and schema is not None:
                man = json.loads(dados)
                man["schema"] = schema
                dados = json.dumps(man).encode()
            if info.filename == corromper:
                dados = bytes([dados[0] ^ 0xFF]) + dados[1:]
            novo.writestr(info.filename, dados)
    return buf.getvalue()


def test_round_trip_devolve_os_blocos_identicos(client, auth, estado):
    antes = _hash(estado)
    r = client.get("/api/backup", headers=auth)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    pacote = r.content

    # estraga o estado de propósito
    estado["settings"].write_text(json.dumps({"language": "en", "speed": 2.0}))
    (estado["base"] / "speaker-profiles.json").write_text("{}")
    (estado["vozes"] / "v1.wav").unlink()
    (estado["vozes"] / "outra.wav").write_bytes(b"lixo")

    aflito = TestClient(app.app, client=("127.0.0.1", 50000))
    r = aflito.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    assert _hash(estado) == antes, "round-trip não devolveu o mesmo conteúdo"
    assert not (estado["vozes"] / "outra.wav").exists(), "voz fora do pacote devia sair"
    # permissões fechadas nos arquivos restaurados
    for p in (estado["settings"], estado["base"] / "apikeys.json",
              estado["base"] / "speaker-profiles.json", estado["vozes"] / "v1.wav"):
        assert (p.stat().st_mode & 0o777) == 0o600, p
    # o app segue de pé, com as settings restauradas e ACEITANDO a chave do pacote
    assert client.get("/api/status",
                      headers={"X-API-Key": app._primary_api_key()}).status_code == 200
    assert app._settings["language"] == "pt"


def test_schema_desconhecido_recusa_sem_tocar(client, auth, estado):
    pacote = client.get("/api/backup", headers=auth).content
    estado["settings"].write_text(json.dumps({"language": "en"}))
    antes = _hash(estado)
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", _reembala(pacote, schema=99),
                                       "application/zip")})
    assert r.status_code == 400 and "desconhecida" in r.text
    assert _hash(estado) == antes


def test_sha_divergente_aborta_com_rollback(client, auth, estado):
    pacote = client.get("/api/backup", headers=auth).content
    estado["settings"].write_text(json.dumps({"language": "en"}))
    antes = _hash(estado)
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", _reembala(pacote, corromper="settings.json"),
                                       "application/zip")})
    assert r.status_code == 400 and "sha256" in r.text
    assert _hash(estado) == antes, "pacote corrompido mexeu no estado"


def test_backup_nao_leva_audio_de_saida(client, auth, estado):
    (app.OUTPUTS_DIR / "gerado.wav").write_bytes(b"RIFF")
    (app.OUTPUTS_DIR / "gerado.json").write_text("{}")
    pacote = client.get("/api/backup", headers=auth).content
    nomes = zipfile.ZipFile(io.BytesIO(pacote)).namelist()
    assert "manifest.json" in nomes and "voices/v1.wav" in nomes
    assert not any(n.startswith("outputs/") for n in nomes)
