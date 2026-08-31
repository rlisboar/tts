"""Testes da camada HTTP (TestClient) — sem carregar modelos MLX.

Cobre a classe de bug que escapou antes (nomes indefinidos dentro de funções
que só rodam em runtime/request).
"""

import io
import json as _json
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app


@pytest.fixture()
def client():
    return TestClient(app.app, raise_server_exceptions=False)


@pytest.fixture()
def auth():
    key = app._primary_api_key()
    return {"X-API-Key": key} if key else {}


# ---------------------------------------------------------------------------
# Básicos
# ---------------------------------------------------------------------------

def test_health_sem_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_auth_exige_chave_da_rede(client):
    # host do TestClient não é loopback -> middleware exige chave
    assert client.get("/api/status").status_code == 401
    r = client.get("/api/status", headers=auth_headers(client))
    assert r.status_code == 200
    assert "backend_id" in r.json()


def auth_headers(client):
    key = app._primary_api_key()
    return {"X-API-Key": key} if key else {}


def test_v1_models(client):
    r = client.get("/v1/models", headers=auth_headers(client))
    ids = [m["id"] for m in r.json()["data"]]
    assert {"tts-1", "tts-1-hd", "whisper-1"} <= set(ids)


def test_job_inexistente_404(client):
    r = client.get("/api/tts/jobs/naoexiste", headers=auth_headers(client))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/tts — validações ANTES do carregamento do modelo
# ---------------------------------------------------------------------------

def test_tts_texto_vazio_400(client):
    r = client.post("/api/tts", json={"text": ""}, headers=auth_headers(client))
    assert r.status_code == 400


def test_tts_texto_longo_400(client):
    r = client.post("/api/tts", json={"text": "x" * 5001}, headers=auth_headers(client))
    assert r.status_code == 400


def test_settings_perf_priority_foi_removida(client):
    """Setting órfã (UI removeu o seletor) não deve mais existir nem voltar."""
    r = client.post("/api/settings", headers=auth_headers(client),
                    json={"perf_priority": "qualidade"})
    assert r.status_code == 200
    assert "perf_priority" not in r.json()


def test_settings_clamp_e_restauracao(client):
    # usa o settings.json real: captura os valores atuais e restaura no fim
    orig = client.get("/api/settings", headers=auth_headers(client)).json()
    try:
        r = client.post("/api/settings", headers=auth_headers(client), json={
            "omni_num_steps": 999, "speed": 99, "chunk_max_chars": 10,
            "perf_priority": "invalido", "omni_seed": -50,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["omni_num_steps"] == 64
        assert body["speed"] == 4.0
        assert body["chunk_max_chars"] == 60
        assert body["omni_seed"] == -1
    finally:
        client.post("/api/settings", headers=auth_headers(client), json=orig)
    atual = client.get("/api/settings", headers=auth_headers(client)).json()
    assert atual["omni_num_steps"] == orig["omni_num_steps"]


# ---------------------------------------------------------------------------
# Export/import de vozes (dirs temporários — não toca em voices/ real)
# ---------------------------------------------------------------------------

def _voz_fake(voices_dir, stem="testeabc123"):
    import soundfile as sf

    sr = 8000
    sf.write(str(voices_dir / f"{stem}.wav"), np.zeros(sr, dtype=np.float32), sr,
             subtype="PCM_16")
    (voices_dir / f"{stem}.json").write_text(_json.dumps(
        {"id": stem, "name": "Teste", "created_at": "2026-01-01", "duration": 1.0}))


@pytest.fixture()
def voices_tmp(tmp_path, monkeypatch):
    vdir = tmp_path / "voices"
    vdir.mkdir()
    monkeypatch.setattr(app, "VOICES_DIR", vdir)
    return vdir


def test_export_import_roundtrip(client, voices_tmp):
    _voz_fake(voices_tmp)

    r = client.get("/api/voices/export", headers=auth_headers(client))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zip_bytes = r.content

    for p in voices_tmp.iterdir():          # simula perda total
        p.unlink()

    r2 = client.post("/api/voices/import", headers=auth_headers(client),
                     files={"zip_file": ("backup.zip", zip_bytes, "application/zip")})
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] and body["vozes"] == 1 and body["orfaos"] == []
    assert (voices_tmp / "testeabc123.wav").exists()
    assert (voices_tmp / "testeabc123.json").exists()


def test_export_nao_inclui_temporarios(client, voices_tmp):
    _voz_fake(voices_tmp)
    (voices_tmp / ".up-lixo.wav").write_bytes(b"\x00" * 32)     # upload crashado
    (voices_tmp / ".rep-lixo.wav").write_bytes(b"\x00" * 32)
    r = client.get("/api/voices/export", headers=auth_headers(client))
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "voices/testeabc123.wav" in names
    assert not any(n.startswith("voices/.") for n in names)


def test_import_avisa_orfaos(client, voices_tmp):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("voices/semjson.wav", b"\x00" * 64)
        z.writestr("voices/semsom.json", "{}")
        z.writestr("voices/lixo.txt", "ignorado")
    r = client.post("/api/voices/import", headers=auth_headers(client),
                    files={"zip_file": ("b.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200
    body = r.json()
    assert set(body["orfaos"]) == {"semjson", "semsom"}
    assert body["ignorados"] == ["voices/lixo.txt"]


def test_import_rejeita_zip_invalido(client, voices_tmp):
    r = client.post("/api/voices/import", headers=auth_headers(client),
                    files={"zip_file": ("b.zip", b"nao-e-zip", "application/zip")})
    assert r.status_code == 400


def test_import_cap_total_zip_bomba(client, voices_tmp, monkeypatch):
    monkeypatch.setattr(app, "_IMPORT_MAX_TOTAL", 100)   # 100 bytes p/ o teste
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("voices/a.wav", b"\x00" * 80)
        z.writestr("voices/a.json", b"{}" * 40)          # 80 bytes — estoura o total
    r = client.post("/api/voices/import", headers=auth_headers(client),
                    files={"zip_file": ("b.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert "grande demais" in r.json()["detail"]


def test_import_sem_auth_401(client, voices_tmp):
    r = client.post("/api/voices/import",
                    files={"zip_file": ("b.zip", b"x", "application/zip")})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Endurecimento da exposição pela internet (proxy)
# ---------------------------------------------------------------------------

def test_docs_exigem_chave_da_rede(client, auth):
    # host do TestClient não é loopback → docs/openapi exigem chave
    r = client.get("/openapi.json")
    assert r.status_code == 401
    r = client.get("/docs")
    assert r.status_code == 401
    r = client.get("/openapi.json", headers=auth)
    assert r.status_code == 200


def test_upload_stt_cap_413(client, auth, monkeypatch):
    monkeypatch.setenv("TTS_MAX_UPLOAD_MB", "0")
    r = client.post("/api/transcribe", headers=auth,
                    files={"audio": ("a.wav", b"x" * 16, "audio/wav")},
                    data={"source_lang": "pt"})
    assert r.status_code == 413


def test_chave_invalida_continua_401(client):
    r = client.get("/api/status", headers={"X-API-Key": "x" * 64})
    assert r.status_code == 401
