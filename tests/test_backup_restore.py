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
    app._apikeys_publica()      # a troca inteira sai do snapshot, não só o role


def _hash(estado) -> dict:
    alvos = [estado["settings"], estado["base"] / "apikeys.json",
             estado["base"] / "speaker-profiles.json",
             estado["vozes"] / "v1.wav", estado["vozes"] / "v1.json"]
    return {str(p.name): hashlib.sha256(p.read_bytes()).hexdigest() for p in alvos}


def _reembala(zip_bytes: bytes, *, schema=None, corromper=None,
              tirar_vozes=False) -> bytes:
    """Reescreve o pacote mudando o schema, um byte ou tirando as vozes."""
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
            if tirar_vozes and info.filename.startswith("voices/"):
                continue
            if tirar_vozes and info.filename == "manifest.json":
                man = json.loads(dados)
                man["arquivos"] = {k: v for k, v in man["arquivos"].items()
                                   if not k.startswith("voices/")}
                dados = json.dumps(man).encode()
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
    # espelho explícito: o padrão agora MESCLA (#788), então a voz intrusa só sai
    # quando o dono pede o espelho
    r = aflito.post("/api/restore?espelho=1", headers=auth,
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
    def devagar(preparados, rollback, espelho=False):
        for nome, origem in preparados:
            destino = (app._caminho_estado(nome) if nome in app._ESTADO_SIMPLES
                       else app.VOICES_DIR / origem.name)
            destino.parent.mkdir(exist_ok=True)
            origem.replace(destino)
            time.sleep(0.05)
        return [], [n for n, _o in preparados]

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


# --- #788: padrão MESCLA, espelho é explícito e tudo tem volta ----------------
def test_padrao_mescla_e_preserva_voz_fora_do_pacote(client, auth, estado):
    """Restore só com settings não pode zerar o `voices/` (#788)."""
    pacote = client.get("/api/backup", headers=auth).content
    intrusa = estado["vozes"] / "outra.wav"
    intrusa.write_bytes(b"lixo")
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    assert intrusa.exists(), "o padrão apagou voz fora do pacote"
    assert r.json()["espelho"] is False and r.json()["apagados"] == []


def test_espelho_lista_o_que_apagou_e_guarda_snapshot(client, auth, estado,
                                                       monkeypatch, tmp_path):
    """`espelho=1` apaga — mas antes grava snapshot durável com o que saiu."""
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    pacote = client.get("/api/backup", headers=auth).content
    intrusa = estado["vozes"] / "outra.wav"
    intrusa.write_bytes(b"lixo")
    r = client.post("/api/restore?espelho=1", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    assert not intrusa.exists()
    assert "outra.wav" in r.json()["apagados"]
    snap = app.Path(r.json()["backup"])
    assert (snap / "voices" / "outra.wav").read_bytes() == b"lixo"
    assert (snap / "voices" / "outra.wav").stat().st_mode & 0o777 == 0o600
    assert backup_dir  # o diretório é o `STATE_DIR`


def test_pacote_sem_vozes_com_espelho_exige_confirmacao(client, auth, estado):
    """Espelho com pacote sem vozes apagaria tudo: recusa até `sem_vozes=1`."""
    pacote = client.get("/api/backup", headers=auth).content
    so_settings = _reembala(pacote, tirar_vozes=True)
    r = client.post("/api/restore?espelho=1", headers=auth,
                    files={"arquivo": ("b.zip", so_settings, "application/zip")})
    assert r.status_code == 400 and "sem_vozes" in r.text
    assert (estado["vozes"] / "v1.wav").exists()
    r = client.post("/api/restore?espelho=1&sem_vozes=1", headers=auth,
                    files={"arquivo": ("b.zip", so_settings, "application/zip")})
    assert r.status_code == 200, r.text
    assert not list(estado["vozes"].glob("*.wav"))


def test_snapshot_do_restore_e_duravel_antes_de_publicar(client, auth, estado,
                                                         monkeypatch, tmp_path):
    """O snapshot tem de ir ao DISCO antes de qualquer escrita em `voices/` (#788).

    `copy2` fecha o arquivo — sobrevive a crash de processo, não a queda de energia.
    Sem `fsync`, o conteúdo pode ficar no page cache com `voices/` já reescrito. A
    célula conta as duas coisas e exige um `fsync` por arquivo copiado, mais o do
    diretório, ANTES de o `voices/` ser tocado."""
    monkeypatch.setattr(app, "STATE_DIR", tmp_path / "estado")
    eventos: list[tuple[str, str]] = []
    real_copy2 = app.shutil.copy2
    real_copyfile = app.shutil.copyfile
    real_unlink = app.Path.unlink

    def _anota(destino):
        # só o snapshot DURÁVEL interessa: o rollback efêmero não precisa de fsync
        if "restore-backups" in str(destino):
            eventos.append(("copia", str(destino)))

    def _copia(origem, destino, *a, **k):
        _anota(destino)
        return real_copy2(origem, destino, *a, **k)

    def _copia1(origem, destino, *a, **k):
        _anota(destino)
        return real_copyfile(origem, destino, *a, **k)

    def _fsync(fd):
        eventos.append(("fsync", str(fd)))

    def _unlink(self, *a, **k):
        eventos.append(("unlink", self.name))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(app.shutil, "copy2", _copia)
    monkeypatch.setattr(app.shutil, "copyfile", _copia1)
    monkeypatch.setattr(app.os, "fsync", _fsync)
    monkeypatch.setattr(app.Path, "unlink", _unlink)
    pacote = client.get("/api/backup", headers=auth).content
    (estado["vozes"] / "outra.wav").write_bytes(b"lixo")
    eventos.clear()

    r = client.post("/api/restore?espelho=1", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    # `copy2` chama `copyfile` por dentro: o mesmo destino aparece duas vezes
    copias = sorted({e[1] for e in eventos if e[0] == "copia"})
    fsyncs = [e for e in eventos if e[0] == "fsync"]
    assert copias, "nada foi copiado para o snapshot"
    assert len(fsyncs) >= len(copias), f"{len(copias)} arquivos e só {len(fsyncs)} fsync"
    # e tudo isso ANTES de apagar a voz que sobra (a publicação)
    primeiro_unlink = next((i for i, e in enumerate(eventos) if e[0] == "unlink"), None)
    if primeiro_unlink is not None:
        assert len([e for e in eventos[:primeiro_unlink] if e[0] == "fsync"]) >= len(copias), \
            "publicou antes de o snapshot estar durável"


def test_dois_restores_no_mesmo_segundo_nao_dividem_a_pasta(monkeypatch, tmp_path):
    """Dois snapshots no mesmo segundo têm de ser pastas distintas (#788)."""
    monkeypatch.setattr(app, "STATE_DIR", tmp_path)
    monkeypatch.setattr(app, "time", type("T", (), {"strftime": staticmethod(lambda *a: "20260101-120000")}))
    um = app._snapshot_duravel()
    dois = app._snapshot_duravel()
    assert um != dois
    assert um.is_dir() and dois.is_dir()
