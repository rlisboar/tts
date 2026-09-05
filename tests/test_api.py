"""Testes da camada HTTP (TestClient) — sem carregar modelos MLX.

Cobre a classe de bug que escapou antes (nomes indefinidos dentro de funções
que só rodam em runtime/request).
"""

import io
import json as _json
import zipfile
from pathlib import Path

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
    body = r.json()
    assert body["detail"] == "Não autorizado"
    assert "hint" in body


def test_apikeys_reveal_com_chave_valida(client, auth):
    # TestClient não é loopback, mas chave válida libera o secret
    r = client.get("/api/apikeys?reveal=1", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["local"] is False
    assert d["can_reveal"] is True
    assert d["keys"] and d["keys"][0].get("secret")
    assert isinstance(d.get("lan_urls"), list)


def test_apikeys_cria_autentica_e_apaga(client, auth):
    snap_enabled = app._apikeys.get("enabled", True)
    snap_keys = [dict(k) for k in (app._apikeys.get("keys") or [])]
    try:
        r = client.post("/api/apikeys", headers=auth, json={"name": "pytest-tmp"})
        assert r.status_code == 200
        secret = r.json()["key"]["secret"]
        kid = r.json()["key"]["id"]
        assert secret and kid
        assert client.get("/api/status", headers={"X-API-Key": secret}).status_code == 200
        assert client.delete(f"/api/apikeys/{kid}", headers=auth).status_code == 200
        assert client.get("/api/status", headers={"X-API-Key": secret}).status_code == 401
    finally:
        with app._apikeys_lock:
            app._apikeys = {"enabled": snap_enabled, "keys": [dict(k) for k in snap_keys]}
            app._save_apikeys()
        app.API_KEY = app._primary_api_key() or app._ENV_API_KEY or None


def test_auth_ip_deste_mac_dispensa_chave(monkeypatch):
    monkeypatch.setattr(app, "_own_ips", lambda: {"192.168.15.31"})
    c = TestClient(app.app, raise_server_exceptions=False, client=("192.168.15.31", 50000))
    assert c.get("/api/status").status_code == 200
    d = c.get("/api/apikeys?reveal=1").json()
    assert d["local"] is True and d["can_reveal"] is True
    assert d["keys"] and d["keys"][0].get("secret")


def test_auth_outro_ip_exige_chave_e_bloqueia_cadastro(monkeypatch):
    monkeypatch.setattr(app, "_own_ips", lambda: {"192.168.15.31"})
    c = TestClient(app.app, raise_server_exceptions=False, client=("192.168.15.177", 50000))
    assert c.get("/api/status").status_code == 401
    assert c.post("/api/apikeys", json={"name": "invasor"}).status_code == 401
    snap_keys = [dict(k) for k in (app._apikeys.get("keys") or [])]
    try:
        key = app._primary_api_key()
        r = c.post("/api/apikeys", headers={"X-API-Key": key}, json={"name": "pytest-lan"})
        assert r.status_code == 200 and r.json()["key"]["secret"]
    finally:
        with app._apikeys_lock:
            app._apikeys["keys"] = snap_keys
            app._save_apikeys()
        app.API_KEY = app._primary_api_key() or app._ENV_API_KEY or None


def test_guarda_traversal_ids(client, auth):
    # ids com "." ou comprimento excessivo são rejeitados antes de montar Path
    r = client.get("/api/outputs/a..b/audio", headers=auth)
    assert r.status_code == 404 and r.json()["detail"] == "Id inválido"
    r = client.get("/api/voices/" + "a" * 200 + "/audio", headers=auth)
    assert r.status_code == 404 and r.json()["detail"] == "Id inválido"
    # jobs: endpoint valida existência do job antes — 404 em qualquer caso,
    # nunca conteúdo de fora de outputs/
    r = client.get("/api/tts/jobs/a..b/pieces/0", headers=auth)
    assert r.status_code == 404


def test_status_traz_versao(client, auth):
    r = client.get("/api/status", headers=auth)
    assert r.status_code == 200
    v = r.json().get("version")
    assert isinstance(v, str) and len(v) >= 3
    assert isinstance(r.json().get("lan_urls"), list)


def test_tunnel_start_stop_chamam_launchctl(client, auth, monkeypatch):
    chamadas = []

    def fake_run(args, **kw):
        chamadas.append(args)
        class R: returncode, stderr, stdout = 0, "", ""
        return R()

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    r = client.post("/api/tunnel/stop", headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/api/tunnel/start", headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert any("bootout" in c for c in chamadas) and any("bootstrap" in c for c in chamadas)


def test_tunnel_status_estrutura(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_tunnel_proc_running", lambda: True)
    monkeypatch.setattr(app, "_tunnel_launchd_loaded", lambda: True)
    monkeypatch.setattr(app, "_public_proxy_check", lambda url, timeout=6.0: {"ok": True, "latency_ms": 10})
    d = client.get("/api/tunnel/status?url=https://x/ttsproxy", headers=auth).json()
    assert d["tunnel_running"] is True and d["launchd_loaded"] is True
    assert d["public_check"]["ok"] is True
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["public_check"] is None


def test_chat_fluxo_confirma(client, auth, monkeypatch):
    import time as _t
    respostas = iter([
        '{"final": false, "reply": "Qual o tom?"}',
        '{"final": true, "text": "Bem-vindos ao episódio 5!"}',
        '{"final": false, "reply": "ok, ajusto"}',
    ])
    monkeypatch.setattr(app, "_chat_llm", lambda msgs: respostas.__next__())
    r = client.post("/api/chat/start", headers=auth, json={"objective": "fala de abertura"})
    assert r.status_code == 200 and r.json()["status"] == "thinking"
    sid = r.json()["session_id"]
    d = {}
    for _ in range(40):  # worker assíncrono preenche a resposta
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        if d["status"] != "thinking":
            break
        _t.sleep(0.05)
    assert d["status"] == "chatting" and "tom" in d["reply"]
    r = client.post(f"/api/chat/{sid}", headers=auth, json={"message": "pode mandar"})
    assert r.status_code == 200 and r.json()["status"] == "thinking"
    for _ in range(40):
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        if d["status"] == "confirmed":
            break
        _t.sleep(0.05)
    assert d["status"] == "confirmed" and "episódio 5" in d["text"]
    # a conversa NÃO para: nova fala reabre a rodada e o último texto aprovado persiste
    r = client.post(f"/api/chat/{sid}", headers=auth, json={"message": "muda o tom"})
    assert r.status_code == 200 and r.json()["status"] == "thinking"
    for _ in range(40):
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        if d["status"] != "thinking":
            break
        _t.sleep(0.05)
    assert d["status"] == "chatting"
    assert client.get(f"/api/chat/{sid}", headers=auth).json()["last_text"] == "Bem-vindos ao episódio 5!"
    assert client.delete(f"/api/chat/{sid}", headers=auth).status_code == 200


def test_chat_interrupt_descarta_resposta_em_voo(client, auth, monkeypatch):
    """Barge-in: fala nova enquanto a IA pensa não toma 409 e a resposta velha
    (que chega depois) não entra na sessão."""
    import time as _t
    import threading as _th
    solta = _th.Event()
    chamadas = []

    def _llm(msgs):
        chamadas.append([m["content"] for m in msgs if m["role"] == "user"])
        if len(chamadas) == 1:
            solta.wait(5)                       # 1ª resposta fica pendurada
            return '{"final": false, "reply": "RESPOSTA VELHA"}'
        return '{"final": false, "reply": "RESPOSTA NOVA"}'

    monkeypatch.setattr(app, "_chat_llm", _llm)
    sid = client.post("/api/chat/start", headers=auth,
                      json={"objective": "primeira fala"}).json()["session_id"]
    for _ in range(40):                          # espera o worker 1 travar no LLM
        if chamadas:
            break
        _t.sleep(0.05)
    assert client.get(f"/api/chat/{sid}", headers=auth).json()["status"] == "thinking"

    # sem interrupt continua 409 (contrato antigo dos agentes)
    assert client.post(f"/api/chat/{sid}", headers=auth,
                       json={"message": "outra"}).status_code == 409

    r = client.post(f"/api/chat/{sid}", headers=auth,
                    json={"message": "na verdade, muda tudo", "interrupt": True})
    assert r.status_code == 200 and r.json()["interrupted"] is True
    solta.set()                                  # worker velho responde agora — tarde demais

    d = {}
    for _ in range(60):
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        if d["status"] != "thinking":
            break
        _t.sleep(0.05)
    assert d["reply"] == "RESPOSTA NOVA"
    assert "RESPOSTA VELHA" not in [m["content"] for m in d["messages"]]
    # as duas falas do humano viraram um único turno 'user' (nada de user seguido)
    papeis = [m["role"] for m in d["messages"]]
    assert all(a != b for a, b in zip(papeis, papeis[1:]))
    assert "muda tudo" in d["messages"][0]["content"]
    client.delete(f"/api/chat/{sid}", headers=auth)


def test_chat_system_setting(client, auth):
    r = client.post("/api/settings", headers=auth,
                    json={"chat_system": "instruções customizadas de teste"})
    assert r.status_code == 200
    s = client.get("/api/settings", headers=auth).json()
    assert s["chat_system"] == "instruções customizadas de teste"


def test_chat_extra_setting_valida_json(client, auth):
    # JSON válido é persistido
    r = client.post("/api/settings", headers=auth,
                    json={"chat_extra": '{"reasoning_effort": "low"}'})
    assert r.status_code == 200
    assert client.get("/api/settings", headers=auth).json()["chat_extra"] \
        == '{"reasoning_effort": "low"}'
    # JSON inválido → 400
    r = client.post("/api/settings", headers=auth, json={"chat_extra": "reasoning=low"})
    assert r.status_code == 400
    # JSON não-objeto → 400
    r = client.post("/api/settings", headers=auth, json={"chat_extra": "[1,2]"})
    assert r.status_code == 400
    # vazio limpa
    r = client.post("/api/settings", headers=auth, json={"chat_extra": ""})
    assert r.status_code == 200 and client.get("/api/settings", headers=auth).json()["chat_extra"] == ""


def test_chat_llm_mescla_chat_extra(monkeypatch):
    import urllib.request
    app._settings["chat_base_url"] = "https://provedor.teste/v1"
    app._settings["chat_extra"] = '{"reasoning_effort": "high", "top_p": 0.5}'
    capturado = {}

    class RespFake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return _json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(req, timeout=None, context=None):
        capturado["body"] = _json.loads(req.data)
        return RespFake()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    try:
        out = app._chat_llm([{"role": "user", "content": "oi"}])
    finally:
        app._settings["chat_extra"] = ""
        app._settings["chat_base_url"] = ""
    assert out == "ok"
    # extras mesclados no body…
    assert capturado["body"]["reasoning_effort"] == "high"
    assert capturado["body"]["top_p"] == 0.5
    # …e com effort fixo no extra, NÃO há retry (uma única chamada)
    assert "temperature" in capturado["body"]


def test_chat_llm_injeta_reasoning_baixo_sem_extra(monkeypatch):
    import urllib.request
    app._settings["chat_base_url"] = "https://provedor.teste/v1"
    app._settings["chat_extra"] = ""
    capturado = {}

    class RespFake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return _json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def fake_urlopen(req, timeout=None, context=None):
        capturado["body"] = _json.loads(req.data)
        return RespFake()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    try:
        app._chat_llm([{"role": "user", "content": "oi"}])
    finally:
        app._settings["chat_base_url"] = ""
        app._settings["chat_extra"] = ""
    assert capturado["body"]["reasoning_effort"] == "low"


def test_chat_start_sem_objetivo_400(client, auth):
    assert client.post("/api/chat/start", headers=auth, json={}).status_code == 400


def test_stt_local_engine_dispatch(monkeypatch):
    import sys
    import types
    import numpy as np
    chamado = {}
    monkeypatch.setattr(app, "_vad_tem_fala", lambda p: True)
    monkeypatch.setattr(app, "_transcribe_parakeet",
                        lambda p: chamado.update(engine="parakeet") or
                        {"text": "ok", "language": "", "segments": []})
    app._settings["stt_local_engine"] = "parakeet"
    r = app._transcribe(Path("x.wav"), language="pt", allow_remote=False)
    assert chamado["engine"] == "parakeet" and r["text"] == "ok"
    # whisper: parakeet NÃO é chamado (mlx_whisper fake via sys.modules)
    chamado.clear()
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = lambda *a, **k: {"text": "w", "language": "", "segments": []}
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    monkeypatch.setattr(app, "_wav_to_mono16k", lambda p: np.zeros(16000, dtype=np.float32))
    app._settings["stt_local_engine"] = "whisper"
    r = app._transcribe(Path("x.wav"), language="pt", allow_remote=False)
    assert r["text"] == "w" and "engine" not in chamado
    app._settings["stt_local_engine"] = "whisper"


def test_stt_local_engine_setting_valida(client, auth):
    r = client.post("/api/settings", headers=auth, json={"stt_local_engine": "grok"})
    assert r.status_code == 400
    r = client.post("/api/settings", headers=auth, json={"stt_local_engine": "parakeet"})
    assert r.status_code == 200
    assert client.get("/api/settings", headers=auth).json()["stt_local_engine"] == "parakeet"
    client.post("/api/settings", headers=auth, json={"stt_local_engine": "whisper"})


def test_stt_whisper_repo_setting(client, auth):
    # vazio = default (turbo)
    app._settings["stt_whisper_repo"] = ""
    assert app._whisper_repo() == app.WHISPER_REPO
    # repo custom (máxima precisão) é respeitado
    r = client.post("/api/settings", headers=auth,
                    json={"stt_whisper_repo": "mlx-community/whisper-large-v3"})
    assert r.status_code == 200
    assert app._whisper_repo() == "mlx-community/whisper-large-v3"
    # repo inválido → 400
    r = client.post("/api/settings", headers=auth, json={"stt_whisper_repo": "repo estranho x"})
    assert r.status_code == 400
    r = client.post("/api/settings", headers=auth, json={"stt_whisper_repo": "gpt-4"})
    assert r.status_code == 400
    client.post("/api/settings", headers=auth, json={"stt_whisper_repo": ""})


def test_transcribe_filtra_alucinacao(client, auth, monkeypatch):
    # ruído transcrito como "E aí" (blacklist) → rejeitado, sem virar mensagem
    monkeypatch.setattr(app, "_vad_tem_fala", lambda p: True)
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "E aí", "language": "pt", "segments": []})
    r = client.post("/api/transcribe", headers=auth, data={"source_lang": "pt"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    d = r.json()
    assert d["rejected"] and d["text"] == ""
    # fala real → passa
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "pode mandar o texto", "language": "pt", "segments": []})
    r = client.post("/api/transcribe", headers=auth, data={"source_lang": "pt"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.json()["text"] == "pode mandar o texto"


def test_chat_sessao_inexistente_404(client, auth):
    assert client.post("/api/chat/deadbeef", headers=auth,
                       json={"message": "oi"}).status_code == 404


# ---------------------------------------------------------------------------
# Biometria de voz (gate de locutor)


@pytest.fixture()
def perfis_vazios(monkeypatch):
    monkeypatch.setattr(app, "_speaker_load", lambda: {})
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)


def test_speaker_gate_off_nao_bloqueia(client, auth, perfis_vazios):
    app._settings["speaker_gate"] = "off"
    assert app._speaker_gate_ok(Path("x.wav")) == {}


def test_speaker_enforce_rejeita_e_aceita(client, auth, monkeypatch):
    VEC_A, VEC_B = [1.0, 0.0], [0.0, 1.0]
    perfis = {"eu": {"vecs": [VEC_A], "updated": 0}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_embed", lambda p: list(VEC_A))
    app._settings["speaker_gate"] = "enforce"
    app._settings["speaker_threshold"] = 0.75
    # mesma voz → passa
    assert app._speaker_gate_ok(Path("x.wav")) == {}
    # outra voz (similaridade 0) → rejeitada
    monkeypatch.setattr(app, "_speaker_embed", lambda p: list(VEC_B))
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out["rejected"] and "não" in out["reason"]
    # modo etiqueta: reconhece e rotula
    monkeypatch.setattr(app, "_speaker_embed", lambda p: list(VEC_A))
    app._settings["speaker_gate"] = "label"
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out["speaker"] == "eu" and out["speaker_sim"] >= 0.99
    # sem perfis cadastrados: gate inerte
    app._settings["speaker_gate"] = "enforce"
    monkeypatch.setattr(app, "_speaker_load", lambda: {})
    assert app._speaker_gate_ok(Path("x.wav")) == {}
    app._settings["speaker_gate"] = "off"
    app._settings["speaker_threshold"] = 0.75


def test_speaker_enroll_lista_e_apaga(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".voz": Path("falso.wav"))
    monkeypatch.setattr(app, "_speaker_embed", lambda p: [0.5, 0.5])
    store = {}
    monkeypatch.setattr(app, "_speaker_load", lambda: store)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)  # store é mutado pelo endpoint
    r = client.post("/api/speaker/enroll", headers=auth,
                    data={"name": "eu"}, files={"audio": ("voz.wav", b"x", "audio/wav")})
    assert r.status_code == 200 and r.json()["samples"] == 1
    lista = client.get("/api/speaker/profiles", headers=auth).json()
    assert lista and lista[0]["name"] == "eu"
    # apagar existente 200 / inexistente 404
    assert client.delete("/api/speaker/eu", headers=auth).status_code == 200
    assert client.delete("/api/speaker/eu", headers=auth).status_code == 404


def test_speaker_settings_validam(client, auth):
    r = client.post("/api/settings", headers=auth, json={"speaker_gate": "maluco"})
    assert r.status_code == 400
    r = client.post("/api/settings", headers=auth,
                    json={"speaker_gate": "enforce", "speaker_threshold": 0.9})
    assert r.status_code == 200
    s = client.get("/api/settings", headers=auth).json()
    assert s["speaker_gate"] == "enforce" and abs(s["speaker_threshold"] - 0.9) < 1e-6
    client.post("/api/settings", headers=auth, json={"speaker_gate": "off"})
