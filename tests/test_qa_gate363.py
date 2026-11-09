"""GATE QA (task_feb6b27f): chave no zip do mic-router.

Contrato do #357 (os itens do `remote_tts_extra` saíram com o TTS remoto, #1059):

1. o zip do mic-router entrega credencial em claro → admin; a listagem segue
   aberta, mascarada, sem `reveal` para `use`;
2. a chave que a UI embute no bytecode (`client/mic_router.py` lê o `config.json`
   do zip) continua sendo aceita pelo servidor.

Instância própria por `TestClient`; `settings.json`/`.apikeys.json` em tmp pelo
conftest (nada do dono é tocado).
"""

import json
import sys
from pathlib import Path

import app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import auth, chave_de_uso, client  # noqa: E402,F401

# Fixtures consumidas pelos testes do arquivo; o `ruff` cobre com `noqa`, o
# `pyflakes` (que o ignora) precisa do `__all__`. Inerte em runtime.
__all__ = [
    "auth",
    "chave_de_uso",
    "client",
]


# ----------------------------------------------------------- zip do mic-router

def test_use_nao_baixa_o_zip(client, chave_de_uso):
    r = client.get("/api/client/mic-router", headers=chave_de_uso)
    assert r.status_code == 403, f"chave de uso baixou o zip ({r.status_code})"
    assert "admin" in r.text.lower()


def test_admin_baixa_o_zip_com_a_chave_no_config(client, auth):
    r = client.get("/api/client/mic-router", headers=auth)
    assert r.status_code == 200
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        nomes = z.namelist()
        assert any(n.endswith("mic_router.py") for n in nomes), nomes
        cfg = json.loads(z.read(next(n for n in nomes if n.endswith("config.json"))))
    assert cfg["api_key"], "o zip saiu sem chave"
    # e a chave embutida é aceita de volta pelo servidor (é o que o cliente usa)
    h = {"X-API-Key": cfg["api_key"]}
    assert client.get("/api/status", headers=h).status_code == 200


# ------------------------------------------------------------- 4) listagem coerente

def test_use_lista_chaves_mascaradas_sem_reveal(client, chave_de_uso):
    r = client.get("/api/apikeys", headers=chave_de_uso)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo.get("can_reveal") is False
    assert all("secret" not in k or k.get("secret") for k in corpo["keys"])

    r = client.get("/api/apikeys?reveal=1", headers=chave_de_uso)
    assert r.status_code == 200
    assert r.json().get("can_reveal") is False


def test_admin_revela(client, auth):
    r = client.get("/api/apikeys?reveal=1", headers=auth)
    assert r.status_code == 200
    assert r.json().get("can_reveal") is True


# ------------------------------------------------ 5) mic_router aceita a chave

def test_mic_router_le_o_config_e_manda_a_chave(tmp_path, client, auth):
    """O cliente do zip tem de funcionar: `config.json` ao lado -> header."""
    import importlib.util

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server_url": "http://127.0.0.1:9", "api_key": "chave-do-config"}))
    fonte = tmp_path / "mic_router_copia.py"
    original = Path(app.BASE) / "client" / "mic_router.py"
    fonte.write_text(original.read_text())
    spec = importlib.util.spec_from_file_location("mic_router_gate", fonte)
    modulo = importlib.util.module_from_spec(spec)

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec.loader.exec_module(modulo)
    finally:
        os.chdir(cwd)
    assert modulo.CFG.get("key") == "chave-do-config", modulo.CFG
    assert modulo.CFG.get("url") == "http://127.0.0.1:9", modulo.CFG
