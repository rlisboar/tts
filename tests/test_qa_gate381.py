"""GATE QA do #377 (`task_dc5b9633`): backup/restore não pode ser meia-boca.

O que este gate precisa provar, e por quê:

- round-trip devolve os QUATRO blocos idênticos (hash) e o app segue de pé — é o
  sentido da feature;
- versão de schema desconhecida é recusada SEM tocar em nada — pacote de uma
  build futura não pode sobrescrever o estado da atual;
- sha256 divergente aborta com rollback — pacote corrompido no meio é o caso
  realista de download quebrado;
- permissões finais 0600 nos quatro blocos — os três primeiros são segredo;
- o pacote NÃO leva áudio de `outputs/`;
- as DUAS rotas exigem admin: chave `use` não pode baixar nem trocar o estado;
- mordida: pular a conferência de sha tem de deixar o teste vermelho.
"""

import hashlib
import io
import json
import zipfile

pytest_plugins = ["test_api"]

from test_backup_restore import _hash, _reembala, estado  # noqa: E402

# `estado` é fixture reexportado; o pyflakes do hook não lê `noqa` (#200)
__all__ = ["estado"]


def _pacote(client, auth) -> bytes:
    r = client.get("/api/backup", headers=auth)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    return r.content


def test_round_trip_identico_e_app_de_pe(client, auth, estado):
    antes = _hash(estado)
    pacote = _pacote(client, auth)
    estado["settings"].write_text(json.dumps({"language": "en"}))
    (estado["vozes"] / "v1.wav").unlink()

    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    assert _hash(estado) == antes
    assert client.get("/api/status",
                      headers={"X-API-Key": app_key()}).status_code == 200


def app_key() -> str:
    import app

    return app._primary_api_key()


def test_schema_desconhecido_nao_toca_em_nada(client, auth, estado):
    pacote = _pacote(client, auth)
    estado["settings"].write_text(json.dumps({"language": "en"}))
    antes = _hash(estado)
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", _reembala(pacote, schema=99),
                                       "application/zip")})
    assert r.status_code == 400
    assert _hash(estado) == antes


def test_sha_divergente_aborta_sem_tocar(client, auth, estado):
    pacote = _pacote(client, auth)
    estado["settings"].write_text(json.dumps({"language": "en"}))
    antes = _hash(estado)
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", _reembala(pacote, corromper="settings.json"),
                                       "application/zip")})
    assert r.status_code == 400 and "sha256" in r.text
    assert _hash(estado) == antes


def test_permissoes_0600_nos_quatro_blocos(client, auth, estado):
    pacote = _pacote(client, auth)
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 200, r.text
    for p in (estado["settings"], estado["base"] / "apikeys.json",
              estado["base"] / "speaker-profiles.json", estado["vozes"] / "v1.wav"):
        assert (p.stat().st_mode & 0o777) == 0o600, p


def test_pacote_sem_audio_de_outputs(client, auth, estado):
    (app_outputs() / "gerado.wav").write_bytes(b"RIFF")
    nomes = zipfile.ZipFile(io.BytesIO(_pacote(client, auth))).namelist()
    assert "manifest.json" in nomes
    assert not any(n.startswith("outputs/") for n in nomes)


def app_outputs():
    import app

    return app.OUTPUTS_DIR


def test_chave_de_uso_nao_baixa_nem_restaura(client, auth, chave_de_uso, estado):  # noqa: F811
    assert client.get("/api/backup", headers=chave_de_uso).status_code == 403
    pacote = _pacote(client, auth)
    r = client.post("/api/restore", headers=chave_de_uso,
                    files={"arquivo": ("b.zip", pacote, "application/zip")})
    assert r.status_code == 403


def test_mordida_sem_sha_o_restore_corrompido_passa(client, auth, estado, monkeypatch):
    """Contraprova do próprio gate: sem a conferência, o pacote corrompido entra."""
    import app

    monkeypatch.setattr(app, "_sha256_arquivo", lambda p: "0" * 64)
    pacote = _pacote(client, auth)
    estado["settings"].write_text(json.dumps({"language": "en"}))
    antes = _hash(estado)
    r = client.post("/api/restore", headers=auth,
                    files={"arquivo": ("b.zip", _reembala(pacote, corromper="settings.json"),
                                       "application/zip")})
    # com a conferência ligada o corrompido é recusado; desligada, entra
    assert r.status_code == 200, r.text
    assert _hash(estado) != antes
    assert hashlib.sha256(estado["settings"].read_bytes()).hexdigest() in _hash(estado).values()
