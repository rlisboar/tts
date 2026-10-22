"""Backup/restore do ESTADO (#377) — round-trip, recusa por versão/sha e rollback."""

import copy
import hashlib
import io
import json
import threading
import time
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


def _reembala_nomes(zip_bytes: bytes, trocas: dict) -> bytes:
    """Reescreve o pacote renomeando entradas (e o manifest junto)."""
    origem = zipfile.ZipFile(io.BytesIO(zip_bytes))
    man = json.loads(origem.read("manifest.json"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as novo:
        for info in origem.infolist():
            if info.filename == "manifest.json":
                continue
            dados = origem.read(info.filename)
            novo.writestr(trocas.get(info.filename, info.filename), dados)
        man["arquivos"] = {trocas.get(n, n): s for n, s in man.get("arquivos", {}).items()}
        novo.writestr("manifest.json", json.dumps(man))
    return buf.getvalue()


def test_nome_fora_da_lista_e_recusado(client, auth, estado):
    pacote = client.get("/api/backup", headers=auth).content
    antes = _hash(estado)
    ruim = _reembala_nomes(pacote, {"voices/v1.wav": "voices/sub/v1.wav"})
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", ruim, "application/zip")})
    assert r.status_code == 400 and "inesperado" in r.text
    assert _hash(estado) == antes


def test_arquivo_listado_e_ausente_do_pacote_recusa(client, auth, estado):
    pacote = client.get("/api/backup", headers=auth).content
    antes = _hash(estado)
    origem = zipfile.ZipFile(io.BytesIO(pacote))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as novo:
        for info in origem.infolist():
            if info.filename == "voices/v1.json":
                continue                      # prometido no manifest, ausente no zip
            novo.writestr(info.filename, origem.read(info.filename))
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert _hash(estado) == antes


def test_pacote_sem_chaves_nao_apaga_as_que_estao_no_disco(client, auth, estado):
    """Restaurar só configuração não pode zerar as chaves de quem restaurou."""
    chaves = estado["base"] / "apikeys.json"
    guardado = chaves.read_bytes()
    chaves.unlink()
    pacote = client.get("/api/backup", headers=auth).content      # sem .apikeys.json
    chaves.write_bytes(guardado)
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    assert chaves.exists() and json.loads(chaves.read_text())["keys"][0]["id"] == "k1"


def test_restores_concorrentes_nao_misturam_estado(client, auth, estado):
    """Dois restores ao mesmo tempo: o resultado é UM dos pacotes, nunca mistura."""
    # pacote A com uma voz e outra setting
    estado["settings"].write_text(json.dumps({"language": "pt", "marca": "A"}))
    pac_a = client.get("/api/backup", headers=auth).content
    # pacote B: setting diferente e a voz trocada
    estado["settings"].write_text(json.dumps({"language": "en", "marca": "B"}))
    (estado["vozes"] / "v1.wav").write_bytes(b"RIFF" + b"\xff" * 64)
    pac_b = client.get("/api/backup", headers=auth).content
    (estado["vozes"] / "v1.json").unlink()          # estraga antes de restaurar

    # publica devagar ARQUIVO A ARQUIVO: sem a trava as duas publicações se
    # entrelaçam e o estado final mistura os pacotes
    def devagar(preparados, rollback):
        for nome, origem in preparados:
            destino = (app._caminho_estado(nome) if nome in app._ESTADO_SIMPLES
                       else app.VOICES_DIR / origem.name)
            destino.parent.mkdir(exist_ok=True)
            origem.replace(destino)
            time.sleep(0.05)

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(app, "_publica_estado", devagar)
        erros = []

        def dispara(pacote):
            try:
                caminho = estado["base"] / f"pac-{id(pacote)}-{threading.get_ident()}.zip"
                caminho.write_bytes(pacote)
                app._executa_restore(caminho)
            except Exception as exc:                  # noqa: BLE001
                erros.append(repr(exc))

        threads = [threading.Thread(target=dispara, args=(p,))
                   for p in (pac_a, pac_b, pac_a)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    assert not erros, erros
    marca = json.loads(estado["settings"].read_text())["marca"]
    wav = (estado["vozes"] / "v1.wav").read_bytes()
    # v1.wav e settings têm de ser do MESMO pacote (A: zeros, B: 0xff)
    assert (marca == "A") == (wav.endswith(b"\x00" * 4)), (marca, wav[-4:])
    assert (estado["vozes"] / "v1.json").exists()
