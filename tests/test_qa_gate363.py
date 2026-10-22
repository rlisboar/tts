"""GATE QA (task_feb6b27f): `remote_tts_extra` fechado e chave no zip do mic-router.

Contrato do #357:

1. `remote_tts_extra` é campo de ADMIN — chave `use` que o mandar leva
   `admin_ignored` e o valor não muda;
2. mesmo com o valor aplicado, `text` e `voice` NÃO valem para todos os trechos:
   ficam fora do corpo remoto (avisam uma vez no stderr);
3. o corpo remoto continua recebendo o `text` do TRECHO e os outros campos do
   extra (`speed` etc.);
4. o zip do mic-router entrega credencial em claro → admin; a listagem segue
   aberta, mascarada, sem `reveal` para `use`;
5. a chave que a UI embute no bytecode (`client/mic_router.py` lê o `config.json`
   do zip) continua sendo aceita pelo servidor.

Instância própria por `TestClient`; `settings.json`/`.apikeys.json` em tmp pelo
conftest (nada do dono é tocado).
"""

import json
import sys
from pathlib import Path

import app
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import auth, chave_de_uso, client  # noqa: E402,F401

# Fixtures consumidas pelos testes do arquivo; o `ruff` cobre com `noqa`, o
# `pyflakes` (que o ignora) precisa do `__all__`. Inerte em runtime.
__all__ = [
    "auth",
    "chave_de_uso",
    "client",
]


@pytest.fixture
def avisos(monkeypatch, capsys):
    """Avisos saem em stderr (uma vez por valor). Devolve a função que os lê."""
    monkeypatch.setattr(app, "_extra_remoto_avisados", set())

    def _ler() -> str:
        return capsys.readouterr().err

    return _ler


# ------------------------------------------------- 1) `text`/`voice` não valem

def test_extra_com_texto_e_voz_sai_do_corpo_e_avisa(avisos):
    extra = app._extra_remoto(json.dumps({"text": "fixo", "voice": "v9", "speed": 1.2}))
    assert extra == {"speed": 1.2}, extra
    junto = avisos()
    assert "'text'" in junto and "'voice'" in junto


def test_aviso_sai_uma_vez_por_valor(avisos):
    bruto = json.dumps({"text": "fixo"})
    app._extra_remoto(bruto)
    app._extra_remoto(bruto)
    assert avisos().count("'text'") == 1


@pytest.mark.parametrize("bruto", ["", None, "  ", "não é json", "[1,2]", '"texto"'])
def test_valor_invalido_vira_vazio_sem_estourar(bruto):
    assert app._extra_remoto(bruto) == {}


def test_json_quebrado_avisa_uma_vez(avisos):
    app._extra_remoto("{quebrado")
    app._extra_remoto("{quebrado")
    assert avisos().count("não é JSON válido") == 1


def test_corpo_remoto_usa_o_texto_do_trecho_e_aceita_speed(monkeypatch):
    """O ponto: `text` do extra não sobrescreve o do trecho, `speed` sim."""
    visto = {}

    class _Resp:
        status_code = 200
        content = b"RIFF" + b"\0" * 40

        def raise_for_status(self):
            return None

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002
        visto.update(json or {})
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setitem(app._settings, "remote_tts_extra",
                        json.dumps({"text": "TEXTO FIXO", "voice": "voz-fixa", "speed": 1.3}))
    monkeypatch.setitem(app._settings, "remote_tts_url", "http://exemplo")
    try:
        app._tts_remote_chunk("trecho do job", "pt", {"speed": 1.0}, 24000, "voz-do-job")
    except Exception:  # noqa: BLE001 — o áudio devolvido é lixo, o corpo é o que importa
        pass
    assert visto.get("text") == "trecho do job", visto
    assert visto.get("voice") == "voz-do-job", visto
    assert visto.get("speed") == 1.3, visto


# ------------------------------------------------------ 2) campo é de ADMIN

def test_chave_de_uso_nao_aplica_o_extra(client, auth, chave_de_uso):
    antes = app._settings.get("remote_tts_extra")
    r = client.post("/api/settings", headers=chave_de_uso,
                    json={"remote_tts_extra": '{"speed": 1.9}'})
    assert r.status_code == 200, r.text
    assert "remote_tts_extra" in (r.json().get("admin_ignored") or []), r.json()
    assert app._settings.get("remote_tts_extra") == antes


def test_admin_aplica_o_extra(client, auth):
    r = client.post("/api/settings", headers=auth, json={"remote_tts_extra": '{"speed": 1.9}'})
    assert r.status_code == 200, r.text
    assert app._settings.get("remote_tts_extra") == '{"speed": 1.9}'


# ----------------------------------------------------------- 3) zip do mic-router

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
