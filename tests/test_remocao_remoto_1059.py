"""#1059 — só modelos LOCAIS: TTS, STT e tradução remotos saíram do app.

O que fica e é cobrado aqui:
- settings ANTIGO (o do mini tem todas as chaves velhas) não quebra o boot, não
  volta no GET e some do disco na próxima gravação — sem o aviso de "instância
  desatualizada" do #197, que é para chave de build MAIS NOVA;
- `POST /api/settings` com chave obsoleta não dá 500 nem grava nada;
- o LLM da Conversa/Live (exceção do dono) segue herdando `remote_base_url` e
  `remote_api_key`, e o modelo que vinha de `remote_translate_model` migra para
  `chat_model` em vez de virar `gpt-4o-mini` em silêncio;
- tradução e transcrição vão ao modelo local mesmo com o toggle velho ligado.

Settings SINTÉTICO em tmp (#446 §C): nunca o arquivo do dono.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app

RAIZ = Path(app.__file__).resolve().parent

# o que um settings.json de antes do #1059 pode trazer (o do mini traz tudo)
_VELHAS = {
    "remote_tts": True, "remote_tts_url": "http://rtx-host:8800/tts",
    "remote_tts_voice": "voz-rtx", "remote_tts_extra": '{"num_steps": 8}',
    "remote_tts_model": "tts-1", "remote_tts_fallback": False,
    "remote_translate": True, "remote_translate_model": "qwen2.5-14b-instruct",
    "remote_stt": True, "remote_stt_model": "whisper-1",
    "remote_stt_base_url": "http://rtx-host:8001/v1", "remote_stt_key": "sk-stt-velha",
    "free_local_on_remote": True, "stt_beam": 8,
    "omni_denoise": False, "omni_preprocess_prompt": False,
    "omni_postprocess_output": False, "omni_audio_chunk_duration": 20.0,
    "omni_audio_chunk_threshold": 40.0,
}


def test_lista_de_obsoletas_cobre_o_settings_velho():
    """A lista é a fonte da tolerância: chave velha fora dela viraria "preservada
    de build mais nova" e o segredo `remote_stt_key` ficaria no disco."""
    assert set(_VELHAS) == set(app._SETTINGS_OBSOLETAS)
    assert not set(app._SETTINGS_OBSOLETAS) & set(app._SETTINGS_DEFAULTS)
    assert not set(app._SETTINGS_OBSOLETAS) & set(app._SETTINGS_ADMIN)
    assert "remote_stt_key" not in app._SETTINGS_SECRETS


def test_boot_com_settings_velho_sobe_e_migra_o_modelo_do_llm(tmp_path):
    """Import REAL do app num subprocesso, com o settings velho no caminho."""
    sett = tmp_path / "settings.json"
    sett.write_text(json.dumps({**_VELHAS, "chat_model": "",
                                "remote_base_url": "http://llm.lan/v1",
                                "remote_api_key": "sk-llm"}))
    env = {**os.environ,
           "TTS_SETTINGS_PATH": str(sett),
           "TTS_STATE_DIR": str(tmp_path / "state"),
           "TTS_OUTPUTS_DIR": str(tmp_path / "outputs"),
           "TTS_VOICES_DIR": str(tmp_path / "voices"),
           "TTS_APIKEYS_PATH": str(tmp_path / ".apikeys.json"),
           "TTS_LEGACY_APIKEY_PATH": str(tmp_path / ".apikey"),
           "TTS_SPEAKER_PATH": str(tmp_path / ".speaker-profiles.json"),
           "TTS_JOBS_HIST_PATH": str(tmp_path / "state" / "jobs-history.json")}
    for k in ("TTS_CHAT_BASE_URL", "TTS_CHAT_MODEL", "TTS_CHAT_API_KEY"):
        env.pop(k, None)
    codigo = (
        "import json, app\n"
        "b, m, k = app._chat_provider()\n"
        "print(json.dumps({'velhas_na_ram': sorted(set(app._settings) & app._SETTINGS_OBSOLETAS),"
        " 'chat_model': app._settings.get('chat_model'), 'provedor': [b, m, k]}))\n")
    r = subprocess.run([sys.executable, "-c", codigo], cwd=str(RAIZ), env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-1200:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["velhas_na_ram"] == [], "chave obsoleta voltou para a RAM no boot"
    assert out["chat_model"] == "qwen2.5-14b-instruct", \
        "o modelo que o LLM usava (via remote_translate_model) não migrou para chat_model"
    assert out["provedor"] == ["http://llm.lan/v1", "qwen2.5-14b-instruct", "sk-llm"]
    # o import não grava (#535): o arquivo segue como estava até a 1ª gravação
    assert json.loads(sett.read_text())["remote_stt_key"] == "sk-stt-velha"


def test_migracao_nao_pisa_em_chat_model_nem_copia_o_default():
    alvo = {"chat_model": "gpt-escolhido"}
    app._migra_settings_obsoletas({"remote_translate_model": "outro"}, alvo)
    assert alvo["chat_model"] == "gpt-escolhido"
    alvo = {"chat_model": ""}
    app._migra_settings_obsoletas({"remote_translate_model": "gpt-4o-mini"}, alvo)
    assert alvo["chat_model"] == "", "o default não precisa virar valor explícito"
    app._migra_settings_obsoletas("lixo", alvo)          # arquivo estranho: nada


def test_save_descarta_obsoletas_sem_aviso_e_preserva_chave_nova(monkeypatch, tmp_path, capsys):
    sett = tmp_path / "settings.json"
    sett.write_text(json.dumps({**_VELHAS, "chave_de_build_nova": 1}))
    monkeypatch.setattr(app, "SETTINGS_PATH", sett)
    monkeypatch.setattr(app, "_settings_desconhecidas_avisadas", set())
    app._save_settings()
    disco = json.loads(sett.read_text())
    assert not set(disco) & set(_VELHAS), "obsoleta sobreviveu à gravação"
    assert disco.get("chave_de_build_nova") == 1, "o #197 (chave de build nova) quebrou"
    err = capsys.readouterr().err
    assert "chave_de_build_nova" in err
    assert not any(k in err for k in _VELHAS), "obsoleta virou aviso de instância velha"


@pytest.fixture()
def client():
    return TestClient(app.app, raise_server_exceptions=False)


@pytest.fixture()
def auth():
    key = app._primary_api_key()
    return {"X-API-Key": key} if key else {}


@pytest.fixture()
def settings_isolado(monkeypatch, tmp_path):
    sett = tmp_path / "settings.json"
    sett.write_text("{}")
    monkeypatch.setattr(app, "SETTINGS_PATH", sett)
    monkeypatch.setattr(app, "_settings", dict(app._SETTINGS_DEFAULTS))
    return sett


def test_post_com_chave_obsoleta_nao_quebra_nem_grava(client, auth, settings_isolado):
    r = client.post("/api/settings", headers=auth, json={**_VELHAS, "speed": 1.1})
    assert r.status_code == 200, r.text[:300]
    assert app._settings["speed"] == pytest.approx(1.1), "o resto do blob não valeu"
    assert not set(app._settings) & set(_VELHAS)
    assert not set(json.loads(settings_isolado.read_text())) & set(_VELHAS)
    g = client.get("/api/settings", headers=auth).json()
    assert not set(g) & set(_VELHAS), "GET ainda anuncia campo removido"
    assert not set(g.get("admin_fields") or []) & set(_VELHAS)


def test_llm_herda_remote_base_url_e_key(monkeypatch, settings_isolado):
    """Célula que morde: tirar a herança do `_chat_provider` derruba a Conversa e
    o Live de quem só preencheu `remote_*` (o mini)."""
    for k in ("TTS_CHAT_BASE_URL", "TTS_CHAT_MODEL", "TTS_CHAT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    app._settings.update(remote_base_url="http://llm.lan/v1/", remote_api_key="sk-x",
                         chat_base_url="", chat_api_key="", chat_model="")
    assert app._chat_provider() == ("http://llm.lan/v1", "gpt-4o-mini", "sk-x")
    app._settings.update(chat_base_url="http://outro/v1", chat_api_key="sk-c", chat_model="m")
    assert app._chat_provider() == ("http://outro/v1", "m", "sk-c")


def test_traducao_vai_ao_modelo_local_mesmo_com_toggle_velho(monkeypatch, settings_isolado):
    """Quem tinha `remote_translate: true` cai no LOCAL sem erro: nenhum HTTP."""
    app._settings.update(remote_base_url="http://llm.lan/v1", remote_api_key="sk-x")
    app._settings["remote_translate"] = True        # chave velha que vazasse para a RAM
    monkeypatch.setenv("TTS_TRANSLATE_BASE_URL", "http://nao-chamar/v1")
    import requests

    def _proibido(*a, **k):
        raise AssertionError("tradução foi à rede")
    monkeypatch.setattr(requests, "post", _proibido)
    chamado = {}
    fake = types.ModuleType("mlx_lm")
    fake.load = lambda repo: (object(), types.SimpleNamespace(
        apply_chat_template=lambda msgs, **k: "PROMPT"))

    def _gera(model, tok, prompt=None, **k):
        chamado["prompt"] = prompt
        return "Hello world"
    fake.generate = _gera
    monkeypatch.setitem(sys.modules, "mlx_lm", fake)
    monkeypatch.setattr(app, "_mt", {"model": None, "tok": None, "repo": None})
    assert app._translate("Olá mundo", "en") == "Hello world"
    assert chamado, "o modelo local não foi chamado"


def test_unload_only_remote_de_cliente_antigo_nao_descarrega(client, auth, monkeypatch):
    def _proibido(*a, **k):
        raise AssertionError("descarregou modelo local")
    monkeypatch.setattr(app, "_unload_local_models", _proibido)
    r = client.post("/api/models/unload", headers=auth, json={"only_remote": True})
    assert r.status_code == 200 and r.json() == {"unloaded": []}
