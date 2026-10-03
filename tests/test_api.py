"""Testes da camada HTTP (TestClient) — sem carregar modelos MLX.

Cobre a classe de bug que escapou antes (nomes indefinidos dentro de funções
que só rodam em runtime/request).
"""

import contextlib
import hashlib
import io
import json as _json
import logging
import re
import os
import shutil
import sys
import threading
import time
import types
import warnings
import wave
import zipfile
from collections import deque
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app


def _hash_dos_modulos(base=None) -> str:
    """sha256-8 do CONTEÚDO dos módulos do build, calculado de forma independente.

    Espelha `app._build_hash` (inclusive o `<ausente>` de módulo faltando), mas lê
    de `base` — os testes do /api/build apontam `app.BASE` para uma árvore de mentira
    e precisam do mesmo cálculo do lado de fora.
    """
    h = hashlib.sha256()
    raiz = Path(base) if base is not None else app.BASE
    for nome in app._BUILD_MODULOS:
        try:
            h.update((raiz / nome).read_bytes())
        except OSError:
            h.update(b"<ausente>")
    return h.hexdigest()[:8]


# #220: a expectativa do /api/build é CONGELADA NO IMPORT deste módulo, não
# recalculada no instante da asserção. `app._BUILD_CODIGO` nasce no import do app;
# este nasce microssegundos depois, na mesma fase de coleta. Recalcular na asserção
# fazia o par de testes ficar REFÉM DA ÁRVORE VIVA: a árvore é compartilhada por 6
# agentes, e um colega salvando um módulo no meio da rodada trocava o disco de agora
# — os dois testes caíam com uma "regressão do /api/build" que não existia (medido
# no gate do frontend em 20:09Z, com o audio-ml editando durante a rodada).
# Um save entre o import do app e o deste módulo ainda faz as duas expectativas
# divergirem: quem trata esse caso é a tolerância por mtime, em
# `_confere_que_o_codigo_e_do_boot` — e ela só é aceita quando PROVADA.
_HASH_NO_IMPORT = _hash_dos_modulos()


def _modulos_escritos_desde_o_boot(base=None) -> list:
    """Módulos do build escritos DEPOIS do hash do boot (#220).

    O critério é `mtime > app._BUILD_TS_HASH`: se alguém salvou o arquivo depois de o
    app hasheá-lo, o hash do boot pode não descrever o conteúdo de agora — e é isso
    que autoriza a tolerância do par, em vez de assumi-la. `mtime` e não comparação
    de conteúdo porque um escritor que volta ao estado anterior apagaria o rastro.
    """
    raiz = Path(base) if base is not None else app.BASE
    escritos = []
    for nome in app._BUILD_MODULOS:
        try:
            if (raiz / nome).stat().st_mtime > app._BUILD_TS_HASH:
                escritos.append(nome)
        except OSError:
            pass
    return escritos


def _confere_que_o_codigo_e_do_boot(d: dict) -> None:
    """O par de asserções do /api/build: conteúdo (#190) e boot (#214), imune à
    ÁRVORE VIVA (#220).

    Com ninguém tendo escrito desde o boot, a expectativa CONGELADA no import deste
    módulo tem de bater exatamente — é a prova de que o hash é do CONTEÚDO. Se
    alguém escreveu (mtime), a tolerância é PROVADA e o que se exige é que a rota
    siga o boot e não o disco de agora."""
    assert d["codigo"] == app._BUILD_CODIGO, "o campo tem de ser o hash do BOOT"
    escritos = _modulos_escritos_desde_o_boot()
    if escritos:
        print(f"    [220] escritos depois do boot: {', '.join(escritos)} — "
              f"tolerância provada por mtime")
        disco = _hash_dos_modulos()
        if disco != app._BUILD_CODIGO:
            assert d["codigo"] != disco, "a rota seguiu o disco de agora, não o boot"
    else:
        assert d["codigo"] == _HASH_NO_IMPORT, \
            "hash é do CONTEÚDO dos módulos (expectativa congelada no import)"


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
    # backend de IA por env: o env MANDA sobre o settings (#103/#176) e, sem isto,
    # um dono com `TTS_CHAT_BACKEND`/`TTS_CHAT_BACKEND_LIVE` exportado media a rota
    # dele nos testes do caminho stub (7 falsos vermelhos, gate #177).
    for var in ("TTS_CHAT_BACKEND", "TTS_CHAT_BACKEND_LIVE"):
        monkeypatch.delenv(var, raising=False)
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


def test_save_preserva_chave_de_build_desconhecida(client, auth, monkeypatch):
    """#197: o save reescreve o arquivo inteiro a partir da RAM — uma chave que ESTA
    build não conhece (gravada por uma build nova) não pode ser apagada por tabela."""
    monkeypatch.setattr(app, "_settings_desconhecidas_avisadas", set())
    disco = _json.loads(app.SETTINGS_PATH.read_text())
    disco["campo_de_build_nova"] = {"modo": "x"}
    disco.pop("stt_denoise")                    # e uma nossa ausente volta materializada
    app.SETTINGS_PATH.write_text(_json.dumps(disco))

    assert client.post("/api/settings", headers=auth,
                       json={"stt_anti_ruido": True}).status_code == 200
    depois = _json.loads(app.SETTINGS_PATH.read_text())
    assert depois["campo_de_build_nova"] == {"modo": "x"}, "chave desconhecida apagada"
    assert depois["stt_denoise"] == app._settings["stt_denoise"], \
        "chave nossa não foi materializada a partir da RAM"
    assert set(app._SETTINGS_DEFAULTS) <= set(depois), "save tirou chave conhecida"


def test_save_avisa_uma_vez_por_chave_desconhecida(client, auth, monkeypatch, capsys):
    """O aviso é sinal de instância/build desatualizada (par do `GET /api/build`) —
    e é UMA vez por chave, não a cada save."""
    monkeypatch.setattr(app, "_settings_desconhecidas_avisadas", set())
    disco = _json.loads(app.SETTINGS_PATH.read_text())
    disco["campo_de_build_nova"] = 1
    app.SETTINGS_PATH.write_text(_json.dumps(disco))

    app._save_settings()
    assert "campo_de_build_nova" in capsys.readouterr().err
    app._save_settings()
    assert "campo_de_build_nova" not in capsys.readouterr().err, "avisou de novo"


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
    app._settings["chat_backend"] = "openai"   # este teste é do CORPO da requisição
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
    app._settings["chat_backend"] = "openai"   # este teste é do CORPO da requisição
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


# ---------------------------------------------------------------------------
# Live (LIVE-1) — WS /api/live/ws: sessão, protocolo, auth e tetos.
# ---------------------------------------------------------------------------

@pytest.fixture()
def live_limpo(monkeypatch):
    """Zera o registro de sessões e fixa o caminho STUB (sem `live_pipeline`),
    que é o fallback suportado quando o módulo do LIVE-3 não está disponível."""
    monkeypatch.setattr(app, "_live_mod", None)
    monkeypatch.setattr(app, "_live_turns_mod", None)
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 0)   # telemetria só no teste dela
    with app._live_lock:
        app._live_sessions.clear()
    try:
        yield
    finally:
        with app._live_lock:
            app._live_sessions.clear()


def _ws_cliente(host="127.0.0.1"):
    """TestClient para WS: loopback por padrão (auth dispensada), outro host p/ testar chave."""
    return TestClient(app.app, raise_server_exceptions=False, client=(host, 50000))


@pytest.fixture()
def ws_client():
    """TestClient LOOPBACK: o `client` do módulo usa host 'testclient', que exige chave."""
    return _ws_cliente()


def _recv(ws, timeout=None):
    """Próximo frame (o WebSocketTestSession não tem timeout no receive)."""
    return ws.receive()


def _setup_ok(ws, **extra):
    ws.send_json({"type": "setup", **extra})
    return ws.receive_json()


def test_live_ws_loopback_abre_sessao_com_ready(ws_client, live_limpo):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        pronto = _setup_ok(ws)
        assert pronto["type"] == "ready"
        assert pronto["session_id"] and pronto["audio"]["sr"] == 24000
        assert pronto["in_audio"]["sr"] == 16000
        with app._live_lock:
            assert len(app._live_sessions) == 1


def test_setup_sem_voz_e_sem_vozes_grava_devolve_erro(ws_client, live_limpo, monkeypatch):
    """`_resolve_voice` levanta HTTPException (não ValueError): sem ramo próprio o
    handshake morria sem NENHUM frame — o cliente pendurava no receive e não sabia
    que faltava voz."""
    monkeypatch.setattr(app, "list_voices", lambda: [])
    with ws_client.websocket_connect("/api/live/ws") as ws:
        ws.send_json({"type": "setup"})
        erro = ws.receive_json()
        assert erro["code"] == "setup_invalido" and "voz" in erro["message"]
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_build_diz_qual_codigo_esta_rodando(client, auth):
    """#190: o `/api/build` é o detector de instância velha — o hash tem de vir do
    CONTEÚDO dos módulos (recalculado aqui de forma independente) e a instância velha
    é reconhecida justamente por não ter esta rota (404).

    #220: a expectativa é `_HASH_NO_IMPORT` (congelada no import deste módulo), não o
    disco do instante da asserção — é o que tira o par da dependência do timing."""
    r = client.get("/api/build", headers=auth)
    assert r.status_code == 200
    d = r.json()
    _confere_que_o_codigo_e_do_boot(d)
    assert d["version"] == app._VERSION
    assert d["admin_fields"] == len(app._SETTINGS_ADMIN)
    assert d["boot_ts"] > 0 and d["boot_ms"] >= 0
    assert "app.py" in d["modulos"] and "live_pipeline.py" in d["modulos"]
    assert client.get("/api/build").status_code == 401, "sob /api/ exige chave"


def test_build_par_nao_e_refem_da_arvore_viva(client, auth, tmp_path, monkeypatch):
    """#220: escrita de terceiro num módulo DEPOIS do import não derruba o par.

    É a mordida do ticket, e monta o cenário que o gate do frontend viveu (20:09Z,
    com o audio-ml editando durante a rodada): o disco de agora passa a ser OUTRO
    hash e o par tem de continuar valendo — antes a expectativa era recalculada no
    instante da asserção e os dois testes caíam."""
    for nome in app._BUILD_MODULOS:           # cópia preservando o mtime original
        shutil.copy2(app.BASE / nome, tmp_path / nome)
    monkeypatch.setattr(app, "BASE", tmp_path)
    (tmp_path / "live_turns.py").write_text("# escrito por um TERCEIRO depois do import\n")
    disco = _hash_dos_modulos(tmp_path)
    assert disco != _HASH_NO_IMPORT, \
        "cenário não montado: o disco de agora tem de diferir do import"
    assert "live_turns.py" in _modulos_escritos_desde_o_boot(tmp_path), \
        "o mtime do módulo escrito é o sinal que autoriza a tolerância"
    # o par, exercitado também pelo endpoint (é ele o alvo):
    d = client.get("/api/build", headers=auth).json()
    _confere_que_o_codigo_e_do_boot(d)
    assert d["admin_fields"] == len(app._SETTINGS_ADMIN)
    # o que a asserção ANTIGA (expectativa == disco de agora) diria neste cenário:
    assert disco != app._BUILD_CODIGO, \
        "com o disco de agora != boot, a asserção antiga teria ficado vermelha"


def test_build_codigo_e_do_boot_nao_do_disco_de_agora(client, auth, tmp_path, monkeypatch):
    """#214: o hash é CONGELADO no import. Editado um módulo depois (sem reiniciar),
    `/api/build` continua reportando o código CARREGADO — se ele fosse calculado no
    1º uso, uma instância que nunca serviu a rota daria "bate" falso contra a árvore
    e o gate acusaria o alvo errado (o próprio motivo de o #190 existir).

    #220: a asserção final compara com o hash do BOOT (`_BUILD_CODIGO`), que é do
    import — não com uma expectativa recalculada aqui."""
    for nome in app._BUILD_MODULOS:           # árvore de mentira = cópia do repo
        (tmp_path / nome).write_bytes((app.BASE / nome).read_bytes())
    monkeypatch.setattr(app, "BASE", tmp_path)
    # Sem pré-condição "árvore == boot": com 6 agentes editando a árvore, um módulo
    # pode mudar entre o import e aqui. O que este teste precisa é que `_build_hash`
    # LEIA O DISCO (e não cacheie) — é o que a mutação abaixo prova — e que a rota
    # siga o hash do BOOT.
    antes = app._build_hash()
    (tmp_path / "live_turns.py").write_text("# editado DEPOIS do import\n")
    assert app._build_hash() != antes, "o disco mudou de verdade"

    d = client.get("/api/build", headers=auth).json()
    assert d["codigo"] == app._BUILD_CODIGO, \
        "a instância reportou o disco de agora, não o código que carregou"
    assert d["codigo"] != _hash_dos_modulos(tmp_path), \
        "e o disco de agora é OUTRO hash: a rota não pode tê-lo seguido"


def test_build_exige_chave_fora_do_loopback():
    c = _ws_cliente("203.0.113.9")
    assert c.get("/api/build").status_code == 401
    assert c.get("/api/build", headers={"X-API-Key": app._primary_api_key()}).status_code == 200


def test_live_ws_exige_chave_fora_do_loopback(live_limpo):
    c = _ws_cliente("203.0.113.9")
    with c.websocket_connect("/api/live/ws") as ws:
        erro = ws.receive_json()
        assert erro["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401
    with c.websocket_connect(f"/api/live/ws?key={app._primary_api_key()}") as ws:
        assert _setup_ok(ws)["type"] == "ready"


def test_live_ws_setup_invalido_diz_o_campo(ws_client, live_limpo):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        ws.send_json({"type": "setup", "history": [{"role": "chefe", "text": "oi"}]})
        erro = ws.receive_json()
        assert erro["code"] == "setup_invalido" and "history[0].role" in erro["message"]
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_live_ws_voz_desconhecida_cai_no_padrao_e_vad_clampa(ws_client, live_limpo):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        pronto = _setup_ok(ws, voice_id="../fora", vad={"silence_ms": 99999, "prefix_ms": -5})
        assert pronto["type"] == "ready"
        assert pronto["vad"] == {"silence_ms": 5000, "prefix_ms": 0}
        assert pronto["voice_id"] == _resolve_voice_efetiva()


def _resolve_voice_efetiva():
    return app._resolve_voice(None)


def test_live_ws_audio_binario_e_turno(ws_client, live_limpo):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_bytes(b"\x00\x01" * 1600)          # 100 ms PCM16 16k
        ws.send_bytes(b"\x00\x01" * 800)
        ws.send_json({"type": "end_of_speech"})
        inicio = _ate(ws, {"speech_start"})
        fim = _ate(ws, {"turn_complete"})
        assert inicio["type"] == "speech_start" and fim["type"] == "turn_complete"
        sess = list(app._live_sessions.values())[0]
        assert sess["buffer_bytes_turno"] == (1600 + 800) * 2, "o áudio do cliente chegou"
        assert not sess["buffer"], "o buffer do turno é zerado quando ele fecha"
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "interrupted"


def test_live_ws_ping_pong_e_comando_desconhecido(ws_client, live_limpo):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_json({"type": "ping", "t": 42})
        assert ws.receive_json() == {"type": "pong", "t": 42}
        ws.send_json({"type": "pause"})
        assert ws.receive_json()["code"] == "comando"


def test_pipeline_que_nao_nasce_nao_prende_a_sessao(ws_client, live_limpo, monkeypatch):
    """`_live_pipe_novo` fora do try deixava a sessão presa no registry (sem task de
    envio, nada a fechava) e o processo dsh já criado órfão. Agora degrada."""
    def explode(sess):
        raise RuntimeError("boom")
    monkeypatch.setattr(app, "_live_pipe_novo", explode)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws)["type"] == "ready"
        erro = ws.receive_json()                  # a sessão avisa que ficou sem pipeline
        assert erro["code"] == "pipeline" and "boom" in erro["message"]
        ws.send_json({"type": "ping", "t": 7})    # e segue viva
        assert ws.receive_json() == {"type": "pong", "t": 7}
    time.sleep(0.2)
    assert not app._live_sessions, "sessão presa no registry"


def test_live_ws_teto_de_sessoes(ws_client, live_limpo, monkeypatch):
    """O teto é decidido no REGISTRO (atômico com a inserção), não no connect: a
    checagem antiga ficava antes do `await` do setup e duas conexões que passassem
    juntas furavam o limite. O cliente manda o `setup` e recebe `busy` + 1013."""
    monkeypatch.setattr(app, "_LIVE_MAX_SESSIONS", 1)
    with ws_client.websocket_connect("/api/live/ws") as ws1:
        _setup_ok(ws1)
        with ws_client.websocket_connect("/api/live/ws") as ws2:
            ws2.send_json({"type": "setup"})
            erro = ws2.receive_json()
            assert erro["code"] == "busy"
            with pytest.raises(WebSocketDisconnect) as exc:
                ws2.receive_json()
            assert exc.value.code == 1013


def test_busy_e_mandado_fora_do_live_lock(ws_client, live_limpo, monkeypatch):
    """#209: `_live_lock` é `threading.Lock` e o handler o pega por código
    SÍNCRONO (`_live_sweep`/`_live_hist_*`). Se o `error{busy}` sair com o lock
    preso e o send suspender, a outra corrotina bloqueia a thread do event loop
    e nada mais roda. Aqui o espião olha o estado do lock NO MOMENTO do send."""
    from starlette.websockets import WebSocket

    monkeypatch.setattr(app, "_LIVE_MAX_SESSIONS", 1)
    visto = []
    original = WebSocket.send_json

    async def espiao(self, dado, *a, **k):
        if isinstance(dado, dict) and dado.get("code") == "busy":
            visto.append(app._live_lock.locked())
        return await original(self, dado, *a, **k)

    monkeypatch.setattr(WebSocket, "send_json", espiao)
    with ws_client.websocket_connect("/api/live/ws") as ws1:
        _setup_ok(ws1)
        with ws_client.websocket_connect("/api/live/ws") as ws2:
            ws2.send_json({"type": "setup"})
            assert ws2.receive_json()["code"] == "busy"
    assert visto == [False], "o `error{busy}` saiu com o `_live_lock` preso"


def test_retomada_com_teto_cheio_nao_leva_busy(ws_client, live_limpo, pipeline_fake,
                                               engine_fake, hist_limpo, monkeypatch):
    """Retomada SUBSTITUI a entrada do `sid`: com o registry cheio ela não pode
    levar `busy` (era o que acontecia quando o teto era checado antes do setup)."""
    monkeypatch.setattr(app, "_LIVE_MAX_SESSIONS", 1)
    with ws_client.websocket_connect("/api/live/ws") as ws1:
        _setup_ok(ws1)
        sess = list(app._live_sessions.values())[0]
        sess["pipe"].history[:] = [{"role": "user", "content": "meu nome é Ana"}]
        app._live_hist_guarda(sess)               # registro existe -> retomável
        sid = sess["id"]
        with ws_client.websocket_connect("/api/live/ws") as ws2:
            pronto = _setup_ok(ws2, session_id=sid)
            assert pronto["type"] == "ready" and pronto["resumed"] is True
            assert pronto["session_id"] == sid


def test_sid_repetido_com_socket_vivo_substitui_a_antiga(ws_client, live_limpo,
                                                         monkeypatch):
    """#222: id do cliente JÁ vivo é reusado (reconexão que não fechou o socket
    antigo / dois clientes com o mesmo id). A entrada era sobrescrita e a sessão
    antiga continuava VIVA e FORA do registro: fora do sweep (nunca vencida, só
    morria se o cliente fechasse) e fora da contagem do teto — com teto 1, N
    sockets com o mesmo id passavam. Política: quem nasce depois manda (mesma
    regra do registro de retomada) e a antiga é fechada pelo servidor com motivo
    PRÓPRIO (`session_substituida`), não como "ociosa"."""
    monkeypatch.setattr(app, "_LIVE_MAX_SESSIONS", 1)
    with ws_client.websocket_connect("/api/live/ws") as ws1:
        sid = _setup_ok(ws1)["session_id"]
        antiga = list(app._live_sessions.values())[0]
        with ws_client.websocket_connect("/api/live/ws") as ws2:
            pronto = _setup_ok(ws2, session_id=sid)   # mesmo id e teto 1: sem busy
            assert pronto["type"] == "ready" and pronto["session_id"] == sid
            assert antiga["substituida"] is True, "a antiga não foi marcada para sair"
            with app._live_lock:
                assert len(app._live_sessions) == 1, "o teto foi furado pelo id repetido"
                assert app._live_sessions[sid] is not antiga, "a entrada não trocou de dono"
            antiga["visto"] = time.monotonic() - 999     # "ociosa" no papel
            assert app._live_sweep() == [], \
                "a substituída saiu do registro: o sweep por TTL não a alcança"
            # o MOTIVO não pode ser "ociosa": é outro caminho (a marcação acima
            # é o gatilho da task de envio) — sem esta linha, mutar o motivo
            # penduraria o `receive_json` abaixo em vez de falhar
            assert app._live_motivo_fechamento(antiga)[0] == "session_substituida"
            aviso = ws1.receive_json()                   # o servidor fecha a antiga
            assert aviso["code"] == "session_substituida"
            with pytest.raises(WebSocketDisconnect):
                ws1.receive_json()
            with app._live_lock:                         # o pop da antiga não leva a nova
                assert app._live_sessions[sid] is not antiga
            ws2.send_json({"type": "ping"})              # a nova segue atendendo
            assert _ate(ws2, {"pong"})["type"] == "pong"
    with app._live_lock:
        assert app._live_sessions == {}, "sessão presa no registro"


def test_live_ws_ttl_fecha_sessao_ociosa(ws_client, live_limpo, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_TTL_S", 30)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        sid = _setup_ok(ws)["session_id"]
        with app._live_lock:                        # simula ociosidade
            app._live_sessions[sid]["visto"] = time.monotonic() - 999
        assert app._live_sweep() == [sid]
        aviso = ws.receive_json()                   # a task de envio fecha a sessão
        assert aviso["code"] == "session_ttl"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with app._live_lock:
        assert app._live_sessions == {}


def test_live_ws_buffer_trunca_no_teto(ws_client, live_limpo, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_MAX_BUFFER", 4000)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        ws.send_bytes(b"\x00\x01" * 5000)           # 10000 bytes > teto
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, {"speech_start"})["type"] == "speech_start"
        fim = _ate(ws, {"turn_complete"})
        assert sess["buffer_bytes_turno"] <= app._LIVE_MAX_BUFFER, "buffer passou do teto"
        assert fim.get("truncated") is True, "o cliente tem de saber que foi cortado"
        assert sess["truncado"] is False, "o aviso vale por turno"


def test_live_ws_primeiro_frame_precisa_ser_setup(ws_client, live_limpo, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_SETUP_TIMEOUT_S", 0.2)
    c = _ws_cliente("203.0.113.9")
    with c.websocket_connect(f"/api/live/ws?key={app._primary_api_key()}") as ws:
        ws.send_bytes(b"\x00\x00")                  # binário antes do setup
        erro = ws.receive_json()
        assert erro["code"] == "setup_binario"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_live_ws_sem_setup_fecha_por_timeout(live_limpo, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_SETUP_TIMEOUT_S", 0.15)
    c = _ws_cliente("203.0.113.9")
    with c.websocket_connect(f"/api/live/ws?key={app._primary_api_key()}") as ws:
        erro = ws.receive_json()
        assert erro["code"] == "setup_timeout"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4408


def test_live_ws_end_of_speech_sem_audio_avisa(ws_client, live_limpo):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_json({"type": "end_of_speech"})
        assert ws.receive_json()["code"] == "sem_audio"


# ---------------------------------------------------------------------------
# LIVE-1.5 — ticket de um uso (chave fora da query string).
# ---------------------------------------------------------------------------

@pytest.fixture()
def tickets_limpos():
    with app._live_lock:
        app._live_sessions.clear()
        app._live_tickets.clear()
    try:
        yield
    finally:
        with app._live_lock:
            app._live_sessions.clear()
            app._live_tickets.clear()


def test_live_ticket_emite_e_consome_uma_vez(client, auth, tickets_limpos):
    """#98: o ticket vale UMA vez — o segundo WS com o mesmo ticket é 4401."""
    t = client.post("/api/live/ticket", headers=auth).json()
    assert t["expires_in"] == 60 and len(t["ticket"]) > 20

    c = _ws_cliente("203.0.113.9")              # fora do loopback: sem credencial nada passa
    with c.websocket_connect(f"/api/live/ws?ticket={t['ticket']}") as ws:
        assert _setup_ok(ws)["type"] == "ready"
    with c.websocket_connect(f"/api/live/ws?ticket={t['ticket']}") as ws:
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_ticket_usado_cai_para_a_chave(client, auth, tickets_limpos):
    """#212: o ticket não é o ÚLTIMO recurso — usado/expirado/inventado, a checagem
    segue para `?key=`/header. Antes o `return` cortava e um cliente com credencial
    boa levava 4401 só porque repetiu o parâmetro."""
    t = client.post("/api/live/ticket", headers=auth).json()
    chave = app._primary_api_key()
    c = _ws_cliente("203.0.113.9")
    with c.websocket_connect(f"/api/live/ws?ticket={t['ticket']}&key={chave}") as ws:
        assert _setup_ok(ws)["type"] == "ready"          # 1ª: o ticket vale
    with c.websocket_connect(f"/api/live/ws?ticket={t['ticket']}&key={chave}") as ws:
        assert _setup_ok(ws)["type"] == "ready", "ticket usado cortou a chave válida"
    with c.websocket_connect(f"/api/live/ws?ticket=inventado&key={chave}") as ws:
        assert _setup_ok(ws)["type"] == "ready", "ticket inválido cortou a chave válida"


def test_live_ticket_expirado_recusa(tickets_limpos):
    ticket = app._live_ticket_emite()
    with app._live_lock:                        # envelhece o ticket
        app._live_tickets[ticket] = time.monotonic() - 1
    c = _ws_cliente("203.0.113.9")
    with c.websocket_connect(f"/api/live/ws?ticket={ticket}") as ws:
        assert ws.receive_json()["code"] == "unauthorized"


def test_live_ticket_nao_vaza_memoria(tickets_limpos, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_TICKET_MAX", 5)
    emitidos = [app._live_ticket_emite() for _ in range(20)]
    assert len(app._live_tickets) <= 5
    assert emitidos[-1] in app._live_tickets           # o mais novo sobrevive
    assert emitidos[0] not in app._live_tickets        # o mais antigo saiu


def test_live_ticket_limpeza_por_ttl(tickets_limpos):
    t1, t2 = app._live_ticket_emite(), app._live_ticket_emite()
    with app._live_lock:
        app._live_tickets[t1] = time.monotonic() - 1
    app._live_ticket_limpa()
    assert t1 not in app._live_tickets and t2 in app._live_tickets
    assert not app._live_ticket_consome("ticket-que-nao-existe")
    assert not app._live_ticket_consome("")


# ---------------------------------------------------------------------------
# LIVE-3a — adapter do pipeline no `_live_engine` (módulo real, fakes nos modelos).
# ---------------------------------------------------------------------------

@pytest.fixture()
def pipeline_fake(monkeypatch):
    """Pipeline real com stt/llm/tts/prewarm fakes (sem MLX)."""
    import live_pipeline
    monkeypatch.setattr(app, "_live_mod", live_pipeline)   # (o `live_limpo` zera)
    monkeypatch.setattr(live_pipeline, "_prewarm_app", lambda: None)
    monkeypatch.setattr(live_pipeline, "_stt_app", lambda pcm, lang: "que horas são")
    monkeypatch.setattr(live_pipeline, "_llm_stream_app",
                        lambda msgs: iter(["São ", "dez ", "horas."]))
    # `**kw`: o pipeline pode passar contexto extra (ex.: voice_id) para o TTS
    monkeypatch.setattr(live_pipeline, "_tts_app",
                        lambda txt, omni, **kw: np.zeros(480, dtype=np.float32))
    return live_pipeline


def _ate(ws, tipos, maximo=15):
    """Lê até um dos tipos (ignora áudio e eventos de SESSÃO/STREAM, ex.: `prewarm`).

    `maximo` conta eventos úteis, não frames crus: no backend dsh o LLM entrega
    delta por TOKEN (um `assistant_text` por token, cada um com áudio) e contar
    frame cru estourava o teto antes do `turn_complete` (task_6db0e2cc). O erro
    lista o que passou, para um vermelho aqui dizer o que o servidor mandou."""
    vistos = []
    while len(vistos) < maximo:
        m = _recv(ws)
        if m.get("bytes"):
            continue
        ev = _json.loads(m["text"])
        if ev["type"] in ("stats", "assistant_text", "latency") and ev["type"] not in tipos:
            continue
        vistos.append(ev["type"])
        if ev["type"] in tipos:
            return ev
    raise AssertionError(f"não veio nenhum de {tipos} (veio: {vistos})")


def _le_turno(ws, maximo=12):
    """Lê o turno até o fim dele (`turn_complete`, `error` ou `interrupted`).

    `interrupted` é terminal para o turno: sem tratá-lo aqui o `receive()` do
    teste de cancel ficava bloqueado esperando um `turn_complete` que não vem —
    até o TTL da sessão (300 s de suíte por causa de um teste).

    `maximo` conta EVENTOS DE CONTROLE, não deltas: no backend dsh o texto vem
    delta por token, então um turno carrega dezenas de `assistant_text` e o teto
    ficava estourado antes do fim do turno (task_6db0e2cc)."""
    eventos, audio, controles = [], 0, 0
    while controles < maximo:
        m = ws.receive()
        if m.get("bytes"):
            audio += len(m["bytes"])
            continue
        ev = _json.loads(m["text"])
        if ev["type"] in ("prewarm", "stats"):   # eventos de sessão, não do turno
            continue
        if ev["type"] != "assistant_text":
            controles += 1
        eventos.append(ev)
        if ev["type"] in ("turn_complete", "error", "interrupted"):
            break
    return eventos, audio


def test_live_pipeline_roda_o_turno_e_manda_audio(ws_client, live_limpo, pipeline_fake):
    """O gancho `_live_engine` vira o pipeline: eventos do protocolo + áudio 24k."""
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws, history=[{"role": "user", "text": "oi"}])["type"] == "ready"
        sess = list(app._live_sessions.values())[0]
        assert sess["pipe"] is not None
        ws.send_bytes(b"\x00\x01" * 1600)
        ws.send_json({"type": "end_of_speech"})
        eventos, audio = _le_turno(ws)
        tipos = [e["type"] for e in eventos]
        assert "speech_start" in tipos and tipos[-1] in ("turn_complete", "error"), tipos
        assert "transcript_user" in tipos and "assistant_text" in tipos
        assert audio > 0, "o áudio sintetizado tem de sair como binário"
        # contexto: o replay do setup entra no prompt e o turno é anexado
        assert [h["role"] for h in sess["pipe"].history] == ["user", "user", "assistant"]


def test_live_pipeline_sem_audio_avisa_pelo_pipeline(ws_client, live_limpo, pipeline_fake):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, {"error"})["code"] == "sem_audio"


def test_live_pipeline_cancel_e_interrupted(ws_client, live_limpo, pipeline_fake, monkeypatch):
    """`cancel` derruba o pipeline e o cliente recebe `interrupted` (barge-in)."""
    import live_pipeline as lp
    monkeypatch.setattr(lp, "_llm_stream_app",
                        lambda msgs: (time.sleep(0.3) or d for d in ["a", "b"]))
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_bytes(b"\x00\x01" * 1600)
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, {"speech_start"})["type"] == "speech_start"
        ws.send_json({"type": "cancel"})
        eventos, _ = _le_turno(ws)
        assert any(e["type"] == "interrupted" for e in eventos), [e["type"] for e in eventos]


def test_live_sem_modulo_cai_no_stub(ws_client, live_limpo):
    """Fallback: sem `live_pipeline`, o stub antigo segue atendendo o protocolo."""
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_bytes(b"\x00\x01" * 1600)
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, {"speech_start"})["type"] == "speech_start"
        assert _ate(ws, {"turn_complete"})["stub"] is True


# ---------------------------------------------------------------------------
# LIVE-2 (FSM de turnos) costurada no handler: eventos, flush e set_speaking.
# ---------------------------------------------------------------------------

class _EvFake:
    def __init__(self, tipo, audio=b"", **kw):
        self.tipo, self.audio = tipo, audio
        self.t_ms, self.ts, self.amostra = 0, 0.0, 0
        self.t_decisao_ms, self.prob, self.rms_dbfs = 0, 0.0, -120.0
        self.curto, self.barge_in = kw.get("curto", False), kw.get("barge_in", False)
        self.fala_ms, self.barge_falso, self.detalhe = kw.get("fala_ms", 0), False, ""

    def to_json(self):
        d = {"type": self.tipo, "t_ms": self.t_ms, "curto": self.curto,
             "barge_in": self.barge_in}
        if self.tipo == "speech_end":
            d["fala_ms"] = self.fala_ms
        if self.audio:
            d["audio_bytes"] = len(self.audio)
        return d


class _EngineFake:
    """FSM falsa: `feed`/`flush` devolvem eventos roteirizados e registram chamadas."""

    roteiro: list = []
    calls: list = []

    def __init__(self, config=None):
        self.config = config

    def feed(self, pcm):
        _EngineFake.calls.append(("feed", len(pcm)))
        return list(_EngineFake.roteiro)

    def flush(self):
        _EngineFake.calls.append(("flush", 0))
        return list(_EngineFake.roteiro)

    def cancel(self):
        _EngineFake.calls.append(("cancel", 0))
        return {}

    def set_speaking(self, ligado, nivel_dbfs=None, duracao_ms=None):
        # `duracao_ms` é o que a janela de playback passou a mandar junto do ENVIO
        # do chunk (#167); sem aceitar o kwarg o handler morria no meio e o turno
        # nunca emitia `turn_complete` (a suíte parecia TRAVAR, não falhar).
        # Registrado DE PROPÓSITO: um dublê que só engole o kwarg novo esconde o
        # PRÓXIMO igual a este — o teste abaixo assere que a duração chega.
        _EngineFake.calls.append(("set_speaking", ligado, nivel_dbfs, duracao_ms))


@pytest.fixture()
def engine_fake(monkeypatch):
    """Injeta a FSM falsa no lugar de `live_turns` e limpa o roteiro."""
    _EngineFake.roteiro, _EngineFake.calls = [], []
    mod = types.SimpleNamespace(TurnEngine=_EngineFake, Config=lambda **kw: kw)
    monkeypatch.setattr(app, "_live_turns_mod", mod)
    return _EngineFake


def test_live_fsm_eventos_viram_protocolo(ws_client, live_limpo, engine_fake):
    engine_fake.roteiro = [_EvFake("speech_start")]
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_bytes(b"\x00\x01" * 1600)
        ev = _json.loads(_recv(ws)["text"])
        assert ev["type"] == "speech_start"
    assert ("feed", 3200) in engine_fake.calls


def test_live_fsm_speech_end_abre_turno_com_o_audio_do_evento(ws_client, live_limpo, engine_fake):
    engine_fake.roteiro = [_EvFake("speech_start"),
                           _EvFake("speech_end", audio=b"\x07\x00" * 800, fala_ms=400, curto=True)]
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_bytes(b"\x00\x01" * 1600)
        assert _json.loads(_recv(ws)["text"])["type"] == "speech_start"
        fim = _json.loads(_recv(ws)["text"])
        assert fim["type"] == "speech_end" and fim["curto"] is True
        # o turno abre com o áudio do EVENTO (pré-roll), não com o do handler
        assert _ate(ws, {"turn_complete"})["type"] == "turn_complete"


def test_live_fsm_end_of_speech_do_cliente_chama_flush(ws_client, live_limpo, engine_fake):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_json({"type": "end_of_speech"})
        for _ in range(40):                      # o handler roda em outra task
            if any(c[0] == "flush" for c in engine_fake.calls):
                break
            time.sleep(0.05)
        assert any(c[0] == "flush" for c in engine_fake.calls)


def test_live_fsm_barge_in_derruba_o_turno(ws_client, live_limpo, engine_fake):
    engine_fake.roteiro = [_EvFake("barge_in")]
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_bytes(b"\x00\x01" * 1600)
        assert _json.loads(_recv(ws)["text"])["type"] == "barge_in"
        assert _json.loads(_recv(ws)["text"])["type"] == "interrupted"


def test_live_set_speaking_marca_o_playback(ws_client, live_limpo, engine_fake):
    """Obrigação do handler: marcar/desmarcar o playback — daí sai o limiar do eco."""
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        _live_envia_audio = app._live_envia_audio
        _live_envia_audio(sess, b"\x10\x27" * 480)      # 2 chunks de áudio
        _live_envia_audio(sess, b"\x10\x27" * 480)
        for _ in range(40):
            if any(c[0] == "set_speaking" for c in engine_fake.calls):
                break
            time.sleep(0.05)
        marcas = [c for c in engine_fake.calls if c[0] == "set_speaking"]
        assert marcas and marcas[0][1] is True, "precisa marcar speaking ao enviar"
        assert marcas[0][2] is not None, "o nivel_dbfs do chunk alimenta o limiar do eco"
        assert marcas[0][3] and marcas[0][3] > 0, \
            "a duracao_ms do chunk tem de ir junto (#167) — é o que a janela usa"
        for _ in range(40):
            if any(len(c) > 1 and c[1] is False for c in marcas):
                break
            time.sleep(0.05)
            marcas = [c for c in engine_fake.calls if c[0] == "set_speaking"]
        assert marcas[-1][1] is False, "precisa desmarcar quando a fila de áudio zera"


def test_live_truncated_no_turn_complete_do_pipeline(ws_client, live_limpo, pipeline_fake, monkeypatch):
    """O truncamento da sessão chega ao cliente no `turn_complete` (estado da sessão;
    o `buffer_bytes` em si é do pipeline)."""
    monkeypatch.setattr(app, "_LIVE_MAX_BUFFER", 1000)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        ws.send_bytes(b"\\x00\\x01" * 2000)          # 4000 bytes > teto de 1000
        ws.send_json({"type": "end_of_speech"})
        eventos, _ = _le_turno(ws)
        fim = [e for e in eventos if e["type"] == "turn_complete"][0]
        assert fim["truncated"] is True
        assert sess["buffer_bytes_turno"] <= 1000
        ws.send_bytes(b"\\x00\\x01" * 100)           # próximo turno cabe
        ws.send_json({"type": "end_of_speech"})
        eventos, _ = _le_turno(ws)
        assert [e for e in eventos if e["type"] == "turn_complete"][0]["truncated"] is False


def test_live_turno_curto_abre_o_turno_com_o_flag_de_barge_in(ws_client, live_limpo,
                                                               engine_fake, pipeline_fake):
    """Decisão do PM: o descarte saiu do pré-STT — `curto` é dica e o turno SEMPRE
    abre; quem decide eco x humano é o pipeline, a partir do flag `barge_in`."""
    import inspect
    import live_pipeline
    assert "barge" in inspect.signature(live_pipeline.LivePipeline.end_of_speech).parameters, \
        "o nome do kwarg do pipeline mudou: alinhe a chamada em `_live_abre_turno`"
    engine_fake.roteiro = [_EvFake("speech_end", audio=b"\x01\x00" * 400,
                                   fala_ms=180, curto=True, barge_in=True)]
    vistos = []
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]

        # assinatura EXPLÍCITA (sem **k): um kwarg com nome trocado estoura aqui,
        # em vez de virar "flag nunca chega" silencioso (regressão do #97)
        def _eos(*a, barge=False, **k):
            vistos.append(barge)
            app._live_envia_json(sess, {"type": "turn_complete", "descartado": True,
                                        "curto": True})
            return True

        sess["pipe"].end_of_speech = _eos
        ws.send_bytes(b"\x00\x01" * 1600)
        fim = _ate(ws, {"turn_complete"})
        assert vistos == [True], "o flag do evento tem de chegar ao pipeline"
        assert sess["turno_barge_in"] is True and sess["turno_curto"] is True
        assert fim["descartado"] is True, "o descarte agora vem do pipeline"
        assert sess["descartados"] == 1, "o descarte do pipeline é contado"


def test_live_t_decisao_ms_registrado_para_o_orcamento(ws_client, live_limpo, engine_fake):
    """`t_ms` é retroagido; a latência real da decisão é `t_decisao_ms`."""
    ev = _EvFake("speech_end", audio=b"\\x01\\x00" * 800, fala_ms=400)
    ev.t_decisao_ms = 1234
    engine_fake.roteiro = [ev]
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        ws.send_bytes(b"\\x00\\x01" * 1600)
        assert _ate(ws, {"turn_complete"})["type"] == "turn_complete"
        assert sess["t_decisao_ms"] == 1234


# ---------------------------------------------------------------------------
# #103 — override por ambiente do provedor de chat (teste/smoke não grava no
# settings real; incidente do stub de 2026-09-25).
# ---------------------------------------------------------------------------

def test_chat_provider_env_tem_precedencia(monkeypatch):
    monkeypatch.setitem(app._settings, "chat_base_url", "http://settings.example/v1")
    monkeypatch.setitem(app._settings, "chat_model", "modelo-do-arquivo")
    monkeypatch.setitem(app._settings, "chat_api_key", "chave-do-arquivo")
    monkeypatch.setenv("TTS_CHAT_BASE_URL", "http://127.0.0.1:53992/v1")
    monkeypatch.setenv("TTS_CHAT_MODEL", "stub-live")
    monkeypatch.setenv("TTS_CHAT_API_KEY", "chave-do-stub")
    assert app._chat_provider() == ("http://127.0.0.1:53992/v1", "stub-live", "chave-do-stub")


def test_chat_provider_env_e_por_campo(monkeypatch):
    """Setar só um campo não derruba os outros (o resto continua vindo do arquivo)."""
    monkeypatch.setitem(app._settings, "chat_base_url", "http://settings.example/v1")
    monkeypatch.setitem(app._settings, "chat_model", "modelo-do-arquivo")
    monkeypatch.delenv("TTS_CHAT_BASE_URL", raising=False)
    monkeypatch.delenv("TTS_CHAT_API_KEY", raising=False)
    monkeypatch.setenv("TTS_CHAT_MODEL", "stub-live")
    base, model, _ = app._chat_provider()
    assert base == "http://settings.example/v1" and model == "stub-live"


def test_chat_provider_sem_env_segue_a_cadeia_antiga(monkeypatch):
    for var in ("TTS_CHAT_BASE_URL", "TTS_CHAT_MODEL", "TTS_CHAT_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(app._settings, "chat_base_url", "")
    monkeypatch.setitem(app._settings, "remote_base_url", "http://traducao.example/v1")
    monkeypatch.setitem(app._settings, "chat_model", "")
    monkeypatch.setitem(app._settings, "remote_translate_model", "gpt-4o-mini")
    base, model, _ = app._chat_provider()
    assert base == "http://traducao.example/v1" and model == "gpt-4o-mini"


def test_chat_provider_env_vazio_nao_conta(monkeypatch):
    monkeypatch.setitem(app._settings, "chat_base_url", "http://settings.example/v1")
    monkeypatch.setenv("TTS_CHAT_BASE_URL", "   ")
    assert app._chat_provider()[0] == "http://settings.example/v1"


# ---------------------------------------------------------------------------
# DSH-1 (#122) — backend de IA "dsh" (harness via ACP). O caminho openai fica
# intacto; aqui o dsh é sempre o fake/`monkeypatch` (sem processo nem Node).
# ---------------------------------------------------------------------------

@pytest.fixture()
def dsh_limpo(monkeypatch):
    """Sem env nem cache herdados: cada teste parte do estado de produção limpo.

    `TTS_CHAT_BACKEND_LIVE` entra na lista desde a #176: o env dele tem precedência
    sobre o settings e, sem o delenv, um dono com o Live em `dsh` no ambiente fazia
    o teste do caminho global medir outra rota. O CAMPO `chat_backend_live` também é
    neutralizado: a fixture de estado isola o `settings.json` COPIANDO o do dono, e
    o seletor do Live (o cenário recomendado no #175!) vaza para toda a suíte —
    medido: com `chat_backend_live: "dsh"` no arquivo, dois testes do caminho global
    ficavam vermelhos sem nada de errado no código."""
    for var in ("TTS_CHAT_BACKEND", "TTS_CHAT_BACKEND_LIVE", "TTS_CHAT_DSH_BIN",
                "TTS_CHAT_DSH_PROFILE",
                "TTS_CHAT_DSH_MODEL", "TTS_CHAT_DSH_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(app._settings, "chat_backend_live", "")   # vazio = herda
    monkeypatch.setattr(app, "_dsh_models_cache", {})
    monkeypatch.setattr(app, "_chat_dsh_livres", [])
    monkeypatch.setattr(app, "_chat_dsh_prewarm_thread", None)


@pytest.fixture()
def dsh_fake_bin(tmp_path, monkeypatch):
    """`chat_dsh_bin` apontando para um wrapper que roda o servidor ACP falso.

    Assim o pre-warm exercita o caminho REAL do app (resolve o binário, spawna,
    handshake, prewarm) sem depender do dsh instalado nem do Node. O nome não
    começa com `dsh`, de propósito: pula o check de Node e fica hermético."""
    fake = Path(__file__).resolve().parent / "fake_acp.py"
    alvo = tmp_path / "fake-acp"
    alvo.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    alvo.chmod(0o755)
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    monkeypatch.setitem(app._settings, "chat_dsh_bin", str(alvo))
    return str(alvo)


def test_prewarm_sobe_o_dsh_sem_abrir_a_conversa(dsh_limpo, dsh_fake_bin):
    """#155: o processo/sessão já está quente antes do 1º turno da Conversa."""
    assert app._chat_dsh_livres == []
    th = app._chat_dsh_prewarm("teste")
    assert th is not None
    th.join(20)
    assert not th.is_alive()
    assert len(app._chat_dsh_livres) == 1, "o processo quente tem que voltar ao pool"
    _chave, cli = app._chat_dsh_livres[0]
    assert cli.alive and cli.session_id, "sessão ACP já aberta pelo pre-warm"
    assert cli.ultimo_boot_ms > 0


def test_prewarm_e_noop_com_openai(dsh_limpo, monkeypatch):
    """Quem usa openai não paga NADA: sem thread, sem spawn, sem pool."""
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    assert app._chat_dsh_prewarm("teste") is None
    assert app._chat_dsh_livres == []


def test_prewarm_que_falha_nao_derruba_e_so_loga(dsh_limpo, monkeypatch, capsys):
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    monkeypatch.setitem(app._settings, "chat_dsh_bin", "/nao/existe/dsh")
    th = app._chat_dsh_prewarm("teste")
    th.join(20)
    assert not th.is_alive()                     # thread morre, nada explode
    assert app._chat_backend() == "dsh"          # backend NÃO muda
    assert app._chat_dsh_livres == []            # processo morto não fica quente
    # stderr, não stdout: o stdout é lido como DADO por quem faz `import app`
    # (teste de telemetria do ORT) e o pre-warm sobe em thread (task_6db0e2cc).
    assert "pre-warm falhou" in capsys.readouterr().err


def test_prewarm_e_idempotente_e_nao_deixa_config_velha_quente(dsh_limpo, dsh_fake_bin):
    """Duas chamadas não viram dois processos; trocar a config descarta o antigo."""
    app._chat_dsh_prewarm("t1").join(20)
    app._chat_dsh_prewarm("t2").join(20)
    assert len(app._chat_dsh_livres) == 1
    _chave, antigo = app._chat_dsh_livres[0]
    novo_modelo = '["openrouter","z-ai/glm-5.3-flash"]'
    app._settings["chat_dsh_model"] = novo_modelo
    try:
        app._chat_dsh_prewarm("t3").join(20)
        assert len(app._chat_dsh_livres) == 1, "não pode acumular config antiga"
        _chave, novo = app._chat_dsh_livres[0]
        assert novo is not antigo
        assert novo.modelo == novo_modelo
        assert not antigo.alive, "o processo da config antiga tem que ser fechado"
    finally:
        app._settings["chat_dsh_model"] = app.dsh_client.DSH_DEFAULT_MODEL


def test_pool_nao_devolve_cliente_da_config_antiga_como_nova(dsh_limpo, dsh_fake_bin):
    """Cliente EM VOO + prewarm da config nova ANTES da devolução: a chave tem de
    viajar com o cliente. Carimbada na devolução (chave global de agora), o cliente
    do modelo antigo voltava ao pool marcado como novo — e o turno seguinte rodava
    no modelo antigo, calado."""
    app._settings["chat_dsh_model"] = '["openrouter","modelo-antigo"]'
    try:
        antigo = app._chat_dsh_cliente()          # em voo (não devolvido)
        antigo.prewarm()
        app._settings["chat_dsh_model"] = '["openrouter","modelo-novo"]'
        novo = app._chat_dsh_cliente()            # o prewarm da config nova chega antes
        novo.prewarm()
        app._chat_dsh_devolve(novo)
        app._chat_dsh_devolve(antigo)             # devolução DEPOIS da troca
        assert app._chat_dsh_cliente().modelo == '["openrouter","modelo-novo"]'
    finally:
        app._settings["chat_dsh_model"] = app.dsh_client.DSH_DEFAULT_MODEL


def test_devolucao_dupla_nao_duplica_o_pool(dsh_limpo, dsh_fake_bin):
    """#218: devolver o MESMO cliente duas vezes deixava duas entradas idênticas.
    `_chat_dsh_cliente` dá `pop()` numa e a outra continuava na lista, então a
    entrega seguinte devolvia o MESMO objeto — ainda em uso pelo chamador anterior.
    Efeito: dois turnos no mesmo processo/sessão ACP, e o resumo do Live (que existe
    para não disputar o slot de prompt) passa a disputar. Aqui o pool fica com UMA
    entrada e a segunda entrega é obrigada a ser outro objeto."""
    a = app._chat_dsh_cliente()
    a.prewarm()
    app._chat_dsh_devolve(a)
    app._chat_dsh_devolve(a)                     # devolução DUPLA do mesmo objeto
    assert len(app._chat_dsh_livres) == 1, "devolução dupla duplicou o pool"
    assert app._chat_dsh_livres[0][1] is a, "e não pode ter virado outra entrada"
    b = app._chat_dsh_cliente()
    c = app._chat_dsh_cliente()
    assert b is a, "a primeira entrega tem de ser o cliente que voltou ao pool"
    assert b is not c, "duas entregas seguidas deram o MESMO cliente"
    c.prewarm()                                  # sem processo o pool recusa (não é bug daqui)
    app._chat_dsh_devolve(b)
    app._chat_dsh_devolve(c)
    assert len(app._chat_dsh_livres) == 2, "dois clientes distintos podem ficar quentes"


def test_pool_nao_segura_o_lock_durante_o_close(dsh_limpo, dsh_fake_bin, monkeypatch):
    """Descartar cliente de config antiga faz `session/close` (timeout de 60 s) e um
    dsh vivo mas mudo não responde: fechando DENTRO do lock, todo uso do pool — o
    próximo turno da Conversa, o resumo do Live — esperava o processo velho."""
    liberado = threading.Event()
    fechados = []

    class Antigo:
        alive = True

        def close(self):
            assert liberado.wait(2.0), "o close segurou o lock do pool"
            fechados.append(self)

    class Devolvido:
        alive = True
        _pool_chave = ("config", "de-agora")

    monkeypatch.setattr(app, "_chat_dsh_livres", [(("config", "velha"), Antigo())])
    th = threading.Thread(target=app._chat_dsh_cliente)
    th.start()
    time.sleep(0.2)                     # a thread já está no close do antigo
    app._chat_dsh_devolve(Devolvido())  # só precisa do lock: não pode esperar o close
    liberado.set()
    th.join(5)
    assert not th.is_alive()
    assert fechados, "o cliente da config antiga tem de ser fechado"


def test_chat_start_dispara_o_prewarm(dsh_limpo, monkeypatch):
    chamadas = []
    monkeypatch.setattr(app, "_chat_dsh_prewarm",
                        lambda motivo="": chamadas.append(motivo) or None)
    monkeypatch.setattr(app, "_chat_worker", lambda *a, **k: None)
    c = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    r = c.post("/api/chat/start", json={"objective": "abrir a conversa"})
    assert r.status_code == 200
    assert chamadas == ["chat/start"]


def test_chat_backend_default_e_openai(monkeypatch, dsh_limpo):
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    assert app._chat_backend() == "openai"
    monkeypatch.setitem(app._settings, "chat_backend", "lixo")
    assert app._chat_backend() == "openai"


def test_chat_backend_env_tem_precedencia(monkeypatch, dsh_limpo):
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setenv("TTS_CHAT_BACKEND", "DSH")
    assert app._chat_backend() == "dsh"


def test_chat_dsh_cfg_default_seguro_e_env_por_campo(monkeypatch, dsh_limpo):
    monkeypatch.setitem(app._settings, "chat_dsh_model", "")
    monkeypatch.setitem(app._settings, "chat_dsh_effort", "off")
    cfg = app._chat_dsh_cfg()
    assert cfg["model"] == '["dsflash","deepseek-flash-41"]'
    assert cfg["profile"] == "tts-studio"
    monkeypatch.setenv("TTS_CHAT_DSH_EFFORT", "HIGH")
    monkeypatch.setenv("TTS_CHAT_DSH_MODEL", '["openrouter","z-ai/glm-5.3-flash"]')
    cfg = app._chat_dsh_cfg()
    assert cfg["effort"] == "high"
    assert cfg["model"] == '["openrouter","z-ai/glm-5.3-flash"]'
    # rota sem chave volta ao default seguro
    monkeypatch.setenv("TTS_CHAT_DSH_MODEL", '["deepseek-official","deepseek-v4-pro"]')
    assert app._chat_dsh_cfg()["model"] == '["dsflash","deepseek-flash-41"]'


def test_chat_backend_live_campo_env_admin_e_validacao(client, auth, monkeypatch, dsh_limpo):
    """#176: campo do Live é separado, admin, e vazio = herda o global."""
    assert "chat_backend_live" in app._SETTINGS_ADMIN
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "")
    assert app._chat_backend_live() == "openai", "vazio herda o global"
    monkeypatch.setenv("TTS_CHAT_BACKEND_LIVE", "DSH")
    assert app._chat_backend_live() == "dsh", "env tem precedência por campo"
    monkeypatch.delenv("TTS_CHAT_BACKEND_LIVE")
    # vazio continua válido no POST; inválido é 400 com rollback
    antes = dict(app._settings)
    r = client.post("/api/settings", headers=auth, json={"chat_backend_live": "xpto"})
    assert r.status_code == 400 and "chat_backend_live" in r.json()["detail"]
    assert app._settings == antes
    r = client.post("/api/settings", headers=auth, json={"chat_backend_live": "dsh"})
    assert r.status_code == 200 and app._settings["chat_backend_live"] == "dsh"
    r = client.post("/api/settings", headers=auth, json={"chat_backend_live": ""})
    assert r.status_code == 200 and app._settings["chat_backend_live"] == ""
    assert app._chat_backend_live() == "openai"


def test_chat_llm_despacha_para_o_backend_dsh(monkeypatch, dsh_limpo):
    chamadas = []
    monkeypatch.setattr(app, "_chat_llm_dsh", lambda msgs: chamadas.append(msgs) or "via-dsh")
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    assert app._chat_llm([{"role": "user", "content": "oi"}]) == "via-dsh"
    assert chamadas == [[{"role": "user", "content": "oi"}]]
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setattr(app, "_chat_llm_openai", lambda msgs: "via-openai")
    assert app._chat_llm([]) == "via-openai"


def test_dsh_error_vira_502_no_chat_llm(monkeypatch, dsh_limpo):
    class CliQuebrado:
        alive = True

        def collect(self, _msgs):
            raise app.dsh_client.DshError("binário 'dsh' não encontrado no PATH")

        def close(self):
            pass
    monkeypatch.setattr(app, "_chat_dsh_cliente", lambda: CliQuebrado())
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    with pytest.raises(HTTPException) as e:
        app._chat_llm([])
    assert e.value.status_code == 502 and "não encontrado" in e.value.detail


def test_chat_dsh_campos_sao_admin(dsh_limpo):
    for campo in ("chat_backend", "chat_dsh_bin", "chat_dsh_profile",
                  "chat_dsh_model", "chat_dsh_effort"):
        assert campo in app._SETTINGS_ADMIN


def test_post_settings_valida_backend_effort_e_modelo(client, auth, monkeypatch, dsh_limpo):
    antes = dict(app._settings)
    r = client.post("/api/settings", headers=auth,
                    json={"chat_backend": "nada", "speed": 2.0})
    assert r.status_code == 400 and "chat_backend" in r.json()["detail"]
    assert app._settings == antes, "400 não pode deixar a RAM divergindo do disco"
    r = client.post("/api/settings", headers=auth,
                    json={"chat_dsh_effort": "turbo", "speed": 2.0})
    assert r.status_code == 400 and "chat_dsh_effort" in r.json()["detail"]
    r = client.post("/api/settings", headers=auth, json={"chat_dsh_model": "solto"})
    assert r.status_code == 400 and "chat_dsh_model" in r.json()["detail"]
    r = client.post("/api/settings", headers=auth, json={
        "chat_backend": "dsh", "chat_dsh_effort": "low",
        "chat_dsh_model": '["dsflash","deepseek-flash-41"]', "chat_dsh_profile": "tts-studio"})
    assert r.status_code == 200
    assert app._settings["chat_backend"] == "dsh"
    assert app._settings["chat_dsh_effort"] == "low"
    # vazio no model volta ao default seguro, não grava string vazia
    client.post("/api/settings", headers=auth, json={"chat_dsh_model": ""})
    assert app._settings["chat_dsh_model"] == '["dsflash","deepseek-flash-41"]'


def test_get_chat_dsh_models_e_cache(client, auth, monkeypatch, dsh_limpo):
    chamadas = []
    monkeypatch.setattr(app.dsh_client, "descobrir_modelos", lambda **kw: chamadas.append(kw) or {
        "bin": "/usr/local/bin/dsh", "profile": "tts-studio", "node": "25.2.1",
        "models": [{"id": '["dsflash","deepseek-flash-41"]', "label": "Flash",
                    "provider": "dsflash", "modelo": "deepseek-flash-41",
                    "efforts": ["off", "low"], "isDefault": False}]})
    r = client.get("/api/chat/dsh/models", headers=auth)
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["ok"] and corpo["backend"] == "dsh"
    assert corpo["models"][0]["id"] == '["dsflash","deepseek-flash-41"]'
    assert corpo["current"]["effort"] == "off"
    assert corpo["default_model"] == '["dsflash","deepseek-flash-41"]'
    assert r.headers["content-type"].startswith("application/json")
    client.get("/api/chat/dsh/models", headers=auth)
    assert len(chamadas) == 1, "o discovery sobe um dsh — tem que ter cache"


def test_dsh_models_uma_descoberta_por_vez(monkeypatch, dsh_limpo):
    """#213: com o cache frio, dois GETs simultâneos passavam juntos e subiam 2+
    processos `dsh` (a tela chama no clique). O lock faz o segundo esperar e
    reaproveitar o cache do primeiro."""
    chamadas = []
    solta = threading.Event()

    def descobre(**_kw):
        chamadas.append(1)
        solta.wait(3)                # segura a 1ª com o lock preso
        return {"models": [], "default_model": None, "current": {}}

    monkeypatch.setattr(app.dsh_client, "descobrir_modelos", descobre)
    c1, c2 = _ws_cliente("127.0.0.1"), _ws_cliente("127.0.0.1")
    saida = []
    t1 = threading.Thread(target=lambda: saida.append(c1.get("/api/chat/dsh/models").status_code))
    t1.start()
    time.sleep(0.2)
    t2 = threading.Thread(target=lambda: saida.append(c2.get("/api/chat/dsh/models").status_code))
    t2.start()
    time.sleep(0.2)
    solta.set()
    t1.join(10)
    t2.join(10)
    assert saida == [200, 200]
    assert len(chamadas) == 1, f"a descoberta rodou {len(chamadas)}x (lock não segurou)"


def test_get_chat_dsh_models_erro_explicativo(client, auth, monkeypatch, dsh_limpo):
    def explode(**_kw):
        raise app.dsh_client.DshError("node 23.11.0 é antigo: o dsh exige >= 24.2")
    monkeypatch.setattr(app.dsh_client, "descobrir_modelos", explode)
    r = client.get("/api/chat/dsh/models", headers=auth)
    assert r.status_code == 502
    assert ">= 24.2" in r.json()["detail"]


def test_get_chat_dsh_models_traz_estado_do_bridge(client, auth, monkeypatch, dsh_limpo):
    """#159: o endpoint diz se o HOST tem o patch do bridge — e cacheia junto."""
    chamadas = []
    monkeypatch.setattr(app.dsh_client, "descobrir_modelos",
                        lambda **kw: {"models": [], "bin": "dsh", "profile": "p", "node": "25"})
    monkeypatch.setattr(app.dsh_client, "estado_bridge",
                        lambda **kw: chamadas.append(kw) or {
                            "estado": "clean", "arquivo": "/x/index.js",
                            "versao": "0.1.5-rc.3", "motivo": "sem o patch"})
    r = client.get("/api/chat/dsh/models", headers=auth)
    assert r.status_code == 200
    assert r.json()["bridge"] == "clean"
    assert r.json()["bridge_detalhe"]["versao"] == "0.1.5-rc.3"
    client.get("/api/chat/dsh/models", headers=auth)
    assert len(chamadas) == 1, "o estado do bridge vem do cache da descoberta"
    assert chamadas[0]["bin"] == app._chat_dsh_cfg()["bin"]


def test_bridge_unknown_nao_quebra_o_endpoint(client, auth, monkeypatch, dsh_limpo):
    """Degradação: pacote/binário/node ausente → `unknown`, nunca 500."""
    monkeypatch.setattr(app.dsh_client, "descobrir_modelos",
                        lambda **kw: {"models": [], "bin": "dsh", "profile": "p", "node": "25"})
    monkeypatch.setattr(app, "_dsh_bridge_estado",
                        lambda: {"estado": "unknown", "motivo": "pacote não encontrado"})
    r = client.get("/api/chat/dsh/models", headers=auth)
    assert r.status_code == 200
    assert r.json()["bridge"] == "unknown"
    assert "não encontrado" in r.json()["bridge_detalhe"]["motivo"]


def test_bridge_estado_que_explode_vira_unknown(monkeypatch, dsh_limpo):
    def explode(**_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(app.dsh_client, "estado_bridge", explode)
    assert app._dsh_bridge_estado()["estado"] == "unknown"
    assert "boom" in app._dsh_bridge_estado()["motivo"]


def test_rota_dsh_models_nao_colide_com_chat_sid(client, auth, monkeypatch, dsh_limpo):
    """`/api/chat/{sid}` (uma barra) não pode engolir `/api/chat/dsh/models`."""
    monkeypatch.setattr(app.dsh_client, "descobrir_modelos",
                        lambda **kw: {"models": [], "bin": "dsh", "profile": "p", "node": "25"})
    assert client.get("/api/chat/dsh/models", headers=auth).status_code == 200


# ---------------------------------------------------------------------------
# LIVE-5 (#95) — retomada por session_id, compressão de contexto e tetos.
# ---------------------------------------------------------------------------

@pytest.fixture()
def hist_limpo(monkeypatch):
    with app._live_lock:
        app._live_historico.clear()
    monkeypatch.setattr(app, "_live_resume_fn", lambda msgs: "RESUMO curto")
    yield
    with app._live_lock:
        app._live_historico.clear()


def test_resume_restaura_contexto(ws_client, live_limpo, pipeline_fake, engine_fake, hist_limpo):
    """Novo WS com `session_id` repõe o contexto e o `ready` avisa `resumed`."""
    engine_fake.roteiro = [_EvFake("speech_end", audio=b"\x01\x00" * 800, fala_ms=400)]
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws, system_instruction="persona X")["resumed"] is False
        sess = list(app._live_sessions.values())[0]
        sess["pipe"].history[:] = [{"role": "user", "content": "meu nome é Ana"},
                                   {"role": "assistant", "content": "prazer, Ana"}]
        sid = sess["id"]
        voz_original = sess["voice_id"]
        ws.send_bytes(b"\x00\x01" * 1600)      # turno → guarda o contexto
        assert _ate(ws, {"turn_complete"})["type"] == "turn_complete"
    # retomada SEM `voice_id` no setup: contexto, system E VOZ voltam do registro
    with ws_client.websocket_connect("/api/live/ws") as ws:
        pronto = _setup_ok(ws, session_id=sid)
        assert pronto["resumed"] is True and pronto["session_id"] == sid
        novo = list(app._live_sessions.values())[0]
        assert [m["content"] for m in novo["pipe"].history][:2] == [
            "meu nome é Ana", "prazer, Ana"]
        assert novo["system"] == "persona X", "system entra no prompt, fora do resumo"
        assert novo["voice_id"] == voz_original, "a voz da sessão tem de voltar"
    # e quando o setup MANDA voz, ela vence a guardada
    with ws_client.websocket_connect("/api/live/ws") as ws:
        pronto = _setup_ok(ws, session_id=sid, voice_id="__design__")
        assert pronto["resumed"] is True and pronto["voice_id"] == "__design__"


def test_resume_sem_voice_id_usa_a_voz_do_registro(ws_client, live_limpo, pipeline_fake,
                                                   engine_fake, hist_limpo):
    """Regressão medida no re-gate do #97: sem este caso, a retomada volta a usar a
    voz PADRÃO em silêncio (o `system` tinha o teste, a voz não).

    Caminho é o real: o setup pede uma voz (design), o turno guarda o registro e a
    retomada — SEM `voice_id` — tem de anunciar e usar a MESMA voz guardada."""
    engine_fake.roteiro = [_EvFake("speech_end", audio=b"\x01\x00" * 800, fala_ms=400)]
    voz_padrao = app._resolve_voice(None)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        pronto = _setup_ok(ws, voice_id="__design__")
        assert pronto["voice_id"] == "__design__" and pronto["voice_id"] != voz_padrao
        sess = list(app._live_sessions.values())[0]
        sid = sess["id"]
        ws.send_bytes(b"\x00\x01" * 1600)
        assert _ate(ws, {"turn_complete"})["type"] == "turn_complete"

    with ws_client.websocket_connect("/api/live/ws") as ws:      # SEM voice_id
        pronto = _setup_ok(ws, session_id=sid)
        assert pronto["resumed"] is True
        assert pronto["voice_id"] == "__design__", "a voz guardada tem de voltar"
        assert pronto["voice_id"] != voz_padrao, "não pode cair na voz padrão"
        assert list(app._live_sessions.values())[0]["voice_id"] == "__design__"


def test_retomada_com_sessao_antiga_viva_nao_derruba_a_nova(ws_client, live_limpo,
                                                            pipeline_fake, engine_fake,
                                                            hist_limpo):
    """O `sid` da retomada é o do cliente: o pop incondicional do `finally` da sessão
    ANTIGA apagava a entrada da NOVA — ela saía do sweep/TTL e da contagem do teto, e
    o registro de retomada voltava a ser regravado com o contexto velho."""
    engine_fake.roteiro = [_EvFake("speech_end", audio=b"\x01\x00" * 800, fala_ms=400)]
    with contextlib.ExitStack() as pilha:
        ws1 = pilha.enter_context(ws_client.websocket_connect("/api/live/ws"))
        _setup_ok(ws1)
        sess = list(app._live_sessions.values())[0]
        sess["pipe"].history[:] = [{"role": "user", "content": "meu nome é Ana"}]
        sid = sess["id"]
        ws1.send_bytes(b"\x00\x01" * 1600)
        assert _ate(ws1, {"turn_complete"})["type"] == "turn_complete"
        antiga = sess
        ws2 = pilha.enter_context(ws_client.websocket_connect("/api/live/ws"))
        assert _setup_ok(ws2, session_id=sid)["resumed"] is True
        nova = app._live_sessions[sid]
        nova["pipe"].history[:] = [{"role": "user", "content": "contexto novo"}]
        app._live_hist_guarda(nova)
        ws1.close()                                # a ANTIGA morre com a nova viva
        time.sleep(0.4)
        assert sid in app._live_sessions, "a sessão NOVA saiu do registry"
        # escrita atrasada da antiga (socket zumbi) não pode voltar o contexto
        app._live_hist_guarda(antiga)
        assert app._live_historico[sid]["msgs"][0]["content"] == "contexto novo"


def test_stub_sem_pipeline_manda_pelo_envia_json(ws_client, live_limpo, hist_limpo,
                                                monkeypatch):
    """#211: sem pipeline (stub) o `turn_complete` tem de sair por
    `_live_envia_json`, como o irmão `_live_engine` — pela fila crua ele não zerava
    o buffer do mic, não consumia `truncado`, não devolvia `st_stage` a idle e não
    gravava o registro de retomada."""
    monkeypatch.setattr(app, "_live_pipe_novo", lambda sess: None)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws)["type"] == "ready"
        sess = list(app._live_sessions.values())[0]
        sess["buffer"].extend(b"\x01\x00" * 50)
        sess["truncado"] = True
        app._live_stage(sess, "stt")            # como o fim-de-fala faz
        app._live_abre_turno(sess, b"\x01\x00" * 50, barge_in=False)
        ev = _ate(ws, {"turn_complete"})
        assert ev.get("stub") is True and ev.get("truncated") is True
        assert not sess["buffer"], "buffer do mic não foi zerado"
        assert sess["truncado"] is False, "aviso de truncado não foi consumido"
        assert sess.get("st_stage") == "idle", "st_stage ficou preso em 'stt'"


def test_cliente_com_id_proprio_retoma_com_o_mesmo_id(ws_client, live_limpo, pipeline_fake,
                                                      engine_fake, hist_limpo):
    """#210: id escolhido pelo CLIENTE tem de retomar. Antes ele era aceito e
    ignorado na 1ª conexão (o servidor devolvia sid aleatório), então reconectar
    com o mesmo id nunca retomava e o registro enchia de sids órfãos."""
    with ws_client.websocket_connect("/api/live/ws") as ws1:
        pronto = _setup_ok(ws1, session_id="meu-id-fixo")
        assert pronto["session_id"] == "meu-id-fixo", "id do cliente ignorado"
        assert pronto["resumed"] is False
        sess = app._live_sessions["meu-id-fixo"]
        sess["pipe"].history[:] = [{"role": "user", "content": "meu nome é Ana"}]
        app._live_hist_guarda(sess)
    with ws_client.websocket_connect("/api/live/ws") as ws2:
        pronto = _setup_ok(ws2, session_id="meu-id-fixo")
        assert pronto["resumed"] is True, "o id do cliente não retomou"
        assert pronto["session_id"] == "meu-id-fixo"


def test_resume_recusa_id_desconhecido_e_expirado(ws_client, live_limpo, hist_limpo, monkeypatch):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws, session_id="nao-existe")["resumed"] is False
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws)["resumed"] is False
        sid = list(app._live_sessions.values())[0]["id"]
        app._live_historico[sid] = {"msgs": [{"role": "user", "content": "oi"}],
                                    "visto": time.monotonic() - 10 ** 6}
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws, session_id=sid)["resumed"] is False, "TTL vencido"


def test_resume_valida_session_id(ws_client, live_limpo, hist_limpo):
    with ws_client.websocket_connect("/api/live/ws") as ws:
        ws.send_json({"type": "setup", "session_id": "id com espaço"})
        assert ws.receive_json()["code"] == "setup_invalido"


def test_compressao_do_live_segue_o_backend_efetivo(ws_client, live_limpo, pipeline_fake,
                                                    hist_limpo, monkeypatch):
    """Live=dsh com a Conversa no endpoint (a combinação que o #175 recomenda): o
    resumo do Live sai pelo dsh, não pelo remoto. Com o dsh marcado indisponível
    (#146), cai no endpoint — é o backend EFETIVO da sessão."""
    monkeypatch.setattr(app, "_live_resume_fn", None)
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
    chamados = []
    monkeypatch.setattr(app, "_chat_llm", lambda msgs: chamados.append("openai") or "R")
    monkeypatch.setattr(app, "_chat_llm_dsh", lambda msgs: chamados.append("dsh") or "R")
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        hist = lambda: [{"role": "user", "content": "x" * 300} for _ in range(40)]
        sess["pipe"].history[:] = hist()
        assert app._live_hist_comprime(sess) is True
        assert chamados == ["dsh"], "o resumo do Live tem de seguir o backend do Live"
        sess["pipe"]._dsh_indisponivel = True
        sess["pipe"].history[:] = hist()
        assert app._live_hist_comprime(sess) is True
        assert chamados == ["dsh", "openai"], "dsh caído -> resumo no backend efetivo"


def test_compressao_dispara_no_limiar_e_preserva_recentes(ws_client, live_limpo, pipeline_fake, hist_limpo, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_HIST_MAX_MSGS", 4)
    vistos = []
    monkeypatch.setattr(app, "_live_resume_fn", lambda msgs: vistos.append(msgs) or "RESUMO")
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        sess["pipe"].history[:] = [{"role": "user", "content": f"m{i}"} for i in range(5)]
        app._live_hist_pos_turno(sess)
        for _ in range(60):
            if sess["pipe"].history and sess["pipe"].history[0]["content"].startswith("Resumo"):
                break
            time.sleep(0.05)
        assert vistos, "o resumidor não foi chamado"
        assert sess["pipe"].history[0]["content"].startswith("Resumo do que já foi dito:")
        assert sess["pipe"].history[-1]["content"] == "m9" or sess["pipe"].history[-1]["content"] == "m4"


def test_compressao_que_falha_mantem_o_cru(ws_client, live_limpo, pipeline_fake, hist_limpo, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_HIST_MAX_MSGS", 2)
    monkeypatch.setattr(app, "_live_resume_fn",
                        lambda msgs: (_ for _ in ()).throw(RuntimeError("llm fora")))
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        sess["pipe"].history[:] = [{"role": "user", "content": f"m{i}"} for i in range(4)]
        assert app._live_hist_comprime(sess) is False
        assert len(sess["pipe"].history) == 4, "sem resumo o histórico fica cru"
        assert "RuntimeError" in sess["resumo_erro"]


def test_caps_do_historico_sao_previsiveis(ws_client, live_limpo, hist_limpo, monkeypatch):
    monkeypatch.setattr(app, "_LIVE_MAX_HISTORICOS", 3)
    monkeypatch.setattr(app, "_LIVE_RESUME_TTL_S", 30)
    with app._live_lock:
        for i in range(6):
            app._live_historico[f"s{i}"] = {"msgs": [{"role": "user", "content": f"m{i}"}],
                                            "visto": time.monotonic() - (10 - i)}
    app._live_hist_varre()
    assert len(app._live_historico) == 3, "teto total"
    assert set(app._live_historico) == {"s3", "s4", "s5"}, "saíram os mais antigos"


def test_status_publica_historicos(client, auth, hist_limpo):
    d = client.get("/api/status", headers=auth).json()
    assert isinstance(d["live_historicos"], int) and d["live_historicos_max"] >= 1


# ---------------------------------------------------------------------------
# #107 — tradutor/STT remotos: base vazia dá erro EXPLICATIVO (não MissingSchema).
# ---------------------------------------------------------------------------

@pytest.fixture()
def sem_bases_remotas(monkeypatch):
    for var in ("TTS_TRANSLATE_BASE_URL", "TTS_TRANSLATE_MODEL", "TTS_STT_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(app._settings, "remote_base_url", "")
    monkeypatch.setitem(app._settings, "remote_stt_base_url", "")


def test_traducao_base_vazia_da_400_explicativo(sem_bases_remotas):
    """Antes: `MissingSchema: Invalid URL '/chat/completions'` cru."""
    with pytest.raises(HTTPException) as exc:
        app._traducao_remota_cfg()
    assert exc.value.status_code == 400 and "Base URL" in exc.value.detail
    with pytest.raises(HTTPException) as exc2:      # e pelo caminho que o cliente usa
        app._translate_remote("olá", "en")
    assert exc2.value.status_code == 400


def test_traducao_env_tem_precedencia(sem_bases_remotas, monkeypatch):
    monkeypatch.setitem(app._settings, "remote_base_url", "http://rtx.example/v1")
    monkeypatch.setenv("TTS_TRANSLATE_BASE_URL", "http://127.0.0.1:53993/v1")
    monkeypatch.setenv("TTS_TRANSLATE_MODEL", "modelo-do-smoke")
    assert app._traducao_remota_cfg() == ("http://127.0.0.1:53993/v1", "modelo-do-smoke")


def test_traducao_sem_env_segue_a_cadeia_antiga(sem_bases_remotas, monkeypatch):
    monkeypatch.setitem(app._settings, "remote_base_url", "http://rtx.example/v1/")
    base, modelo = app._traducao_remota_cfg()
    assert base == "http://rtx.example/v1" and modelo == app._settings["remote_translate_model"]


def test_stt_remoto_base_vazia_tambem_e_explicativo(sem_bases_remotas, tmp_path):
    import wave as _wave
    p = tmp_path / "a.wav"
    with _wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 160)
    with pytest.raises(HTTPException) as exc:
        app._transcribe_remote(p, "pt")
    assert exc.value.status_code == 400 and "STT remoto" in exc.value.detail


# ---------------------------------------------------------------------------
# #101/#108 — contraprova dos filtros do venv: o que é NOSSO não pode sumir.
# ---------------------------------------------------------------------------

def test_filtros_do_venv_escondem_so_o_que_e_do_venv():
    """Contraprova dos filtros (#101/#108), avaliando os filtros INSTALADOS.

    Sem `simplefilter` aqui de propósito: `recwarn`/`simplefilter` põem um filtro
    na frente e enxergam tudo; o `catch_warnings` cru copia os filtros correntes,
    que é o que o gate realmente aplica."""
    with warnings.catch_warnings(record=True) as capturados:
        warnings.warn(RuntimeWarning("invalid value encountered in divide"))   # venv
        warnings.warn(DeprecationWarning("builtin type SwigPyPacked has no __module__ attribute"))  # venv
        warnings.warn(RuntimeWarning("aviso nosso de teste"))                 # nosso
    textos = [str(w.message) for w in capturados]
    assert textos == ["aviso nosso de teste"], textos


def test_live_erro_do_pipeline_nao_derruba_a_sessao(ws_client, live_limpo, engine_fake, pipeline_fake):
    """F1 (aprovado pelo PM): assinatura divergente do pipeline falha ALTO (o cliente
    recebe `error{pipeline}`) e a sessão continua viva — antes, a muleta engolia e o
    turno ficava mudo."""
    engine_fake.roteiro = [_EvFake("speech_end", audio=b"\\x01\\x00" * 400, fala_ms=400)]
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]

        def _eos_assinatura_velha(*a, barge_in=False):    # ← o nome antigo
            return True

        sess["pipe"].end_of_speech = _eos_assinatura_velha
        ws.send_bytes(b"\\x00\\x01" * 1600)                  # dispara o turno
        erro = _ate(ws, {"error"})
        assert erro["code"] == "pipeline" and "TypeError" in erro["message"]
        ws.send_json({"type": "ping", "t": 9})               # ...e a sessão segue de pé
        assert _ate(ws, {"pong"})["t"] == 9


# ---------------------------------------------------------------------------
# LIVE-OBS-1 (#118) — telemetria da sessão: evento `stats` no WS + log de
# metadados. O esquema é o contrato publicado na task (e no LIVE.md).
# ---------------------------------------------------------------------------

def test_live_ws_stats_esquema_e_cadencia(ws_client, live_limpo, monkeypatch):
    """`stats` chega periodicamente SEM tráfego do cliente, com o esquema do
    contrato, contadores acumulados do mic e relógio monotônico — é dele que o
    painel (#119) tira os medidores e a suspeita de captação travada."""
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 30)          # cadência rápida de suíte
    monkeypatch.setattr(app, "_LIVE_TICK_S", 0.02)
    monkeypatch.setattr(app, "_provedor_estado",
                        {"estado": "desconhecido", "http": None, "ts": 0.0})
    pcm = b"\x00\x40" * 1600                                # ~100 ms @16 kHz PCM16
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws)["type"] == "ready"
        ws.send_bytes(pcm)
        stats, prazo = [], time.monotonic() + 2.0
        while len(stats) < 3 and time.monotonic() < prazo:
            m = ws.receive()
            if m.get("bytes"):
                continue
            ev = _json.loads(m["text"])
            if ev["type"] == "stats":
                stats.append(ev)
    assert len(stats) == 3, "cadência: 3 stats em ~2 s com STATS_MS=30"
    s = stats[-1]
    assert s["t_ms"] >= 0
    assert s["mic"]["frames"] == 1 and s["mic"]["bytes"] == len(pcm)
    assert s["mic"]["desde_ultimo_ms"] is not None
    assert isinstance(s["mic"]["dbfs"], float)              # RMS medido no SERVIDOR
    assert s["motor"]["estado"] in {"ocioso", "ouvindo", "falando", "fechando"}
    assert s["turno"]["stage"] in {"idle", "stt", "llm", "tts"}
    assert s["playback"]["speaking"] is False and s["playback"]["chunks"] == 0
    assert s["sessao"]["ttl_s"] == app._LIVE_TTL_S and s["sessao"]["criadas"] >= 1
    assert "erro" not in s and "provedor" not in s          # ausentes quando não houve
    tms = [x["t_ms"] for x in stats]
    assert tms == sorted(tms)                               # relógio da sessão monotônico
    assert tms[1] - tms[0] >= 20                            # espaçados, não em rajada


class _EngFake:
    """Mínimo que o `_live_stats` consulta no motor (limiares vigentes + contadores)."""

    limiar_energia_dbfs = -45.2
    limiar_energia_turno_dbfs = -48.1
    turno_aberto = False
    estado = None

    def estatisticas(self):
        # chaves REAIS do motor (`barge_in_falso`, `barge_in_com_turno_aberto`) —
        # o dublê com `barge_falso` mascarava o erro de chave do `_live_stats` (#216).
        # As DUAS portas do barge entram de propósito: é somá-las que faz o contador
        # existir no regime do eco (playback começando com o turno JÁ aberto), que é
        # o regime em que o harness do #216 mede — com só `barge_in` no dublê, um
        # `_live_stats` que voltasse a ler uma porta só passaria em silêncio.
        return {"barge_in": 2, "barge_in_com_turno_aberto": 3, "barge_in_falso": 1}


def test_live_stats_erro_provedor_e_limiares_no_payload(monkeypatch):
    """Último erro da sessão (código + estágio + idade), estado do provedor de
    chat DO PROCESSO (torna o 530/timeout visível — hipótese (b) do #117) e
    limiares vigentes do motor entram no `stats`; sem ocorrência, a chave sai."""
    agora = time.monotonic()
    monkeypatch.setattr(app, "_provedor_estado",
                        {"estado": "erro", "http": 530, "ts": agora - 1.0})
    sess = {"id": "s-tel", "t0": agora, "criada": time.time() - 5, "visto": agora,
            "turno": 3, "buffer_bytes_turno": 6400, "t_decisao_ms": 486,
            "engine": _EngFake(), "falando": True, "st_frames": 7, "st_bytes": 22400,
            "st_ultimo_frame": agora, "st_dbfs": -31.4, "st_prob": 0.87,
            "st_chunks": 12, "st_audio_bytes": 284160, "st_erro": None,
            "st_stage": "tts", "st_stage_ini": agora - 0.8, "st_stage_ms": 0}
    sess["st_stage"] = "tts"                                # o erro veio no estágio tts
    app._live_observa(sess, {"type": "error", "code": "pipeline", "message": "boom"})
    s = app._live_stats(sess)
    assert s["erro"]["code"] == "pipeline" and s["erro"]["stage"] == "tts"
    assert 0 <= s["erro"]["idade_ms"] <= 500                # idade conta DESDE o erro
    assert s["provedor"]["estado"] == "erro" and s["provedor"]["http"] == 530
    assert 900 <= s["provedor"]["idade_ms"] <= 2000
    assert s["motor"]["limiar_dbfs"] == -45.2               # limiares VIGENTES
    # 2 (turno novo por barge) + 3 (barge com turno já aberto): o total, não a 1ª porta
    assert s["motor"]["barge_ativos"] == 5 and s["motor"]["barge_falsos"] == 1
    assert s["playback"]["speaking"] is True and s["playback"]["chunks"] == 12
    # #250: `error{pipeline}` é TERMINAL — o estágio volta a "idle" (o estágio da
    # morte fica preservado no `st_erro.stage`, conferido acima); antes ficava
    # preso em "tts" com o `ms` crescendo até o TTL.
    assert s["turno"]["stage"] == "idle" and s["turno"]["n"] == 3
    # sem erro e sem provedor marcado: chaves AUSENTES (o frontend usa isso)
    sess["st_erro"] = None
    monkeypatch.setattr(app, "_provedor_estado",
                        {"estado": "desconhecido", "http": None, "ts": 0.0})
    s2 = app._live_stats(sess)
    assert "erro" not in s2 and "provedor" not in s2


class _EngJanela:
    """Motor que REGISTRA `set_turno_aberto` — a janela do #167 precisa de dono
    observável (o `_live_janela_turno` só chama quando o motor tem o método)."""

    def __init__(self):
        self.turno_aberto = False
        self.marcas = []

    def set_turno_aberto(self, aberto):
        self.marcas.append(bool(aberto))
        self.turno_aberto = bool(aberto)

    def estatisticas(self):
        return {"barge_in": 0, "barge_in_com_turno_aberto": 0, "barge_in_falso": 0}


class _FilaFake:
    def __init__(self):
        self.itens = []

    def put(self, x):
        self.itens.append(x)


def test_live_erro_pipeline_fecha_o_turno_e_a_janela():
    """#250 (probe pós-subida, prod d252693): `error{code:"pipeline"}` deixa a
    sessão VIVA, mas o turno MORREU — o livro-caixa só fechava em
    `turn_complete`/`interrupted`. Dois estragos medidos em produção: o
    `stats.turno.stage` ficava preso em "llm" com o `ms` crescendo até o TTL
    (painel anunciando "IA pensando" num turno morto, ao lado do motor "ocioso"),
    e a janela do #167 ficava ABERTA quando o turno já tinha mandado áudio — a
    fala seguinte era lida como barge de um turno que não existe. Agora o erro
    terminal fecha a janela e volta o estágio a "idle"; o estágio ONDE morreu
    fica no `st_erro.stage`. Erros que NÃO fecham turno (`turno_em_curso`,
    `sem_audio`, `busy`…) não tocam em nada."""
    agora = time.monotonic()
    eng = _EngJanela()
    sess = {"id": "s-250", "t0": agora, "criada": time.time() - 5, "visto": agora,
            "turno": 1, "engine": eng, "st_erro": None, "fila": _FilaFake(),
            "st_stage": "llm", "st_stage_ini": agora - 2.0, "st_stage_ms": 0}
    eng.set_turno_aberto(True)               # o turno já mandou áudio (#167 aberta)
    app._live_envia_json(sess, {"type": "error", "code": "pipeline",
                                "message": "BadRequestError: chat sem provedor"})
    assert sess["st_stage"] == "idle" and sess["st_stage_ini"] is None
    assert sess["st_erro"]["code"] == "pipeline" and sess["st_erro"]["stage"] == "llm"
    assert eng.marcas == [True, False], \
        "a janela do #167 tem de FECHAR no error terminal"
    assert eng.turno_aberto is False
    s = app._live_stats(sess)
    assert s["turno"]["stage"] == "idle" and s["turno"]["ms"] == 0
    assert s["erro"]["stage"] == "llm"       # estágio da morte preservado
    # ...e o evento segue para o cliente (nada foi engolido):
    canal, obj = sess["fila"].itens[0]
    assert canal == "json" and obj["type"] == "error" and obj["code"] == "pipeline"

    # erros que NÃO fecham turno: estágio e janela intocados (contrato antigo)
    eng2 = _EngJanela()
    sess2 = {"id": "s-250b", "t0": agora, "criada": time.time() - 5, "visto": agora,
             "turno": 1, "engine": eng2, "st_erro": None, "fila": _FilaFake(),
             "st_stage": "stt", "st_stage_ini": agora - 0.2, "st_stage_ms": 0}
    app._live_envia_json(sess2, {"type": "error", "code": "turno_em_curso",
                                 "message": "ocupado"})
    assert sess2["st_stage"] == "stt" and eng2.marcas == []
    app._live_observa(sess2, {"type": "error", "code": "sem_audio", "message": "x"})
    assert sess2["st_stage"] == "stt"


def test_engine_do_live_nasce_com_as_alavancas_do_167_ligadas(monkeypatch):
    """O DEFAULT do app é o produto que o dono recebe (#216).

    As três alavancas do #167 nasciam desligadas e cada uma tinha, no código, uma
    medição que a justificou EM ISOLADO — o fix existia e o app do dono não o
    entregava. Este teste pina o que a COMBINAÇÃO mediu: as duas que resolvem o vão
    entram ligadas, a terceira fica fora. Sem ele, um `os.environ.get(..., "0")`
    reintroduzido em silêncio devolve o comportamento antigo sem quebrar nada."""
    for k in ("TTS_LIVE_BARGE_JANELA_TURNO", "TTS_LIVE_PLAYBACK_DURACAO",
              "TTS_LIVE_ECO_SO_TOCANDO"):
        monkeypatch.delenv(k, raising=False)
    sess = {"vad": {"prefix_ms": 300, "silence_ms": 500}}
    cfg = app._live_engine_novo(sess).config
    assert cfg.barge_janela_turno is True
    assert cfg.playback_por_duracao is True
    assert cfg.eco_so_tocando is False
    # e cada uma continua desligável pelo env (o A/B do harness depende disso)
    monkeypatch.setenv("TTS_LIVE_BARGE_JANELA_TURNO", "0")
    monkeypatch.setenv("TTS_LIVE_PLAYBACK_DURACAO", "0")
    monkeypatch.setenv("TTS_LIVE_ECO_SO_TOCANDO", "1")
    cfg2 = app._live_engine_novo({"vad": {"prefix_ms": 300, "silence_ms": 500}}).config
    assert (cfg2.barge_janela_turno, cfg2.playback_por_duracao,
            cfg2.eco_so_tocando) == (False, False, True)


def test_live_ws_stats_para_ao_fechar_a_sessao(ws_client, live_limpo, monkeypatch, caplog):
    """Com a sessão fechada o emissor de `stats` termina (o `finally` aguarda a
    task), a sessão sai do registro e o log registra `fecha` com os contadores."""
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 30)
    monkeypatch.setattr(app, "_LIVE_TICK_S", 0.02)
    caplog.set_level(logging.INFO, logger="live")
    with ws_client.websocket_connect("/api/live/ws") as ws:
        sid = _setup_ok(ws)["session_id"]
        for _ in range(50):                                 # 1ª stats tem de chegar
            m = ws.receive()
            if not m.get("bytes") and _json.loads(m["text"])["type"] == "stats":
                break
        else:
            pytest.fail("nenhuma stats antes do fechar")
    for _ in range(100):                                    # fechou → limpeza rodou
        with app._live_lock:
            if sid not in app._live_sessions:
                break
        time.sleep(0.02)
    with app._live_lock:
        assert sid not in app._live_sessions
    fecha = [r.getMessage() for r in caplog.records if r.getMessage().startswith("fecha")]
    assert fecha and sid in fecha[-1]


def test_live_ws_log_só_metadados(ws_client, live_limpo, pipeline_fake, caplog):
    """Invariante de privacidade (#118): o log `live` NUNCA contém áudio nem
    texto transcrito/gerado — só metadados. O transcript SAI no fio
    (`transcript_user`), mas não pode vazar para o log."""
    caplog.set_level(logging.INFO, logger="live")
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        ws.send_bytes(b"\x01\x00" * 1600)
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, ("turn_complete",))["type"] == "turn_complete"
    texto = caplog.text
    assert "que horas são" not in texto                     # transcript do STT fake
    assert "dez" not in texto and "horas" not in texto      # deltas do LLM fake
    msgs = [r.getMessage() for r in caplog.records]         # formato: `evento chave=valor`
    assert any(m.startswith("abre") for m in msgs)
    assert any(m.startswith("stage_inicio") and "stage=llm" in m for m in msgs)
    assert any(m.startswith("stage_fim") and "stage=llm" in m for m in msgs)
    assert any(m.startswith("fecha") for m in msgs)


def test_gates_do_remoto_ligam_com_env_mesmo_com_settings_vazio(sem_bases_remotas, monkeypatch):
    """#112: o override do tradutor/STT era INERTE — os gates só olhavam o settings,
    então o cliente caía no local em silêncio (200 vazio em vez do erro do provedor)."""
    assert app._use_remote_translate() is False and app._use_remote_stt() is False
    monkeypatch.setenv("TTS_TRANSLATE_BASE_URL", "http://127.0.0.1:53992/v1")
    assert app._use_remote_translate() is True, "env sozinho tem de ativar o remoto"
    assert app._use_remote_stt() is True, "STT herda a base do tradutor"
    monkeypatch.delenv("TTS_TRANSLATE_BASE_URL")
    assert app._use_remote_translate() is False
    monkeypatch.setenv("TTS_STT_BASE_URL", "http://127.0.0.1:53992/v1")
    assert app._use_remote_stt() is True and app._use_remote_translate() is False


def test_gates_sem_env_seguem_a_regra_antiga(monkeypatch):
    for var in ("TTS_TRANSLATE_BASE_URL", "TTS_STT_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(app._settings, "remote_base_url", "")
    monkeypatch.setitem(app._settings, "remote_translate", True)
    monkeypatch.setitem(app._settings, "remote_stt", True)
    assert app._use_remote_translate() is False, "toggle sem base continua local"
    monkeypatch.setitem(app._settings, "remote_base_url", "http://rtx.example/v1")
    assert app._use_remote_translate() is True and app._use_remote_stt() is True


def test_traducao_com_env_usa_o_remoto_de_verdade(sem_bases_remotas, monkeypatch):
    """Caminho do cliente: com o env setado, `_translate` vai ao remoto (o chamador
    vê o erro do provedor em vez de cair no local)."""
    chamadas = []

    class _Resp:
        ok = True
        text = ""
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "olá"}}]}

    def _post(url, **kw):
        chamadas.append(url)
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setenv("TTS_TRANSLATE_BASE_URL", "http://127.0.0.1:53992/v1")
    assert app._translate("hello", "pt") == "olá"
    assert chamadas == ["http://127.0.0.1:53992/v1/chat/completions"]


# ---------------------------------------------------------------------------
# #114 — remoto morto cai no local: o cliente distingue "vazio" de "caiu".
# ---------------------------------------------------------------------------

@pytest.fixture()
def remoto_morto(monkeypatch):
    """Gate do STT ligado (env) + remoto que estoura + VAD dizendo "sem fala"."""
    monkeypatch.setenv("TTS_STT_BASE_URL", "http://127.0.0.1:53992/v1")
    def _morre(*a, **k):
        raise RuntimeError("HTTP 530 do provedor")
    monkeypatch.setattr(app, "_transcribe_remote", _morre)
    monkeypatch.setattr(app, "_vad_tem_fala", lambda p: False)


def _sobe_wav(client, rota="/api/transcribe", **kw):
    buf = io.BytesIO(_wav_bytes(0.2))
    return client.post(rota, headers=auth_headers(client),
                       files={"audio": ("a.wav", buf.getvalue(), "audio/wav")}, **kw)


def test_transcribe_avisa_quando_o_remoto_cai(client, remoto_morto):
    """Antes: 200 com texto vazio e só um print no servidor — indistinguível."""
    r = _sobe_wav(client)
    assert r.status_code == 200
    assert r.headers.get("x-tts-remote-fallback") == "1"
    assert "530" in r.headers.get("x-tts-remote-error", "")
    body = r.json()
    assert body["remote_fallback"] is True and body["remote_error"]
    assert body["text"] == ""


def test_transcribe_sem_queda_nao_avisa(client, monkeypatch):
    monkeypatch.setenv("TTS_STT_BASE_URL", "http://127.0.0.1:53992/v1")
    monkeypatch.setattr(app, "_transcribe_remote",
                        lambda p, lang: {"text": "ok", "language": "pt", "segments": []})
    r = _sobe_wav(client)
    assert r.status_code == 200 and r.json()["text"] == "ok"
    assert "x-tts-remote-fallback" not in r.headers
    assert "remote_fallback" not in r.json()


def test_openai_transcriptions_avisa_no_header_ate_em_texto(client, remoto_morto):
    """O formato `text` não tem corpo JSON: o header é o que cobre todos."""
    buf = io.BytesIO(_wav_bytes(0.2))
    r = client.post("/v1/audio/transcriptions", headers=auth_headers(client),
                    files={"file": ("a.wav", buf.getvalue(), "audio/wav")},
                    data={"response_format": "text"})
    assert r.status_code == 200 and r.headers.get("x-tts-remote-fallback") == "1"
