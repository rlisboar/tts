"""Testes da camada HTTP (TestClient) — sem carregar modelos MLX.

Cobre a classe de bug que escapou antes (nomes indefinidos dentro de funções
que só rodam em runtime/request).
"""

import hashlib
import io
import json as _json
import re
import os
import shutil
import sys
import threading
import time
import types
import wave
import zipfile
from collections import deque
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app


@pytest.fixture()
def client():
    return TestClient(app.app, raise_server_exceptions=False)


@pytest.fixture()
def auth():
    key = app._primary_api_key()
    return {"X-API-Key": key} if key else {}


@pytest.fixture(autouse=True)
def _isolamento_disco(monkeypatch, tmp_path):
    """Snapshot de app._settings e do arquivo de settings antes de cada teste,
    restaurados depois: os POSTs em /api/settings gravam em disco e na RAM, e um
    teste não pode deixar valor para o próximo.

    O arquivo já é uma CÓPIA isolada em tmp (ver `_estado_isolado` no conftest),
    então o restore aqui é isolamento dentro do processo — antes ele era também a
    única barreira contra a suíte reescrever o settings.json de produção."""
    snap = dict(app._settings)
    arq = app.SETTINGS_PATH
    conteudo = arq.read_text() if arq.exists() else None
    # chaves/perfis também não podem deixar rastro no repo
    monkeypatch.setattr(app, "APIKEYS_PATH", tmp_path / ".apikeys.json")
    monkeypatch.setattr(app, "LEGACY_APIKEY_PATH", tmp_path / ".apikey")
    monkeypatch.setattr(app, "SPEAKER_PATH", tmp_path / ".speaker-profiles.json")
    try:
        yield
    finally:
        app._settings.clear()
        app._settings.update(snap)
        try:
            if conteudo is None:
                if arq.exists():
                    arq.unlink()
            elif arq.exists() and arq.read_text() != conteudo:
                arq.write_text(conteudo)
        except OSError:
            pass


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


# ---------------------------------------------------------------------------
# /api/tts — override de `model` só vale depois de o pedido PASSAR (#23)
#
# A UI manda `model` no body da geração e espera ver a escolha aplicada (o worker
# lê `_settings["model"]`), então o override é global por contrato. O defeito era
# aplicá-lo ANTES de validar: um 400/404/429 deixava `settings.json` com outro
# modelo e a UI/API alinhadas com um pedido que nem rodou.
# ---------------------------------------------------------------------------

def _settings_no_disco() -> str | None:
    return app.SETTINGS_PATH.read_text() if app.SETTINGS_PATH.exists() else None


def _pedido_invalido_nao_troca_modelo(client, auth, payload, status):
    antes_ram, antes_disco = app._settings["model"], _settings_no_disco()
    assert payload.get("model") != antes_ram, "o teste só vale se o override é diferente"
    r = client.post("/api/tts", headers=auth, json=payload)
    assert r.status_code == status, r.text
    assert app._settings["model"] == antes_ram
    assert _settings_no_disco() == antes_disco, "pedido recusado não pode ir ao disco"


def test_tts_texto_vazio_com_model_nao_troca_modelo(client, auth):
    _pedido_invalido_nao_troca_modelo(
        client, auth, {"text": "", "model": "outro/modelo-400"}, 400)


def test_tts_texto_longo_com_model_nao_troca_modelo(client, auth):
    _pedido_invalido_nao_troca_modelo(
        client, auth, {"text": "x" * 5001, "model": "outro/modelo-longo"}, 400)


def test_tts_sem_voz_com_model_nao_troca_modelo(client, auth, monkeypatch):
    """404 de voz: `_resolve_voice` só estoura quando não há nenhuma voz gravada."""
    monkeypatch.setattr(app, "list_voices", lambda: [])
    _pedido_invalido_nao_troca_modelo(
        client, auth, {"text": "oi", "model": "outro/modelo-404"}, 404)


def test_tts_sem_slot_429_com_model_nao_troca_modelo(client, auth, monkeypatch):
    """429 de fila cheia é recusa ANTES de criar o job — não pode valer o override.
    `kokoro` + voz de design passa na validação sem depender do voices/ do repo."""
    def _cheio():
        raise HTTPException(429, "Muitos áudios ativos; tente novamente em instantes")

    monkeypatch.setattr(app, "_jobs_capacity_check", _cheio)
    _pedido_invalido_nao_troca_modelo(
        client, auth, {"text": "oi", "model": "kokoro", "voice_id": "__design__"}, 429)


def test_tts_pedido_valido_aplica_e_persiste_o_modelo(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **k: None)   # sem MLX
    r = client.post("/api/tts", headers=auth, json={"text": "oi", "model": "kokoro"})
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "kokoro"          # família do pedido, não a antiga
    assert r.json()["model_aplicado_global"] is True
    assert app._settings["model"] == "kokoro"       # aceito: vale na RAM...
    assert _json.loads(app.SETTINGS_PATH.read_text())["model"] == "kokoro"   # ...e no disco


def _espera(pred, limite=3.0):
    t0 = time.time()
    while time.time() - t0 < limite:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_tts_model_de_chave_de_uso_vale_so_no_pedido(client, auth, chave_de_uso, monkeypatch):
    """Integração com a #21 (pedido do PM): `model` é campo ADMIN no /api/settings.
    Chave de uso gera com o modelo que pediu, mas não sequestra a config global —
    nem na RAM, nem no disco."""
    chamadas = []
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **k: chamadas.append(a))
    antes_ram, antes_disco = app._settings["model"], _settings_no_disco()
    assert antes_ram != "kokoro"
    r = client.post("/api/tts", headers=chave_de_uso,
                    json={"text": "oi", "model": "kokoro", "voice_id": "__design__"})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "kokoro"
    assert r.json()["model_aplicado_global"] is False
    assert r.json()["family"] == "kokoro"           # família do pedido, não a global
    assert app._settings["model"] == antes_ram
    assert _settings_no_disco() == antes_disco
    assert _espera(lambda: chamadas), "job não subiu"
    assert chamadas[0][6] == "kokoro", "o job tem de receber o modelo do pedido"
    # pedido inválido da chave de uso também não mexe em nada
    assert client.post("/api/tts", headers=chave_de_uso,
                       json={"text": "", "model": "outro/x"}).status_code == 400
    assert app._settings["model"] == antes_ram and _settings_no_disco() == antes_disco


# ---------------------------------------------------------------------------
# /api/tts — voice_id tem de ficar DENTRO de voices/ (#33)
#
# `VOICES_DIR / f"{voice_id}.wav"` aceitava qualquer valor: `"../fora"` resolvia
# para um WAV fora do diretório e seguia para o worker/remoto como voz cadastrada.
# Mesma classe no `default_voice` das settings (gravava o valor cru).
# ---------------------------------------------------------------------------

@pytest.fixture()
def vozes_isoladas(tmp_path, monkeypatch):
    """voices/ em tmp, com uma voz registrada; devolve (dir, sentinela_externa)."""
    vd = tmp_path / "voices"
    vd.mkdir()
    (vd / "voz-ok.wav").write_bytes(b"RIFF0000WAVEfmt ")
    (vd / "voz-ok.json").write_text(_json.dumps({"id": "voz-ok", "name": "Voz OK"}))
    # id exótico (espaço/`>`): `_safe_id` rejeitaria, mas o arquivo está dentro de voices/
    (vd / "voz exotica.wav").write_bytes(b"RIFF0000WAVEfmt ")
    (vd / "voz exotica.json").write_text(_json.dumps({"id": "voz exotica", "name": "Exótica"}))
    # sentinela DO LADO DE FORA: é para onde "../fora" apontaria
    sentinela = tmp_path / "fora.wav"
    sentinela.write_bytes(b"RIFF0000WAVEfmt ")
    monkeypatch.setattr(app, "VOICES_DIR", vd)
    return vd, sentinela


def _voz_do_job(client, auth, monkeypatch, payload, vozes_isoladas):
    """Manda o pedido e devolve (voice_id, voice_path) que o job recebeu."""
    chamadas = []
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **k: chamadas.append(a))
    r = client.post("/api/tts", headers=auth, json=payload)
    assert r.status_code == 200, r.text
    assert _espera(lambda: chamadas), "job não subiu"
    return chamadas[0][2], chamadas[0][3]


def test_tts_voice_id_registrado_usa_o_caminho_de_voices(client, auth, monkeypatch, vozes_isoladas):
    vd, _ = vozes_isoladas
    vid, vpath = _voz_do_job(client, auth, monkeypatch,
                             {"text": "oi", "voice_id": "voz-ok"}, vozes_isoladas)
    assert vid == "voz-ok" and vpath == vd / "voz-ok.wav"


def test_tts_voice_id_com_traversal_nao_escapa_de_voices(client, auth, monkeypatch, vozes_isoladas):
    vd, sentinela = vozes_isoladas
    assert sentinela.exists()
    monkeypatch.setitem(app._settings, "default_voice", "voz-ok")   # fallback determinístico
    for bruto in ("../fora", "../../etc/passwd"):
        vid, vpath = _voz_do_job(client, auth, monkeypatch,
                                 {"text": "oi", "voice_id": bruto}, vozes_isoladas)
        assert vpath == vd / "voz-ok.wav", bruto      # e não vd/../fora.wav
        assert vid == "voz-ok" and vpath.parent == vd


def test_tts_voz_registrada_com_id_exotico_continua_valendo(client, auth, monkeypatch, vozes_isoladas):
    """Compat: id com espaço/`>` (não passa no `_safe_id`) segue funcionando —
    o critério é o caminho resolvido, não o alfabeto do id."""
    vd, _ = vozes_isoladas
    vid, vpath = _voz_do_job(client, auth, monkeypatch,
                             {"text": "oi", "voice_id": "voz exotica"}, vozes_isoladas)
    assert vid == "voz exotica" and vpath == vd / "voz exotica.wav"


def test_voice_path_recusa_symlink_para_fora_e_aceita_para_dentro(client, auth, monkeypatch, vozes_isoladas):
    """DEcisão do PM na #69: a checagem segue o ARQUIVO de propósito — só vale o que
    está dentro de `voices/` DEPOIS do symlink resolvido. Link para fora é recusado
    (a voz cai no padrão, como qualquer desconhecida); link para dentro vale."""
    vd, sentinela = vozes_isoladas
    os.symlink(sentinela, vd / "link.wav")            # voices/link.wav -> fora do dir
    os.symlink(vd / "voz-ok.wav", vd / "linkdentro.wav")
    assert app._voice_path("link") is None
    assert app._voice_path("linkdentro") == vd / "linkdentro.wav"

    monkeypatch.setitem(app._settings, "default_voice", "voz-ok")
    vid, vpath = _voz_do_job(client, auth, monkeypatch,
                             {"text": "oi", "voice_id": "link"}, vozes_isoladas)
    assert vid == "voz-ok" and vpath == vd / "voz-ok.wav", "recusado cai no padrão"


def test_voice_path_recusa_traversal_symlink_quebrado_e_aninhado(vozes_isoladas):
    """Guarda da regra estrita: só filho DIRETO de `voices/`; traversal, symlink
    quebrado e `sub/` ficam fora — e o absoluto para dentro continua valendo."""
    vd, _ = vozes_isoladas
    (vd / "sub").mkdir()
    (vd / "sub" / "ninho.wav").write_bytes(b"RIFF")
    os.symlink(vd / "nao-existe.wav", vd / "quebrado.wav")
    assert app._voice_path("../fora") is None
    assert app._voice_path("../../etc/passwd") is None
    assert app._voice_path("quebrado") is None
    assert app._voice_path("sub/ninho") is None
    assert app._voice_path(str(vd / "voz-ok")) == vd / "voz-ok.wav"   # absoluto p/ dentro


def test_settings_default_voice_nao_aceita_traversal(client, auth, monkeypatch, vozes_isoladas):
    vd, sentinela = vozes_isoladas
    assert client.post("/api/settings", headers=auth,
                       json={"default_voice": "../fora"}).status_code == 200
    assert app._settings["default_voice"] is None      # antes: ficava "../fora"
    assert client.post("/api/settings", headers=auth,
                       json={"default_voice": "voz-ok"}).status_code == 200
    assert app._settings["default_voice"] == "voz-ok"
    # e o pedido SEM voz própria usa esse padrão (caminho dentro de voices/)
    vid, vpath = _voz_do_job(client, auth, monkeypatch, {"text": "oi"}, vozes_isoladas)
    assert vid == "voz-ok" and vpath == vd / "voz-ok.wav"


def test_tts_valida_com_a_familia_do_override_sem_aplicar(client, auth, monkeypatch):
    """A família do pedido já vale na VALIDAÇÃO (é o que decide voz/design):
    `kokoro` aceita voice design sem instruct e não deve dar 400."""
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **k: None)
    app._settings["model"] = "omnivoice"
    r = client.post("/api/tts", headers=auth,
                    json={"text": "oi", "model": "kokoro", "voice_id": "__design__"})
    assert r.status_code == 200, r.text
    assert r.json()["family"] == "kokoro"


def test_settings_perf_priority_foi_removida(client):
    """Setting órfã (UI removeu o seletor) não deve mais existir nem voltar."""
    r = client.post("/api/settings", headers=auth_headers(client),
                    json={"perf_priority": "qualidade"})
    assert r.status_code == 200
    assert "perf_priority" not in r.json()


# ---------------------------------------------------------------------------
# Rate limiter — memória sob paths com id e identidades descartáveis (#24)
#
# A chave do bucket é (identidade, path). Com o path CRU, pollar N jobs = N
# buckets "vivos" enquanto houver tráfego, e o teto antigo só varria o que já
# tinha expirado — rajada de paths/identidades dentro da janela crescia sem
# limite. Agora o id do path colapsa em `*` e existe teto com evicção previsível.
# ---------------------------------------------------------------------------

def _identidade(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _limiter_de_rede(ip="203.0.113.9"):
    """TestClient que NÃO é loopback (loopback é isento do limitador)."""
    return TestClient(app.app, raise_server_exceptions=False, client=(ip, 50000))


@pytest.fixture()
def limiter_limpo(monkeypatch):
    """Isola `_rate_hits` (global do processo), tira o 429 do caminho e desliga a
    auth: o middleware de AUTH roda antes do limitador, então com chave inventada
    a resposta é 401 e o bucket nem é tocado (medido)."""
    antes = {k: v for k, v in app._rate_hits.items()}
    app._rate_hits.clear()
    for nome in ("_RATE_DEFAULT", "_RATE_POLL", "_RATE_HEAVY"):
        monkeypatch.setattr(app, nome, 10 ** 9)
    monkeypatch.setattr(app, "_apikeys", {"enabled": False, "keys": []})
    try:
        yield
    finally:
        app._rate_hits.clear()
        app._rate_hits.update(antes)


def test_rate_normaliza_o_id_do_path():
    n = app._rate_path_normalizado
    assert n("/api/tts/jobs/9f2b1c") == "/api/tts/jobs/*"
    assert n("/api/tts/jobs/9f2b1c/pieces/0") == "/api/tts/jobs/*/pieces/*"
    assert n("/api/voices/ab12cd/audio") == "/api/voices/*/audio"
    assert n("/api/outputs/ab12/audio") == "/api/outputs/*/audio"
    assert n("/api/apikeys/k1/rotate") == "/api/apikeys/*/rotate"
    assert n("/api/chat/9f2") == "/api/chat/*"
    # sub-rotas estáticas e paths livres ficam como estão
    assert n("/api/voices/import") == "/api/voices/import"
    assert n("/api/voices/design") == "/api/voices/design"
    assert n("/api/speaker/profiles") == "/api/speaker/profiles"
    assert n("/api/speaker/check") == "/api/speaker/check"
    assert n("/api/apikeys/enabled") == "/api/apikeys/enabled"
    assert n("/api/chat/start") == "/api/chat/start"
    assert n("/api/status") == "/api/status"
    assert n("/api/xyz/1/a") == "/api/xyz/1/a"


def test_rate_limite_continua_saindo_do_path_cru():
    """Normalizar é só para a CHAVE do bucket: o teto pesado de /api/voices/import
    não pode ser confundido com o de /api/voices/<id>."""
    assert app._rate_limit_for("/api/voices/import") == app._RATE_HEAVY
    assert app._rate_limit_for("/api/voices/ab12cd/audio") == app._RATE_DEFAULT
    assert app._rate_limit_for("/api/tts") == app._RATE_HEAVY
    assert app._rate_limit_for("/api/tts/jobs/9f2b1c") == app._RATE_POLL


def test_rate_um_bucket_so_para_muitos_ids_de_job(limiter_limpo):
    c = _limiter_de_rede()
    h = {"X-API-Key": "chave-do-cliente"}
    # job inexistente → 404 na rota, mas o bucket é contado no middleware
    codigos = {c.get(f"/api/tts/jobs/{i:010x}", headers=h).status_code for i in range(300)}
    assert codigos == {404}
    assert len(app._rate_hits) == 1
    assert list(app._rate_hits)[0] == (_identidade("chave-do-cliente"), "/api/tts/jobs/*")


def test_rate_poda_prefere_o_bucket_tocado_ha_mais_tempo(limiter_limpo, monkeypatch):
    """Evicção previsível e determinística: sai o mais antigo por último toque, e o
    bucket recente fica — sem depender do relógio de uma rajada."""
    monkeypatch.setattr(app, "_RATE_MAX_BUCKETS", 50)
    agora = time.monotonic()
    ident = _identidade("chave")
    for i in range(1, 51):                     # idades de 59 s (i=1) a 10 s (i=50)
        app._rate_hits[(ident, f"/api/livre/{i}")] = deque([agora - (60 - i)])
    app._rate_hits[(ident, "/api/livre/0")] = deque([agora])   # o mais recente
    assert len(app._rate_hits) == 51 > app._RATE_MAX_BUCKETS
    app._rate_poda(agora)
    assert len(app._rate_hits) <= 50
    assert (ident, "/api/livre/0") in app._rate_hits, "o recente não pode ser evituado"
    assert (ident, "/api/livre/1") not in app._rate_hits, "o mais antigo sai primeiro"


def test_rate_teto_de_buckets_nunca_estoura(limiter_limpo, monkeypatch):
    """Rajada de paths livres (não normalizáveis): o teto segura e nada leva 429."""
    monkeypatch.setattr(app, "_RATE_MAX_BUCKETS", 50)
    c = _limiter_de_rede()
    h = {"X-API-Key": "chave"}
    for i in range(200):
        assert c.get(f"/api/livre/{i}", headers=h).status_code == 404
    assert len(app._rate_hits) <= 50, "teto estourado"
    # a poda roda ANTES de pegar o bucket: a requisição atual nunca fica órfã
    assert (_identidade("chave"), "/api/livre/199") in app._rate_hits


def test_rate_evicao_nao_gera_429_e_identidade_ativa_continua_contando(limiter_limpo, monkeypatch):
    monkeypatch.setattr(app, "_RATE_MAX_BUCKETS", 30)
    monkeypatch.setattr(app, "_RATE_DEFAULT", 5)
    c = _limiter_de_rede()
    for i in range(100):                       # identidades descartáveis de passagem
        assert c.get("/api/livre", headers={"X-API-Key": f"efemera{i}"}).status_code == 404
    h = {"X-API-Key": "ativa"}
    codigos = [c.get("/api/livre", headers=h).status_code for _ in range(6)]
    assert codigos == [404] * 5 + [429], codigos


def test_csp_vai_no_header_e_com_nonce_no_script_inline(client):
    """#36 (header, não `<meta>`) + #76 (nonce por resposta no `<script>` inline,
    porque `script-src` ficou SEM 'unsafe-inline')."""
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy")
    assert "frame-ancestors 'none'" in csp
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "no-referrer"
    # o nonce do header é o MESMO da tag servida — sem isso o app não roda
    nonce_header = re.search(r"'nonce-([^\"']+)'", csp).group(1)
    nonce_tag = re.search(r'<script nonce="([^"]+)">', r.text).group(1)
    assert nonce_header == nonce_tag
    assert "'unsafe-inline'" not in csp.split("style-src")[0], "script-src sem unsafe-inline"
    assert "style-src 'self' 'unsafe-inline'" in csp, "style-src NÃO era para mudar"
    assert "'wasm-unsafe-eval'" in csp and "cdn.jsdelivr.net" in csp
    assert r.text.count("integrity=") == 2, "SRI dos bundles de CDN intacto"


def test_csp_nonce_muda_a_cada_resposta(client):
    n1 = re.search(r"'nonce-([^\"']+)'", client.get("/").headers["content-security-policy"]).group(1)
    n2 = re.search(r"'nonce-([^\"']+)'", client.get("/").headers["content-security-policy"]).group(1)
    assert n1 != n2


def test_csp_nonce_tambem_no_index_direto_e_sem_nonce_na_api(client, auth):
    r = client.get("/index.html")
    assert r.status_code == 200
    assert re.search(r'<script nonce="([^"]+)">', r.text)
    assert "'nonce-" in r.headers["content-security-policy"]
    # fora do SPA não há tag para casar: header sem nonce (e igual à policy base)
    assert client.get("/api/status", headers=auth).headers["content-security-policy"] == app._CSP_POLICY


def test_csp_do_header_igual_ao_meta_enquanto_os_dois_existem(client):
    """Transição: o frontend só remove o `<meta>` quando o header existir, e
    enquanto os dois existem a policy efetiva é a interseção — então os textos
    têm de ser idênticos (divergir aqui é bug silencioso)."""
    html = (app.BASE / "static" / "index.html").read_text()
    # fora os comentários: o próprio HTML documenta a remoção e pode citar o meta
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    achado = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', html)
    if achado is None:              # frontend já removeu o meta: nada a comparar
        return
    assert achado.group(1) == app._CSP_POLICY


def test_csp_em_rota_de_api_e_fora_dos_docs(client, auth):
    assert "content-security-policy" in client.get("/api/status", headers=auth).headers
    # /docs e /redoc montam a própria página (CSS do CDN) e ficam como sempre foram
    for path in ("/docs", "/redoc"):
        r = client.get(path, headers=auth)      # /docs exige chave fora do loopback
        assert r.status_code == 200, path
        assert "content-security-policy" not in r.headers, path
        assert r.headers.get("x-frame-options") == "DENY"


def test_csp_tambem_com_base_path(monkeypatch):
    """O middleware do base path roda antes: com /ttsproxy/ a policy continua
    saindo, COM nonce casando com a tag (o proxy não reescreve header)."""
    monkeypatch.setattr(app, "_BASE_PATH", "/ttsproxy")
    c = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    r = c.get("/ttsproxy/")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy")
    assert csp and "'nonce-" in csp
    assert re.search(r"'nonce-([^\"']+)'", csp).group(1) == \
        re.search(r'<script nonce="([^"]+)">', r.text).group(1)


def test_status_separa_modelo_carregado_do_configurado(client, auth, monkeypatch):
    """Acompanhamento do gate #23: `model` no /api/status é o do ÚLTIMO carregado
    (chave de uso com `model` próprio carrega o modelo dela sem trocar settings), e
    `model_settings` é o configurado — sem isso o painel confundia pedido com config."""
    antes = app._settings["model"]
    monkeypatch.setitem(app._model_state, "model", "outro/modelo-carregado")
    app._settings["model"] = "omnivoice"
    try:
        d = client.get("/api/status", headers=auth).json()
        assert d["model"] == "outro/modelo-carregado"  # verdade sobre a VRAM
        assert d["model_settings"] == "omnivoice"      # o que está configurado
    finally:
        # o fixture autouse já restaura `_settings`, mas o restore explícito não
        # depende dele (o teste pode ser chamado fora deste arquivo)
        app._settings["model"] = antes


def test_status_publica_o_tamanho_do_rate_limiter(client, auth):
    """Era invisível: sem métrica, o teto/memória do limitador só aparecia como
    "o app está com 1 GB" em caixa exposta."""
    d = client.get("/api/status", headers=auth).json()
    assert isinstance(d["rate_limit_buckets"], int)
    assert d["rate_limit_buckets_max"] == app._RATE_MAX_BUCKETS >= 100


def test_settings_clamp_e_restauracao(client):
    # arquivo isolado (cópia do real): captura os valores atuais e restaura no fim
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


def test_settings_400_nao_deixa_ram_divergindo_do_disco(client):
    """Campo inválido não pode aplicar os demais só na RAM.

    O apply mutate `_settings` campo a campo e só grava o disco no fim; antes, o
    primeiro 400 interrompia antes do _save_settings() e a config ficava valendo
    até o restart, para então sumir — lia-se como "esta configuração não salva".
    """
    auth = auth_headers(client)
    disco_antes = (app.SETTINGS_PATH.read_text()
                   if app.SETTINGS_PATH.exists() else None)
    beam_antes = client.get("/api/settings", headers=auth).json()["stt_beam"]

    payload = {"stt_beam": 3, "chat_extra": "reasoning=low"}    # o 2º é inválido
    r = client.post("/api/settings", headers=auth, json=payload)
    assert r.status_code == 400
    assert app._settings["stt_beam"] == beam_antes, "rollback: nada aplicado na RAM"
    assert client.get("/api/settings", headers=auth).json()["stt_beam"] == beam_antes
    disco_depois = (app.SETTINGS_PATH.read_text()
                    if app.SETTINGS_PATH.exists() else None)
    # Compara as CHAVES DO POST, não o arquivo inteiro: o contrato é "nada do
    # POST inválido vai ao disco". Outro escritor legítimo pode tocar o arquivo
    # no meio (thread de job, outra suíte em paralelo) e a comparação byte a byte
    # virava falso vermelho intermitente.
    a, d = _json.loads(disco_antes or "{}"), _json.loads(disco_depois or "{}")
    assert {k: d.get(k) for k in payload} == {k: a.get(k) for k in payload}, \
        "rollback: nada do POST inválido foi ao disco"

    # um save válido volta a persistir de fato (não só em memória)
    assert client.post("/api/settings", headers=auth, json={"stt_beam": 4}).status_code == 200
    assert _json.loads(app.SETTINGS_PATH.read_text())["stt_beam"] == 4


def test_settings_anti_ruido_persiste(client, auth):
    """As duas chaves novas de filtro têm de sobreviver ao restart (RAM == disco)."""
    r = client.post("/api/settings", headers=auth_headers(client),
                    json={"stt_anti_ruido": False, "stt_denoise": False})
    assert r.status_code == 200
    assert r.json()["stt_anti_ruido"] is False and r.json()["stt_denoise"] is False
    disco = _json.loads(app.SETTINGS_PATH.read_text())
    assert disco["stt_anti_ruido"] is False and disco["stt_denoise"] is False
    # devolve o ligado: o arquivo é cópia isolada, mas o teste não deixa rastro
    client.post("/api/settings", headers=auth,
                json={"stt_anti_ruido": True, "stt_denoise": True})


def test_rate_limit_isenta_loopback(client):
    """O navegador no próprio Mac polla a API em sub-segundo por design; o teto de
    120/min virava 429 interno (no log) e derrubava o polling do próprio job."""
    c = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    assert {c.get("/api/status").status_code for _ in range(140)} == {200}


def test_rate_limit_comporta_o_polling_da_ui(client, auth):
    # /api/tts/jobs/<id> é pollado a ~150 ms durante uma geração inteira, e começa
    # com "/api/tts" — não pode ser contado como geração pesada
    codigos = {client.get("/api/tts/jobs/limite-polling", headers=auth).status_code
               for _ in range(300)}
    assert 429 not in codigos


def test_rate_limit_geracao_continua_pesada(client, auth):
    # o POST /api/tts (gerar de fato) segue no teto baixo, mesmo com o polling liberado
    assert app._rate_limit_for("/api/tts") == app._RATE_HEAVY
    assert app._rate_limit_for("/api/tts/jobs/abc123") == app._RATE_POLL
    assert app._rate_limit_for("/v1/audio/speech") == app._RATE_HEAVY
    assert app._rate_limit_for("/api/settings") == app._RATE_DEFAULT


def test_rate_limit_continua_valendo_para_o_resto(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_RATE_DEFAULT", 5)
    codigos = [client.get("/api/tunnel/status", headers=auth).status_code for _ in range(12)]
    assert 429 in codigos


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


@pytest.fixture()
def export_tmp(tmp_path, monkeypatch):
    """voices/ e outputs/ em tmp: o export grava o zip num temporário no outputs."""
    vd, od = tmp_path / "voices", tmp_path / "outputs"
    vd.mkdir()
    od.mkdir()
    monkeypatch.setattr(app, "VOICES_DIR", vd)
    monkeypatch.setattr(app, "OUTPUTS_DIR", od)
    return vd, od


def _zip_do_export(client, vd):
    r = client.get("/api/voices/export", headers=auth_headers(client))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "tts-studio-vozes.zip" in r.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(r.content)), r


def test_export_vozes_streamado_zip_valido_sem_lixo(client, export_tmp):
    """#32: o zip é montado em DISCO e streamado; o conteúdo e o contrato do
    antigo (BytesIO) ficam iguais — e o temporário não sobra."""
    vd, od = export_tmp
    (vd / "v1.wav").write_bytes(b"RIFF" + b"\x00" * 5000)
    (vd / "v1.json").write_text(_json.dumps({"id": "v1", "name": "Voz 1"}))
    (vd / ".up-lixo").write_bytes(b"tmp")       # upload em curso: fora do zip
    (vd / ".rep-lixo").write_bytes(b"tmp")

    z, r = _zip_do_export(client, vd)
    assert z.testzip() is None
    assert sorted(z.namelist()) == ["voices/v1.json", "voices/v1.wav"]
    assert z.read("voices/v1.wav") == (vd / "v1.wav").read_bytes()
    # o zip viveu no disco: o TestClient roda o background e o temporário sai
    assert list(od.iterdir()) == [], "temporário do export ficou para trás"


def test_export_sem_vozes_continua_zip_valido(client, export_tmp):
    vd, od = export_tmp
    z, _ = _zip_do_export(client, vd)
    assert z.namelist() == []
    assert list(od.iterdir()) == []


def test_export_monta_o_zip_em_ARQUIVO_nao_em_memoria(client, export_tmp, monkeypatch):
    """Detector do ponto do #32: se voltar a montar em `BytesIO`, o pico de RAM
    (~= tamanho do zip) volta com ele. Espião no `ZipFile` vê o alvo da escrita."""
    vd, _ = export_tmp
    (vd / "v1.wav").write_bytes(b"RIFF" + b"\x00" * 1000)
    alvos = []
    original = zipfile.ZipFile

    def espiao(file, *a, **kw):
        alvos.append(file)
        return original(file, *a, **kw)

    monkeypatch.setattr(zipfile, "ZipFile", espiao)
    assert client.get("/api/voices/export", headers=auth_headers(client)).status_code == 200
    assert alvos, "o endpoint não abriu zip nenhum"
    assert not isinstance(alvos[0], io.BytesIO), "zip montado em memória (BytesIO)"


def test_export_falha_no_meio_nao_deixa_temporario(client, export_tmp, monkeypatch):
    """Falha ao zipar (disco cheio, arquivo some no meio) não pode deixar `.vozes-*`."""
    vd, od = export_tmp
    (vd / "v1.wav").write_bytes(b"RIFF")

    def _write_boom(self, filename, arcname=None, **kw):
        raise OSError("boom")

    monkeypatch.setattr(zipfile.ZipFile, "write", _write_boom)
    r = client.get("/api/voices/export", headers=auth_headers(client))
    assert r.status_code == 500                      # o endpoint re-levanta
    assert list(od.iterdir()) == [], "temporário ficou para trás na falha"


def test_export_nao_inclui_temporarios(client, voices_tmp):
    _voz_fake(voices_tmp)
    (voices_tmp / ".up-lixo.wav").write_bytes(b"\x00" * 32)     # upload crashado
    (voices_tmp / ".rep-lixo.wav").write_bytes(b"\x00" * 32)
    r = client.get("/api/voices/export", headers=auth_headers(client))
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "voices/testeabc123.wav" in names
    assert not any(n.startswith("voices/.") for n in names)


def _wav_bytes(segundos=1.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x01" * int(24000 * segundos))
    return buf.getvalue()


def _zip_de_voz(meta: dict, wav_name="v1.wav", json_name="v1.json"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(wav_name, _wav_bytes(0.2))
        z.writestr(json_name, _json.dumps(meta))
    return buf.getvalue()


def test_import_alinha_id_ao_nome_do_arquivo(client, voices_tmp, monkeypatch, tmp_path):
    """#37: id fora de [A-Za-z0-9_-] (ex.: `<img …>`, de backup à mão) deixava a voz
    na lista e 404 em /audio e /peaks (o `_safe_id` da rota é estrito). O import
    alinha o `id` ao stem — que já passou pela regex no nome do arquivo."""
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path / "out")
    (tmp_path / "out").mkdir()
    r = client.post("/api/voices/import", headers=auth_headers(client),
                    files={"zip_file": ("b.zip", _zip_de_voz(
                        {"id": '<img src=x onerror="x">', "name": "Exótica"}),
                        "application/zip")})
    assert r.status_code == 200, r.text
    assert r.json()["renomeados"] == [
        {"arquivo": "v1.json", "de": '<img src=x onerror="x">', "para": "v1"}]
    assert _json.loads((voices_tmp / "v1.json").read_text())["id"] == "v1"
    assert [v["id"] for v in app.list_voices() if v.get("id") == "v1"]
    for rota in ("/api/voices/v1/audio", "/api/voices/v1/peaks"):
        assert client.get(rota, headers=auth_headers(client)).status_code == 200, rota


def test_import_id_valido_mas_diferente_do_arquivo_tambem_alinhha(client, voices_tmp, monkeypatch, tmp_path):
    """Id válido, porém diferente do nome do arquivo, quebrava as MESMAS rotas
    (a UI lista `outra` e o caminho é montado como `outra.wav`)."""
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path / "out")
    (tmp_path / "out").mkdir()
    r = client.post("/api/voices/import", headers=auth_headers(client),
                    files={"zip_file": ("b.zip", _zip_de_voz({"id": "outra", "name": "V"}),
                                        "application/zip")})
    assert r.status_code == 200
    assert r.json()["renomeados"] == [{"arquivo": "v1.json", "de": "outra", "para": "v1"}]
    assert client.get("/api/voices/v1/audio", headers=auth_headers(client)).status_code == 200


def test_import_id_igual_ao_arquivo_nao_e_renomeado(client, voices_tmp, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path / "out")
    (tmp_path / "out").mkdir()
    r = client.post("/api/voices/import", headers=auth_headers(client),
                    files={"zip_file": ("b.zip", _zip_de_voz({"id": "v1", "name": "V"}),
                                        "application/zip")})
    assert r.status_code == 200 and r.json()["renomeados"] == []


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


def test_auth_ip_deste_mac_exige_chave(monkeypatch):
    # Um IP LAN do próprio Mac também exige chave: o túnel SSH reverso pode
    # usar esse IP como origem para uma requisição vinda da internet.
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False, client=("203.0.113.10", 50000))
    assert c.get("/api/status").status_code == 401


def test_auth_outro_ip_exige_chave_e_bloqueia_cadastro(monkeypatch):
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False, client=("203.0.113.99", 50000))
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


def _agentes_fake(tmp_path, monkeypatch, *, ssh=True, cloudflare=True):
    """Plistas temporárias p/ os agentes de acesso público (SSH e Cloudflare)."""
    ssh_p = tmp_path / "studio.tts.tunnel.plist"
    cf_p = tmp_path / "com.local.cloudflared-tts.plist"
    if ssh:
        ssh_p.write_text("<plist/>")
    if cloudflare:
        cf_p.write_text("<plist/>")
    monkeypatch.setattr(app, "_tunnel_plist", lambda: ssh_p)
    monkeypatch.setattr(app, "_cf_plist", lambda: cf_p)
    monkeypatch.setattr(app, "_LAUNCHCTL_ATRASO", 0)  # sem esperar o atraso real


def _aguarda_launchctl():
    """Espera as chamadas adiadas (thread) disparadas por stop/restart."""
    for t in list(app._launchctl_threads):
        t.join(timeout=2)
    app._launchctl_threads.clear()


def test_tunnel_start_stop_chamam_launchctl(client, auth, monkeypatch, tmp_path):
    chamadas = []

    def fake_run(args, **kw):
        chamadas.append(args)
        class R: returncode, stderr, stdout = 0, "", ""
        return R()

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    _agentes_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_launchd_loaded", lambda label: False)   # nada carregado
    monkeypatch.setattr(app, "_tunnel_proc_running", lambda: False)
    monkeypatch.setattr(app, "_cf_proc_running", lambda: False)
    r = client.post("/api/tunnel/stop", headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/api/tunnel/start", headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    _aguarda_launchctl()
    assert any("bootout" in c for c in chamadas) and any("bootstrap" in c for c in chamadas)
    # os dois caminhos (SSH e Cloudflare) são ligados/desligados juntos
    assert sum(1 for c in chamadas if "bootstrap" in c) == 2
    assert sum(1 for c in chamadas if "bootout" in c) == 2


def test_tunnel_start_agente_carregado_e_parado_usa_kickstart(client, auth, monkeypatch, tmp_path):
    chamadas = []

    def fake_run(args, **kw):
        chamadas.append(args)
        class R: returncode, stderr, stdout = 0, "", ""
        return R()

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    _agentes_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_launchd_loaded", lambda label: True)    # carregado, sem processo
    monkeypatch.setattr(app, "_tunnel_proc_running", lambda: False)
    monkeypatch.setattr(app, "_cf_proc_running", lambda: False)
    r = client.post("/api/tunnel/start", headers=auth)
    assert r.status_code == 200
    _aguarda_launchctl()
    # bootstrap em agente já carregado falha no launchctl — vai de kickstart
    assert sum(1 for c in chamadas if "kickstart" in c) == 2
    assert not any("bootstrap" in c for c in chamadas)


def test_tunnel_start_ja_rodando_nao_reinicia(client, auth, monkeypatch, tmp_path):
    chamadas = []

    def fake_run(args, **kw):
        chamadas.append(args)
        class R: returncode, stderr, stdout = 0, "", ""
        return R()

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    _agentes_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_launchd_loaded", lambda label: True)
    monkeypatch.setattr(app, "_tunnel_proc_running", lambda: True)
    monkeypatch.setattr(app, "_cf_proc_running", lambda: True)
    assert client.post("/api/tunnel/start", headers=auth).status_code == 200
    _aguarda_launchctl()
    # kickstart -k no que já está no ar derrubaria a resposta desta requisição
    assert not any("kickstart" in c for c in chamadas)


def test_tunnel_sem_agente_instalado(client, auth, monkeypatch, tmp_path):
    _agentes_fake(tmp_path, monkeypatch, ssh=False, cloudflare=False)
    for rota in ("/api/tunnel/start", "/api/tunnel/stop", "/api/tunnel/restart"):
        r = client.post(rota, headers=auth)
        assert r.status_code == 400, rota


def test_cors_expoe_retry_after_no_429(client, monkeypatch, voices_tmp, tmp_path):
    """`Retry-After` não é safelisted: sem `expose_headers` o cliente
    cross-origin não lê o tempo pedido no 429 e repete no escuro (o front usa 5 s
    de fallback justamente por isso). Aqui o 429 é o de admissão, com Origin de
    outro host e cliente de loopback (o limitador isenta loopback)."""
    solta = threading.Event()
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **kw: solta.wait(20))
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(app, "_JOBS_ACTIVE_MAX", 1)
    _voz_fake(voices_tmp, stem="voz-ativa")
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        c = TestClient(app.app, raise_server_exceptions=False,
                       client=("127.0.0.1", 50000))
        h = {"Origin": "http://localhost:5198"}
        corpo = {"text": "oi", "voice_id": "voz-ativa"}
        assert c.post("/api/tts", json=corpo, headers=h).status_code == 200
        r = c.post("/api/tts", json=corpo, headers=h)
        assert r.status_code == 429
        exposto = (r.headers.get("access-control-expose-headers") or "").lower()
        assert "retry-after" in exposto, exposto
        assert r.headers.get("access-control-allow-origin") == "*"
        assert r.headers.get("Retry-After") == "5"
    finally:
        solta.set()
        app._jobs.clear()
        app._jobs.update(orig)


def test_cors_nas_respostas_de_erro(client):
    """Cliente cross-origin (ex.: claudinhos no navegador) precisa LER o 401 —
    sem `access-control-allow-origin` na resposta o navegador entrega
    "Failed to fetch" em vez de "chave inválida" (o preflight passava porque
    quem respondia era o CORS, mas a resposta real saía de fora dele)."""
    r = client.get("/api/status", headers={"Origin": "http://localhost:5198"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "*"
    r = client.get("/api/status", headers={"Origin": "http://localhost:5198",
                                           "X-API-Key": "chave-errada"})
    assert r.status_code == 401 and r.headers.get("access-control-allow-origin") == "*"
    # preflight segue respondendo
    r = client.options("/api/status", headers={"Origin": "http://localhost:5198",
                                               "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200 and r.headers.get("access-control-allow-origin") == "*"


def test_tunnel_status_estrutura(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_tunnel_proc_running", lambda: True)
    monkeypatch.setattr(app, "_tunnel_launchd_loaded", lambda: True)
    monkeypatch.setattr(app, "_cf_proc_running", lambda: True)
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    monkeypatch.setattr(app, "_public_proxy_check", lambda url, timeout=6.0: {"ok": True, "latency_ms": 10})
    d = client.get("/api/tunnel/status?url=https://x/ttsproxy", headers=auth).json()
    assert d["tunnel_running"] is True and d["launchd_loaded"] is True
    assert d["cloudflared_running"] is True and d["cloudflared_loaded"] is False
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


def test_chat_entrega_entra_no_historico(client, auth, monkeypatch):
    """O texto entregue (final=true) precisa virar turno do assistente. Sem isso
    o histórico ficava com dois 'user' seguidos e sem registro da entrega, e na
    rodada seguinte o modelo reconfirmava o MESMO texto em vez de atender o
    pedido novo."""
    import time as _t
    vistos = []
    respostas = iter([
        '{"final": false, "reply": "Rascunho: ... Pode ser?"}',
        '{"final": true, "text": "Verificando a saude do Cluster."}',
        '{"final": false, "reply": "Rascunho novo: ... Pode ser?"}',
    ])

    def _llm(msgs):
        vistos.append([m["role"] for m in msgs if m["role"] != "system"])
        return next(respostas)

    monkeypatch.setattr(app, "_chat_llm", _llm)

    def _espera(sid):
        for _ in range(60):
            d = client.get(f"/api/chat/{sid}", headers=auth).json()
            if d["status"] != "thinking":
                return d
            _t.sleep(0.05)
        raise AssertionError("worker não respondeu")

    sid = client.post("/api/chat/start", headers=auth,
                      json={"objective": "Verifica a saude do Cluster."}).json()["session_id"]
    _espera(sid)
    client.post(f"/api/chat/{sid}", headers=auth, json={"message": "Sim."})
    d = _espera(sid)
    assert d["status"] == "confirmed"
    # a entrega está no histórico, com o texto entregue
    entregas = [m for m in d["messages"]
                if m["role"] == "assistant" and "Verificando a saude" in m["content"]]
    assert len(entregas) == 1

    client.post(f"/api/chat/{sid}", headers=auth,
                json={"message": "Agora envia. Por que você não conectou antes?"})
    d = _espera(sid)
    papeis = vistos[-1]
    assert all(a != b for a, b in zip(papeis, papeis[1:])), papeis
    assert papeis[-2] == "assistant"          # a entrega vem antes da fala nova
    assert d["status"] == "chatting"          # pedido novo abre rascunho novo
    client.delete(f"/api/chat/{sid}", headers=auth)


def test_tts_recusa_com_429_em_vez_de_descartar_job_ativo(client, auth, monkeypatch,
                                                          voices_tmp, tmp_path):
    """N > limite de ativos: o N+1º leva 429 e os N ativos seguem com status e
    trechos (o cliente não pode perder o stream no meio da geração).

    Antes: com todos os _JOBS_MAX em voo o evict não achava terminado para
    descartar e apagava o job mais antigo — rodando — junto do .job-*.
    """
    solta = threading.Event()
    vistos = []

    def _fake_job(job_id, *a, **kw):
        vistos.append(job_id)
        solta.wait(10)              # fica "running" até o teste mandar soltar

    monkeypatch.setattr(app, "_run_tts_job", _fake_job)
    # Trechos num dir PRÓPRIO: o `.job-*` do outputs/ real é apagado sem olhar
    # por qualquer outro processo que importe app.py (a limpeza órfã do boot é
    # incondicional) — e a suíte roda com o servidor vivo e outros agentes ao
    # lado. Sem isto a asserção de "trechos preservados" cai por terceiro.
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path)
    _voz_fake(voices_tmp, stem="voz-ativa")
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_ACTIVE_MAX", 2)
        corpo = {"text": "oi", "voice_id": "voz-ativa"}
        ids = []
        for _ in range(2):
            r = client.post("/api/tts", headers=auth, json=corpo)
            assert r.status_code == 200, r.text
            ids.append(r.json()["job_id"])
        for jid in ids:             # trechos do job ativo no disco
            pdir = app._piece_dir(jid)
            pdir.mkdir(exist_ok=True)
            (pdir / "0.wav").write_bytes(b"RIFF")

        r = client.post("/api/tts", headers=auth, json=corpo)
        assert r.status_code == 429
        assert "2/2" in r.json()["detail"] and r.headers["retry-after"] == "5"
        for jid in ids:
            # ativos intactos: status acessível e trechos preservados
            assert client.get(f"/api/tts/jobs/{jid}", headers=auth).json()["status"] == "running"
            assert (app._piece_dir(jid) / "0.wav").exists()
        assert vistos == ids        # o 429 não subiu thread
    finally:
        solta.set()
        for jid in ids:
            shutil.rmtree(app._piece_dir(jid), ignore_errors=True)
        app._jobs.clear()
        app._jobs.update(orig)


def test_21_pedidos_no_teto_padrao_recusam_so_o_21o(monkeypatch, voices_tmp, tmp_path):
    """Aceite da #22 no teto PADRÃO (20): 21 pedidos → 20 jobs aceitos e o 21º
    em 429, com os 20 ativos intactos no histórico.

    Cliente de loopback de propósito: o limitador de taxa isenta loopback, senão
    os últimos POSTs levariam 429 do limitador (outra mensagem) e o teste mediria
    a coisa errada."""
    solta = threading.Event()
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **kw: solta.wait(20))
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path)
    _voz_fake(voices_tmp, stem="voz-ativa")
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        c = TestClient(app.app, raise_server_exceptions=False,
                       client=("127.0.0.1", 50000))
        corpo = {"text": "oi", "voice_id": "voz-ativa"}
        ids, recusados = [], []
        for _ in range(app._JOBS_ACTIVE_MAX + 1):
            r = c.post("/api/tts", json=corpo)
            if r.status_code == 200:
                ids.append(r.json()["job_id"])
            else:
                recusados.append((r.status_code, r.json()["detail"]))
        assert len(ids) == app._JOBS_ACTIVE_MAX
        assert [c_ for c_, _ in recusados] == [429]
        assert f"({app._JOBS_ACTIVE_MAX}/{app._JOBS_ACTIVE_MAX})" in recusados[0][1]
        if not os.environ.get("TTS_JOBS_ACTIVE_MAX"):      # teto documentado
            assert app._JOBS_ACTIVE_MAX == 20
        assert all(i in app._jobs for i in ids)          # nenhum ativo descartado
        assert app._jobs_ativos() == app._JOBS_ACTIVE_MAX
    finally:
        solta.set()
        app._jobs.clear()
        app._jobs.update(orig)


def test_speech_queue_bypass(client, auth, monkeypatch, tmp_path):
    """queue=false tira a fala da fila do servidor (a Conversa faz o pipeline no
    navegador); sem a flag o contrato antigo continua usando a fila."""
    import wave
    vistos = []

    def _fake_job(job_id, text, voice_id, voice_path, language, omni,
                  model_override=None, use_queue=True):
        vistos.append(use_queue)
        oid = "t" + job_id
        wav = app.OUTPUTS_DIR / f"{oid}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 2400)
        app._jobs[job_id].update(status="done", output={"id": oid, "duration": 0.1})

    monkeypatch.setattr(app, "_run_tts_job", _fake_job)
    monkeypatch.setattr(app, "_resolve_voice", lambda v: "voz-teste")

    corpo = {"model": "tts-1", "input": "oi", "response_format": "wav"}
    assert client.post("/v1/audio/speech", headers=auth, json=corpo).status_code == 200
    assert client.post("/v1/audio/speech", headers=auth,
                       json={**corpo, "queue": False}).status_code == 200
    assert vistos == [True, False]


def test_tradutor_e_modificador_recusam_antes_do_stt(client, auth, monkeypatch):
    """Sem slot, o 429 sai antes do STT: o cliente não espera a transcrição (e a
    tradução) inteiras para receber um erro que já era previsível — nem reenvia
    o áudio depois."""
    monkeypatch.setattr(app, "_JOBS_ACTIVE_MAX", 1)
    monkeypatch.setattr(app, "_jobs_ativos", lambda: 1)     # sem slot livre

    def _stt_nao_devia_rodar(*a, **kw):
        raise AssertionError("STT rodou sem slot de job")

    monkeypatch.setattr(app, "_preparar_fala_para_voz", _stt_nao_devia_rodar)
    for rota in ("/api/translate-speech", "/api/modify-speech"):
        r = client.post(rota, headers=auth,
                        files={"audio": ("a.wav", b"RIFF0000", "audio/wav")})
        assert r.status_code == 429, rota
        assert "1/1" in r.json()["detail"] and r.headers["retry-after"] == "5"


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
    # _release_mlx_memory real faz "import mlx.core as mx" (common.py:383) e
    # inicializa o Metal — mesmo com mlx_whisper falso. Falso aqui para a suíte
    # não carregar MLX, mas registrando que o caminho whisper continua devolvendo
    # a memória ao SO (app.py:3853).
    liberou = []
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: liberou.append(1))
    antes = {m for m in sys.modules if m == "mlx" or m.startswith("mlx.")}
    app._settings["stt_local_engine"] = "whisper"
    r = app._transcribe(Path("x.wav"), language="pt", allow_remote=False)
    assert r["text"] == "w" and "engine" not in chamado
    assert liberou, "_transcribe (whisper) deve chamar _release_mlx_memory"
    depois = {m for m in sys.modules if m == "mlx" or m.startswith("mlx.")}
    assert depois == antes, f"caminho whisper carregou MLX: {sorted(depois - antes)}"
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
    monkeypatch.setitem(app._settings, "stt_anti_ruido", True)   # independe do settings.json real
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


def test_status_expoe_caminho_do_vad(client, auth, monkeypatch):
    """/api/status publica o caminho do VAD que está vivo: None enquanto não
    carregou e "onnx"/"torch-jit" depois. Sem isto o fallback para o torch só
    apareceria no stderr do servidor. Não carrega modelo (não puxa MLX/torch)."""
    monkeypatch.setattr(app, "_vad_backend", "")
    assert client.get("/api/status", headers=auth).json()["vad_backend"] is None

    monkeypatch.setattr(app, "_vad_backend", "onnx")
    assert client.get("/api/status", headers=auth).json()["vad_backend"] == "onnx"


def test_status_expoe_ocupacao_da_admissao(client, auth, monkeypatch):
    """429 por teto de ativos não se confunde com 429 do limitador de taxa:
    /api/status mostra ocupação e teto."""
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_ACTIVE_MAX", 7)
        app._jobs.update(a={"status": "running"}, b={"status": "done"})
        d = client.get("/api/status", headers=auth).json()
        assert d["jobs_active"] == 1 and d["jobs_active_max"] == 7
        assert d["jobs_history_max"] == app._JOBS_MAX
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


# ---------------------------------------------------------------------------
# /api/youtube-audio — allowlist de host
#
# `host.endswith("youtube.com")` puro aceitava "evil-youtube.com" e
# "youtube.com.evil.com" (domínios registráveis por terceiros, com outro
# conteúdo). Legítimo é o domínio exato ou subdomínio DELE — com o ponto.
# Além disso o yt-dlp pode sair da allowlist sozinho ao seguir um link de
# redirecionamento (cai no extractor genérico e baixa de outro site).
# ---------------------------------------------------------------------------

YT_URLS_OK = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=x",
    "https://m.youtube.com/watch?v=x",
    "https://music.youtube.com/watch?v=x",
    "https://www.youtube-nocookie.com/embed/x",
    "https://M.YOUTUBE-NOCOOKIE.COM/embed/x",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtube.com./watch?v=x",              # FQDN com ponto final
]

YT_URLS_IMPOSTORAS = [
    "https://evil-youtube.com/watch?v=x",
    "https://notyoutube.com/watch?v=x",
    "https://youtube.com.evil.com/watch?v=x",
    "https://youtu.be.evil.com/watch?v=x",
    "https://evil-youtu.be/watch?v=x",
    "https://evil-youtube-nocookie.com/embed/x",
    "https://youtube-nocookie.com.evil.io/embed/x",
    "https://youtube.com@evil.com/watch?v=x",      # userinfo não é host
    "ftp://youtube.com/watch?v=x",                 # esquema
]


def test_yt_host_allowed_tabela():
    for host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
                 "www.youtube-nocookie.com", "youtube-nocookie.com",
                 "YOUTUBE.COM", "youtube.com."):
        assert app._yt_host_allowed(host), host
    for host in ("evil-youtube.com", "youtube.com.evil.com", "youtu.be.evil.com",
                 "notyoutube.com", "youtube.co", "youtube.com.br", "youtu.b",
                 "", None, "   "):
        assert not app._yt_host_allowed(host), host


@pytest.mark.parametrize("url", YT_URLS_OK)
def test_youtube_audio_aceita_dominios_legitimos(client, auth, monkeypatch, url):
    monkeypatch.setattr(app, "_youtube_audio", lambda u, s, e: b"RIFF0000WAVE")
    r = client.post("/api/youtube-audio", headers=auth,
                    json={"url": url, "start": 0, "end": 5})
    assert r.status_code == 200, r.text
    assert r.content == b"RIFF0000WAVE"


@pytest.mark.parametrize("url", YT_URLS_IMPOSTORAS)
def test_youtube_audio_rejeita_host_parecido(client, auth, monkeypatch, url):
    chamadas = []
    monkeypatch.setattr(app, "_youtube_audio",
                        lambda u, s, e: chamadas.append(u) or b"RIFF0000WAVE")
    r = client.post("/api/youtube-audio", headers=auth,
                    json={"url": url, "start": 0, "end": 5})
    assert r.status_code == 400, r.text
    assert "YouTube" in r.json()["detail"]
    assert chamadas == [], "link impostor nem deveria chegar no yt-dlp"


def test_yt_final_host_le_as_variantes_do_info():
    """Host da página REALMENTE aberta — é o único lugar onde um
    redirecionamento para fora da allowlist aparece."""
    assert app._yt_final_host({"webpage_url": "https://www.youtube.com/watch?v=x"}) == \
        "www.youtube.com"
    assert app._yt_final_host({"original_url": "https://youtu.be/x"}) == "youtu.be"
    assert app._yt_final_host({"entries": [{"webpage_url": "https://m.youtube.com/watch?v=x"}]}) \
        == "m.youtube.com"
    assert app._yt_final_host({"id": "x"}) == ""      # sem URL: não bloqueia
    assert app._yt_final_host(None) == ""


def _fake_yt_dlp(monkeypatch, webpage_url):
    """yt_dlp falso: grava um WAV de 1 s no outtmpl (o ffmpeg real segue rodando)
    e devolve um info com a URL final que o teste quiser. Não vai à rede."""
    chamadas = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            chamadas.append(url)
            caminho = self.opts["outtmpl"].replace("%(ext)s", "wav")
            with wave.open(caminho, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(b"\x00\x00" * 24000)
            return {"webpage_url": webpage_url, "original_url": url}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    return chamadas


def test_youtube_audio_aceita_url_curta_do_youtube(monkeypatch):
    """youtu.be resolve para www.youtube.com: o reforço de host não pode derrubar
    o caminho legítimo (nem gastar as 4 tentativas de client)."""
    chamadas = _fake_yt_dlp(monkeypatch, "https://www.youtube.com/watch?v=abc")
    data = app._youtube_audio("https://youtu.be/abc", 0.0, 0.5)
    assert data[:4] == b"RIFF" and len(data) > 2000
    assert chamadas == ["https://youtu.be/abc"]


def test_youtube_audio_bloqueia_redirecionamento_para_fora(monkeypatch):
    chamadas = _fake_yt_dlp(monkeypatch, "https://naogravadora.com/v/abc")
    with pytest.raises(RuntimeError, match="saiu do YouTube"):
        app._youtube_audio("https://www.youtube.com/redirect?q=https://naogravadora.com/v/abc",
                           0.0, 2.0)
    assert len(chamadas) == 1, "não insiste nos outros clients: o host já resolveu"


def test_youtube_audio_nao_estoura_com_ipv6_malformada(client, auth, monkeypatch):
    """#44 (achado do gate #19): `urlparse("http://[::1")` levanta ValueError no
    PRÓPRIO urlparse — o endpoint devolvia 500 em vez de 400 "Use um link do YouTube"."""
    chamadas = []
    monkeypatch.setattr(app, "_youtube_audio",
                        lambda u, s, e: chamadas.append(u) or b"RIFF0000WAVE")
    for url in ("http://[::1", "http://[::1]:7860/x", "http://[::1]/watch?v=x"):
        r = client.post("/api/youtube-audio", headers=auth,
                        json={"url": url, "start": 0, "end": 2})
        assert r.status_code == 400, (url, r.status_code, r.text)
    assert chamadas == []
    assert app._yt_url_host("http://[::1") == ""
    assert app._url_parse("http://[::1") is None


def test_youtube_redirect_q_tambem_entra_na_allowlist(client, auth, monkeypatch):
    """#47: `youtube.com/redirect?q=<fora>` baixava de fora — o host final nem sempre
    aparece no info do yt-dlp, então a checagem pós-download passava batido. O alvo
    agora é validado ANTES do yt-dlp (400 imediato, sem baixar e descartar)."""
    chamadas = []
    monkeypatch.setattr(app, "_youtube_audio",
                        lambda u, s, e: chamadas.append(u) or b"RIFF0000WAVE")

    def post(url):
        chamadas.clear()
        return client.post("/api/youtube-audio", headers=auth,
                           json={"url": url, "start": 0, "end": 2})

    externo = post("https://www.youtube.com/redirect?q=https://evil.com/v")
    assert externo.status_code == 400 and "saiu do YouTube" in externo.json()["detail"]
    assert chamadas == [], "não pode baixar para depois descartar"
    assert post("https://www.youtube.com/redirect?q=https%3A%2F%2Fevil.com%2Fv").status_code == 400
    assert post("https://www.youtube.com/redirect?q=//evil.com/x").status_code == 400
    assert post("https://www.youtube.com/redirect?q=http://evil-youtube.com/v").status_code == 400

    for url in ("https://www.youtube.com/redirect?q=https://www.youtube.com/watch?v=abc",
                "https://www.youtube.com/redirect?q=/watch?v=abc",
                "https://www.youtube.com/redirect?q=javascript:void(0)",
                "https://www.youtube.com/redirect",
                "https://www.youtube.com/watch?v=abc"):
        r = post(url)
        assert r.status_code == 200, (url, r.status_code, r.text)
        assert chamadas == [url], url


def test_youtube_audio_400_quando_sai_da_allowlist(client, auth, monkeypatch):
    _fake_yt_dlp(monkeypatch, "https://naogravadora.com/v/abc")
    r = client.post("/api/youtube-audio", headers=auth,
                    json={"url": "https://www.youtube.com/redirect?q=https://naogravadora.com/v/abc",
                          "start": 0, "end": 2})
    assert r.status_code == 400, r.text
    assert "saiu do YouTube" in r.json()["detail"]
    assert "naogravadora.com" in r.json()["detail"]


# Ponto cego e decisão de projeto da checagem pós-download (gate #39): ela lê o
# host FINAL que o yt-dlp reporta. Se o info não trouxer esse host (ou trouxer só
# a própria URL de redirect, que é youtube.com), ela não vê o destino do `q=` e
# deixa passar — fail-OPEN, de propósito, para não reprovar link bom.

def test_yt_final_host_com_original_url_de_redirect_nao_ve_o_destino():
    """`original_url` é a URL PEDIDA: num link de redirect o host visto é
    youtube.com, não o destino. Só `webpage_url` (ou entries) denuncia o fora."""
    url = "https://www.youtube.com/redirect?q=https://naogravadora.com/v/abc"
    assert app._yt_final_host({"original_url": url}) == "www.youtube.com"
    assert app._yt_host_allowed("www.youtube.com")          # passa, mesmo indo p/ fora
    assert app._yt_final_host({"webpage_url": url, "original_url": url}) == "www.youtube.com"


def test_youtube_audio_info_sem_url_final_fail_open(monkeypatch):
    """Sem URL no info (`_yt_final_host` == ""), o download SEGUE: fail-open."""
    chamadas = []

    class FakeYDLSemUrl:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            chamadas.append(url)
            caminho = self.opts["outtmpl"].replace("%(ext)s", "wav")
            with wave.open(caminho, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                w.writeframes(b"\x00\x00" * 24000)
            return {"id": "abc"}                    # sem webpage_url/original_url

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDLSemUrl))
    assert app._yt_final_host({"id": "abc"}) == ""
    data = app._youtube_audio("https://www.youtube.com/redirect?q=https://naogravadora.com/v/abc",
                              0.0, 0.5)
    assert data[:4] == b"RIFF" and len(data) > 2000
    assert len(chamadas) == 1


# ---------------------------------------------------------------------------
# /api/settings — chave de USO x administração (#21)
#
# Gerar fala é uma coisa; repontar a instalação para outro provedor e ler a
# credencial dele é outra. Admin = loopback | TTS_ROD_ADMIN_KEY | chave com
# `role: admin`. Chave com `role: use` gera fala, mas não vê segredo nem troca
# conexão externa. Linha sem `role` (arquivo antigo) mantém a política antiga.
# ---------------------------------------------------------------------------

_SEGREDO_REMOTO = "sk-remoto-abcdefghij"
_SEGREDO_CHAT = "sk-chat-zyxwvutsrq"


@pytest.fixture()
def chave_de_uso(client, auth):
    """Cria uma chave `role=use` (via admin) e restaura `_apikeys` no fim."""
    snap_enabled = app._apikeys.get("enabled", True)
    snap_keys = [dict(k) for k in (app._apikeys.get("keys") or [])]
    try:
        r = client.post("/api/apikeys", headers=auth,
                        json={"name": "pytest-uso", "role": "use"})
        assert r.status_code == 200, r.text
        assert r.json()["key"]["role"] == "use"
        yield {"X-API-Key": r.json()["key"]["secret"]}
    finally:
        with app._apikeys_lock:
            app._apikeys = {"enabled": snap_enabled, "keys": [dict(k) for k in snap_keys]}
            app._save_apikeys()
        app.API_KEY = app._primary_api_key() or app._ENV_API_KEY or None


def test_settings_chave_de_uso_nao_le_segredo(client, auth, chave_de_uso):
    client.post("/api/settings", headers=auth,
                json={"remote_api_key": _SEGREDO_REMOTO, "chat_api_key": _SEGREDO_CHAT})
    admin = client.get("/api/settings", headers=auth).json()
    assert admin["is_admin"] is True
    assert admin["remote_api_key"] == _SEGREDO_REMOTO
    assert admin["chat_api_key"] == _SEGREDO_CHAT

    vista = client.get("/api/settings", headers=chave_de_uso).json()
    assert vista["is_admin"] is False
    assert vista["remote_api_key"] != _SEGREDO_REMOTO
    assert vista["remote_api_key"].startswith("••••")
    assert vista["remote_api_key"].endswith(_SEGREDO_REMOTO[-4:])   # dá p/ conferir qual é
    assert vista["chat_api_key"].endswith(_SEGREDO_CHAT[-4:])
    # o que não é segredo continua visível, e os campos de admin vêm etiquetados
    assert vista["pre_prompt"] == admin["pre_prompt"]
    assert {"remote_api_key", "model", "chat_base_url"} <= set(vista["admin_fields"])


def test_settings_chave_de_uso_nao_muda_conexao_externa(client, auth, chave_de_uso):
    antes = dict(app._settings)
    r = client.post("/api/settings", headers=chave_de_uso, json={
        "remote_base_url": "http://rtx:8000/v1", "remote_tts": True,
        "remote_tts_url": "http://rtx:8000", "model": "outro/modelo",
        "chat_api_key": "sk-nova-chave", "speed": 2.5, "pre_prompt": "cabeçalho",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["admin_ignored"]) == {
        "remote_base_url", "remote_tts", "remote_tts_url", "model", "chat_api_key"}
    # o que é de uso passou e foi persistido
    assert body["speed"] == 2.5 and body["pre_prompt"] == "cabeçalho"
    assert app._settings["speed"] == 2.5
    # as conexões ficaram intactas (não é 403: a UI manda um blob único no Salvar)
    for k in ("remote_base_url", "remote_tts", "remote_tts_url", "model", "chat_api_key"):
        assert app._settings[k] == antes[k], k


def test_settings_admin_e_loopback_aplicam_tudo(client, auth):
    payload = {"remote_base_url": "http://rtx:8000/v1", "remote_tts": True,
               "remote_tts_url": "http://rtx:8000", "model": "outro/modelo",
               "remote_api_key": _SEGREDO_REMOTO, "chat_api_key": "sk-nova", "speed": 1.75}
    body = client.post("/api/settings", headers=auth, json=payload).json()
    assert body.get("admin_ignored") == []
    assert body["is_admin"] is True
    for k, v in payload.items():
        assert app._settings[k] == v, k

    c = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    assert c.get("/api/settings").json()["is_admin"] is True      # loopback dispensa chave
    assert c.get("/api/settings").json()["remote_api_key"] == _SEGREDO_REMOTO


def test_settings_mascara_reenviada_mantem_o_segredo(client, auth, chave_de_uso):
    """A UI carrega o form (com a máscara) e reenvia o blob no Salvar: o valor
    "••••abcd" não pode sobrescrever a chave de verdade."""
    client.post("/api/settings", headers=auth, json={"remote_api_key": _SEGREDO_REMOTO})
    blob = client.get("/api/settings", headers=chave_de_uso).json()
    for k in ("is_admin", "admin_fields", "chat_system_default"):
        blob.pop(k, None)
    assert blob["remote_api_key"].startswith("••••")
    assert client.post("/api/settings", headers=chave_de_uso, json=blob).status_code == 200
    assert app._settings["remote_api_key"] == _SEGREDO_REMOTO
    # e o admin reenviando a máscara à mão também mantém
    client.post("/api/settings", headers=auth, json={"remote_api_key": "••••ghij"})
    assert app._settings["remote_api_key"] == _SEGREDO_REMOTO
    # limpar o campo (string vazia) segue limpando
    client.post("/api/settings", headers=auth, json={"remote_api_key": ""})
    assert app._settings["remote_api_key"] == ""


def test_settings_post_nao_devolve_segredo_em_claro_para_chave_de_uso(client, auth, chave_de_uso):
    """#61: a resposta do POST É as settings, então tem de passar pela MESMA vista do
    GET — antes a chave de uso lia o segredo cru no retorno do Salvar."""
    client.post("/api/settings", headers=auth,
                json={"remote_api_key": _SEGREDO_REMOTO, "remote_stt_key": _SEGREDO_CHAT})
    corpo = client.post("/api/settings", headers=chave_de_uso, json={"speed": 1.5}).json()
    assert corpo["is_admin"] is False and corpo["speed"] == 1.5
    for campo, cru in (("remote_api_key", _SEGREDO_REMOTO), ("remote_stt_key", _SEGREDO_CHAT)):
        assert corpo[campo] != cru, campo
        assert corpo[campo].startswith("••••") and corpo[campo].endswith(cru[-4:]), campo

    admin = client.post("/api/settings", headers=auth, json={"speed": 1.0}).json()
    assert admin["is_admin"] is True and admin["remote_api_key"] == _SEGREDO_REMOTO

    # reenviar a própria resposta mascarada (o que a UI faz ao reabrir o form)
    # não sobrescreve o segredo guardado
    client.post("/api/settings", headers=chave_de_uso, json=corpo)
    assert app._settings["remote_api_key"] == _SEGREDO_REMOTO
    assert app._settings["remote_stt_key"] == _SEGREDO_CHAT


def test_settings_env_admin_key_deixa_chave_comum_de_fora(monkeypatch):
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "adm-secreta-do-operador")
    c = TestClient(app.app, raise_server_exceptions=False, client=("203.0.113.9", 50000))
    comum = {"X-API-Key": app._primary_api_key()}
    assert c.get("/api/settings", headers=comum).json()["is_admin"] is False
    assert c.get("/api/settings",
                 headers={"X-API-Key": "adm-secreta-do-operador"}).json()["is_admin"] is True
    r = c.post("/api/settings", headers=comum, json={"model": "x/y", "speed": 1.25})
    assert r.json()["admin_ignored"] == ["model"] and r.json()["speed"] == 1.25


def test_apikeys_role_separa_admin_de_uso(client, auth):
    snap_enabled = app._apikeys.get("enabled", True)
    snap_keys = [dict(k) for k in (app._apikeys.get("keys") or [])]
    try:
        r = client.post("/api/apikeys", headers=auth, json={"name": "de uso", "role": "use"})
        assert r.status_code == 200
        kid, secret = r.json()["key"]["id"], r.json()["key"]["secret"]
        uso = {"X-API-Key": secret}

        assert client.get("/api/apikeys", headers=uso).json()["can_reveal"] is False
        assert client.post("/api/apikeys", headers=uso, json={"name": "x"}).status_code == 403
        assert client.get("/api/status", headers=uso).status_code == 200   # gera fala normalmente

        # PATCH promove a chave e ela volta a administrar
        r = client.patch(f"/api/apikeys/{kid}", headers=auth,
                         json={"name": "agora admin", "role": "admin"})
        assert r.status_code == 200 and r.json()["key"]["role"] == "admin"
        assert client.post("/api/apikeys", headers=uso, json={"name": "x"}).status_code == 200

        # PATCH é parcial (task #60): só `role` preserva o nome; só `name` preserva
        # o papel; `name` vazio conta como "não mexi no nome". A UI manda os dois.
        r = client.patch(f"/api/apikeys/{kid}", headers=auth, json={"role": "use"})
        assert r.status_code == 200 and r.json()["key"]["role"] == "use"
        assert r.json()["key"]["name"] == "agora admin"
        r = client.patch(f"/api/apikeys/{kid}", headers=auth, json={"name": "só nome"})
        assert r.status_code == 200 and r.json()["key"]["name"] == "só nome"
        assert r.json()["key"]["role"] == "use", "nome não pode mexer no papel"
        for corpo in ({"name": "", "role": "admin"}, {"name": "   ", "role": "admin"}):
            r = client.patch(f"/api/apikeys/{kid}", headers=auth, json=corpo)
            assert r.status_code == 200 and r.json()["key"]["role"] == "admin"
            assert r.json()["key"]["name"] == "só nome", corpo
        r = client.patch(f"/api/apikeys/{kid}", headers=auth,
                         json={"name": "os dois", "role": "use"})
        assert r.status_code == 200 and r.json()["key"]["name"] == "os dois"
        assert r.json()["key"]["role"] == "use"
        # role inválido continua 400 e não deixa nome pela metade
        assert client.patch(f"/api/apikeys/{kid}", headers=auth,
                            json={"name": "n", "role": "chefe"}).status_code == 400
        assert client.patch(f"/api/apikeys/{kid}", headers=auth,
                            json={"name": "n", "role": "chefe"}).status_code == 400
        assert client.get("/api/apikeys", headers=auth).json()["keys"][0]["name"] != "n"
    finally:
        with app._apikeys_lock:
            app._apikeys = {"enabled": snap_enabled, "keys": [dict(k) for k in snap_keys]}
            app._save_apikeys()
        app.API_KEY = app._primary_api_key() or app._ENV_API_KEY or None


def test_admin_key_autentica_e_sustenta_a_auth_sozinha(monkeypatch):
    """Achado da #21: com `TTS_ROD_ADMIN_KEY` setada, a chave admin não passava
    no middleware (401, `_key_is_valid` não a conhecia) e a chave comum não era
    admin (403) — nada administrava pela rede. Admin key é credencial: autentica
    E administra; e sozinha ela já basta para exigir chave da rede."""
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "adm-do-operador")
    monkeypatch.setattr(app, "_ENV_API_KEY", "")
    monkeypatch.setattr(app, "_apikeys", {"enabled": True, "keys": []})
    c = TestClient(app.app, raise_server_exceptions=False, client=("203.0.113.9", 50000))
    h = {"X-API-Key": "adm-do-operador"}
    assert app._auth_enabled() is True               # só com a env admin, rede não fica aberta
    assert c.get("/api/apikeys").status_code == 401
    assert c.get("/api/apikeys", headers=h).status_code == 200
    assert c.get("/api/apikeys", headers=h).json()["can_reveal"] is True
    assert c.post("/api/apikeys", headers=h, json={"name": "criada-pela-admin"}).status_code == 200
    # e a chave nova (sem role, legado) fica inútil como admin enquanto TTS_ROD_ADMIN_KEY existe
    nova = c.get("/api/apikeys", headers=h).json()["keys"][0]["masked"]
    assert nova
    assert c.get("/api/apikeys", headers=h).status_code == 200


def test_apikeys_sem_role_mantem_politica_antiga(client, auth):
    """Compat: linha criada antes do campo `role` (ou sem role explícito) e sem
    `TTS_ROD_ADMIN_KEY` continua administrando — nada muda para quem já usava."""
    assert app._ADMIN_API_KEY == ""
    assert app._key_role(app._primary_api_key()) is None
    # a chave legada segue podendo administrar (GET sem máscara + POST aplicando)
    assert client.get("/api/settings", headers=auth).json()["is_admin"] is True
    body = client.post("/api/settings", headers=auth,
                       json={"model": "legado/modelo"}).json()
    assert body.get("admin_ignored") == [] and app._settings["model"] == "legado/modelo"
