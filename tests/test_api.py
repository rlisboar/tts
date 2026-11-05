"""Testes da camada HTTP (TestClient) — sem carregar modelos MLX.

Cobre a classe de bug que escapou antes (nomes indefinidos dentro de funções
que só rodam em runtime/request).
"""

import concurrent.futures
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
import weakref
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


def _snapshot_dos_modulos(base=None) -> dict:
    """{nome: (mtime, bytes)} do conteúdo dos módulos do build, lido de `base`.

    Espelha `app._build_hash` (inclusive o `<ausente>` de módulo faltando), mas do
    lado de fora — os testes do /api/build apontam `app.BASE` para uma árvore de
    mentira e precisam do mesmo cálculo. #227: guardar os BYTES (e não reler o
    disco na asserção) é o que dá a prova de conteúdo com um escritor em curso —
    a releitura pega a fase oposta à do `codigo` e a prova virava moeda.
    """
    raiz = Path(base) if base is not None else app.BASE
    fora = {}
    for nome in app._BUILD_MODULOS:
        try:
            st = (raiz / nome).stat()
            fora[nome] = (st.st_mtime, (raiz / nome).read_bytes())
        except OSError:
            fora[nome] = (0.0, None)              # ausente conta como `<ausente>`
    return fora


def _hash_do_snapshot(snap: dict) -> str:
    h = hashlib.sha256()
    for nome in app._BUILD_MODULOS:
        h.update(snap[nome][1] if snap[nome][1] is not None else b"<ausente>")
    return h.hexdigest()[:8]


def _hash_dos_modulos(base=None) -> str:
    """sha256-8 do CONTEÚDO de AGORA dos módulos (releitura do disco)."""
    return _hash_do_snapshot(_snapshot_dos_modulos(base))


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
#
# #227: a expectativa vem do SNAPSHOT (bytes + mtimes tirados AQUI), não de uma
# releitura do disco na asserção — com um escritor em curso a releitura pega a fase
# oposta à do `codigo`. `_JANELA_SUJA` marca o caso em que a nossa cópia já não
# descreve o que o boot hasheou (escrita entre o boot e este import): só ali a prova
# de conteúdo não vale.
_SNAPSHOT = _snapshot_dos_modulos()
_HASH_NO_IMPORT = _hash_do_snapshot(_SNAPSHOT)
_JANELA_SUJA = [n for n, (m, _) in _SNAPSHOT.items() if m > app._BUILD_TS_HASH]


def _modulos_escritos_desde_o_boot(base=None) -> list:
    """Módulos do build escritos DEPOIS do hash do boot (#220).

    O critério é `mtime > app._BUILD_TS_HASH`: se alguém salvou o arquivo depois de o
    app hasheá-lo, o hash do boot pode não descrever o conteúdo de agora — e é isso
    que autoriza a tolerância do par, em vez de assumi-la. `mtime` e não comparação
    de conteúdo porque um escritor que volta ao estado anterior apagaria o rastro.

    #242: com o TS no FUTURO (relógio recuado por NTP, ou a sabotagem do gate) nenhum
    mtime é maior que ele e a tolerância ficava INERTE. Skew é assumido para o lado
    seguro: tudo conta como escrito (a tolerância liga e a prova de conteúdo segue
    valendo por `_HASH_NO_IMPORT`).
    """
    raiz = Path(base) if base is not None else app.BASE
    skew = app._BUILD_TS_HASH > time.time()
    escritos = []
    for nome in app._BUILD_MODULOS:
        try:
            if skew or (raiz / nome).stat().st_mtime > app._BUILD_TS_HASH:
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
    siga o boot e não o disco de agora.

    #227: a prova de CONTEÚDO não depende de a árvore estar quieta — o SNAPSHOT é
    anterior ao escritor, então ela vale igual com a árvore viva (é o que impede o
    campo de virar literal). A exceção é a escrita NA JANELA entre o boot e este
    import (`_JANELA_SUJA`): aí a nossa cópia descreve outro conteúdo."""
    assert d["codigo"] == app._BUILD_CODIGO, "o campo tem de ser o hash do BOOT"
    escritos = _modulos_escritos_desde_o_boot()
    if escritos:
        print(f"    [220] escritos depois do boot: {', '.join(escritos)} — "
              f"tolerância provada por mtime")
        disco = _hash_dos_modulos()
        if disco != app._BUILD_CODIGO:
            assert d["codigo"] != disco, "a rota seguiu o disco de agora, não o boot"
        if _JANELA_SUJA:
            return                       # a nossa cópia não descreve o boot
    assert d["codigo"] == _HASH_NO_IMPORT, \
        "hash é do CONTEÚDO dos módulos (expectativa congelada no import)"


@pytest.fixture()
def client():
    """Cliente de teste. O registro de `_jobs` é zerado pelo conftest
    (`_registro_limpo_entre_testes`, #505): aqui só se monta o cliente."""
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
    # `boot_ts`/`rev` são para o probe (#344): 200 sozinho não distingue "de pé" de
    # "de pé no código de ontem". O resto continua fora daqui (detalhe interno fica
    # no /api/build, que exige chave).
    corpo = r.json()
    assert corpo["ok"] is True
    assert isinstance(corpo["boot_ts"], int) and corpo["boot_ts"] > 0
    assert isinstance(corpo["rev"], str) and corpo["rev"]
    assert set(corpo) == {"ok", "boot_ts", "rev"}


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


def test_resolve_voice_pula_meta_sem_wav(monkeypatch, vozes_isoladas):
    """Fallback do clone não pode escolher a voz mais recente se ela não tem WAV.

    JSON sem amostra (ou preset virtual) quebra a síntese com FileNotFoundError
    no meio da frase. O pedido explícito por id continua devolvendo esse id —
    quem pediu, pediu. O fallback (voz ausente / desconhecida) é que pula.
    """
    vd, _ = vozes_isoladas
    (vd / "sem-wav.json").write_text(_json.dumps({
        "id": "sem-wav", "name": "Sem WAV", "created_at": "2099-01-01T00:00:00"}))
    monkeypatch.setitem(app._settings, "default_voice", None)
    for pedido in (None, "nao-existe"):
        escolhida = app._resolve_voice(pedido)
        assert escolhida != "sem-wav"
        assert app._voice_path(escolhida) is not None
    assert app._resolve_voice("sem-wav") == "sem-wav"
    monkeypatch.setitem(app._settings, "default_voice", "voz-ok")
    assert app._resolve_voice(None) == "voz-ok"


def test_nofile_nao_desce_quando_o_mlx_pede_2048():
    """O setrlimit do mlx_lm não pode baixar um teto que o boot já subiu.

    Sem privilégio, hard que desceu para 4096 não volta. Piso baixo ainda sobe.
    """
    inf = 2**63 - 1
    f = app._nofile_nao_desce
    assert f((65536, 65536), (2048, 4096)) == (65536, 65536)
    assert f((65536, inf), (2048, 4096)) == (65536, inf)
    assert f((256, 256), (2048, 4096)) == (2048, 4096)
    assert f((2048, 4096), (65536, 65536)) == (65536, 65536)
    assert f((100, inf), (50, 4096)) == (100, inf)


def test_guarda_setrlimit_nao_chama_o_real_se_nada_sobe():
    """Descer NOFILE vira no-op. Outro recurso, e uma subida, passam intactos."""
    import resource
    chamadas = []
    atual = {"nofile": (65536, 2**63 - 1)}

    def orig(which, limits):
        chamadas.append((which, limits))
        if which == resource.RLIMIT_NOFILE:
            atual["nofile"] = tuple(limits)

    def getrlimit(which):
        assert which == resource.RLIMIT_NOFILE
        return atual["nofile"]

    guarded = app._guarda_setrlimit(orig, getrlimit)
    guarded(resource.RLIMIT_NOFILE, (2048, 4096))
    assert chamadas == []
    assert atual["nofile"] == (65536, 2**63 - 1)
    # subir o soft vale; o hard ilimitado não pode ser cortado para 70000
    guarded(resource.RLIMIT_NOFILE, (70000, 70000))
    assert chamadas == [(resource.RLIMIT_NOFILE, (70000, 2**63 - 1))]
    guarded(resource.RLIMIT_CORE, (0, 0))
    assert chamadas[-1] == (resource.RLIMIT_CORE, (0, 0))


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


@pytest.fixture()
def auth_freio_folgado(monkeypatch):
    """Sem o freio de força bruta no caminho (#785): as células paramétricas fazem
    dezenas de 401 e passariam a receber 429 no meio."""
    monkeypatch.setattr(app, "_AUTH_FALHAS_MAX", 10 ** 6)
    app._auth_falhas.clear()
    yield
    app._auth_falhas.clear()


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
    app._apikeys_publica()          # o leitor sem lock usa o snapshot (#799)
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


def test_csp_nonce_nao_depende_da_forma_da_tag(tmp_path, monkeypatch):
    """A injeção do nonce não pode depender de a tag ser a linha exata `<script>`.

    Era o modo de falha da #76: `script-src` está SEM 'unsafe-inline', então uma
    tag que o `re.sub` não casasse derrubava a página INTEIRA em runtime, sem sinal
    (bastava um atributo, um `type="module"` ou o código na mesma linha — o
    backend inteiro documentava a fragilidade). Agora o critério é "script inline
    sem nonce", independente da forma.
    """
    alvo = tmp_path / "static"
    alvo.mkdir()
    (alvo / "index.html").write_text(
        '<script type="module">import x from "./x.js";</script>\n'
        '<script>const a = 1;</script>\n'
        '<script src="https://cdn.example/b.js" integrity="sha384-AAA"></script>\n'
        '<script id="ja-tem" nonce="antigo">const b = 2;</script>\n'
        '<script   defer  >const c = 3;</script>\n',
        encoding="utf-8")
    monkeypatch.setattr(app, "BASE", tmp_path)
    fora = app._index_com_nonce("NOVO")

    # 3 das 4 inline (a que já tinha nonce fica como está), de QUALQUER forma
    assert fora.count('nonce="NOVO"') == 3, fora
    assert '<script nonce="NOVO" type="module">' in fora
    assert '<script nonce="NOVO">const a = 1;' in fora
    assert '<script nonce="NOVO"   defer  >' in fora
    # com nonce próprio e o de CDN ficam como estão
    assert 'nonce="antigo"' in fora and 'nonce="NOVO" nonce=' not in fora
    assert '<script src="https://cdn.example/b.js"' in fora


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
    # /docs e /redoc montam a própria página (CSS do CDN): recebem a policy de
    # DOCS (#282) — não mais "sem CSP" — e continuam com X-Frame-Options
    for path in ("/docs", "/redoc"):
        r = client.get(path, headers=auth)      # /docs exige chave fora do loopback
        assert r.status_code == 200, path
        assert r.headers.get("content-security-policy") == app._CSP_DOCS, path
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
        app._apikeys_publica()
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


def _settings_em_disco() -> dict:
    """Conteúdo do `settings.json` EM USO. Materializa se ainda não existir: o
    caminho é o tmp da sessão e nem toda ordem de teste passa por um save antes —
    num worktree, que nasce sem o arquivo do dono, nenhuma passa (#659)."""
    if not app.SETTINGS_PATH.exists():
        app._save_settings()
    else:
        disco = _json.loads(app.SETTINGS_PATH.read_text())
        if not set(app._SETTINGS_DEFAULTS) <= set(disco):
            app._save_settings()
    return _json.loads(app.SETTINGS_PATH.read_text())


def test_save_preserva_chave_de_build_desconhecida(client, auth, monkeypatch):
    """#197: o save reescreve o arquivo inteiro a partir da RAM — uma chave que ESTA
    build não conhece (gravada por uma build nova) não pode ser apagada por tabela."""
    monkeypatch.setattr(app, "_settings_desconhecidas_avisadas", set())
    disco = _settings_em_disco()
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
    disco = _settings_em_disco()
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


def _tunel_fake(monkeypatch, *, rodando=True, destino="192.168.15.48"):
    """Substitui a leitura do processo do túnel (#346: o status reporta o destino)."""
    monkeypatch.setattr(app, "_destino_do_tunel", lambda: {
        "rodando": rodando, "destino": destino if rodando else "",
        "destino_e_lan": bool(rodando and destino), "porta_vps": "7860"})


def _conector_fake(monkeypatch, *, rodando=True):
    monkeypatch.setattr(app, "_cf_estado", lambda: {
        "rodando": rodando, "motivo": "" if rodando else "conector do TTS parado",
        "token_file": "x.token"})


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
    _tunel_fake(monkeypatch, rodando=False)
    _conector_fake(monkeypatch, rodando=False)
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
    _tunel_fake(monkeypatch, rodando=False)
    _conector_fake(monkeypatch, rodando=False)
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
    _tunel_fake(monkeypatch, rodando=True)
    _conector_fake(monkeypatch, rodando=True)
    assert client.post("/api/tunnel/start", headers=auth).status_code == 200
    _aguarda_launchctl()
    # kickstart -k no que já está no ar derrubaria a resposta desta requisição
    assert not any("kickstart" in c for c in chamadas)


def test_tunnel_sem_agente_instalado(client, auth, monkeypatch, tmp_path):
    _agentes_fake(tmp_path, monkeypatch, ssh=False, cloudflare=False)
    for rota in ("/api/tunnel/start", "/api/tunnel/stop", "/api/tunnel/restart"):
        r = client.post(rota, headers=auth)
        assert r.status_code == 400, rota


# --- #346: o status tem de ler o DESTINO (não o literal do bind) e casar o
# conector pelo token do próprio plist. Antes, `pgrep -f "ssh .*-R 127.0.0.1:7860"`
# não via o destino e mudar a porta quebrava a detecção; e qualquer conector da
# máquina (o do openwebui) fazia o do TTS parecer no ar.
def _ps_fake(monkeypatch, linhas):
    monkeypatch.setattr(app, "_ps_comandos", lambda: list(linhas))


def test_status_le_o_destino_do_tunel_e_denuncia_loopback(client, auth, monkeypatch, tmp_path):
    _agentes_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _conector_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [
        "  123 /usr/bin/ssh -N -R 127.0.0.1:7860:127.0.0.1:7860 eu@vps",
    ])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["tunnel_running"] is True
    assert d["tunnel_destino"] == "127.0.0.1"
    assert d["destino_e_lan"] is False, "loopback tem de aparecer como destino NÃO-LAN"


def test_status_aceita_lan_em_porta_diferente_da_padrao(client, auth, monkeypatch, tmp_path):
    """O literal `127.0.0.1:7860` na detecção antiga quebrava com outra porta."""
    _agentes_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _conector_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [
        "  999 /usr/bin/ssh -N -R 127.0.0.1:9876:192.168.15.48:7860 eu@vps",
    ])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["tunnel_running"] is True and d["destino_e_lan"] is True
    assert d["tunnel_destino"] == "192.168.15.48"


# --- #496: o conector é casado pelo token do próprio plist e o túnel tem de ser
# casado pelo NOSSO agente: qualquer `ssh -R` da máquina que viesse antes na lista
# fazia o status dizer "túnel rodando" com destino/porta de outro serviço.
def _plist_do_tunel(monkeypatch, tmp_path, host="eu@vps", porta="7860"):
    """Plist do NOSSO túnel com ProgramArguments de verdade (o `_agentes_fake`
    grava `<plist/>` vazio, que é o caso "sem individualização")."""
    _agentes_fake(tmp_path, monkeypatch)
    app._tunnel_plist().write_text(
        "<plist><dict><key>ProgramArguments</key><array>"
        "<string>/bin/zsh</string><string>/h/.tts-studio/tunnel.sh</string>"
        f"<string>{host}</string><string>{porta}</string>"
        "</array></dict></plist>")


def test_status_nao_confunde_com_outro_tunel_antes_na_lista(client, auth, monkeypatch, tmp_path):
    """Outro `ssh -R` (openwebui) primeiro na lista: o status tem de ler o do TTS,
    não o primeiro que aparecer."""
    _plist_do_tunel(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _conector_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [
        "  111 /usr/bin/ssh -N -R 127.0.0.1:8080:127.0.0.1:8080 eu@vps",
        "  222 /usr/bin/ssh -N -R 127.0.0.1:7860:192.168.15.48:7860 eu@vps",
    ])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["tunnel_running"] is True
    assert d["tunnel_destino"] == "192.168.15.48", "leu o túnel de OUTRO serviço"
    assert d["destino_e_lan"] is True


def test_status_nao_mente_verde_com_apenas_o_tunel_alheio(client, auth, monkeypatch, tmp_path):
    """Só o túnel do outro serviço está de pé: o do TTS está PARADO (antes o
    status dizia rodando com o destino alheio)."""
    _plist_do_tunel(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _conector_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [
        "  111 /usr/bin/ssh -N -R 127.0.0.1:8080:127.0.0.1:8080 eu@vps",
    ])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["tunnel_running"] is False and d["tunnel_destino"] == ""


def test_status_aceita_a_porta_do_plist_e_recusa_a_de_outro(client, auth, monkeypatch, tmp_path):
    """A porta remota do plist é o que individualiza: mesma VPS, portas diferentes."""
    _plist_do_tunel(monkeypatch, tmp_path, porta="9876")
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _conector_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [
        "  111 /usr/bin/ssh -N -R 127.0.0.1:7860:10.1.2.3:7860 eu@vps",
        "  222 /usr/bin/ssh -N -R 127.0.0.1:9876:192.168.15.48:7860 eu@vps",
    ])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["tunnel_destino"] == "192.168.15.48"


def test_status_sem_plist_legivel_prefere_a_chave_do_tts(client, auth, monkeypatch, tmp_path):
    """Sem ProgramArguments (instalação antiga), a chave `-i …/tts_tunnel` do
    nosso script desempata; sem nada disso, aceitar é melhor que ficar cego."""
    _agentes_fake(tmp_path, monkeypatch)          # plist vazio
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _conector_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [
        "  111 /usr/bin/ssh -N -R 127.0.0.1:8080:127.0.0.1:8080 -i /h/.ssh/outra eu@vps",
        "  222 /usr/bin/ssh -N -R 127.0.0.1:7860:192.168.15.48:7860 -i /h/.ssh/tts_tunnel eu@vps",
    ])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["tunnel_destino"] == "192.168.15.48"


def test_conector_nao_confunde_com_outro_cloudflared_da_maquina(client, auth, monkeypatch, tmp_path):
    """Conector do openwebui no ar NÃO pode fazer o do TTS parecer rodando."""
    _agentes_fake(tmp_path, monkeypatch)
    plist = app._cf_plist()
    plist.write_text("<plist><string>--token-file</string><string>/h/.cloudflared/tts.token</string></plist>")
    monkeypatch.setattr(app, "_tunnel_launchd_loaded", lambda: None)
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: True)
    _tunel_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [
        "  50 /opt/homebrew/bin/cloudflared tunnel --token-file /h/.cloudflared/openwebui.token run",
    ])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["cloudflared_running"] is False
    assert "parado" in d["cloudflared_motivo"]
    # com o token do TTS na linha de comando, sim
    _ps_fake(monkeypatch, [
        "  50 /opt/homebrew/bin/cloudflared tunnel --token-file /h/.cloudflared/openwebui.token run",
        "  51 /opt/homebrew/bin/cloudflared tunnel --token-file /h/.cloudflared/tts.token run",
    ])
    assert client.get("/api/tunnel/status", headers=auth).json()["cloudflared_running"] is True


def test_status_explica_plist_sem_token(client, auth, monkeypatch, tmp_path):
    _agentes_fake(tmp_path, monkeypatch)
    app._cf_plist().write_text("<plist/>")          # sem --token-file
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _tunel_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, ["  1 /opt/homebrew/bin/cloudflared tunnel run"])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["cloudflared_running"] is False and "token-file" in d["cloudflared_motivo"]


def test_label_do_conector_vem_do_ambiente(client, auth, monkeypatch, tmp_path):
    """`CLOUDFLARE_LABEL` é o mesmo knob do `cloudflare.sh`: o status tem de usar
    a label configurada em vez da fixa — senão a instalação com outra label
    aparece como "não instalado"."""
    _agentes_fake(tmp_path, monkeypatch, cloudflare=False)
    monkeypatch.setattr(app, "CF_LABEL", "com.exemplo.tts-custom")
    outro = tmp_path / "com.exemplo.tts-custom.plist"
    outro.write_text("<plist><string>--token-file</string><string>/h/x.token</string></plist>")
    monkeypatch.setattr(app, "_cf_plist", lambda: outro)
    monkeypatch.setattr(app, "_cf_launchd_loaded", lambda: False)
    _tunel_fake(monkeypatch, rodando=False)
    _ps_fake(monkeypatch, [])
    d = client.get("/api/tunnel/status", headers=auth).json()
    assert d["cloudflared_label"] == "com.exemplo.tts-custom"
    assert d["cloudflared_installed"] is True


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
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5198"
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
    # #780: sem curinga — o CORS ECOA a origem autorizada (a UI de dev do repo)
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5198"
    r = client.get("/api/status", headers={"Origin": "http://localhost:5198",
                                           "X-API-Key": "chave-errada"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5198"
    # preflight segue respondendo
    r = client.options("/api/status", headers={"Origin": "http://localhost:5198",
                                               "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5198"


def test_tunnel_status_estrutura(client, auth, monkeypatch):
    _tunel_fake(monkeypatch)
    monkeypatch.setattr(app, "_tunnel_launchd_loaded", lambda: True)
    _conector_fake(monkeypatch)
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
    # `_speaker_gate_ok` lê o estado (perfis + erro de leitura), não só o dict
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: ({}, ""))
    monkeypatch.setattr(app, "_speaker_load", lambda: {})
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)


def test_speaker_gate_off_nao_bloqueia(client, auth, perfis_vazios):
    app._settings["speaker_gate"] = "off"
    assert app._speaker_gate_ok(Path("x.wav")) == {}


def test_speaker_enforce_rejeita_e_aceita(client, auth, monkeypatch):
    VEC_A, VEC_B = [1.0, 0.0], [0.0, 1.0]
    perfis = {"eu": {"vecs": [VEC_A], "updated": 0}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: list(VEC_A))
    app._settings["speaker_gate"] = "enforce"
    app._settings["speaker_threshold"] = 0.75
    # mesma voz → passa
    assert app._speaker_gate_ok(Path("x.wav")) == {}
    # outra voz (similaridade 0) → rejeitada
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: list(VEC_B))
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out["rejected"] and "não" in out["reason"]
    # modo etiqueta: reconhece e rotula
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: list(VEC_A))
    app._settings["speaker_gate"] = "label"
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out["speaker"] == "eu" and out["speaker_sim"] >= 0.99
    # sem perfis cadastrados: gate inerte
    app._settings["speaker_gate"] = "enforce"
    monkeypatch.setattr(app, "_speaker_load", lambda: {})
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: ({}, ""))
    assert app._speaker_gate_ok(Path("x.wav")) == {}
    app._settings["speaker_gate"] = "off"
    app._settings["speaker_threshold"] = 0.75


def test_transcribe_avisa_a_escala_do_escore_quando_o_limiar_e_asnorm(client, auth, monkeypatch):
    """Com FAR alvo o escore é AS-normalizado e a resposta diz o modo.

    Sem este campo o cliente (e o "Testar" da tela) comparava um número
    normalizado com o `speaker_threshold` cru — 2,4 contra 0,60 (#525)."""
    perfis = {"eu": {"vecs": [[1.0, 0.0]], "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [1.0, 0.0])
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "pode mandar o texto", "language": "pt", "segments": []})
    monkeypatch.setitem(app._settings, "speaker_gate", "label")
    monkeypatch.setattr(app, "_speaker_limiar", lambda m: (0.5, "asnorm"))
    r = client.post("/api/transcribe", headers=auth, data={"source_lang": "pt"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    corpo = r.json()
    assert corpo["speaker"] == "eu", corpo
    assert corpo["speaker_escore_modo"] == "asnorm"
    # no caminho CRU o campo não aparece: ali o número se compara com o rigor direto
    monkeypatch.setattr(app, "_speaker_limiar", lambda m: (0.6, "cru"))
    r = client.post("/api/transcribe", headers=auth, data={"source_lang": "pt"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert "speaker_escore_modo" not in r.json()


def test_speaker_perfil_de_outro_extrator_autentica_e_pede_recadastro(
        client, auth, monkeypatch):
    """Perfil gravado pelo extrator ANTIGO segue valendo, marcado para recadastro.

    Vetor de modelos diferentes não se compara: sem o `modelo` no perfil a
    atualização de extrator trancaria o dono fora em `enforce` (#525). A regra:
    o gate pontua cada perfil pelo extrator que o gerou."""
    legado = {"eu": {"vecs": [[1.0, 0.0]]}}          # sem `modelo` = Resemblyzer
    # simula o DEPOIS do R2: o extrator atual é outro. Com os dois iguais (o
    # estado de hoje) nada seria "legado" e o teste passaria vazio.
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)   # extrator novo
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (legado, ""))
    # o extrator NOVO devolve outro espaço: só acerta quem pontuar pelo extrator
    # que gerou o vetor (com o atual para todo mundo, a similaridade cai a zero)
    pedidos = []

    def _embed(_p, modelo=None):
        pedidos.append(modelo)
        return [0.0, 1.0] if modelo == "ecapa:v1" else [1.0, 0.0]

    monkeypatch.setattr(app, "_speaker_embed", _embed)
    app._settings["speaker_gate"] = "enforce"
    try:
        assert app._speaker_gate_ok(Path("x.wav")) == {}, "perfil legado foi rejeitado"
        assert app.SPEAKER_MODELO_LEGADO in pedidos

        monkeypatch.setattr(app, "_speaker_load", lambda: legado)
        lista = client.get("/api/speaker/profiles", headers=auth).json()
        assert lista[0]["precisa_recadastrar"] is True
        assert lista[0]["modelo"] == app.SPEAKER_MODELO_LEGADO
    finally:
        app._settings["speaker_gate"] = "off"


def test_speaker_escore_usa_centroide_e_nao_o_maximo(monkeypatch):
    """Média das amostras decide; uma amostra fora não puxa o perfil sozinha.

    O R2 mediu o centróide vencendo o máximo em todos os modelos — com máximo, uma
    gravação ruidosa (aqui `[0, 1]`) dá escore 1,0 para uma voz que não é do
    perfil em `[1, 0]`; o centróide de `[[1,0],[0,1]]` dá 0,707."""
    perfis = {"eu": {"vecs": [[1.0, 0.0], [0.0, 1.0]], "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [1.0, 0.0])
    _nome, sim, houve, _modo = app._speaker_pontua(Path("x.wav"), perfis)
    assert houve and abs(sim - 0.7071) < 1e-3


# --- #599: o extrator vencedor do R2 (ECAPA) precisa LIGAR --------------------
# O código checava um `tools/bakeoff/ecapa_voxceleb.pt` que nunca existiu, então
# `_speaker_modelo_efetivo()` devolvia o legado em qualquer instalação e nenhum
# perfil nascia ECAPA — o AS-norm por FAR ficava inalcançável na prática.
def test_speaker_ecapa_ok_exige_o_DIRETORIO_do_extrator(tmp_path, monkeypatch):
    """Liga pelo layout do SpeechBrain (hyperparams + pesos), não por um `.pt`."""
    monkeypatch.setattr(app, "_SPEAKER_MODELO_DIR", tmp_path)
    assert app._speaker_ecapa_ok() is False, "diretório vazio não pode ligar o ECAPA"
    for arq in app._SPEAKER_ARQ_OBRIG:
        (tmp_path / arq).write_bytes(b"x")
    assert app._speaker_ecapa_ok() is True
    # #598: o `label_encoder.ckpt` é o que o `Pretrainer` busca no load; sem ele a
    # disponibilidade dizia True e o embedding voltava None, calado.
    (tmp_path / "label_encoder.ckpt").unlink()
    assert app._speaker_ecapa_ok() is False, (
        "sem o label_encoder.ckpt o load offline morre — não pode se dizer disponível")
    (tmp_path / "label_encoder.ckpt").write_bytes(b"x")
    (tmp_path / "embedding_model.ckpt").unlink()
    assert app._speaker_ecapa_ok() is False, "sem os pesos o load falharia em toda amostra"


def test_speaker_modelo_dir_respeita_o_ambiente(tmp_path):
    """`TTS_SPEAKER_MODELO_DIR` é o contrato do `models/README.md` e era ignorado."""
    env = {**os.environ, "TTS_SPEAKER_MODELO_DIR": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    saida = __import__("subprocess").run(
        [sys.executable, "-c", "import app; print(app._SPEAKER_MODELO_DIR)"],
        cwd=str(Path(__file__).resolve().parent.parent), env=env, check=True,
        capture_output=True, text=True)
    assert saida.stdout.strip() == str(tmp_path)


def test_extrator_nao_e_carregado_no_boot(tmp_path):
    """Importar o app NÃO carrega encoder nenhum (#606).

    O ECAPA custa ~460 MB de RSS e ~0,5 s (medido no #526) e o boot só sobe HTTP:
    a carga é do primeiro uso de identidade. Sem esta célula, um `EncoderClassifier`
    no import passava despercebido — e com ele o boot inteiro ficava refém do
    torch. Também prova que `_speaker_modelo_efetivo()` só CONFERE, não carrega."""
    env = {**os.environ, "TTS_SPEAKER_MODELO_DIR": str(tmp_path),
           "TTS_SETTINGS_PATH": str(tmp_path / "settings.json"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_ROD_API_KEY", "TTS_ADMIN_KEY"):
        env.pop(var, None)
    # além dos encoders, os MÓDULOS: um `import torch`/`speechbrain`/`resemblyzer`
    # no boot (sem instanciar nada) passaria pela checagem de `None` e já custaria
    # a carga da lib (#638)
    saida = __import__("subprocess").run(
        [sys.executable, "-c",
         "import sys, app; app._speaker_modelo_efetivo();"
         "print(app._speaker_ecapa_enc is None, app._speaker_encoder is None,"
         "      'torch' in sys.modules, 'speechbrain' in sys.modules,"
         "      'resemblyzer' in sys.modules)"],
        cwd=str(Path(__file__).resolve().parent.parent), env=env, check=True,
        capture_output=True, text=True)
    assert saida.stdout.split() == ["True", "True", "False", "False", "False"], (
        f"boot tocou no extrator: {saida.stdout.split()} "
        f"(encoders None, None e torch/speechbrain/resemblyzer fora de sys.modules)")


def _enc_falso():
    """Encoder de mentira com a forma que o código usa."""
    class _Enc:
        def encode_batch(self, _t):
            return types.SimpleNamespace(squeeze=lambda: types.SimpleNamespace(
                numpy=lambda: np.array([3.0, 4.0])))
    return _Enc()


class _SemGrad:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_speaker_ecapa_e_liberado_por_ociosidade_e_recarrega(tmp_path, monkeypatch,
                                                             client, auth):
    """Passado o TTL, o encoder é SOLTO (~460 MB) e a próxima consulta recarrega.

    Mesma política do classificador do #507 (#612). O veredito não muda com o
    reload: é o mesmo modelo, só caro."""
    relogio = [1000.0]
    monkeypatch.setattr(app, "_SPEAKER_TTL_S", 600.0)
    monkeypatch.setattr(app.time, "monotonic", lambda: relogio[0])
    monkeypatch.setattr(app, "_wav_to_mono16k", lambda p: np.ones(32000, dtype=np.float32))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        from_numpy=lambda x: x, no_grad=_SemGrad))
    cargas = []

    def _carrega():                    # substitui o load real, inclusive o estado
        cargas.append(1)
        app._speaker_ecapa_enc = _enc_falso()
        app._speaker_ultimo_uso = app.time.monotonic()
        return app._speaker_ecapa_enc

    monkeypatch.setattr(app, "_speaker_encoder_ecapa", _carrega)
    app._speaker_embed_ecapa(Path("x.wav"))          # carrega (1)
    assert len(cargas) == 1 and app._speaker_ecapa_enc is not None
    # referência FRACA: o que tem de morrer é o OBJETO, não só o global do módulo
    # (regressão que deixa o encoder preso em outro lugar passava sem isto, #627)
    fraco = weakref.ref(app._speaker_ecapa_enc)
    app._speaker_vigia_passo()
    assert app._speaker_ecapa_enc is not None, "sem ociosidade não pode liberar"

    relogio[0] += 601.0                              # passou do TTL
    assert app._speaker_vigia_passo() is True
    assert app._speaker_ecapa_enc is None, "o encoder ficou carregado depois do TTL"
    assert fraco() is None, "a referência sumiu mas o encoder CONTINUA vivo (preso)"
    # e a próxima consulta recarrega e devolve o MESMO vetor
    vetor = app._speaker_embed_ecapa(Path("x.wav"))
    assert len(cargas) == 2, "não recarregou depois de liberar"
    assert [round(v, 3) for v in vetor] == [0.6, 0.8]


def test_speaker_ttl_respeita_o_ambiente(tmp_path):
    """`TTS_SPEAKER_TTL_S` muda o TTL (o dono devolve a RAM quando quiser)."""
    env = {**os.environ, "TTS_SPEAKER_TTL_S": "42",
           # caminho POR EXECUÇÃO: antes era literal fixo (classe #12)
           "TTS_SETTINGS_PATH": str(tmp_path / "settings.json"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    saida = __import__("subprocess").run(
        [sys.executable, "-c", "import app; print(app._SPEAKER_TTL_S)"],
        cwd=str(Path(__file__).resolve().parent.parent), env=env, check=True,
        capture_output=True, text=True)
    assert saida.stdout.strip() == "42.0"


def test_speaker_fallback_avisa_uma_vez_e_aparece_no_get(tmp_path, monkeypatch, capsys,
                                                        client, auth):
    """Sem pesos, a queda para o Resemblyzer AVISA (log 1×) e sai no GET (#606).

    O fallback era silencioso: a instalação ficava no extrator legado para sempre
    e nem a tela nem o log diziam por quê. `motivo` tem de ser acionável."""
    monkeypatch.setattr(app, "_SPEAKER_MODELO_DIR", tmp_path)      # diretório vazio
    monkeypatch.setattr(app, "_speaker_fallback_avisado", False)
    motivo = app._speaker_ecapa_motivo()
    assert "baixa_modelos.sh" in motivo and "ausente" in motivo
    assert app._speaker_modelo_efetivo() == app.SPEAKER_MODELO_LEGADO
    assert app._speaker_modelo_efetivo() == app.SPEAKER_MODELO_LEGADO   # 2ª: cala
    assert app._speaker_fallback_avisado is True
    # o aviso é `print` no STDERR — por isso `capsys` (o `caplog` não vê `print`,
    # e era o furo que deixava a célula verde com o aviso apagado, #638). Exatamente
    # UMA linha, apesar de duas chamadas: a guarda é o que segura o ruído.
    linhas = [ln for ln in capsys.readouterr().err.splitlines() if "extrator legado" in ln]
    assert len(linhas) == 1, f"aviso saiu {len(linhas)}x (esperado 1): {linhas}"
    # e o estado é VISÍVEL por fora, acionável: GET de settings e /api/status
    s = client.get("/api/settings", headers=auth).json()
    assert s["extrator_efetivo"] == app.SPEAKER_MODELO_LEGADO
    assert "baixa_modelos.sh" in s["extrator_motivo"]
    assert client.get("/api/status", headers=auth).json()["extrator_locutor"] == \
        app.SPEAKER_MODELO_LEGADO
    # com os pesos no lugar não há motivo nem aviso
    for arq in app._SPEAKER_ARQ_OBRIG:
        (tmp_path / arq).write_bytes(b"x")
    monkeypatch.setattr(app, "_speaker_fallback_avisado", False)
    assert app._speaker_ecapa_motivo() == ""
    assert app._speaker_modelo_efetivo() == app.SPEAKER_MODELO_ATUAL
    assert "extrator_motivo" not in client.get("/api/settings", headers=auth).json()


def test_speaker_embed_escolhe_o_extrator_do_modelo_pedido(monkeypatch):
    """`modelo` decide o extrator: ECAPA só no atual e com os pesos; senão legado."""
    monkeypatch.setattr(sys, "modules", {**sys.modules})   # isola o import do legado
    sys.modules["resemblyzer"] = types.SimpleNamespace(
        VoiceEncoder=lambda: types.SimpleNamespace(embed_utterance=lambda f: [1.0, 0.0]),
        preprocess_wav=lambda p: np.zeros(16000, dtype=np.float32))
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: False)
    assert app._speaker_embed(Path("x.wav"), app.SPEAKER_MODELO_ATUAL) == [1.0, 0.0]

    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)
    ecapa = []
    monkeypatch.setattr(app, "_speaker_embed_ecapa", lambda p: ecapa.append(p) or [0.0, 2.0])
    assert app._speaker_embed(Path("x.wav"), app.SPEAKER_MODELO_ATUAL) == [0.0, 2.0]
    assert ecapa == [Path("x.wav")], "com os pesos, o modelo atual tem de ir pelo ECAPA"
    # perfil LEGADO continua pontuado pelo Resemblyzer, mesmo com o ECAPA disponível
    assert app._speaker_embed(Path("x.wav"), app.SPEAKER_MODELO_LEGADO) == [1.0, 0.0]


def test_speaker_embed_ecapa_normaliza_e_respeita_o_piso_de_duracao(monkeypatch):
    """Vetor L2-normalizado (é cosseno que se compara) e < 0,6 s não gera embedding."""
    class _T:
        """Parecido com `torch.Tensor` no mínimo que o código usa: `[None]`, `squeeze()`,
        `numpy()`. O `squeeze` de verdade devolve (192,), não (1,192) — o fake faz igual."""
        def __getitem__(self, _i): return self
        def squeeze(self): return _Achatado()
        def numpy(self): return np.array([[3.0, 4.0]])

    class _Achatado:
        def numpy(self): return np.array([3.0, 4.0])

    class _Enc:
        def encode_batch(self, _t): return _T()

    class _SemGrad:
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    monkeypatch.setitem(sys.modules, "torch",
                        types.SimpleNamespace(from_numpy=lambda x: x, no_grad=_SemGrad))
    monkeypatch.setattr(app, "_speaker_encoder_ecapa", lambda: _Enc())
    monkeypatch.setattr(app, "_wav_to_mono16k", lambda p: np.ones(32000, dtype=np.float32))
    vetor = app._speaker_embed_ecapa(Path("x.wav"))
    assert abs(float(np.linalg.norm(vetor)) - 1.0) < 1e-6, "o embedding tem de sair normalizado"
    assert [round(v, 3) for v in vetor] == [0.6, 0.8]
    monkeypatch.setattr(app, "_wav_to_mono16k", lambda p: np.ones(8000, dtype=np.float32))
    assert app._speaker_embed_ecapa(Path("x.wav")) is None


def test_speaker_check_pontua_como_o_gate_e_diz_o_veredito(client, auth, monkeypatch):
    """O "Testar" tem de reproduzir o gate: centróide, limiar e modo efetivos.

    Antes pontuava pelo MÁXIMO das amostras e devolvia só `sim`: com
    `[[1,0],[0,1]]` e a voz em `[1,0]` o máximo dá 1,0 (passa) e o centróide
    0,707 (reprova com rigor 0,75) — a tela dizia "✅ passa" e o gate barrava."""
    perfis = {"eu": {"vecs": [[1.0, 0.0], [0.0, 1.0]],
                     "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [1.0, 0.0])
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".vozchk": Path("f.wav"))
    monkeypatch.setitem(app._settings, "speaker_threshold", 0.75)
    r = client.post("/api/speaker/check", headers=auth,
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    corpo = r.json()
    assert corpo["speaker"] == "eu"
    assert abs(corpo["sim"] - 0.707) < 1e-2, "pontuou pelo máximo, não pelo centróide"
    assert corpo["limiar"] == 0.75 and corpo["modo"] == "cru" and corpo["passaria"] is False
    # `autorizado` é o nome anterior do MESMO veredito e segue saindo (a tela lê os
    # dois por feature-detect; nada de quebrar cliente velho)
    assert corpo["autorizado"] is False
    # abaixo do rigor o veredito do servidor acompanha (nada de conta na tela)
    monkeypatch.setitem(app._settings, "speaker_threshold", 0.6)
    corpo = client.post("/api/speaker/check", headers=auth,
                        files={"audio": ("a.wav", b"x", "audio/wav")}).json()
    assert corpo["passaria"] is True and corpo["autorizado"] is True


STORE: dict = {}


def _perfil_legado_com_ecapa(monkeypatch):
    """Perfil do extrator ANTIGO e o ECAPA disponível, com o dublê de dimensões."""
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".voz": Path("f.wav"))
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None:
                        [0.5, 0.5] if modelo == app.SPEAKER_MODELO_ATUAL
                        else [0.5, 0.5] + [0.0] * 254)
    STORE.clear()
    STORE["eu"] = {"vecs": [[1.0, 0.0] + [0.0] * 254, [0.9, 0.1] + [0.0] * 254],
                   "modelo": app.SPEAKER_MODELO_LEGADO, "updated": 0}
    monkeypatch.setattr(app, "_speaker_load", lambda: STORE)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)


def test_speaker_enroll_em_perfil_legado_recadastra_sem_500(client, auth, monkeypatch):
    """Gravar num perfil LEGADO com o ECAPA ligado é o RECADASTRO (#534).

    O perfil segue autenticando pelo extrator antigo enquanto o novo não tem o
    mínimo — a amostra vai para `pendente`, e nada compara 192 x 256 (era 500)."""
    _perfil_legado_com_ecapa(monkeypatch)
    r = client.post("/api/speaker/enroll", headers=auth,
                    data={"name": "eu"}, files={"audio": ("v.wav", b"x", "audio/wav")})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["samples"] == 1 and corpo["completo"] is False and corpo["recadastro"] is True
    assert corpo["em_uso"] == app.SPEAKER_MODELO_LEGADO, "o perfil perdeu o extrator que autentica"
    p = STORE["eu"]
    assert p["modelo"] == app.SPEAKER_MODELO_LEGADO and len(p["vecs"]) == 2, "legado foi zerado"
    assert p["pendente"]["modelo"] == app.SPEAKER_MODELO_ATUAL
    assert len(p["pendente"]["vecs"]) == 1
    # no 3º do espaço novo o recadastro conclui: o ECAPA passa a valer e o antigo sai
    for _ in range(2):
        corpo = client.post("/api/speaker/enroll", headers=auth,
                            data={"name": "eu"},
                            files={"audio": ("v.wav", b"x", "audio/wav")}).json()
    assert corpo["completo"] is True and corpo["recadastro"] is False
    assert "pendente" not in STORE["eu"]
    assert STORE["eu"]["modelo"] == app.SPEAKER_MODELO_ATUAL and len(STORE["eu"]["vecs"]) == 3
    # e o perfil sai da lista como completo, sem `precisa_recadastrar`
    lista = client.get("/api/speaker/profiles", headers=auth).json()
    assert lista[0]["modelo"] == app.SPEAKER_MODELO_ATUAL
    assert lista[0]["precisa_recadastrar"] is False and lista[0]["pendente"] is None


def test_speaker_check_em_perfil_legado_com_ecapa_nao_estoura(client, auth, monkeypatch):
    """O diagnóstico do "Testar" também não pode comparar espaços diferentes."""
    _perfil_legado_com_ecapa(monkeypatch)
    r = client.post("/api/speaker/check", headers=auth,
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 200, r.text
    assert r.json()["modo"] == "cru" and r.json()["passaria"] in (True, False)


def _coorte_falsa(monkeypatch, vecs):
    """Coorte sintética no cache: evita depender do arquivo real do repositório."""
    monkeypatch.setattr(app, "_speaker_cohorte_cache", {app._speaker_modelo_efetivo(): vecs})


def test_speaker_far_alvo_normaliza_o_escore_e_usa_o_limiar_calibrado(monkeypatch):
    """Com alvo de FAR o escore é AS-normalizado e o limiar vem do R2.

    A coorte é de impostores: quanto mais longe dela, maior o escore. O limiar
    calibrado (2,434 para FAR 1 %) é em unidades normalizadas — comparar com o
    0,6 cru seria erro de escala."""
    perfis = {"eu": {"vecs": [[1.0, 0.0]], "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    _coorte_falsa(monkeypatch, [[0.0, 1.0], [0.1, 0.99], [0.2, 0.98],
                                [0.3, 0.95], [0.4, 0.92]])
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [1.0, 0.0])
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1")
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    try:
        lim, modo = app._speaker_limiar(app._speaker_modelo_efetivo())
        assert modo == "asnorm"
        # limiar da tabela calibrada PARA ESTE modelo (o R2 calibrou por modelo)
        assert lim == app._SPEAKER_LIMIAR_FAR[app._speaker_modelo_efetivo()]["1"]
        _n, sim, _h, modo_escore = app._speaker_pontua(Path("x.wav"), perfis)
        assert modo_escore == "asnorm"
        # mesma voz (cosseno 1,0 contra a coorte toda em 0) ⇒ escore normalizado
        # bem acima do limiar de FAR 1 %, com o modo declarado na resposta
        assert sim > lim
        out = app._speaker_gate_ok(Path("x.wav"))
        assert out == {"speaker_escore_modo": "asnorm"}, out
    finally:
        monkeypatch.setitem(app._settings, "speaker_far_alvo", "")
        monkeypatch.setitem(app._settings, "speaker_gate", "off")


def test_speaker_sem_pesos_cai_no_extrator_legado(client, auth, monkeypatch):
    """Sem lib/pesos do extrator novo, o app usa o legado e MARCA assim.

    Sem download em runtime (#525): a ausência de `speechbrain` ou do arquivo de
    pesos não pode gerar perfil etiquetado com um extrator que não roda — nasceria
    "precisa recadastrar" e o gate compararia espaços diferentes."""
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: False)
    assert app._speaker_modelo_efetivo() == app.SPEAKER_MODELO_LEGADO
    store = {}
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 5.0)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 30.0)
    r = _enroll(client, auth, monkeypatch, store)
    assert r.status_code == 200, r.text
    assert store["eu"]["modelo"] == app.SPEAKER_MODELO_LEGADO
    assert r.json()["modelo"] == app.SPEAKER_MODELO_LEGADO
    # e com o extrator novo disponível o mesmo código o usa
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)
    assert app._speaker_modelo_efetivo() == app.SPEAKER_MODELO_ATUAL


def test_speaker_coorte_do_repositorio_carrega():
    """O arquivo de coorte versionado tem de servir o extrator ATUAL.

    Sem ele o alvo de FAR nunca vale — e `_speaker_cohorte` engole o erro e cai no
    escore cru, então o defeito passaria calado."""
    app._speaker_cohorte_cache.clear()
    vecs = app._speaker_cohorte(app._speaker_modelo_efetivo())
    assert len(vecs) >= 20, "coorte versionada ausente/curta para o extrator atual"
    assert all(len(v) == len(vecs[0]) for v in vecs), "vetores de dimensões diferentes"


def test_speaker_far_alvo_sem_coorte_cai_no_escore_cru(monkeypatch):
    """Sem arquivo de coorte o alvo não inventa limiar: volta ao cru e ao 0,6."""
    _coorte_falsa(monkeypatch, [])
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "0.1")
    monkeypatch.setitem(app._settings, "speaker_threshold", 0.6)
    try:
        lim, modo = app._speaker_limiar(app._speaker_modelo_efetivo())
        assert modo == "cru" and lim == 0.6
    finally:
        monkeypatch.setitem(app._settings, "speaker_far_alvo", "")


def test_speaker_setting_do_far_alvo_aceita_so_os_alvos(monkeypatch):
    """A setting só guarda o que a calibração cobre (1 % e 0,1 %)."""
    for entrada, esperado in (("1", "1"), ("0,1", "0.1"), ("0.1", "0.1"),
                              ("5", ""), ("", ""), (None, "")):
        limpo = app._apply_settings({"speaker_far_alvo": entrada})
        assert app._settings["speaker_far_alvo"] == esperado, entrada
        del limpo


def _enroll(client, auth, monkeypatch, store, emb=None, nome="eu", forcar=False):
    monkeypatch.setattr(app, "_save_audio_upload",
                        lambda up, prefix=".voz": Path("falso.wav"))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: list(emb or [1.0, 0.0]))
    monkeypatch.setattr(app, "_speaker_load", lambda: store)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)
    url = "/api/speaker/enroll" + ("?forcar=true" if forcar else "")
    return client.post(url, headers=auth, data={"name": nome},
                       files={"audio": ("a.wav", b"x", "audio/wav")})


def test_speaker_cadastro_recusa_fala_curta_e_ruido(client, auth, monkeypatch):
    """Fala curta e ruído alto são recusados DIZENDO o motivo (#525).

    Duração de FALA pelo VAD, não de arquivo — e a amostra só entra com
    `forcar=true`, devolvendo os problemas para o dono ver."""
    store = {}
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 1.2)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 25.0)
    r = _enroll(client, auth, monkeypatch, store)
    assert r.status_code == 400 and "fala curta" in r.json()["detail"]
    assert store == {} or not store.get("eu")

    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 5.0)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 4.0)
    r = _enroll(client, auth, monkeypatch, store)
    assert r.status_code == 400 and "ruído" in r.json()["detail"]

    r = _enroll(client, auth, monkeypatch, store, forcar=True)
    assert r.status_code == 200, r.text
    assert r.json()["problemas"], "forçar tem de devolver os problemas"


def test_speaker_cadastro_recusa_amostra_que_destoa(client, auth, monkeypatch):
    """Amostra cuja média contra o centróide fica abaixo do limiar não entra."""
    store = {"eu": {"vecs": [[1.0, 0.0], [0.98, 0.2]],
                    "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 5.0)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 30.0)
    monkeypatch.setitem(app._settings, "speaker_threshold", 0.6)
    r = _enroll(client, auth, monkeypatch, store, emb=[0.1, 0.99])   # ~0,2 < 0,6
    assert r.status_code == 400 and "destoa" in r.json()["detail"]
    r = _enroll(client, auth, monkeypatch, store, emb=[0.1, 0.99], forcar=True)
    assert r.status_code == 200 and r.json()["samples"] == 3


def test_speaker_cadastro_diz_quantas_amostras_faltam(client, auth, monkeypatch):
    """Menos de 3 amostras não é erro: é cadastro INCOMPLETO, com o que falta."""
    store = {}
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 5.0)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 30.0)
    r = _enroll(client, auth, monkeypatch, store)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["completo"] is False and corpo["faltando"] == ["mais 2 amostra(s)"]
    r = _enroll(client, auth, monkeypatch, store, emb=[0.99, 0.05])
    assert r.json()["faltando"] == ["mais 1 amostra(s)"]
    r = _enroll(client, auth, monkeypatch, store, emb=[0.98, 0.1])
    assert r.json()["completo"] is True and r.json()["faltando"] == []


def test_speaker_enroll_lista_e_apaga(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".voz": Path("falso.wav"))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [0.5, 0.5])
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


def test_speaker_far_alvo_so_aceita_os_alvos_calibrados_e_sai_no_get(client, auth):
    """O FAR alvo é lista fechada (1 % e 0,1 %), normalizada e VISÍVEL no GET.

    O seletor na tela entra no #528; enquanto ele não existe, quem liga é o POST —
    e o GET tem de devolver o valor para a tela saber que o modo normalizado está
    ativo (`speaker_threshold` deixa de decidir)."""
    r = client.post("/api/settings", headers=auth, json={"speaker_far_alvo": "0,1"})
    assert r.status_code == 200 and r.json()["speaker_far_alvo"] == "0.1"
    assert client.get("/api/settings", headers=auth).json()["speaker_far_alvo"] == "0.1"
    # fora da calibração não vira "quase": limpa e volta ao escore cru
    client.post("/api/settings", headers=auth, json={"speaker_far_alvo": "5"})
    assert client.get("/api/settings", headers=auth).json()["speaker_far_alvo"] == ""
    client.post("/api/settings", headers=auth, json={"speaker_far_alvo": ""})


def test_speaker_anticlone_desligado_nao_muda_a_resposta(monkeypatch):
    """GANCHO do anti-clone: desligado (default) nem o campo aparece (#525).

    Ligado, o gate consulta a MARCA d'água e classifica o áudio — sem mexer no
    veredito, que é política do #613."""
    import marca_dagua

    perfis = {"eu": {"vecs": [[1.0, 0.0]], "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [1.0, 0.0])
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "detecta_prob", lambda p: 0.97)
    monkeypatch.setitem(app._settings, "speaker_gate", "label")
    monkeypatch.setitem(app._settings, "speaker_anticlone", False)
    sem = app._speaker_gate_ok(Path("x.wav"))
    assert sem.get("speaker") == "eu" and "anticlone" not in sem
    # LIGADO: classifica pela marca e NÃO mexe no veredito
    monkeypatch.setitem(app._settings, "speaker_anticlone", True)
    com = app._speaker_gate_ok(Path("x.wav"))
    assert com["anticlone"] == "marcado" and com["anticlone_prob"] == 0.97
    assert {k: v for k, v in com.items()
            if k not in ("anticlone", "anticlone_prob", "speaker_aviso")} == sem
    # áudio cru e detector fora também são ditos, sem aprovar nem reprovar
    monkeypatch.setattr(marca_dagua, "detecta_prob", lambda p: 0.02)
    assert app._speaker_gate_ok(Path("x.wav"))["anticlone"] == "cru"
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: False)
    monkeypatch.setattr(marca_dagua, "motivo", lambda: "pesos ausentes")
    fora = app._speaker_gate_ok(Path("x.wav"))
    assert fora["anticlone"] == "indisponivel" and fora["anticlone_motivo"]


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


def test_youtube_redirect_q_tambem_entra_na_allowlist(client, auth, monkeypatch,
                                                      limiter_limpo):
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


def test_youtube_audio_400_quando_sai_da_allowlist(client, auth, monkeypatch,
                                                   limiter_limpo):
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
        # limpeza APAGA arquivo do dono: `use` não decide (#543)
        "auto_cleanup": True, "auto_cleanup_minutes": 7,
        "auto_cleanup_max_outputs": 1,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["admin_ignored"]) == {
        "remote_base_url", "remote_tts", "remote_tts_url", "model", "chat_api_key",
        "auto_cleanup", "auto_cleanup_minutes", "auto_cleanup_max_outputs"}
    # o que é de uso passou e foi persistido
    assert body["speed"] == 2.5 and body["pre_prompt"] == "cabeçalho"
    assert app._settings["speed"] == 2.5
    # as conexões e a limpeza ficaram intactas (não é 403: a UI manda blob único)
    for k in ("remote_base_url", "remote_tts", "remote_tts_url", "model",
              "chat_api_key", "auto_cleanup", "auto_cleanup_minutes",
              "auto_cleanup_max_outputs"):
        assert app._settings[k] == antes[k], k


def test_default_do_limiar_de_locutor_bate_com_a_ui():
    """O default espelhado na UI tem de ser o do SERVIDOR (#525).

    O campo mostrava 0,75 quando o settings vinha sem valor, e o servidor aplica
    `_SETTINGS_DEFAULTS` — quem abrisse a tela sem valor veria um rigor que não
    está valendo, e salvar gravaria o número errado."""
    import pathlib

    html = (pathlib.Path(app.__file__).resolve().parent / "static" / "index.html"
            ).read_text(encoding="utf-8")
    padrao = app._SETTINGS_DEFAULTS["speaker_threshold"]
    assert f'cfgNum("spThr", s.speaker_threshold, {padrao})' in html
    assert f'speaker_threshold: parseFloat($("spThr")?.value) || {padrao},' in html
    # e o próprio campo nasce com ele (o `value` do input é o que a tela mostra)
    assert f'id="spThr" min="0.35" max="0.9" step="0.05" value="{padrao}"' in html


def test_settings_admin_muda_a_limpeza_do_outputs(client, auth):
    """Admin manda teto 1 e os três campos de limpeza passam (#543).

    É o dano que motivou o campo ser admin: uma chave `use` remota levando
    `auto_cleanup_max_outputs: 1` apagaria tudo menos um do diretório do dono."""
    antes = dict(app._settings)
    body = client.post("/api/settings", headers=auth, json={
        "auto_cleanup": True, "auto_cleanup_minutes": 3,
        "auto_cleanup_max_outputs": 1}).json()
    assert body.get("admin_ignored") == []
    assert (app._settings["auto_cleanup"], app._settings["auto_cleanup_minutes"],
            app._settings["auto_cleanup_max_outputs"]) == (True, 3, 1)
    for k in ("auto_cleanup", "auto_cleanup_minutes", "auto_cleanup_max_outputs"):
        app._settings[k] = antes[k]


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
        r = client.post("/api/apikeys", headers=auth, json={"name": "pytest-de-uso", "role": "use"})
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
    app._apikeys_publica()
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
# O RESTART é o que prova o `role` (achado P1 da auditoria #282): o arquivo é a
# memória do processo novo. Estes testes releem o disco de verdade, em vez de só
# olhar a RAM como a fixture `chave_de_uso` fazia — era por isso que o gate antigo
# não via o papel sumir na gravação.
# ---------------------------------------------------------------------------

@pytest.fixture()
def apikeys_isolados(monkeypatch, tmp_path):
    """Arquivo de chaves próprio (o `_isolamento_disco` já usa tmp; aqui é
    explícito para o round-trip ser provável) e regra LEGADA pinada: com
    `TTS_ROD_ADMIN_KEY` no ambiente do dono o veredito de admin mudaria (#206)."""
    monkeypatch.setattr(app, "APIKEYS_PATH", tmp_path / ".apikeys.json")
    monkeypatch.setattr(app, "LEGACY_APIKEY_PATH", tmp_path / ".apikey")
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "")
    monkeypatch.setattr(app, "_apikeys",
                        {"enabled": True,
                         "keys": [dict(k) for k in (app._apikeys.get("keys") or [])]})
    app._save_apikeys()          # o arquivo nasce com as chaves vivas, em tmp
    return tmp_path


def _rele_apikeys() -> None:
    """Relê o arquivo — literalmente o que um restart faz (sem reiniciar nada)."""
    app._load_apikeys()
    app.API_KEY = app._primary_api_key() or app._ENV_API_KEY or None


def test_apikeys_role_sobrevive_a_releitura_do_arquivo(client, auth, apikeys_isolados):
    """`role` tem de ir ao disco E voltar dele.

    Antes: `_save_apikeys` gravava só id/name/secret/created_at e `_load_apikeys`
    relia o mesmo conjunto, então a chave `use` voltava legada (admin) na primeira
    subida. A mordida é o round-trip inteiro — tirar o `role` da gravação deixa a
    asserção do DISCO vermelha, e a do papel (`403`) vermelha junto."""
    r = client.post("/api/apikeys", headers=auth, json={"name": "pytest-de-uso", "role": "use"})
    assert r.status_code == 200, r.text
    kid, secret = r.json()["key"]["id"], r.json()["key"]["secret"]

    disco = _json.loads((apikeys_isolados / ".apikeys.json").read_text())
    gravada = next(k for k in disco["keys"] if k["id"] == kid)
    assert gravada["role"] == "use", "o papel tem de estar no ARQUIVO, não só na RAM"

    _rele_apikeys()
    assert app._key_role(secret) == "use"
    assert app._key_is_valid(secret) is True
    # e o papel RELIDO manda: a chave de uso continua sem administrar
    assert client.post("/api/apikeys", headers={"X-API-Key": secret},
                       json={"name": "x"}).status_code == 403
    assert app._key_role(app._primary_api_key()) is None   # a legada não foi tocada


def test_apikeys_role_legado_do_arquivo_nao_muda_de_papel(client, auth, apikeys_isolados):
    """Linha SEM `role` (arquivo anterior ao campo) atravessa a migração como
    legado: `None` vai ao disco como `null` explícito, volta como `None` e segue
    administrando pela política antiga. Nada de virar `"None"`/`"null"` na ida e
    volta — string ali cairia na regra de papel ausente."""
    caminho = apikeys_isolados / ".apikeys.json"
    disco = _json.loads(caminho.read_text())
    disco["keys"].append({"id": "legada01", "name": "de antes", "secret": "sk-legada-123",
                          "created_at": "2025-01-01 00:00:00"})        # SEM role
    caminho.write_text(_json.dumps(disco))

    _rele_apikeys()
    assert app._key_role("sk-legada-123") is None
    assert client.post("/api/apikeys", headers={"X-API-Key": "sk-legada-123"},
                       json={"name": "x"}).status_code == 200

    app._save_apikeys()
    disco = _json.loads(caminho.read_text())
    regravada = next(k for k in disco["keys"] if k["id"] == "legada01")
    assert regravada["role"] is None, "legado vai ao disco como null explícito"
    _rele_apikeys()
    assert app._key_role("sk-legada-123") is None


def test_apikeys_role_invalido_no_arquivo_nao_derruba_o_boot(
        client, auth, apikeys_isolados, capsys):
    """Arquivo editado à mão com papel inventado: a leitura é tolerante — ela roda
    no IMPORT do app, então um 400 aqui derrubaria o servidor inteiro. O aviso vai
    para o STDERR (o stdout do processo é lido como DADO por quem faz `import app`,
    mesmo motivo do log do dsh — task_6db0e2cc)."""
    caminho = apikeys_isolados / ".apikeys.json"
    disco = _json.loads(caminho.read_text())
    disco["keys"][0]["role"] = "chefe"
    caminho.write_text(_json.dumps(disco))

    capsys.readouterr()                      # descarta o que já saiu
    _rele_apikeys()
    assert app._key_role(app._primary_api_key()) is None
    assert client.get("/api/apikeys", headers=auth).status_code == 200
    saida = capsys.readouterr()
    assert "role 'chefe' desconhecido" in saida.err
    assert "role" not in saida.out            # stdout limpo


# ---------------------------------------------------------------------------
# Apagar a ÚLTIMA chave abre a LAN sem credencial (achado P1 da auditoria #282):
# a remoção passa a pedir confirmação (409) e só o `forcar=1` assume a postura
# aberta. Com admin/env sustentando a auth a última chave sai direto.
# ---------------------------------------------------------------------------

@pytest.fixture()
def chave_unica(monkeypatch, tmp_path):
    """Uma chave só, nenhum env sustentando a auth (o estado em que a remoção
    realmente derruba a exigência de chave)."""
    monkeypatch.setattr(app, "APIKEYS_PATH", tmp_path / ".apikeys.json")
    monkeypatch.setattr(app, "LEGACY_APIKEY_PATH", tmp_path / ".apikey")
    monkeypatch.setattr(app, "_ENV_API_KEY", "")
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "")
    monkeypatch.setattr(app, "_apikeys",
                        {"enabled": True,
                         "keys": [{"id": "unica01", "name": "só esta",
                                   "secret": "sk-unica-123", "created_at": "x",
                                   "role": None}]})
    app._save_apikeys()
    return {"X-API-Key": "sk-unica-123"}


def test_apikeys_ultima_chave_pede_confirmacao(client, chave_unica):
    """409 ANTES de apagar, com o motivo legível e o caminho de confirmação."""
    r = client.delete("/api/apikeys/unica01", headers=chave_unica)
    assert r.status_code == 409, r.text
    corpo = r.json()
    assert corpo["forcar"] is True
    assert "desliga a exigência de chave" in corpo["detail"]
    assert "LAN" in corpo["detail"] and "forcar=1" in corpo["hint"]
    assert corpo["remaining"] == 0 and corpo["auth_active"] is True
    assert len(app._apikeys["keys"]) == 1, "o 409 NÃO pode ter apagado"
    assert client.get("/api/apikeys", headers=chave_unica).status_code == 200


def test_apikeys_ultima_chave_forcada_desliga_a_auth(client, chave_unica, capsys):
    """`?forcar=1` apaga, avisa no corpo, deixa sinal no LOG (a resposta voa, o
    journal fica) e a postura aberta aparece no `/api/status` — de fora, sem chave."""
    capsys.readouterr()
    r = client.delete("/api/apikeys/unica01?forcar=1", headers=chave_unica)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["remaining"] == 0 and corpo["auth_active"] is False
    assert corpo["avisos"] and "LAN" in corpo["avisos"][0]
    assert app._apikeys["keys"] == []
    assert "ÚLTIMA chave apagada" in capsys.readouterr().err

    lan = TestClient(app.app, raise_server_exceptions=False, client=("203.0.113.9", 50000))
    st = lan.get("/api/status")
    assert st.status_code == 200, "auth desligada: a LAN entra sem chave"
    assert st.json()["api_auth_enabled"] is False


def test_apikeys_ultima_chave_com_admin_key_sai_sem_409(client, chave_unica, monkeypatch):
    """Com `TTS_ROD_ADMIN_KEY` a auth não cai, então não há o que confirmar."""
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "adm-do-operador")
    r = client.delete("/api/apikeys/unica01", headers={"X-API-Key": "adm-do-operador"})
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] == 0 and r.json()["auth_active"] is True
    assert not r.json().get("avisos")


def test_apikeys_nao_ultima_chave_sai_direto(client, chave_unica):
    """Remover chave que não é a última não muda a postura: sem 409, sem aviso."""
    client.post("/api/apikeys", headers=chave_unica, json={"name": "pytest-segunda"})
    assert len(app._apikeys["keys"]) == 2
    r = client.delete("/api/apikeys/unica01", headers=chave_unica)
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] == 1 and r.json()["auth_active"] is True
    assert not r.json().get("avisos")


def test_apikeys_ultima_chave_com_exigencia_desligada_sai_direto(client, chave_unica):
    """Postura aberta já escolhida: apagar a última não a muda, não pede 409."""
    assert client.post("/api/apikeys/enabled", headers=chave_unica,
                       json={"enabled": False}).status_code == 200
    r = client.delete("/api/apikeys/unica01", headers=chave_unica)
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] == 0


# ---------------------------------------------------------------------------
# Régua de autorização por DANO (#282, item 4): apagar/desligar/global = admin;
# consumir = `use`. Antes, chave de uso derrubava o servidor e apagava as vozes.
# ---------------------------------------------------------------------------

#: (método, caminho) elevadas — `use` tem de tomar 403
_ROTAS_ELEVADAS = [
    ("post", "/api/tunnel/restart"),
    ("post", "/api/tunnel/stop"),
    ("post", "/api/tunnel/start"),
    ("delete", "/api/outputs"),
    ("delete", "/api/outputs/naoexiste01"),
    ("post", "/api/models/unload"),
    ("delete", "/api/voices/naoexiste01"),
    ("get", "/api/voices/export"),
    ("delete", "/api/speaker/naoexiste01"),
]

#: consumir — `use` tem de passar (nunca 403)
_ROTAS_CONSUMO = [
    ("post", "/api/live/ticket"),
    ("get", "/api/voices"),
    ("get", "/api/outputs"),
    ("post", "/api/mic-route"),
    ("post", "/api/tts"),          # corpo vazio → 400, não 403
]


@pytest.fixture()
def sem_admin_key(monkeypatch):
    """Sem `TTS_ROD_ADMIN_KEY` a chave legada administra: é o cenário da régua."""
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "")


def _chama(client, metodo, rota, headers, **kw):
    return getattr(client, metodo)(rota, headers=headers, **kw)


def test_rotas_elevadas_403_para_chave_de_uso_e_passam_com_admin(
        client, auth, chave_de_uso, sem_admin_key):
    for metodo, rota in _ROTAS_ELEVADAS:
        r = _chama(client, metodo, rota, chave_de_uso)
        assert r.status_code == 403, f"{metodo.upper()} {rota} devia ser admin, veio {r.status_code}"
        assert "admin" in r.json()["detail"]
        r = _chama(client, metodo, rota, auth)
        assert r.status_code != 403, f"{metodo.upper()} {rota} com admin tomou 403"


def test_rotas_de_consumo_passam_com_chave_de_uso(client, auth, chave_de_uso, sem_admin_key):
    for metodo, rota in _ROTAS_CONSUMO:
        kw = {"json": {}} if rota == "/api/tts" else {}
        r = _chama(client, metodo, rota, chave_de_uso, **kw)
        assert r.status_code != 403, f"{metodo.upper()} {rota} é de consumo e barrou o `use`"


def test_chave_legada_continua_administrando(client, auth, sem_admin_key):
    """Nada muda para quem já usava: chave sem papel (legada) administra."""
    assert app._key_role(auth["X-API-Key"]) is None
    assert client.delete("/api/outputs", headers=auth).status_code == 200


def test_import_de_vozes_e_admin_mas_denoise_e_replace_sao_de_uso(
        client, auth, chave_de_uso, sem_admin_key):
    """Import (restaura backup) é elevado; denoise/replace editam a voz e ficam
    com o `use` — a régua é o dano, não a conveniência."""
    assert client.post("/api/voices/import", headers=chave_de_uso,
                       files={"zip_file": ("v.zip", b"PK\x05\x06" + b"\x00" * 18,
                                           "application/zip")}).status_code == 403
    wav = _wav_bytes(0.3)
    r = client.post("/api/voices/naoexiste01/denoise", headers=chave_de_uso,
                    files={"audio": ("a.wav", wav, "audio/wav")})
    assert r.status_code != 403, r.text


def test_shutdown_exige_admin(client, auth, chave_de_uso, sem_admin_key, monkeypatch):
    """O caso que dói: qualquer chave de uso derrubava o servidor."""
    saida = []
    monkeypatch.setattr(os, "_exit", lambda codigo=0: saida.append(codigo))
    assert client.post("/api/shutdown", headers=chave_de_uso).status_code == 403
    assert saida == []
    assert client.post("/api/shutdown", headers=auth).status_code == 200
    assert _espera(lambda: saida, 2.0) and saida == [0]


def test_status_nao_regride_para_chave_de_uso(client, auth, chave_de_uso, sem_admin_key):
    """`/api/status` é consumo: segue 200 para `use` e diz que ela não é admin."""
    r = client.get("/api/status", headers=chave_de_uso)
    assert r.status_code == 200
    assert client.get("/api/settings", headers=chave_de_uso).json()["is_admin"] is False
    assert client.get("/api/settings", headers=auth).json()["is_admin"] is True


# ---------------------------------------------------------------------------
# Histórico PAGINADO (auditoria #282): a rota lia o diretório inteiro a cada
# chamada (2.870 metas / 24 MB) e devolvia tudo. Ordenação e corte agora saem do
# `mtime` (o `.json` nasce e nunca é reescrito), então só a PÁGINA é parseada.
# ---------------------------------------------------------------------------

def _outputs_falsos(tmp_path, monkeypatch, quantos=120):
    """Diretório de saídas próprio com metas ordenáveis (mtime controlado)."""
    d = tmp_path / "outputs"
    d.mkdir()
    base = 1_700_000_000.0
    for i in range(quantos):
        mid = f"out{i:04d}"
        (d / f"{mid}.json").write_text(_json.dumps({
            "id": mid, "text": f"texto numero {i} assunto {'par' if i % 2 else 'impar'}",
            "duration": 2.0, "elapsed": 0.5,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i * 60)),
        }))
        os.utime(d / f"{mid}.json", (base + i * 60, base + i * 60))
    monkeypatch.setattr(app, "OUTPUTS_DIR", d)
    return d


def test_outputs_pagina_com_total_e_sem_repeticao(client, auth, tmp_path, monkeypatch):
    _outputs_falsos(tmp_path, monkeypatch)
    r = client.get("/api/outputs?limit=50", headers=auth)
    assert r.status_code == 200
    corpo = r.json()
    assert len(corpo["items"]) == 50
    assert corpo["total"] == 120 and r.headers["X-Total-Count"] == "120"
    assert corpo["limit"] == 50 and corpo["offset"] == 0
    ids = [o["id"] for o in corpo["items"]]
    assert ids == sorted(ids, reverse=True)          # mais novo primeiro (mtime)

    p2 = client.get("/api/outputs?limit=50&offset=50", headers=auth).json()
    assert len(p2["items"]) == 50 and p2["total"] == 120
    assert not (set(ids) & {o["id"] for o in p2["items"]}), "página 2 não repete"
    p3 = client.get("/api/outputs?limit=50&offset=100", headers=auth).json()
    assert len(p3["items"]) == 20, "última página vem com o resto"


def test_outputs_compat_lista_e_tetos(client, auth, tmp_path, monkeypatch):
    """Cliente ANTIGO (sem parâmetro nenhum) recebe a LISTA crua de sempre; a partir
    do momento em que pede paginação/filtro, o formato vira objeto e `limit` é
    clampado em 1–500."""
    _outputs_falsos(tmp_path, monkeypatch, quantos=60)
    r = client.get("/api/outputs", headers=auth)
    assert isinstance(r.json(), list), "sem parâmetro nenhum, o formato é o de antes"
    assert len(r.json()) == 60 and r.headers["X-Total-Count"] == "60"

    assert len(client.get("/api/outputs?limit=50", headers=auth).json()["items"]) == 50
    assert len(client.get("/api/outputs?limit=9999", headers=auth).json()["items"]) == 60
    assert client.get("/api/outputs?limit=0", headers=auth).json()["limit"] == 1
    assert client.get("/api/outputs?offset=-5", headers=auth).json()["offset"] == 0
    # `q` sozinho já seleciona o modo paginado (objeto), sem `limit`
    so_q = client.get("/api/outputs?q=assunto par", headers=auth).json()
    assert isinstance(so_q, dict) and so_q["total"] == 30 and len(so_q["items"]) == 30


def test_outputs_busca_e_since(client, auth, tmp_path, monkeypatch):
    """`q` casa no texto cru e `since` corta por data; ambos mexem no `total`."""
    d = _outputs_falsos(tmp_path, monkeypatch, quantos=10)
    assert client.get("/api/outputs?q=assunto par", headers=auth).json()["total"] == 5
    assert client.get("/api/outputs?q=NADA", headers=auth).json()["total"] == 0

    # since = mtime do 5º meta: sobram ele e os 5 mais novos
    quinto = (d / "out0004.json").stat().st_mtime
    corpo = client.get(f"/api/outputs?since={int(quinto)}&limit=500", headers=auth).json()
    assert corpo["total"] == 6
    assert all(o["id"] >= "out0004" for o in corpo["items"])
    assert client.get("/api/outputs?since=ontem", headers=auth).json()["total"] == 10


def test_outputs_meta_corrompido_e_pulado(client, auth, tmp_path, monkeypatch):
    d = _outputs_falsos(tmp_path, monkeypatch, quantos=3)
    (d / "quebrado.json").write_text("{nao e json")
    os.utime(d / "quebrado.json", (1_900_000_000.0, 1_900_000_000.0))   # o mais novo
    r = client.get("/api/outputs", headers=auth)
    assert r.headers["X-Total-Count"] == "4"         # o quebrado conta no total…
    assert [o["id"] for o in r.json()] == ["out0002", "out0001", "out0000"]


# ---------------------------------------------------------------------------
# Teto de QUANTIDADE em outputs/ (#430): o tempo sozinho não segura o diretório
# (3.074 `.wav` acumulados com `auto_cleanup` desligado, o default). A poda é por
# EXCESSO — os recentes ficam — e o que está em `_jobs` nunca sai.
# ---------------------------------------------------------------------------

def _saidas_para_poda(tmp_path, monkeypatch, quantos=6, jobs=(), status="done"):
    d = _outputs_falsos(tmp_path, monkeypatch, quantos=quantos)
    base = 1_700_000_000.0
    for i in range(quantos):
        oid = f"out{i:04d}"
        (d / f"{oid}.wav").write_bytes(b"RIFF" + b"\x00" * 32)
        os.utime(d / f"{oid}.wav", (base + i * 60, base + i * 60))
        jd = d / f".job-{oid}"
        jd.mkdir()
        (jd / "0.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(app, "_jobs", {jid: {"status": status} for jid in jobs})
    return d


def test_poda_por_quantidade_tira_os_antigos_e_preserva_job(tmp_path, monkeypatch):
    """Teto 3 com 6 saídas: saem as mais antigas, menos a que está em `_jobs`.

    O protegido conta no teto (o diretório tem de voltar ao tamanho pedido), mas
    ele mesmo nunca sai — quem paga a conta são os não protegidos seguintes."""
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=6, jobs=("out0000",))
    assert app._poda_outputs_por_quantidade(3) == 3
    ficaram = sorted(p.stem for p in d.glob("*.json"))
    assert ficaram == ["out0000", "out0004", "out0005"]
    for oid in ("out0001", "out0002", "out0003"):       # par e trechos saem juntos
        assert not (d / f"{oid}.wav").exists() and not (d / f"{oid}.json").exists()
        assert not (d / f".job-{oid}").exists()
    assert (d / ".job-out0005").is_dir()                # recente intocado


def test_poda_desligada_com_teto_zero_e_sem_excesso(tmp_path, monkeypatch):
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=4)
    assert app._poda_outputs_por_quantidade(0) == 0
    assert len(list(d.glob("*.json"))) == 4
    assert app._poda_outputs_por_quantidade(4) == 0     # no teto exato, nada sai
    assert len(list(d.glob("*.json"))) == 4
    assert app._poda_outputs_por_quantidade(100) == 0


def test_poda_conta_e_remove_wav_sem_meta(tmp_path, monkeypatch):
    """Sobra antiga sem `.json` (invisível na listagem) também ocupa lugar e sai."""
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=2)
    for i in range(3):
        (d / f"t{i}.wav").write_bytes(b"RIFF" + b"\x00" * 32)
        os.utime(d / f"t{i}.wav", (1_600_000_000.0 + i, 1_600_000_000.0 + i))
    assert app._poda_outputs_por_quantidade(3) == 2      # 5 candidatos, teto 3
    assert sorted(p.name for p in d.glob("t*.wav")) == ["t2.wav"]
    assert sorted(p.stem for p in d.glob("out*.wav")) == ["out0000", "out0001"]


def test_poda_conta_e_remove_json_sem_audio(tmp_path, monkeypatch):
    """Meta sem `.wav` também ocupa lugar (aparece na listagem e não toca)."""
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=2)
    (d / "so_meta.json").write_text('{"id": "so_meta"}')
    os.utime(d / "so_meta.json", (1_600_000_000.0, 1_600_000_000.0))
    assert app._poda_outputs_por_quantidade(2) == 1      # 3 candidatos, teto 2
    assert not (d / "so_meta.json").exists(), "o meta órfão ficou"
    assert (d / "out0001.wav").exists() and (d / "out0001.json").exists()


def test_poda_conta_e_remove_diretorio_de_trechos_orfao(tmp_path, monkeypatch):
    """`.job-<id>` sem `.wav` (síntese interrompida) entra pela própria idade."""
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=2)
    jd = d / ".job-solto"
    jd.mkdir()
    (jd / "0.wav").write_bytes(b"RIFF")
    os.utime(jd, (1_600_000_000.0, 1_600_000_000.0))
    assert app._poda_outputs_por_quantidade(2) == 1      # 3 candidatos, teto 2
    assert not jd.exists(), "o diretório de trechos órfão ficou"
    assert (d / ".job-out0001").is_dir()


def test_poda_nao_conta_nem_come_temporario(tmp_path, monkeypatch):
    """Temporário de upload não é output: nem conta, nem é podado.

    DOIS formatos de propósito: sem sufixo (`.stt-antigo`, o upload do STT) e com
    sufixo `.wav` (`.live-*`, o temporário do Live, #554). O segundo é o que
    MORDE a guarda `startswith(".")` da poda — sem ela o arquivo casaria o
    `glob("*.wav")` e viraria candidato. Quem recolhe temporário é o
    `_limpa_temporarios` (por idade, >6 h).
    """
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=2)
    sem_sufixo = d / ".stt-antigo"
    com_wav = d / ".live-123-456.wav"
    for tmp in (sem_sufixo, com_wav):
        tmp.write_bytes(b"\x00" * 16)
        os.utime(tmp, (1_600_000_000.0, 1_600_000_000.0))   # os mais velhos
    assert app._poda_outputs_por_quantidade(2) == 0      # 2 candidatos, no teto
    assert sem_sufixo.exists() and com_wav.exists(), "temporário comido pela poda"


def test_limpa_temporarios_recolhe_o_live_antigo(tmp_path, monkeypatch):
    """`.live-*` é temporário do Live e TEM de sair com os outros (#554).

    Fora da lista de padrões ele era imortal: a poda não o come (tem ponto no
    nome) e a limpeza por idade não o via — 301 acumulados no diretório do dono.
    """
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=1)
    velho = d / ".live-9-9.wav"
    novo = d / ".live-9-8.wav"
    for arq, mtime in ((velho, 1_600_000_000.0), (novo, time.time())):
        arq.write_bytes(b"\x00" * 16)
        os.utime(arq, (mtime, mtime))
    assert app._limpa_temporarios() >= 1
    assert not velho.exists(), "o `.live-*` velho ficou"
    assert novo.exists(), "temporário recente saiu (a janela de 6 h não valeu)"


@pytest.mark.parametrize("status", ["running", "queued"])
def test_poda_nunca_come_job_em_voo(tmp_path, monkeypatch, status):
    """`running` e `queued` são os casos nomeados no card: os dois ficam."""
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=3, jobs=("out0000",),
                          status=status)
    assert app._poda_outputs_por_quantidade(2) == 1      # sai o 2º mais antigo
    assert (d / "out0000.wav").exists(), f"job {status} foi podado"
    assert not (d / "out0001.wav").exists()
    assert (d / "out0002.wav").exists()


def test_poda_roda_no_loop_mesmo_com_auto_cleanup_desligado(tmp_path, monkeypatch):
    """A poda não pode depender de `auto_cleanup` (esse é só o corte por TEMPO).

    Roda UMA volta do loop (o `sleep` levanta) com `auto_cleanup` no default,
    False: se a chamada descer para dentro do `if`, o diretório fica cheio.
    """
    d = _saidas_para_poda(tmp_path, monkeypatch, quantos=4)
    monkeypatch.setitem(app._settings, "auto_cleanup", False)
    monkeypatch.setitem(app._settings, "auto_cleanup_max_outputs", 2)

    class _Parou(Exception):
        pass

    dormidas = []

    def _sleep(_s):
        dormidas.append(_s)          # o `sleep` é o TOPO da volta: deixa a 1ª
        if len(dormidas) > 1:        # passar e para na 2ª, com a volta já feita
            raise _Parou

    monkeypatch.setattr(app.time, "sleep", _sleep)
    with pytest.raises(_Parou):
        app._auto_cleanup_loop()
    assert len(list(d.glob("*.json"))) == 2, "a volta do loop não podou"


def test_poda_respeita_o_teto_da_setting(client, auth, tmp_path, monkeypatch):
    """O teto vem da setting e a listagem reflete a poda."""
    _saidas_para_poda(tmp_path, monkeypatch, quantos=5)
    monkeypatch.setitem(app._settings, "auto_cleanup_max_outputs", 2)
    assert app._poda_outputs_por_quantidade() == 3
    assert [o["id"] for o in client.get("/api/outputs", headers=auth).json()] == \
        ["out0004", "out0003"]


def test_setting_do_teto_valida_os_limites(client, auth):
    """0 desliga; valor absurdo/ilegível não vira teto inválido."""
    def _teto(valor):
        r = client.post("/api/settings", headers=auth,
                        json={"auto_cleanup_max_outputs": valor})
        assert r.status_code == 200, r.text
        return r.json()["auto_cleanup_max_outputs"]

    try:
        assert _teto(0) == 0                    # 0 desliga de verdade
        assert _teto(-5) == 0                   # negativo cai no piso
        assert _teto(999_999) == 100_000        # teto do absurdo
        assert _teto("nao-numero") == 500       # ilegível volta ao default
        assert _teto(250) == 250
    finally:
        client.post("/api/settings", headers=auth,
                    json={"auto_cleanup_max_outputs": 500})


# ---------------------------------------------------------------------------
# Estado e concorrência da Conversa / unload / caches (auditoria #282).
# ---------------------------------------------------------------------------

def _espera_status(client, auth, sid, alvo, tentativas=120):
    import time as _t
    for _ in range(tentativas):
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        if d["status"] == alvo:
            return d
        _t.sleep(0.02)
    raise AssertionError(f"sessão não chegou em {alvo}: {d}")


def test_chat_nove_conversas_simultaneas_nao_viram_timeout(client, auth, monkeypatch):
    """9 conversas com provedor SAUDÁVEL: a 9ª não pode morrer por tempo de FILA.

    O bug: um único `result(timeout=150)` contava a espera na fila do executor
    (8 threads) como se fosse o provedor falando."""
    import time as _t
    monkeypatch.setattr(app, "_chat_llm", lambda msgs: _t.sleep(0.05) or '{"final": true, "text": "ok"}')
    sids = []
    for i in range(9):
        r = client.post("/api/chat/start", headers=auth, json={"objective": f"o{i}"})
        assert r.status_code == 200
        sids.append(r.json()["session_id"])
    for sid in sids:
        d = _espera_status(client, auth, sid, "confirmed")
        assert "demorou" not in (d.get("error") or "")
        assert d["text"] == "ok"


def test_chat_fila_estourada_nao_e_erro_do_provedor(client, auth, monkeypatch):
    """Fila cheia: o turno SEGUE na fila e o GET mostra `fila: true` — erro
    nenhum, porque o provedor não foi chamado ainda."""
    import concurrent.futures
    import threading
    solta = threading.Event()

    def _llm(msgs):
        solta.wait(10)
        return '{"final": true, "text": "depois da fila"}'

    monkeypatch.setattr(app, "_chat_llm", _llm)
    monkeypatch.setattr(app, "_CHAT_FILA_ESPERA_S", 0.15)
    # pool de UMA thread: a 1ª sessão ocupa, as outras ficam na fila
    monkeypatch.setattr(app, "_chat_executor",
                        concurrent.futures.ThreadPoolExecutor(max_workers=1))
    for i in range(3):
        client.post("/api/chat/start", headers=auth, json={"objective": f"o{i}"})
    r = client.post("/api/chat/start", headers=auth, json={"objective": "fila"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    # o flag `fila` é posto pelo worker quando a espera do pool estoura: poll
    import time as _t
    d = {}
    for _ in range(400):                     # 8 s: sob carga a thread do turno demora
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        if d["fila"]:
            break
        _t.sleep(0.02)
    assert d["fila"] is True and d["error"] == "", d
    solta.set()
    d = _espera_status(client, auth, sid, "confirmed")
    assert d["text"] == "depois da fila" and d["fila"] is False


def test_chat_teto_de_sessoes_e_expurgo(client, auth, monkeypatch):
    """Teto duro de sessões: a mais antiga sai; TTL também continua podando."""
    monkeypatch.setattr(app, "_chat_llm", lambda msgs: '{"final": false, "reply": "?"}')
    monkeypatch.setattr(app, "_CHAT_MAX_SESSIONS", 3)
    with app._chat_lock:
        app._chat_sessions.clear()
    sids = [client.post("/api/chat/start", headers=auth,
                        json={"objective": f"o{i}"}).json()["session_id"] for i in range(5)]
    with app._chat_lock:
        vivos = set(app._chat_sessions)
    assert len(vivos) <= 3, "o teto tem de valer"
    assert sids[0] not in vivos, "a mais antiga é a evictada"
    assert sids[-1] in vivos
    # TTL: uma sessão velha sai na próxima poda
    with app._chat_lock:
        app._chat_sessions[sids[-1]]["updated"] = time.time() - app.CHAT_TTL - 5
        app._chat_purge(time.time())
        assert sids[-1] not in app._chat_sessions


def test_models_unload_com_geracao_em_curso_devolve_409(client, auth, monkeypatch):
    """Job in-process longo segura o `_gen_lock`: 409 em vez de pendurar."""
    monkeypatch.setattr(app, "_GEN_ESPERA_UNLOAD_S", 0.1)
    assert app._gen_lock.acquire(timeout=1)     # simula geração em curso
    try:
        r = client.post("/api/models/unload", headers=auth, json={})
        assert r.status_code == 409, r.text
        assert "em curso" in r.json()["detail"]
        assert r.json()["tts_pendente"]
        assert (r.json()["unloaded"] is not None)
    finally:
        app._gen_lock.release()
    # sem ninguém segurando o lock, volta a 200
    assert client.post("/api/models/unload", headers=auth, json={}).status_code == 200


def test_cache_de_voz_remota_tem_teto_e_sai_na_delecao(client, auth, monkeypatch,
                                                       vozes_isoladas):
    monkeypatch.setattr(app, "_remote_voice_cache", {})
    monkeypatch.setattr(app, "_REMOTE_VOICE_CACHE_MAX", 2)
    app._remote_voice_cache_guarda("v1", ("http://r", "v1", 1))
    app._remote_voice_cache_guarda("v2", ("http://r", "v2", 1))
    app._remote_voice_cache_guarda("v3", ("http://r", "v3", 1))
    assert list(app._remote_voice_cache) == ["v2", "v3"], "o mais antigo sai pelo teto"

    monkeypatch.setattr(app, "_REMOTE_VOICE_CACHE_MAX", 64)
    app._remote_voice_cache["morta"] = ("http://r", "morta", 1)
    assert client.delete("/api/voices/naoexiste01", headers=auth).status_code == 404
    assert "morta" in app._remote_voice_cache        # 404 não limpa nada
    app._remote_voice_cache["existe"] = ("http://r", "existe", 1)
    # `voices/` em tmp (fixture): com o diretório REAL, duas suítes concorrentes
    # disputam o mesmo nome de arquivo e uma apaga o da outra (#435/concorrência)
    vd, _sentinela = vozes_isoladas
    (vd / "existe.wav").write_bytes(b"RIFF")
    assert client.delete("/api/voices/existe", headers=auth).status_code == 200
    assert "existe" not in app._remote_voice_cache


def test_remote_tts_extra_e_campo_admin(client, auth, chave_de_uso, sem_admin_key):
    """O extra sobrepõe o corpo de TODO trecho: chave de uso não pode trocá-lo."""
    admin = client.get("/api/settings", headers=auth).json()["remote_tts_extra"]
    assert admin == "{}" or admin == ""
    r = client.post("/api/settings", headers=chave_de_uso,
                    json={"remote_tts_extra": '{"speed": 0.9}'})
    assert "remote_tts_extra" in r.json()["admin_ignored"]
    assert app._settings["remote_tts_extra"] != '{"speed": 0.9}'
    r = client.post("/api/settings", headers=auth, json={"remote_tts_extra": '{"speed": 0.9}'})
    assert "remote_tts_extra" not in r.json()["admin_ignored"]
    assert app._settings["remote_tts_extra"] == '{"speed": 0.9}'


def test_extra_remoto_sem_text_nem_voice(monkeypatch, capsys):
    """`text`/`voice` do extra não valem para todos os trechos: são descartados
    com aviso (override que muda sozinho é degradação silenciosa)."""
    monkeypatch.setattr(app, "_extra_remoto_avisados", set())
    assert app._extra_remoto('{"speed": 0.9, "num_steps": 8}') == {"speed": 0.9, "num_steps": 8}
    assert app._extra_remoto('{"text": "fixo", "speed": 1.1}') == {"speed": 1.1}
    assert app._extra_remoto('{"voice": "x"}') == {}
    saida = capsys.readouterr()
    assert "traz 'text'" in saida.err and "traz 'voice'" in saida.err
    assert saida.out == ""
    # JSON quebrado e não-dict também são ignorados, e o lixo é avisado UMA vez
    assert app._extra_remoto("{nao e json") == {}
    assert app._extra_remoto("[]") == {}
    assert app._extra_remoto("{nao e json") == {}
    assert capsys.readouterr().err.count("não é JSON válido") == 1
    assert app._extra_remoto("") == {} and app._extra_remoto(None) == {}


def test_zip_do_mic_router_exige_admin(client, auth, chave_de_uso, sem_admin_key):
    """O zip carrega a chave primária em claro: credencial, logo admin."""
    r = client.get("/api/client/mic-router", headers=chave_de_uso)
    assert r.status_code == 403 and "admin" in r.json()["detail"]
    r = client.get("/api/client/mic-router", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        cfg = _json.loads(z.read("mic-router/config.json"))
    assert cfg["api_key"], "o zip entrega a chave por desenho (agora só para admin)"


def test_listagem_de_chaves_segue_aberta_sem_segredo(client, auth, chave_de_uso, sem_admin_key):
    """Decisão: metadado mascarado qualquer chave vê; o segredo só com admin."""
    corpo = client.get("/api/apikeys", headers=chave_de_uso).json()
    assert corpo["can_reveal"] is False
    assert corpo["keys"] and all("secret" not in k for k in corpo["keys"])
    assert client.get("/api/apikeys?reveal=1", headers=chave_de_uso).json()["can_reveal"] is False


# ---------------------------------------------------------------------------
# Rotas sem teste funcional (auditoria #288, item 3). Um `_safe_id`/`unlink`/403
# quebrado passava verde. Onde a rota grava em voices/, voz sintética e o arquivo
# final asserido.
# ---------------------------------------------------------------------------

def test_rotate_apikey_troca_o_secret_e_invalida_o_antigo(client, auth, chave_de_uso,
                                                          sem_admin_key):
    r = client.post("/api/apikeys", headers=auth, json={"name": "pytest-rotacionavel"})
    kid, velho = r.json()["key"]["id"], r.json()["key"]["secret"]
    h_velho = {"X-API-Key": velho}
    assert client.get("/api/status", headers=h_velho).status_code == 200

    assert client.post(f"/api/apikeys/{kid}/rotate", headers=chave_de_uso).status_code == 403
    assert client.post("/api/apikeys/__env__/rotate", headers=auth).status_code == 400
    assert client.post("/api/apikeys/naoexiste/rotate", headers=auth).status_code == 404

    r = client.post(f"/api/apikeys/{kid}/rotate", headers=auth)
    assert r.status_code == 200
    novo = r.json()["key"]["secret"]
    assert novo != velho and r.json()["key"]["role"] is None
    assert client.get("/api/status", headers={"X-API-Key": novo}).status_code == 200
    assert client.get("/api/status", headers=h_velho).status_code == 401, \
        "a chave antiga tem de morrer na hora"
    assert app._key_is_valid(novo) and not app._key_is_valid(velho)


def test_backends_e_mic_route(client, auth):
    d = client.get("/api/backends", headers=auth).json()
    assert d["current"] and any(b["id"] == d["current"] for b in d["backends"])

    assert client.get("/api/mic-route", headers=auth).json()["mode"] in ("app", "real")
    r = client.post("/api/mic-route", headers=auth, json={"mode": "real"})
    assert r.status_code == 200 and r.json()["mode"] == "real"
    assert client.get("/api/mic-route", headers=auth).json()["mode"] == "real"
    assert client.post("/api/mic-route", headers=auth, json={"mode": "x"}).status_code == 400
    client.post("/api/mic-route", headers=auth, json={"mode": "app"})


def test_chat_debug_desligado_e_ligado(client, auth, monkeypatch):
    monkeypatch.delenv("TTS_CHAT_DEBUG", raising=False)
    assert client.post("/api/chat-debug", headers=auth, json={"a": 1}).json()["disabled"] is True
    monkeypatch.setenv("TTS_CHAT_DEBUG", "1")
    assert client.post("/api/chat-debug", headers=auth, json={"a": 1}).status_code == 200


def test_replace_troca_o_audio_da_voz_e_respeita_o_id(client, auth, vozes_isoladas):
    vd, sentinela = vozes_isoladas
    vd.joinpath("voz-real.wav").write_bytes(_wav_bytes(1.0))
    vd.joinpath("voz-real.json").write_text(_json.dumps({"id": "voz-real", "duration": 1.0}))
    novo = _wav_bytes(2.0)
    r = client.post("/api/voices/voz-real/replace", headers=auth,
                    files={"audio": ("a.wav", novo, "audio/wav")})
    assert r.status_code == 200 and r.json()["duration"] > 1.5
    assert vd.joinpath("voz-real.wav").read_bytes() == novo, "o wav da voz foi substituído"
    # áudio curto demais é 400 e o arquivo NOVO fica (o curto foi gravado)
    r = client.post("/api/voices/voz-real/replace", headers=auth,
                    files={"audio": ("a.wav", _wav_bytes(0.2), "audio/wav")})
    assert r.status_code == 400 and "curto" in r.json()["detail"]
    # id inexistente → 404
    assert client.post("/api/voices/naoexiste/replace", headers=auth,
                       files={"audio": ("a.wav", novo, "audio/wav")}).status_code == 404
    # id com espaço: o ARQUIVO existe em voices/, só o `_safe_id` o rejeita — é o
    # ponto que morde se alguém tirar a validação (o cliente normaliza `../`, então
    # a travessia chega aqui como id já resolvido)
    assert client.post("/api/voices/voz%20exotica/replace", headers=auth,
                       files={"audio": ("a.wav", novo, "audio/wav")}).status_code == 404
    # travessia de verdade: `%2E%2E` chega como `..` no parâmetro → 404 do `_safe_id`
    assert client.post("/api/voices/%2E%2E/replace", headers=auth,
                       files={"audio": ("a.wav", novo, "audio/wav")}).status_code == 404
    assert not sentinela.with_suffix(".json").exists()


def test_audio_edit_corta_e_recusa_op_invalida(client, auth):
    r = client.post("/api/audio/edit", headers=auth,
                    files={"audio": ("a.wav", _wav_bytes(2.0), "audio/wav")},
                    data={"op": _json.dumps({"type": "trim", "start": 0.0, "end": 1.0})})
    assert r.status_code == 200
    out, sr = sf_read_bytes(r.content)
    assert 0.8 < out < 1.2, f"o corte tem de devolver ~1 s (veio {out:.2f}s)"
    assert client.post("/api/audio/edit", headers=auth,
                       files={"audio": ("a.wav", _wav_bytes(1.0), "audio/wav")},
                       data={"op": "{nao json"}).status_code == 400
    assert client.post("/api/audio/edit", headers=auth,
                       files={"audio": ("a.wav", b"lixo", "audio/wav")},
                       data={"op": "{}"}).status_code == 400


def sf_read_bytes(dados: bytes) -> tuple[float, int]:
    """(duração, sr) de um WAV em memória — helper local dos testes de áudio."""
    import io
    import wave
    with wave.open(io.BytesIO(dados)) as w:
        return w.getnframes() / float(w.getframerate()), w.getframerate()


def test_models_unload_variantes(client, auth):
    assert client.post("/api/models/unload", headers=auth, json={}).status_code == 200
    r = client.post("/api/models/unload", headers=auth, json={"only_remote": True})
    assert r.status_code == 200 and "unloaded" in r.json()


def test_stt_partial_sem_audio_e_com_stub(client, auth, monkeypatch):
    assert client.post("/api/stt-partial", headers=auth).status_code == 400
    monkeypatch.setattr(app, "_transcribe", lambda caminho, language="auto": {
        "text": "  ola mundo ", "language": "PT"})
    r = client.post("/api/stt-partial", headers=auth,
                    files={"audio": ("a.wav", _wav_bytes(0.5), "audio/wav")})
    assert r.status_code == 200 and r.json() == {"text": "ola mundo", "language": "pt"}


def test_audio_da_saida_e_peaks_da_voz(client, auth, tmp_path, monkeypatch, vozes_isoladas):
    """`/audio` de output e `/peaks` de voz: 404 no id desconhecido e `_safe_id`
    valendo (id com espaço é rejeitado mesmo com o arquivo no lugar)."""
    od = tmp_path / "outputs"
    od.mkdir()
    (od / "out01.wav").write_bytes(_wav_bytes(0.5))
    monkeypatch.setattr(app, "OUTPUTS_DIR", od)
    r = client.get("/api/outputs/out01/audio", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert client.get("/api/outputs/naoexiste/audio", headers=auth).status_code == 404
    assert client.get("/api/outputs/a..b/audio", headers=auth).status_code == 404

    vd, _sentinela = vozes_isoladas
    vd.joinpath("voz-real.wav").write_bytes(_wav_bytes(1.0))
    r = client.get("/api/voices/voz-real/peaks", headers=auth)
    assert r.status_code == 200 and len(r.json()["peaks"]) > 0
    assert client.get("/api/voices/voz%20exotica/peaks", headers=auth).status_code == 404


def test_warmup_dispara_sem_audio(client, auth, monkeypatch):
    chamados = []
    monkeypatch.setattr(app, "_stt_lock", __import__("threading").Lock())
    monkeypatch.setattr(app, "_warm_whisper", lambda: chamados.append("stt"), raising=False)
    r = client.post("/api/translate/warmup", headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Bordas do backend (auditoria #282, apêndice). Cada uma tem teste que falha sem
# o fix.
# ---------------------------------------------------------------------------

def test_405_em_path_desconhecido_vira_404(client, auth):
    """O `StaticFiles` do mount `/` responde 405 para qualquer POST. Path que
    nenhum pattern casa é 404; path que existe com outro método continua 405."""
    assert client.post("/api/naoexiste", headers=auth).status_code == 404
    assert client.post("/api/voices/../fora/replace", headers=auth).status_code == 404
    assert client.post("/api/apikeys/naoexiste/rotate", headers=auth).status_code == 404
    assert client.get("/api/tts", headers=auth).status_code == 405      # path existe
    # 404 do PRÓPRIO handler não é convertido em 405
    assert client.get("/api/voices/naoexiste/audio", headers=auth).status_code == 404


def test_docs_redoc_e_openapi_tem_csp(client, auth):
    """Antes ficavam SEM CSP; agora recebem a policy de docs (CDN liberado)."""
    for rota in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(rota, headers=auth)
        assert r.status_code == 200, rota
        assert r.headers["content-security-policy"] == app._CSP_DOCS, rota
        assert "cdn.jsdelivr.net" in r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_root_path_entra_no_openapi(client, auth):
    """O schema tem de anunciar o prefixo do proxy (antes saía "cru")."""
    esq = client.get("/ttsproxy/openapi.json", headers=auth).json()
    assert esq["servers"] == [{"url": app._BASE_PATH}]
    html = client.get("/ttsproxy/docs", headers=auth).text
    assert f"{app._BASE_PATH}/openapi.json" in html      # Swagger aponta pro prefixo


def test_nome_de_voz_tem_teto(client, auth, vozes_isoladas):
    r = client.post("/api/voices", headers=auth,
                    data={"name": "n" * 400, "denoise": "0"},
                    files={"audio": ("a.wav", _wav_bytes(3.2), "audio/wav")})
    assert r.status_code == 200, r.text
    assert len(r.json()["name"]) == 120


def test_delete_all_outputs_devolve_balanco(client, auth, tmp_path, monkeypatch):
    """Arquivo que não sai não derruba a resposta no meio: vira `falhas`."""
    od = tmp_path / "outputs"
    (od / "travado.json").mkdir(parents=True)       # diretório com nome de .json
    (od / "ok.json").write_text("{}")
    monkeypatch.setattr(app, "OUTPUTS_DIR", od)
    r = client.delete("/api/outputs", headers=auth)
    assert r.status_code == 200
    assert r.json()["removidos"] == 1 and r.json()["falhas_total"] == 1
    assert "travado.json" in r.json()["falhas"][0]


def test_erro_do_job_sem_caminho_absoluto():
    import pathlib as _p
    base = str(_p.Path(app.BASE))
    sujo = f"Traceback em {base}/.venv-mlx/bin/python e {_p.Path.home()}/x.py"
    limpo = app._sanitiza_caminhos(sujo)
    assert base not in limpo and str(_p.Path.home()) not in limpo
    assert ".venv-mlx/bin/python" in limpo and "~/x.py" in limpo


def test_chat_debug_rotaciona_e_e_0600(client, auth, tmp_path, monkeypatch, capsys):
    alvo = tmp_path / "debug.log"
    monkeypatch.setenv("TTS_CHAT_DEBUG", "1")
    monkeypatch.setenv("TTS_CHAT_DEBUG_LOG", str(alvo))
    monkeypatch.setattr(app, "_CHAT_DEBUG_MAX_BYTES", 10)
    assert client.post("/api/chat-debug", headers=auth, json={"a": 1}).json()["arquivo"] == str(alvo)
    assert alvo.exists() and (alvo.stat().st_mode & 0o777) == 0o600
    alvo.write_text("x" * 40)                       # passa do teto
    client.post("/api/chat-debug", headers=auth, json={"a": 2})
    assert alvo.with_suffix(".log.1").exists(), "estourou o teto: tem de rodar"
    assert len(alvo.read_text()) < 40, "o arquivo corrente recomeça"


def test_vad_fail_open_vai_no_resultado(client, auth, vozes_isoladas, monkeypatch):
    monkeypatch.setattr(app, "_vad_tem_fala", lambda *a, **k: True)
    monkeypatch.setattr(app, "_vad_erro", "RuntimeError: silero fora")
    monkeypatch.setattr(app, "_transcribe_parakeet", lambda p: {"text": "oi", "language": "pt"})
    monkeypatch.setitem(app._settings, "stt_local_engine", "parakeet")
    origem = _wav_bytes(1.0)
    r = client.post("/api/stt-partial", headers=auth,
                    files={"audio": ("a.wav", origem, "audio/wav")})
    assert r.status_code == 200
    assert app._transcribe is not None


def test_speaker_gate_label_avisa_sem_embedding(tmp_path, monkeypatch):
    monkeypatch.setitem(app._settings, "speaker_gate", "label")
    monkeypatch.setattr(app, "_speaker_load_estado",
                        lambda: ({"ana": {"vecs": []}}, ""))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: None)
    wav = tmp_path / "a.wav"
    wav.write_bytes(_wav_bytes(0.2))
    saida = app._speaker_gate_ok(wav)
    assert "speaker_aviso" in saida and "etiqueta" in saida["speaker_aviso"]


def test_efeitos_de_settings_rodam_depois_do_commit(client, auth, monkeypatch):
    """Erro no efeito pós-commit não perde a config salva: 200 + `avisos`."""
    def _explode():
        raise RuntimeError("autofree quebrou")

    monkeypatch.setattr(app, "_autofree_local", _explode)
    r = client.post("/api/settings", headers=auth, json={"speed": 1.15})
    assert r.status_code == 200, r.text
    assert r.json()["speed"] == 1.15 and r.json()["avisos"]
    assert app._settings["speed"] == 1.15


def test_settings_invalido_nao_dispara_efeito(client, auth, monkeypatch):
    """Falhou a validação: a RAM volta e o efeito NEM roda (antes ele já tinha rodado)."""
    chamadas = []
    monkeypatch.setattr(app, "_autofree_local", lambda: chamadas.append(1))
    antes = dict(app._settings)
    r = client.post("/api/settings", headers=auth,
                    json={"speed": 1.2, "chat_backend": "invalido"})
    assert r.status_code == 400
    assert chamadas == [] and app._settings["speed"] == antes["speed"]


def test_guarda_nofile_loga_e_mantem_stdout_limpo(capsys):
    """A guarda era muda: agora diz o que recusou, no stderr."""
    import resource
    monkeypatch_vistos = set()
    app._nofile_pedidos_vistos.clear()
    try:
        app._nofile_log((2048, 4096), (65536, 65536), None)
        app._nofile_log((2048, 4096), (65536, 65536), None)     # repetido não repete
        saida = capsys.readouterr()
        assert saida.err.count("[nofile]") == 1
        assert "recusado" in saida.err and "2048/4096" in saida.err
        assert saida.out == ""
    finally:
        app._nofile_pedidos_vistos.update(monkeypatch_vistos)
    assert resource.RLIMIT_NOFILE


def test_hist_tam_conta_chars_e_usa_o_teto_em_chars():
    """O teto de retomada é declarado em CHARS — o nome diz o que é medido."""
    reg = {"msgs": [{"content": "a" * 100}], "system": "s" * 10, "resumo": "r" * 5}
    assert app._live_hist_tam(reg) == 115
    assert app._LIVE_RESUME_MAX_CHARS == 4 * 1024 * 1024


def test_apikeys_enabled_desligar_avisa_sem_409(client, chave_unica):
    """Desligar pela rota de `enabled` é intencional: 200 + aviso, nunca 409."""
    r = client.post("/api/apikeys/enabled", headers=chave_unica, json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["auth_active"] is False
    assert r.json()["avisos"] and "sem credencial" in r.json()["avisos"][0]
    r = client.post("/api/apikeys/enabled", headers=chave_unica, json={"enabled": True})
    assert r.status_code == 200 and not r.json().get("avisos")
    assert r.json()["auth_active"] is True


def test_apikeys_persistir_materializa_o_que_so_esta_na_ram(client, auth):
    """O import não grava (#601); esta rota é a escrita explícita sem mudar nada.

    Cobre o buraco deixado por "import não grava": quem subiu com `.apikey`/env (ou
    numa instalação nova, com a chave padrão só na RAM) materializa em disco para a
    chave sobreviver ao restart e o `run.sh` reencontrar o espelho."""
    app.APIKEYS_PATH.unlink(missing_ok=True)
    r = client.post("/api/apikeys/persistir", headers=auth)
    assert r.status_code == 200, r.text
    assert app.APIKEYS_PATH.exists() and app.LEGACY_APIKEY_PATH.exists()
    assert r.json()["keys"] == len(app._apikeys.get("keys") or [])
    # o espelho é a 1ª chave gerenciada, como em qualquer gravação
    assert app.LEGACY_APIKEY_PATH.read_text().strip() == app._apikeys["keys"][0]["secret"]
    # e a chave de USO não materializa (é escrita elevada)
    uso = {"X-API-Key": app._apikeys["keys"][0]["secret"]}
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    app.APIKEYS_PATH.unlink()
    try:
        assert client.post("/api/apikeys/persistir", headers=uso).status_code == 403
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()


# ---------------------------------------------------------------------------
# `Origin` no loopback (CSRF / DNS-rebinding) — achado P1 da auditoria #282.
# O loopback dispensa auth E rate limit e o CORS é `*`: sem esta guarda, qualquer
# página aberta no navegador do dono LÊ os segredos e derruba o servidor.
# Três pontas obrigatórias aqui: origem boa (não pode regredir), origem ruim
# (tem de barrar) e origem AUSENTE (curl/SDK: comportamento de sempre).
# ---------------------------------------------------------------------------

_ORIGEM_MAQUINA = "http://127.0.0.1:7860"
_ORIGEM_ESTRANGEIRA = "http://evil.tld"


def _cliente_local():
    """TestClient cujo socket é loopback (o caso em que a guarda de Origin age)."""
    return TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def test_origin_boa_nao_regride_o_fluxo_do_mac(client, auth, monkeypatch):
    """A UI da própria máquina abre a porta e o `Host` da requisição é o mesmo:
    origem igual à da requisição, loopback explícito e extensão LISTADA seguem
    recebendo segredo como antes (#813: extensão não é mais aprovada por prefixo)."""
    monkeypatch.setattr(app, "_ORIGENS_PERMITIDAS",
                        tuple(app._ORIGENS_PERMITIDAS)
                        + (_ORIGEM_MAQUINA, "http://localhost:7860",
                           "http://[::1]:7860", "chrome-extension://abc123"))
    c = _cliente_local()
    for origem in ("http://testserver", _ORIGEM_MAQUINA, "http://localhost:7860",
                   "http://[::1]:7860", "chrome-extension://abc123"):
        r = c.get("/api/apikeys?reveal=1", headers={"Origin": origem})
        assert r.status_code == 200, origem
        assert any(k.get("secret") for k in r.json()["keys"]), origem
    # a lista é liberação explícita do dono: a mesma origem que sem liberação
    # tomaria 403 passa a ler
    c2 = _cliente_local()
    app._ORIGENS_PERMITIDAS = tuple(app._ORIGENS_PERMITIDAS) + (_ORIGEM_ESTRANGEIRA,)
    try:
        r = c2.get("/api/apikeys?reveal=1", headers={"Origin": _ORIGEM_ESTRANGEIRA})
        assert r.status_code == 200
        assert any(k.get("secret") for k in r.json()["keys"])
    finally:
        app._ORIGENS_PERMITIDAS = tuple(
            x for x in app._ORIGENS_PERMITIDAS if x != _ORIGEM_ESTRANGEIRA)


def test_tts_cors_origins_listada_libera_e_fora_da_lista_barra():
    """A lista é liberação EXPLÍCITA do dono: o que está nela passa e a guarda
    barra quem ficou de fora (#290, #438, #813)."""
    originais = list(app._ORIGENS_PERMITIDAS)
    app._ORIGENS_PERMITIDAS = ("https://app.exemplo",)
    try:
        c = _cliente_local()
        for rota in ("/api/apikeys?reveal=1", "/api/settings"):
            r = c.get(rota, headers={"Origin": "https://app.exemplo"})
            assert r.status_code == 200, rota
        r = c.get("/api/apikeys?reveal=1", headers={"Origin": "http://outro.exemplo"})
        assert r.status_code == 403
        assert r.headers.get("X-TTS-Origin-Bloqueada") == "1"
    finally:
        app._ORIGENS_PERMITIDAS = tuple(originais)


def test_curinga_desliga_a_guarda_de_origem_escolha_consciente():
    """`*` na lista desliga a guarda inteira: é escolha do dono (quem confia em
    qualquer origem já aceita CSRF de qualquer origem) — aqui fica documentado como
    COMPORTAMENTO, não como acidente."""
    originais = list(app._ORIGENS_PERMITIDAS)
    app._ORIGENS_PERMITIDAS = ("*",)
    try:
        c = _cliente_local()
        r = c.get("/api/apikeys?reveal=1", headers={"Origin": _ORIGEM_ESTRANGEIRA})
        assert r.status_code == 200
        assert any(k.get("secret") for k in r.json()["keys"])
        # a mutante também passa sem chave (o que o CSRF exploraria)
        assert c.post("/api/models/unload",
                      headers={"Origin": _ORIGEM_ESTRANGEIRA}).status_code in (200, 409)
    finally:
        app._ORIGENS_PERMITIDAS = tuple(originais)


def test_proxy_coerente_reconhece_a_mesma_origem(monkeypatch):
    """Atrás do `/ttsproxy` (Cloudflare), `x-forwarded-proto` + `host` coerentes
    descrevem a mesma origem e a UI não é barrada por acidente.

    O nome do túnel tem de estar em `TTS_ALLOWED_HOSTS` (#779): socket loopback com
    `Host` de fora não é mais isento por si só."""
    monkeypatch.setattr(app, "_ALLOWED_HOSTS", ("tts.exemplo",))
    c = _cliente_local()
    r = c.get("/api/apikeys?reveal=1", headers={
        "Origin": "https://tts.exemplo", "x-forwarded-proto": "https",
        "host": "tts.exemplo"})
    assert r.status_code == 200
    assert any(k.get("secret") for k in r.json()["keys"])


def test_x_forwarded_host_forjado_sozinho_nao_vira_mesma_origem():
    """A `_mesma_origem` lê o `Host`, NÃO o `x-forwarded-host`. Página de terceiro
    que só forja `x-forwarded-host` continua barrada — se um dia virar "mesma
    origem", o #438 sobe de P3 (o `Host` é o que o proxy reescreve e o browser não
    controla; o `x-forwarded-*` viaja junto do atacante se o proxy os repassa)."""
    c = _cliente_local()
    r = c.get("/api/apikeys?reveal=1", headers={
        "Origin": _ORIGEM_ESTRANGEIRA, "x-forwarded-host": _ORIGEM_ESTRANGEIRA[7:]})
    assert r.status_code == 403
    assert r.headers.get("X-TTS-Origin-Bloqueada") == "1"


def test_origin_ausente_continua_valendo_como_o_mac(client, auth):
    """Sem `Origin` (curl, SDK, scripts) o loopback segue dispensando a chave —
    é o comportamento de sempre e não pode ter regredido."""
    c = _cliente_local()
    r = c.get("/api/apikeys?reveal=1")
    assert r.status_code == 200
    assert any(k.get("secret") for k in r.json()["keys"])


def test_origin_estrangeira_nao_le_segredo_sem_chave(client, auth):
    """Origem de terceiro (CSRF): qualquer GET de /api exigе chave (#282, #780)."""
    c = _cliente_local()
    h = {"Origin": _ORIGEM_ESTRANGEIRA}
    for rota in ("/api/apikeys?reveal=1", "/api/settings", "/api/client/mic-router"):
        r = c.get(rota, headers=h)
        assert r.status_code == 403, rota
        assert r.headers.get("X-TTS-Origin-Bloqueada") == "1", rota
        assert _ORIGEM_ESTRANGEIRA in r.json()["origin"]
    # sem segredo no corpo (o 403 não vaza nada) e, desde o #780, TODO GET de
    # origem estrangeira exige chave — não só as rotas que revelam segredo
    corpo = c.get("/api/apikeys?reveal=1", headers=h).json()
    assert corpo["detail"] and "keys" not in corpo
    assert c.get("/api/status", headers=h).status_code == 401
    assert c.get("/api/voices", headers=h).status_code == 401

    # COM a chave, a mesma origem lê normalmente
    r = c.get("/api/apikeys?reveal=1", headers={**h, **auth})
    assert r.status_code == 200
    assert any(k.get("secret") for k in r.json()["keys"])


def test_origin_estrangeira_exige_chave_no_metodo_mutante(client, auth, monkeypatch):
    """Mutação por origem de terceiro sem chave: 401. Com a chave, passa."""
    c = _cliente_local()
    h = {"Origin": _ORIGEM_ESTRANGEIRA}
    assert c.delete("/api/outputs", headers=h).status_code == 401
    assert c.delete("/api/outputs", headers={**h, **auth}).status_code == 200
    # `shutdown` responde e chama `os._exit(0)` numa thread: sem interceptar, o
    # processo do pytest morre no MEIO da suíte (log truncado, rc 0, sem resumo).
    saida = []
    monkeypatch.setattr(os, "_exit", lambda codigo=0: saida.append(codigo))
    assert c.post("/api/shutdown", headers=h).status_code == 401
    assert c.post("/api/shutdown", headers={**h, **auth}).status_code == 200
    assert _espera(lambda: saida, 2.0), "o handler tem de pedir o exit após responder"
    assert saida == [0]


def test_origin_estrangeira_nao_herda_o_admin_do_loopback(client, auth, chave_de_uso):
    """Privilégio não vem mais do socket: a página de fora vale pela CREDENCIAL,
    então uma chave `use` continua sem administrar."""
    c = _cliente_local()
    h = {"Origin": _ORIGEM_ESTRANGEIRA, **chave_de_uso}
    assert c.post("/api/apikeys", headers=h, json={"name": "x"}).status_code == 403
    assert c.get("/api/settings", headers=h).json()["is_admin"] is False
    assert c.get("/api/settings", headers={**h, **auth}).json()["is_admin"] is True


def test_origin_barrada_aparece_no_status(client, auth, monkeypatch):
    """/api/status publica a tentativa barrada — CSRF deixa de ser um 403 mudo."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    c = _cliente_local()
    assert c.get("/api/apikeys?reveal=1", headers={"Origin": _ORIGEM_ESTRANGEIRA}).status_code == 403
    # desde o #780 o próprio /api/status também exige chave para origem de fora
    painel = c.get("/api/status", headers={"Origin": _ORIGEM_ESTRANGEIRA,
                                           **auth}).json()["origens_barradas"]
    assert painel["total"] == 1
    assert any(_ORIGEM_ESTRANGEIRA in k for k in painel["recentes"])


def test_origin_da_lan_continua_exigindo_chave(client, auth):
    """A regra da LAN não afrouxou: socket não-loopback exige chave mesmo com
    origem ausente e mesmo com origem da própria máquina."""
    lan = TestClient(app.app, raise_server_exceptions=False, client=("203.0.113.9", 50000))
    for cabecalho in ({}, {"Origin": "http://203.0.113.9:7860"}):
        assert lan.get("/api/apikeys", headers=cabecalho).status_code == 401
        assert lan.get("/api/apikeys", headers={**cabecalho, **auth}).status_code == 200


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


# O `WebSocketTestSession` não tem timeout: regressão que suprima um evento deixa o
# teste — e a suíte INTEIRA — pendurada, sem vermelho (visto no gate #413). Todo
# receive daqui passa por um worker com prazo; None = nada chegou.
_PRAZO_RECV = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def _proximo(ws, segundos: float = 10.0):
    """Próximo frame ou None no prazo. Manda o worker de volta quando vem frame."""
    fut = _PRAZO_RECV.submit(ws.receive)
    try:
        return fut.result(timeout=segundos)
    except concurrent.futures.TimeoutError:
        return None


def _recv(ws, timeout: float = 20.0):
    """Próximo frame com prazo (None se estourar). Era `ws.receive()` cru."""
    return _proximo(ws, timeout)


def _setup_ok(ws, **extra):
    """`setup` + `ready`. Com prazo: handshake travado tem de reprovar, não pendurar."""
    ws.send_json({"type": "setup", **extra})
    m = _proximo(ws, 20.0)
    assert m is not None and m.get("text"), f"sem `ready` do servidor: {m!r}"
    return _json.loads(m["text"])


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


def _arvore_com_escrita_de_terceiro(tmp_path, monkeypatch):
    """Cenário do escritor em curso: árvore de mentira + módulo escrito AGORA
    (mtime > boot, o que liga a tolerância do #220). Devolve o que o disco de
    agora vale, para o chamador conferir a premissa."""
    for nome in app._BUILD_MODULOS:
        shutil.copy2(app.BASE / nome, tmp_path / nome)
    monkeypatch.setattr(app, "BASE", tmp_path)
    (tmp_path / "live_turns.py").write_text("# escrito por um TERCEIRO\n")
    assert "live_turns.py" in _modulos_escritos_desde_o_boot(tmp_path), \
        "cenário não montado: falta o mtime > boot que autoriza a tolerância"
    return _hash_dos_modulos(tmp_path)


def test_build_tolerancia_nao_pode_ser_apagada_em_silencio(client, auth, tmp_path,
                                                           monkeypatch):
    """#227 (mutação 1): com um escritor, quem responde é a TOLERÂNCIA — o par não
    pode passar pela expectativa exata.

    A sabotagem é a expectativa (`_HASH_NO_IMPORT` que não descreve o boot, como se
    a cópia do teste fosse de outro conteúdo: é o caso da JANELA). O par TEM de
    seguir verde por ALI; com a tolerância desligada (`if escritos:` -> `if False:`),
    o ramo exato compara com a expectativa sabotada e reprova."""
    disco = _arvore_com_escrita_de_terceiro(tmp_path, monkeypatch)
    assert disco != app._BUILD_CODIGO, "premissa: o disco de agora não é o boot"
    test_api = sys.modules[__name__]
    monkeypatch.setattr(test_api, "_HASH_NO_IMPORT", "00000000")
    monkeypatch.setattr(test_api, "_JANELA_SUJA", ["live_turns.py"])
    _confere_que_o_codigo_e_do_boot(client.get("/api/build", headers=auth).json())


def test_build_conteudo_do_codigo_vale_com_arvore_viva(client, auth, tmp_path,
                                                       monkeypatch, capsys):
    """#227 (mutação 2): a prova de CONTEÚDO (#190) não pode depender de a árvore
    estar quieta.

    Mesmo cenário do escritor, sem sabotar nada: o par passa PELA tolerância (o
    marcador prova o caminho) e, ainda assim, `codigo` tem de reproduzir o SNAPSHOT
    do import — é o que impede o campo de virar literal (`_BUILD_CODIGO = "cafe1234"`
    passava batido: o ramo da tolerância só olhava o disco)."""
    disco = _arvore_com_escrita_de_terceiro(tmp_path, monkeypatch)
    d = client.get("/api/build", headers=auth).json()
    assert disco != app._BUILD_CODIGO, "premissa: o disco de agora não é o boot"
    assert app._BUILD_CODIGO != "cafe1234", "premissa do mutante do #227"
    _confere_que_o_codigo_e_do_boot(d)
    assert "[220] escritos depois do boot" in capsys.readouterr().out, \
        "o par não passou pela tolerância — o teste não mediu o que promete"


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
        # #313: `barge_ms` entrou no contrato da `vad` (clamp 150–600, default 300)
        assert pronto["vad"] == {"silence_ms": 5000, "prefix_ms": 0, "barge_ms": 300}
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
        # #413: com prazo. O desfecho aceitável é `interrupted`; sessão ociosa passa
        # (o `cancel` pode não ter turno para interromper), mas NUNCA pendura — e um
        # `interrupted` que suma de verdade reprova aqui em vez de travar o gate.
        m = _proximo(ws, 10.0)
        assert m is not None, "o `cancel` não devolveu nada (nem `interrupted`, nem silêncio)"
        if m.get("text"):
            assert _json.loads(m["text"])["type"] in ("interrupted", "pong",
                                                      "stats", "prewarm")


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
    """Pipeline real com stt/llm/tts/prewarm fakes (sem MLX).

    O settings do dono pode estar em `chat_backend=dsh`. Sem este pin o Live
    sobe o dsh de verdade e o turno espera o reasoning do GLM (minutos) em vez
    do llm fake — a suíte deixa de ser hermética."""
    import live_pipeline
    for var in ("TTS_CHAT_BACKEND", "TTS_CHAT_BACKEND_LIVE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "")
    monkeypatch.setattr(app, "_live_mod", live_pipeline)   # (o `live_limpo` zera)
    monkeypatch.setattr(live_pipeline, "_prewarm_app", lambda: None)
    # o #314 passou a passar `julga` como 3º argumento POSICIONAL ao `_stt_app`;
    # sem o parâmetro o dublê estoura e o turno morre no stage=stt
    monkeypatch.setattr(live_pipeline, "_stt_app",
                        lambda pcm, lang, julga=None: "que horas são")
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
        m = _recv(ws)                    # com prazo (#479): regressão reprova, não pendura
        if m is None:
            break
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
        # o barge do motor é PROVA de interrupção e força o aviso mesmo com a sessão
        # ainda sem turno (`_live_cancela(forcar=True)`, #313); `_recv` tem prazo, então
        # evento faltando reprova em 20 s em vez de pendurar a suíte
        m = _recv(ws)
        assert m is not None, "o barge_in não produziu nada depois dele"
        ev = _json.loads(m["text"])
        assert ev["type"] == "interrupted", f"esperava `interrupted`, veio {ev!r}"


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

def _dsh_fecha_pool() -> None:
    """Fecha os clientes dsh que sobraram no pool (teardown entre testes)."""
    for cli in list(getattr(app, "_chat_dsh_livres", None) or []):
        try:
            cli.close()
        except Exception:            # noqa: BLE001 — teardown não derruba a suíte
            pass


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
    monkeypatch.setattr(app, "_dsh_models_erro", {})     # cache negativo (#287)
    monkeypatch.setattr(app.dsh_client, "_node_versoes", {})  # memo do node (#373)
    monkeypatch.setattr(app, "_dsh_morto_ate", 0.0)      # latch de processo
    # FECHA o que o teste anterior deixou no pool: só zerar a lista abandonava o
    # processo do `fake_acp` até o fim da sessão (6 vivos com 30+ min medidos, mesmo
    # dano do uvicorn órfão do #688). O `close()` do dsh já tem prazo próprio.
    _dsh_fecha_pool()
    monkeypatch.setattr(app, "_chat_dsh_livres", [])
    # contador GLOBAL de processos dsh (#287): cliente vazado por um teste não pode
    # consumir o teto do seguinte
    monkeypatch.setattr(app, "_dsh_procs", 0)
    monkeypatch.setattr(app, "_chat_dsh_prewarm_thread", None)


@pytest.fixture(scope="session")
def fake_acp_estavel(tmp_path_factory):
    """Wrapper do servidor ACP falso num diretório ESTÁVEL da sessão (#464).

    Era `tmp_path` do próprio teste, e o diretório é apagado no teardown dele. Um
    cliente `dsh` que SOBREVIVE ao teste (prewarm recém-subido, pool) continuava com
    o caminho antigo e o handshake seguinte morria com `No such file or directory`,
    caindo no retry de 4 s e logando o que parece defeito do harness. Diretório de
    sessão: o binário vale enquanto a rodada existir. O nome não começa com `dsh`,
    de propósito (pula o check de Node, hermético)."""
    fake = Path(__file__).resolve().parent / "fake_acp.py"
    alvo = Path(tmp_path_factory.mktemp("fake-acp")) / "fake-acp"
    alvo.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    alvo.chmod(0o755)
    return str(alvo)


@pytest.fixture()
def dsh_fake_bin(fake_acp_estavel, monkeypatch):
    """`chat_dsh_bin` apontando para o wrapper ESTÁVEL (ver `fake_acp_estavel`)."""
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    monkeypatch.setitem(app._settings, "chat_dsh_bin", fake_acp_estavel)
    return fake_acp_estavel


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


# ---------------------------------------------------------------------------
# Teto GLOBAL de processos dsh (#287): pool (3) + Live (1 por sessão) + descoberta
# somavam até 8 harnesses node sem contador comum. O excedente recebe
# `dsh_ocupado`; a contagem fica visível no `/api/status`.
# ---------------------------------------------------------------------------

@pytest.fixture()
def dsh_zero(monkeypatch):
    """Contador global em zero e o menu de configuração dele preservado."""
    monkeypatch.setattr(app, "_dsh_procs", 0)
    monkeypatch.setattr(app, "_DSH_MAX_PROCESSOS", 2)
    return 2


def test_teto_global_de_dsh_conta_e_libera_slots(dsh_zero):
    clis = []
    for _ in range(2):
        cli = app._DshComSlot(bin="dsh", profile="p", prewarm=False)
        clis.append(cli)
        assert cli._slot_contado is True
    assert app._dsh_procs == 2
    with pytest.raises(app._DshSemSlot) as exc:
        app._DshComSlot(bin="dsh", profile="p", prewarm=False)
    assert "teto" in str(exc.value)
    app._dsh_fecha(clis[0])                    # fechar devolve o slot
    assert app._dsh_procs == 1
    app._dsh_libera_slot(clis[0])              # liberação dupla não subtrai de novo
    assert app._dsh_procs == 1
    app._dsh_fecha(clis[1])
    assert app._dsh_procs == 0


def test_descoberta_com_cache_negativo_responde_na_hora(client, auth, monkeypatch,
                                                        dsh_limpo):
    """dsh quebrado: o 2º GET não paga as 6 tentativas de novo (#287)."""
    import time as _t
    chamadas = []

    def _falha(**kw):
        chamadas.append(1)
        _t.sleep(0.3)                        # custo de um handshake que falha
        raise app.dsh_client.DshError("rc=1 (binário existe e não sobe)")

    monkeypatch.setattr(app.dsh_client, "descobrir_modelos", _falha)
    t0 = _t.perf_counter()
    r1 = client.get("/api/chat/dsh/models", headers=auth)
    primeira = _t.perf_counter() - t0
    assert r1.status_code == 502 and "rc=1" in r1.json()["detail"]

    r2 = client.get("/api/chat/dsh/models", headers=auth)
    assert r2.status_code == 502 and "há pouco" in r2.json()["detail"]
    assert r2.headers["Retry-After"] == "30"
    # a prova de "respondeu na hora" é NÃO TER CHAMADO o descobridor (o tempo serve
    # de ilustração; sob suíte concorrente a parede varia e comparação entre as duas
    # chamadas falseia — medido: 0,46 s na primeira e 0,51 s na segunda sob carga)
    assert len(chamadas) == 1, "o cache negativo não pode spawnar de novo"
    assert primeira > 0

    # trocar a CONFIG invalida (a falha era daquele bin/perfil)
    monkeypatch.setitem(app._settings, "chat_dsh_bin", "/outro/dsh")
    assert client.get("/api/chat/dsh/models", headers=auth).status_code == 502
    assert len(chamadas) == 2


def test_descoberta_limpa_o_cache_negativo_no_sucesso(client, auth, monkeypatch, dsh_limpo):
    import time as _t
    estado = {"falha": True}

    def _tenta(**kw):
        if estado["falha"]:
            raise app.dsh_client.DshError("falhou")
        return {"models": [], "bin": "dsh", "profile": "p", "node": "25"}

    monkeypatch.setattr(app.dsh_client, "descobrir_modelos", _tenta)
    assert client.get("/api/chat/dsh/models", headers=auth).status_code == 502
    estado["falha"] = False
    monkeypatch.setattr(app, "_dsh_models_erro", dict(app._dsh_models_erro, em=_t.time() - 60))
    assert client.get("/api/chat/dsh/models", headers=auth).status_code == 200
    assert app._dsh_models_erro == {}

    estado["falha"] = True
    monkeypatch.setattr(app, "_dsh_models_cache", {})
    assert client.get("/api/chat/dsh/models", headers=auth).status_code == 502


def test_bridge_clean_encurta_o_teto_e_aparece_no_stats(client, auth, monkeypatch,
                                                        dsh_limpo):
    """Sem o patch o harness entrega a resposta INTEIRA no fim: esperar 12 s é só
    atraso. Bridge `clean` encurta o teto, avisa e fica no `stats` (#287)."""
    monkeypatch.setattr(app, "_DSH_MAX_PROCESSOS", 5)
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
    monkeypatch.setitem(app._settings, "chat_dsh_bin", "/nao/existe/dsh")
    monkeypatch.setattr(app, "_live_mod", __import__("live_pipeline", fromlist=["x"]))
    monkeypatch.setattr(app, "_bridge_cache", {})
    monkeypatch.setattr(app, "_dsh_bridge_estado", lambda bin=None: {
        "estado": "clean", "arquivo": "/x/lib/index.js", "versao": "1",
        "motivo": "sem patch"})

    sess = {"history": [], "voice_id": None, "system": "", "dsh": None,
            "fila": __import__("queue").Queue(), "visto": time.monotonic(),
            "st_proximo": 0.0}
    pipe = app._live_pipe_novo(sess)
    assert pipe._teto_primeiro == app._DSH_FIRST_DELTA_CLEAN_S < 12.0
    assert pipe.bridge_estado == "clean"
    assert app._live_stats_ia({"pipe": pipe})["bridge"] == "clean"


def test_descoberta_nao_roda_node_version_duas_vezes(monkeypatch, dsh_limpo):
    """`descobrir_modelos` chamava `resolver_bin` com checa_node e o `_garantir` de
    dentro checava de novo: dois `node --version` por descoberta (#287)."""
    chamadas = []
    monkeypatch.setattr(app.dsh_client, "node_versao",
                        lambda env=None: chamadas.append(1) or (25, 0))
    monkeypatch.setattr(app.dsh_client, "resolver_bin",
                        lambda bin=None, checa_node=True: (
                            chamadas.append("cb") if checa_node else chamadas.append("sb")) or "/usr/bin/python3")

    class _Falso:
        config_options = []
        def prewarm(self):
            self.chamou = True
        def close(self):
            pass

    monkeypatch.setattr(app.dsh_client, "DshClient", lambda **kw: _Falso())
    app.dsh_client.descobrir_modelos(bin="x", profile="p")
    assert "cb" not in chamadas, "resolver_bin não pode checar node aqui"


def test_estado_bridge_tem_todos_os_campos_do_contrato(monkeypatch):
    """O dict de retorno tinha campo FANTASMA (`arquivo_bin` fora do inicial)."""
    monkeypatch.setattr(app.dsh_client, "resolver_bin",
                        lambda bin=None, checa_node=True: "/usr/bin/python3")
    d = app.dsh_client.estado_bridge(bin="x")
    assert set(d) >= {"estado", "arquivo", "arquivo_bin", "versao", "motivo"}
    assert d["estado"] in app.dsh_client.DSH_BRIDGE_ESTADOS


def test_latch_de_processo_evita_novo_handshake(client, auth, monkeypatch, dsh_limpo):
    """Falha de handshake recente não repete as 6 tentativas em cada sessão nova."""
    monkeypatch.setattr(app, "_DSH_MAX_PROCESSOS", 5)
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
    monkeypatch.setattr(app, "_live_mod", __import__("live_pipeline", fromlist=["x"]))
    app._dsh_marca_morto("rc=1 no handshake")

    sess = {"history": [], "voice_id": None, "system": "", "dsh": None,
            "fila": __import__("queue").Queue(), "visto": time.monotonic(),
            "st_proximo": 0.0}
    pipe = app._live_pipe_novo(sess)
    assert pipe._dsh is None and "rc=1" in sess["dsh_indisponivel"]
    tipos = []
    while not sess["fila"].empty():
        tipos.append(sess["fila"].get_nowait()[1].get("type"))
    assert "dsh_indisponivel" in tipos

    # latch vencido: volta a tentar (aqui o binário é falso, então falha no connect)
    monkeypatch.setattr(app, "_dsh_morto_ate", time.monotonic() - 1)
    monkeypatch.setitem(app._settings, "chat_dsh_bin", "/nao/existe/dsh")
    sess2 = {"history": [], "voice_id": None, "system": "", "dsh": None,
             "fila": __import__("queue").Queue(), "visto": time.monotonic(),
             "st_proximo": 0.0}
    app._live_pipe_novo(sess2)
    assert sess2["dsh"] is not None, "fora do latch o cliente é criado de novo"


def test_status_expoe_a_contagem_de_dsh(client, auth, dsh_zero):
    d = client.get("/api/status", headers=auth).json()
    assert d["dsh_procs"] == 0 and d["dsh_procs_max"] == 2


def test_conversa_com_teto_estourado_devolve_429(client, auth, monkeypatch, dsh_limpo):
    monkeypatch.setattr(app, "_DSH_MAX_PROCESSOS", 1)
    monkeypatch.setattr(app, "_dsh_procs", 1)          # já lotado
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    with pytest.raises(app.HTTPException) as exc:
        app._chat_llm_dsh([{"role": "user", "content": "oi"}])
    assert exc.value.status_code == 429 and "dsh_ocupado" in exc.value.detail


def test_live_com_teto_estourado_avisa_e_segue_no_openai(client, auth, monkeypatch, dsh_limpo):
    monkeypatch.setattr(app, "_DSH_MAX_PROCESSOS", 1)
    monkeypatch.setattr(app, "_dsh_procs", 1)
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
    monkeypatch.setattr(app, "_live_mod", __import__("live_pipeline", fromlist=["x"]))
    sess = {"history": [], "voice_id": None, "system": "", "dsh": None,
            "fila": __import__("queue").Queue(), "visto": time.monotonic(),
            "st_proximo": 0.0}
    pipe = app._live_pipe_novo(sess)
    assert pipe._dsh is None                          # sem cliente: cai no openai
    assert "teto" in sess["dsh_ocupado"]              # motivo legível
    tipos = []
    while not sess["fila"].empty():
        tipos.append(sess["fila"].get_nowait()[1].get("type"))
    assert "dsh_ocupado" in tipos


def test_cliente_com_prompt_em_voo_nao_volta_ao_pool(dsh_limpo, monkeypatch):
    """Devolver cliente com prompt em voo envenena o pool (espera de até 300 s e
    `-32602` no próximo): ele é FECHADO em vez de guardado."""
    class _Falso:
        def __init__(self):
            self.fechado = False
            self._pool_chave = ("dsh", "p", "m", "off")   # o pool GUARDARIA este
            self.alive = True
        prompt_em_voo = True
        def close(self):
            self.fechado = True
        def collect(self, msgs):
            return "ok"
    falso = _Falso()
    monkeypatch.setattr(app, "_chat_dsh_cliente", lambda: falso)
    assert app._chat_llm_dsh([{"role": "user", "content": "oi"}]) == "ok"
    assert falso.fechado is True and app._chat_dsh_livres == []


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
                                                    hist_limpo, dsh_fake_bin, monkeypatch):
    """Live=dsh com a Conversa no endpoint (a combinação que o #175 recomenda): o
    resumo do Live sai pelo dsh, não pelo remoto. Com o dsh marcado indisponível
    (#146), cai no endpoint — é o backend EFETIVO da sessão.

    O bin é o do fake (`dsh_fake_bin`): sem o pin o teste lia o settings.json do
    dono, que apontava o bin para um wrapper no tempdir do sistema — e virava
    vermelho assim que o sistema limpava o diretório."""
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

def test_underrun_conta_so_o_vao_maior_que_o_chunk(monkeypatch):
    """#492: `underruns` mede FOME do buffer de playback — vão entre chunks maior
    que o próprio chunk anterior + 250 ms. TTS lento enche a fila e não conta;
    cliente que não drena, sim. O campo tem de sair no `stats`.
    """
    sess = {"id": "s-fome", "t0": time.monotonic(), "criada": time.time(),
            "visto": time.monotonic(), "turno": 1, "engine": _EngFake()}
    app._live_conta_underrun(sess, 24000)          # 500 ms de áudio: 1º chunk
    app._live_conta_underrun(sess, 24000)          # logo em seguida: sem fome
    assert sess["st_underruns"] == 0
    sess["st_ultimo_chunk"] -= 1.0                 # 1000 ms > 500 + 250
    app._live_conta_underrun(sess, 24000)
    assert sess["st_underruns"] == 1
    assert app._live_stats(sess)["playback"]["underruns"] == 1


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
            m = _recv(ws, max(0.5, prazo - time.monotonic()))
            if m is None:                       # prazo estourado (#479): sai e assere
                break
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


def test_live_ws_hold_abre_e_fecha_no_botao_sem_derrubar_o_ws(ws_client, live_limpo,
                                                            monkeypatch):
    """#417/#506: `{"type":"hold"}` do cliente vira `set_hold` no motor REAL.

    O ramo já derrubou o WS (`_abrir_turno` com histórico vazio) e, apagado, nada
    em `pytest tests/` acusava — só a suíte de browser, que roda no estágio
    `instancia`. Aqui o motor é o de verdade (`live_turns`), com a sessão recém
    aberta e SEM frame nenhum: `hold:true` tem de ligar o modo e manter o socket
    de pé, `hold:false` desligar. Os dois lados de `dados.get("on")` são
    exercitados — trocar por literal reprova.
    """
    import live_turns

    monkeypatch.setattr(app, "_live_turns_mod", live_turns)
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 30)      # `live_limpo` deixa 0 (sem stats)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws)["type"] == "ready"
        sess = list(app._live_sessions.values())[0]

        # o `speech_start` com `detalhe: "hold"` é o EVENTO do motor chegando no
        # cliente: sem o despacho de `_live_trata_eventos` ele não sai
        assert _hold_no_stats(ws, sess, True, evento="speech_start") is True
        assert _hold_no_stats(ws, sess, False) is False
        # socket vivo depois dos dois comandos: `error` de fechamento reprova
        ws.send_bytes(b"\x00\x01" * 1600)
        assert _ate(ws, {"stats"})["type"] == "stats"


def _hold_no_stats(ws, sess, ligado: bool, prazo: float = 3.0, evento: str | None = None):
    """Manda o comando e devolve `stats.motor.hold_to_talk` (espera virar `ligado`).

    Com `evento`, exige antes um frame daquele tipo (o evento do motor tem de ter
    sido DESPACHADO, não só aplicado no estado)."""
    ws.send_json({"type": "hold", "on": ligado})
    fim = time.monotonic() + prazo
    visto = False
    while time.monotonic() < fim:
        m = _recv(ws, max(0.5, fim - time.monotonic()))
        if m is None:
            break
        if m.get("bytes"):
            continue
        ev = _json.loads(m["text"])
        assert ev["type"] != "error", ev
        if evento and ev["type"] == evento:
            assert ev.get("detalhe") == "hold", ev
            visto = True
        if ev["type"] == "stats" and ev["motor"]["hold_to_talk"] is ligado:
            if evento and not visto:
                continue                       # o evento ainda não chegou
            return ev["motor"]["hold_to_talk"]
    mot = sess.get("engine")
    estado = mot.estatisticas() if mot is not None else None
    raise AssertionError(f"`hold: {ligado}` não chegou no motor"
                         f"{f' nem disparou {evento}' if evento else ''}: {estado}")


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

    def playback_restante_ms(self):
        return 850          # o `stats` publica: é a entrada da regra do `fim_de_resposta`


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

    def playback_restante_ms(self):
        return None      # sem playback conhecido: o `stats` publica `restante_ms: null`


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
        vista = False
        for _ in range(50):                                 # 1ª stats tem de chegar
            m = _recv(ws)                                   # com prazo (#479)
            if m is None:
                break
            if not m.get("bytes") and _json.loads(m["text"])["type"] == "stats":
                vista = True
                break
        assert vista, "nenhuma stats antes do fechar"
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
    """Sem queda do remoto: sem header e sem campo de fallback.

    O dublê devolve "ok" de propósito. Com `stt_anti_ruido` ligado a blacklist
    rejeita a frase inteira e o 200 volta com texto vazio — o teste só ficava
    verde enquanto o settings.json do dono tinha o filtro desligado (virou
    true às 23:31). Pina o filtro aqui. A blacklist não muda: em produção,
    com o filtro ligado, "ok" continua rejeitado.
    """
    monkeypatch.setitem(app._settings, "stt_anti_ruido", False)
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


def test_binario_do_fake_acp_sobrevive_ao_teardown_do_teste(dsh_fake_bin, dsh_limpo):
    """#464: o wrapper não pode morar no `tmp_path` do próprio teste.

    Um cliente `dsh` que sobrevive ao teste (prewarm recém-subido, pool) seguia com
    o caminho de um diretório JÁ APAGADO e o handshake seguinte morria com
    `No such file or directory` — caía no retry de 4 s e logava o que parece
    defeito do harness. Aqui: o arquivo existe, executa, e o diretório do teste
    pode ser removido sem levá-lo junto."""
    import shutil
    import subprocess as sp

    caminho = Path(dsh_fake_bin)
    assert caminho.is_file() and os.access(caminho, os.X_OK)
    assert shutil.which(str(caminho)), "o binário do prewarm tem de ser executável"
    r = sp.run([str(caminho), "--version"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr[-200:]
    # o diretório de sessão do pytest NÃO é o `tmp_path` deste teste
    assert "/test_binario_do_fake_acp" not in str(caminho), str(caminho)


# --- VOZ-I6 (#534): transcrever só a voz do perfil `speaker_alvo` --------------
# O caminho é separar (MossFormer2 em worker/MPS) e ficar com o fluxo mais próximo
# do centróide do perfil. Aqui o separador é STUB: o teste é do filtro e do
# contrato, não do modelo (que tem rodada real própria no card).
def _perfil_alvo(monkeypatch, nome="eu", vec=[1.0, 0.0]):
    perfis = {nome: {"vecs": [vec], "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    return perfis


def test_transcribe_com_speaker_alvo_usa_o_fluxo_do_perfil(client, auth, monkeypatch):
    """Com o parâmetro, o STT recebe o ARQUIVO SEPARADO e a resposta diz o que caiu."""
    _perfil_alvo(monkeypatch)
    import separa_voz

    monkeypatch.setattr(separa_voz, "disponivel", lambda: True)
    monkeypatch.setattr(separa_voz, "motivo", lambda: "")
    visto = {}

    def _separa(wav, vecs, modelo):
        visto["wav"] = wav
        assert vecs == [[1.0, 0.0]] and modelo
        novo = Path(str(wav) + ".alvo.wav")
        novo.write_bytes(b"alvo")
        return novo, {"escores": [0.9, 0.1], "escolhido": 0,
                      "descartados": {"trechos": 1, "ms": 2100}}

    monkeypatch.setattr(separa_voz, "separa_alvo", _separa)
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    def _transcreve(p, language=None, allow_remote=True):
        visto["stt"] = p
        return {"text": "só o alvo", "language": "pt", "segments": []}

    monkeypatch.setattr(app, "_transcribe", _transcreve)
    r = client.post("/api/transcribe", headers=auth, data={"speaker_alvo": "eu"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["text"] == "só o alvo" and corpo["separacao"] == "ok"
    assert corpo["segmentos_descartados"] == {"trechos": 1, "ms": 2100}
    assert str(visto["stt"]).endswith(".alvo.wav"), "o STT não recebeu o fluxo separado"


def test_transcribe_sem_speaker_alvo_nao_mexe_no_contrato(client, auth, monkeypatch):
    """Sem o parâmetro: nenhuma chamada ao separador e nenhum campo novo."""
    import separa_voz

    def _explode(*_a, **_k):
        raise AssertionError("não devia separar sem speaker_alvo")

    monkeypatch.setattr(separa_voz, "separa_alvo", _explode)
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "oi", "language": "pt", "segments": []})
    corpo = client.post("/api/transcribe", headers=auth,
                        files={"audio": ("a.wav", b"x", "audio/wav")}).json()
    assert corpo["text"] == "oi"
    assert not {"separacao", "segmentos_descartados"} & set(corpo)


def test_speaker_alvo_inexistente_e_400(client, auth, monkeypatch):
    """Nome errado não pode passar calado (transcreveria o áudio inteiro)."""
    _perfil_alvo(monkeypatch)
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    r = client.post("/api/transcribe", headers=auth, data={"speaker_alvo": "ninguem"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 400 and "não existe" in r.json()["detail"]


def test_sem_separador_o_transcript_sai_normal_com_aviso(client, auth, monkeypatch):
    """Sem os pesos: transcreve o original e diz `separacao: indisponivel`."""
    _perfil_alvo(monkeypatch)
    import separa_voz

    monkeypatch.setattr(separa_voz, "disponivel", lambda: False)
    monkeypatch.setattr(separa_voz, "motivo", lambda: "pesos ausentes em /x")
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "original", "language": "pt", "segments": []})
    corpo = client.post("/api/transcribe", headers=auth, data={"speaker_alvo": "eu"},
                        files={"audio": ("a.wav", b"x", "audio/wav")}).json()
    assert corpo["text"] == "original" and corpo["separacao"] == "indisponivel"
    assert "pesos ausentes" in corpo["separacao_motivo"]


def test_v1_transcriptions_aceita_speaker_alvo_como_extensao(client, auth, monkeypatch):
    """A extensão entra no `/v1/audio/transcriptions` sem quebrar o contrato OpenAI."""
    _perfil_alvo(monkeypatch)
    import separa_voz

    monkeypatch.setattr(separa_voz, "disponivel", lambda: True)
    monkeypatch.setattr(separa_voz, "separa_alvo",
                        lambda wav, vecs: (Path(str(wav) + ".a.wav"), {"escores": [1.0, 0.0]}))
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "so alvo", "language": "pt", "segments": []})
    r = client.post("/v1/audio/transcriptions", headers=auth, data={"speaker_alvo": "eu"},
                    files={"file": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 200 and r.json()["text"] == "so alvo"
    # resposta simples segue no formato OpenAI (sem campo extra)
    assert r.json() == {"text": "so alvo"}


def test_speaker_alvo_aceita_chave_de_uso(client, auth, monkeypatch):
    """Chave `use` pode pedir o filtro (é transcrição, não administração)."""
    _perfil_alvo(monkeypatch)
    import separa_voz

    monkeypatch.setattr(separa_voz, "disponivel", lambda: False)
    monkeypatch.setattr(separa_voz, "motivo", lambda: "x")
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("falso.wav"))
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "oi", "language": "pt", "segments": []})
    segredo = app._apikeys["keys"][0]["secret"]
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    try:
        r = client.post("/api/transcribe", headers={"X-API-Key": segredo},
                        data={"speaker_alvo": "eu"},
                        files={"audio": ("a.wav", b"x", "audio/wav")})
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()
    assert r.status_code == 200, r.text


def test_motivo_de_fechamento_substituida_vence_vencida_e_fechar_tem_codigo():
    """#628: ordem e códigos do motivo de fechamento.

    A substituída-e-vencida saía como `session_ttl` e a aba antiga RETOMAVA o id
    que a nova já era dona. E `fechar` (task de envio morta) dizia "ociosa"."""
    import app

    sess = {"vencida": True, "substituida": True, "fechar": True}
    assert app._live_motivo_fechamento(sess)[0] == "session_substituida"
    sess = {"vencida": False, "substituida": True, "fechar": True}
    assert app._live_motivo_fechamento(sess)[0] == "session_substituida"
    sess = {"vencida": True, "substituida": False, "fechar": True}
    assert app._live_motivo_fechamento(sess)[0] == "session_ttl"
    sess = {"vencida": False, "substituida": False, "fechar": True}
    codigo, texto = app._live_motivo_fechamento(sess)
    assert codigo == "session_fechada"
    assert "ociosa" not in texto, "o texto não pode mentir dizendo que ficou ociosa"
    assert app._live_motivo_fechamento(
        {"vencida": False, "substituida": False, "fechar": False}) is None


def test_cos_sim_levanta_para_dimensoes_diferentes():
    """Dimensão diferente é BUG de extrator, não ruído — tem de levantar.

    O `zip` truncava: ECAPA (192-d) contra centróide do Resemblyzer (256-d) dava
    0,055 para a MESMA fala, calado (#534)."""
    with pytest.raises(ValueError, match="dimensões diferentes"):
        app._cos_sim([1.0, 0.0], [1.0, 0.0, 0.0])


def test_separa_alvo_usa_o_extrator_DO_PERFIL_e_nao_o_efetivo(monkeypatch, tmp_path):
    """Perfil legado (256-d) com o ECAPA disponível tem de pontuar pelo LEGADO.

    Pontuando pelo efetivo, o vetor do ECAPA (192-d) era comparado com o centróide
    do Resemblyzer e a escolha do fluxo virava sorte (hoje, com o `_cos_sim`
    estrito, vira `falhou`). Os perfis do dono são todos legados."""
    import separa_voz

    perfis = {"eu": {"vecs": [[1.0, 0.0] + [0.0] * 254], "modelo": app.SPEAKER_MODELO_LEGADO}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    monkeypatch.setattr(separa_voz, "disponivel", lambda: True)
    monkeypatch.setattr(separa_voz, "motivo", lambda: "")
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)   # ECAPA ligado
    pedidos = []

    def _emb(p, modelo=None):
        pedidos.append(modelo)
        # conteúdo decide: fluxo A é silêncio (como a janela original), fluxo B tem
        # amostras — A casa com o perfil no espaço LEGADO, B é o concorrente
        import soundfile as _sf

        amostras = _sf.read(str(p), dtype="float32")[0]
        return ([1.0, 0.0] + [0.0] * 254 if float(abs(amostras).max()) == 0.0
                else [0.0, 1.0] + [0.0] * 254)

    monkeypatch.setattr(app, "_speaker_embed", _emb)
    import soundfile as sf

    def _separa(wav, dir_saida):
        dir_s = Path(dir_saida)
        dir_s.mkdir(exist_ok=True)
        a, b = dir_s / "a.wav", dir_s / "b.wav"
        sf.write(str(a), np.zeros(32000, dtype=np.float32), 16000, subtype="PCM_16")
        sf.write(str(b), np.full(32000, 0.05, dtype=np.float32), 16000,
                 subtype="PCM_16")
        return [a, b]

    monkeypatch.setattr(separa_voz, "separa", _separa)
    mix = Path(str(tmp_path) + "/mix.wav")
    sf.write(str(mix), np.zeros(32000, dtype=np.float32), 16000, subtype="PCM_16")
    escolhido, info = separa_voz.separa_alvo(mix, perfis["eu"]["vecs"],
                                             app.SPEAKER_MODELO_LEGADO)
    assert info["sobrepostas"] >= 1, f"não separou nenhuma janela: {info}"
    assert set(pedidos) == {app.SPEAKER_MODELO_LEGADO}, pedidos
    # o perfil casou (fluxo a) e o concorrente não (fluxo b) em toda janela
    assert all(j[1] > j[2] for j in info["escores_janelas"]), info["escores_janelas"]


def test_descartar_recadastro_parcial_mantem_o_legado(client, auth, monkeypatch):
    """Descartar o parcial não toca no que autentica (#534): 200, pendente some.

    Sem TTL de propósito — quem joga fora é o dono. `use` não pode (é escrita)."""
    _perfil_legado_com_ecapa(monkeypatch)
    client.post("/api/speaker/enroll", headers=auth, data={"name": "eu"},
                files={"audio": ("v.wav", b"x", "audio/wav")})
    assert STORE["eu"]["pendente"]["vecs"], "o parcial nem foi criado"
    r = client.delete("/api/speaker/eu/pendente", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["em_uso"] == app.SPEAKER_MODELO_LEGADO
    assert r.json()["samples"] == 2, "as amostras do legado não podem sair junto"
    assert "pendente" not in STORE["eu"]
    assert STORE["eu"]["modelo"] == app.SPEAKER_MODELO_LEGADO
    # sem parcial em andamento, 404 claro; perfil inexistente, 404
    assert client.delete("/api/speaker/eu/pendente", headers=auth).status_code == 404
    assert client.delete("/api/speaker/ninguem/pendente", headers=auth).status_code == 404
    # chave de uso não descarta (é escrita)
    segredo = app._apikeys["keys"][0]["secret"]
    client.post("/api/speaker/enroll", headers=auth, data={"name": "eu"},
                files={"audio": ("v.wav", b"x", "audio/wav")})
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    try:
        r = client.delete("/api/speaker/eu/pendente", headers={"X-API-Key": segredo})
        assert r.status_code == 403
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()


# --- contrato da limpeza de amostra (#523, decisão do PM no card) -------------
# Recusa é estado ESPERADO: 200 com motivo, original intacto. 4xx só para erro do
# cliente. DNSMOS sem pacote vem `null` + motivo, nunca 0.
def _voz_para_limpar(voices_dir):
    import soundfile as sf

    sr = 16000
    x = (np.random.default_rng(7).standard_normal(sr) * 0.02).astype(np.float32)
    sf.write(str(voices_dir / "limpa00001.wav"), x, sr, subtype="PCM_16")
    (voices_dir / "limpa00001.json").write_text(_json.dumps(
        {"id": "limpa00001", "name": "Limpa", "duration": 1.0}))
    return voices_dir / "limpa00001.wav"


def test_denoise_recusado_e_200_com_motivo_e_original_intacto(client, auth, voices_tmp,
                                                              monkeypatch):
    """Timbre abaixo do limiar: 200, `recusado`, `motivo` e arquivo não tocado."""
    wav = _voz_para_limpar(voices_tmp)
    antes = wav.read_bytes()
    monkeypatch.setitem(app._settings, "voice_limpar_motor", "gating")
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32) * 0.9)
    monkeypatch.setattr(app, "_guarda_timbre", lambda a, b, sr: 0.62)
    monkeypatch.setitem(app._settings, "voice_timbre_min", 0.80)
    r = client.post("/api/voices/limpa00001/denoise", headers=auth)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["ok"] is True and corpo["recusado"] is True
    assert corpo["motivo"] == "timbre"
    assert corpo["limpeza"]["recusado_por"] == "timbre"
    assert wav.read_bytes() == antes, "recusa não pode sobrescrever a amostra"


def test_denoise_sucesso_diz_que_o_dnsmos_nao_foi_medido(client, auth, voices_tmp,
                                                         monkeypatch):
    """Sem `speechmos`, os campos saem `null` e o motivo diz para escrever "não
    medido" — `null` sozinho a tela lê como nota zero."""
    _voz_para_limpar(voices_tmp)
    monkeypatch.setitem(app._settings, "voice_limpar_motor", "gating")
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32))
    monkeypatch.setattr(app, "_guarda_timbre", lambda a, b, sr: 0.99)
    # "indisponivel" é ausência de MODELO (#655): com os ONNX instalados, uma
    # falha de execução passa a sair como "erro" — o duble simula ausência.
    monkeypatch.setattr(app, "_dnsmos", lambda a, sr: None)
    monkeypatch.setattr(app, "_dnsmos_disponivel", lambda: False)
    corpo = client.post("/api/voices/limpa00001/denoise", headers=auth).json()
    assert corpo["recusado"] is False and corpo["ok"] is True
    assert corpo["limpeza"]["dnsmos_antes"] is None
    assert corpo["limpeza"]["dnsmos_depois"] is None
    assert corpo["limpeza"]["dnsmos_motivo"] == "indisponivel"
    # voz inexistente continua sendo erro do cliente (4xx)
    assert client.post("/api/voices/naoexiste/denoise", headers=auth).status_code == 404


def test_speaker_alvo_distingue_perfil_inexistente_de_perfil_vazio(client, auth,
                                                                   monkeypatch):
    """Perfil que existe e está sem amostra tem de mandar CADASTRAR, não dizer que
    não existe — a ação do dono é outra (P3 do gate)."""
    perfis = {"vazio": {"vecs": [], "modelo": app.SPEAKER_MODELO_LEGADO}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("f.wav"))
    r = client.post("/api/transcribe", headers=auth, data={"speaker_alvo": "vazio"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 400 and "sem amostras" in r.json()["detail"]
    r = client.post("/api/transcribe", headers=auth, data={"speaker_alvo": "naoexiste"},
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 400 and "não existe" in r.json()["detail"]


# --- marca d'água AudioSeal (#657, decisão do dono) ---------------------------
_marca_fake = {"chamadas": 0}


def _marca_stub(monkeypatch, ok=True):
    import marca_dagua

    _marca_fake["chamadas"] = 0
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: ok)
    monkeypatch.setattr(marca_dagua, "motivo", lambda: "" if ok else "pesos ausentes")
    monkeypatch.setattr(marca_dagua, "marca_wav",
                        lambda p: _marca_fake.__setitem__("chamadas",
                                                          _marca_fake["chamadas"] + 1) or p)


def _sess_live_fila():
    """Sessão mínima do Live com a FILA real: é o que o cliente recebe."""
    sess = {"id": "s-marca", "fila": app._LiveFila(), "engine": None,
            "audio_pendente": 0}
    return sess


def _pcm_na_fila(sess) -> list:
    """Drena a fila e devolve só o áudio — é o que o cliente receberia."""
    itens = []
    while not sess["fila"].empty():
        itens.append(sess["fila"].get())
    return [c[1] for c in itens if c[0] == "audio"]


def test_marca_dagua_marca_o_trecho_enviado_ao_cliente(monkeypatch):
    """O caminho do LIVE tem de marcar: é o PCM que sai em `_live_envia_audio`.

    A célula antiga chamava `_marca_pcm` direto, então apagar a chamada no
    `_live_envia_audio` deixava tudo verde (mordida que não morde, gate #657)."""
    _marca_stub(monkeypatch)
    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)
    pcm = b"\x00\x01" * 800
    sess = _sess_live_fila()
    app._live_envia_audio(sess, pcm)
    enviado = _pcm_na_fila(sess)
    assert _marca_fake["chamadas"] == 1, "o envio ao cliente não pediu a marca"
    assert enviado and len(enviado[0]) == len(pcm), "PCM tem de ir no mesmo tamanho"
    assert sess["audio_pendente"] == 1, "a contagem de áudio em voo mudou"
    # desligado: o cliente recebe o PCM ORIGINAL, byte a byte
    monkeypatch.setitem(app._settings, "tts_marca_dagua", False)
    sess2 = _sess_live_fila()
    app._live_envia_audio(sess2, pcm)
    assert _pcm_na_fila(sess2) == [pcm] and _marca_fake["chamadas"] == 1
    # sem pesos: o cliente recebe o original e o aviso sai uma vez
    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)
    _marca_stub(monkeypatch, ok=False)
    monkeypatch.setattr(app, "_marca_avisada", False)
    sess3 = _sess_live_fila()
    app._live_envia_audio(sess3, pcm)
    assert _pcm_na_fila(sess3) == [pcm] and app._marca_avisada is True


def test_marca_dagua_e_campo_ADMIN(monkeypatch):
    """Ligar/desligar muda o áudio de TODO mundo: só admin."""
    assert "tts_marca_dagua" in app._SETTINGS_ADMIN
    # LIGADO no default: o ABX do dono passou (só o par 7 audível de 8)
    assert app._SETTINGS_DEFAULTS["tts_marca_dagua"] is True, "o dono aprovou a marca"
    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)
    assert app._apply_settings({"tts_marca_dagua": "false"})["tts_marca_dagua"] is False
    assert app._apply_settings({"tts_marca_dagua": True})["tts_marca_dagua"] is True


# --- áudio do cadastro guardado (#658, decisão do dono) -----------------------
def _dir_audio(monkeypatch, tmp_path):
    import app as _app

    destino = tmp_path / "audio-cadastro"
    monkeypatch.setattr(_app, "SPEAKER_AUDIO_DIR", destino)
    return destino


def _enroll_uma(client, auth, monkeypatch, store, nome="eu"):
    """Uma amostra que passa na qualidade, com o upload dublado."""
    wav = Path(str(Path("/tmp")) + "/amostra-658.wav")   # não é lido de verdade
    monkeypatch.setattr(app, "_save_audio_upload",
                        lambda up, prefix=".voz": (wav.write_bytes(b"ri"), wav)[1])
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [1.0, 0.0])
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 5.0)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 30.0)
    monkeypatch.setattr(app, "_speaker_load", lambda: store)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)
    return client.post("/api/speaker/enroll", headers=auth, data={"name": nome},
                       files={"audio": ("a.wav", b"x", "audio/wav")})


def test_enroll_guarda_o_audio_em_0600(client, auth, monkeypatch, tmp_path):
    """A amostra aceita fica em disco, 0600, fora de voices/ e do export."""
    destino = _dir_audio(monkeypatch, tmp_path)
    monkeypatch.setitem(app._settings, "speaker_guardar_audio", True)
    store: dict = {}
    assert _enroll_uma(client, auth, monkeypatch, store).status_code == 200
    arquivos = list(destino.rglob("*.wav"))
    assert arquivos, "a amostra não foi guardada"
    assert arquivos[0].stat().st_mode & 0o777 == 0o600, "o áudio não está em 0600"
    # desligar a setting para de guardar
    monkeypatch.setitem(app._settings, "speaker_guardar_audio", False)
    antes = len(list(destino.rglob("*.wav")))
    assert _enroll_uma(client, auth, monkeypatch, store).status_code == 200
    assert len(list(destino.rglob("*.wav"))) == antes


def test_apagar_perfil_apaga_o_audio_guardado(client, auth, monkeypatch, tmp_path):
    destino = _dir_audio(monkeypatch, tmp_path)
    monkeypatch.setitem(app._settings, "speaker_guardar_audio", True)
    store: dict = {}
    _enroll_uma(client, auth, monkeypatch, store)
    assert list(destino.rglob("*.wav"))
    assert client.delete("/api/speaker/eu", headers=auth).status_code == 200
    assert not list(destino.rglob("*.wav")), "o áudio ficou órfão"


def test_troca_de_extrator_reextrai_do_audio_guardado(client, auth, monkeypatch,
                                                      tmp_path):
    """Com o áudio guardado o perfil se recadastra SOZINHO (sem regravar)."""
    destino = _dir_audio(monkeypatch, tmp_path)
    monkeypatch.setitem(app._settings, "speaker_guardar_audio", True)
    store: dict = {"eu": {"vecs": [[1.0, 0.0]] + [[0.0, 0.0]] * 0, "modelo": app.SPEAKER_MODELO_LEGADO,
                         "updated": 0}}
    # 3 amostras guardadas do extrator antigo, prontas para reextrair (pasta = id)
    store["eu"]["audio_id"] = "abc123"
    pasta = destino / "abc123"
    pasta.mkdir(parents=True)
    for i in range(3):
        (pasta / f"{i}.wav").write_bytes(b"ri")
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)
    monkeypatch.setattr(app, "_speaker_embed",
                        lambda p, modelo=None: [0.5, 0.5] + [0.0] * 254
                        if modelo == app.SPEAKER_MODELO_ATUAL else [0.5, 0.5])
    monkeypatch.setattr(app, "_speaker_load", lambda: store)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)
    # a reextração vai em SEGUNDO PLANO: o GET não espera por ela
    client.get("/api/speaker/profiles", headers=auth)
    for _ in range(100):
        if store["eu"].get("modelo") == app.SPEAKER_MODELO_ATUAL:
            break
        time.sleep(0.02)
    assert store["eu"]["modelo"] == app.SPEAKER_MODELO_ATUAL, store
    assert len(store["eu"]["vecs"]) == 3 and len(store["eu"]["vecs"][0]) == 256
    lista = client.get("/api/speaker/profiles", headers=auth).json()
    assert lista[0]["precisa_recadastrar"] is False


# --- VOZ-I3b (#613): o anti-clone VETA em `enforce` e avisa em `label` --------
def _perfil_para_anticlone(monkeypatch, prob):
    import marca_dagua

    perfis = {"eu": {"vecs": [[1.0, 0.0]], "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [1.0, 0.0])
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: prob is not None)
    monkeypatch.setattr(marca_dagua, "motivo", lambda: "sem pesos")
    if prob is not None:
        monkeypatch.setattr(marca_dagua, "detecta_prob", lambda p: prob)
    monkeypatch.setitem(app._settings, "speaker_anticlone", True)


def test_anticlone_veta_audio_marcado_em_enforce(monkeypatch):
    """Marca detectada = áudio gerado por TTS: barra em `enforce`, com o motivo."""
    _perfil_para_anticlone(monkeypatch, 0.98)
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out.get("rejected") is True
    assert "clone" in out["reason"] and "0.98" in out["reason"]
    # fala humana (cru) passa
    _perfil_para_anticlone(monkeypatch, 0.01)
    monkeypatch.setitem(app._settings, "speaker_anticlone", True)
    assert not app._speaker_gate_ok(Path("x.wav")).get("rejected")


def test_anticlone_avisa_em_label_e_nao_rejeita(monkeypatch):
    """Em `label` a etiqueta continua e o clone só vira aviso."""
    _perfil_para_anticlone(monkeypatch, 0.98)
    monkeypatch.setitem(app._settings, "speaker_gate", "label")
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out.get("speaker") == "eu" and not out.get("rejected")
    assert "CLONE" in out["speaker_aviso"]


def test_anticlone_sem_detector_nao_rejeita(monkeypatch):
    """Sem pesos, `enforce` não pode barrar todo mundo: fail open com motivo."""
    _perfil_para_anticlone(monkeypatch, None)
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out.get("anticlone") == "indisponivel" and not out.get("rejected")
    # e desligado não consulta nada
    monkeypatch.setitem(app._settings, "speaker_anticlone", False)
    assert "anticlone" not in app._speaker_gate_ok(Path("x.wav"))


def test_audio_nao_colide_entre_nomes_parecidos(client, auth, monkeypatch, tmp_path):
    """`Ana Maria`, `Ana_Maria` e `Ana/Maria` têm pastas próprias (#658).

    Slug de nome colidia nos três e o delete de um apagava o áudio do outro."""
    destino = _dir_audio(monkeypatch, tmp_path)
    monkeypatch.setitem(app._settings, "speaker_guardar_audio", True)
    store: dict = {}
    for nome in ("Ana Maria", "Ana_Maria", "Ana/Maria"):
        assert _enroll_uma(client, auth, monkeypatch, store, nome).status_code == 200
    pastas = [p for p in destino.iterdir() if p.is_dir()]
    assert len(pastas) == 3, [p.name for p in pastas]
    assert len({p.name for p in pastas}) == 3
    # apagar um não pode levar o áudio dos outros
    client.delete("/api/speaker/Ana_Maria", headers=auth)
    restantes = [p for p in destino.iterdir() if p.is_dir()]
    assert len(restantes) == 2


def test_reextracao_com_arquivo_corrompido_nao_derruba_a_lista(client, auth,
                                                               monkeypatch, tmp_path):
    """Um .wav quebrado é registrado e pulado; a lista continua respondendo."""
    destino = _dir_audio(monkeypatch, tmp_path)
    store: dict = {"eu": {"vecs": [[1.0, 0.0]], "modelo": app.SPEAKER_MODELO_LEGADO,
                          "audio_id": "x1", "updated": 0}}
    pasta = destino / "x1"
    pasta.mkdir(parents=True)
    for i in range(4):                     # 0 é o corrompido: se ele abortar, os
        (pasta / f"{i}.wav").write_bytes(b"nao-e-wav")   # outros NÃO são lidos
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)

    def _emb(p, modelo=None):
        if str(p).endswith("0.wav"):
            raise RuntimeError("wav corrompido")
        return [0.5, 0.5] + [0.0] * 254

    monkeypatch.setattr(app, "_speaker_embed", _emb)
    monkeypatch.setattr(app, "_speaker_load", lambda: store)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)
    assert client.get("/api/speaker/profiles", headers=auth).status_code == 200
    for _ in range(100):
        if store["eu"].get("modelo") == app.SPEAKER_MODELO_ATUAL:
            break
        time.sleep(0.02)
    # pulou o corrompido e seguiu: as 3 boas fecham o mínimo e promovem
    assert store["eu"]["modelo"] == app.SPEAKER_MODELO_ATUAL, store["eu"]
    assert len(store["eu"]["vecs"]) == 3
    assert client.get("/api/speaker/profiles", headers=auth).status_code == 200


def test_anticlone_nao_perde_o_motivo_de_voz_nao_autorizada(monkeypatch):
    """Quem já foi barrado por VOZ tem de continuar sabendo disso (#613).

    O "clone" sobrescrevia o motivo do escore, e o dono recebia "marca d'água"
    quando o problema era a voz não ser do perfil."""
    import marca_dagua

    perfis = {"eu": {"vecs": [[1.0, 0.0]], "modelo": app._speaker_modelo_efetivo()}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (perfis, ""))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [0.0, 1.0])  # outra voz
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "detecta_prob", lambda p: 0.99)   # e clone
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    monkeypatch.setitem(app._settings, "speaker_anticlone", True)
    out = app._speaker_gate_ok(Path("x.wav"))
    assert out.get("rejected") is True
    assert "voz não" in out["reason"], out["reason"]
    assert out["anticlone"] == "marcado"        # a informação não se perde
    assert "clone" in out.get("anticlone_motivo", "clone")


def test_transcribe_preque_ao_detector_com_anticlone_ligado(monkeypatch):
    """A 1ª detecção custa 4,6 s: com o anti-clone ligado a rota preaquece."""
    import marca_dagua

    aqueceu = []
    monkeypatch.setattr(marca_dagua, "prewarm", lambda: aqueceu.append(1))
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".stt": Path("f.wav"))
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None, allow_remote=True:
                        {"text": "oi", "language": "pt", "segments": []})
    cliente = TestClient(app.app, raise_server_exceptions=False)
    chave = app._primary_api_key()
    cab = {"X-API-Key": chave} if chave else {}
    monkeypatch.setitem(app._settings, "speaker_anticlone", True)
    cliente.post("/api/transcribe", headers=cab,
                 files={"audio": ("a.wav", b"x", "audio/wav")})
    assert aqueceu, "não preaqueceu com o anti-clone ligado"
    monkeypatch.setitem(app._settings, "speaker_anticlone", False)
    aqueceu.clear()
    cliente.post("/api/transcribe", headers=cab,
                 files={"audio": ("a.wav", b"x", "audio/wav")})
    assert aqueceu == []


def test_audio_do_esquema_de_slug_e_adotado(client, auth, monkeypatch, tmp_path):
    """Pasta do esquema ANTIGO (slug) é migrada em vez de ficar órfã (#658).

    Instalação que guardou áudio entre o `3dd6a70` e o `c8134e9` tem perfil sem
    `audio_id` e a pasta pelo nome; a reextração tem de achar o áudio."""
    destino = _dir_audio(monkeypatch, tmp_path)
    monkeypatch.setitem(app._settings, "speaker_guardar_audio", True)
    store: dict = {"Ana Maria": {"vecs": [[1.0, 0.0]], "modelo": app.SPEAKER_MODELO_LEGADO,
                                 "updated": 0}}
    antiga = destino / "Ana_Maria"                       # slug do nome, esquema velho
    antiga.mkdir(parents=True)
    for i in range(3):
        (antiga / f"{i}.wav").write_bytes(b"ri")
    monkeypatch.setattr(app, "_speaker_ecapa_ok", lambda: True)
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [0.5, 0.5] + [0.0] * 254)
    monkeypatch.setattr(app, "_speaker_load", lambda: store)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)
    client.get("/api/speaker/profiles", headers=auth)
    for _ in range(100):
        if store["Ana Maria"].get("modelo") == app.SPEAKER_MODELO_ATUAL:
            break
        time.sleep(0.02)
    assert store["Ana Maria"].get("audio_id"), "não ganhou id (migração)"
    assert not antiga.exists(), "a pasta antiga ficou órfã"
    assert len(store["Ana Maria"]["vecs"]) == 3, store["Ana Maria"]


def test_default_ligado_nao_reescreve_o_settings_do_dono(tmp_path):
    """Novo default NÃO pode regravar o `settings.json` que já existe (#593).

    Quem já tem arquivo recebe o default na RAM e o arquivo fica byte a byte como
    estava — se ele tivesse `"tts_marca_dagua": false` explícito, esse valor vence.
    """
    import subprocess

    base = Path(__file__).resolve().parent.parent
    arquivo = tmp_path / "settings.json"
    original = '{"speed": 1.15, "idle_unload_minutes": 3}'
    arquivo.write_text(original)
    env = {**os.environ, "TTS_SETTINGS_PATH": str(arquivo),
           "TTS_APIKEYS_PATH": str(tmp_path / "apikeys.json"),
           "TTS_LEGACY_APIKEY_PATH": str(tmp_path / "apikey"),
           "TTS_SPEAKER_PATH": str(tmp_path / "perfis.json"),
           "TTS_SPEAKER_AUDIO_DIR": str(tmp_path / "audio"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_ROD_API_KEY", "TTS_ADMIN_KEY"):
        env.pop(var, None)
    saida = subprocess.run([sys.executable, "-c",
                            "import app; print(app._settings['tts_marca_dagua'])"],
                           cwd=str(base), env=env, check=True,
                           capture_output=True, text=True)
    assert saida.stdout.strip() == "True", "o default novo não chegou na RAM"
    assert arquivo.read_text() == original, "o import reescreveu o settings do dono"
    # valor EXPLÍCITO no disco vence o default
    arquivo.write_text('{"tts_marca_dagua": false}')
    saida = subprocess.run([sys.executable, "-c",
                            "import app; print(app._settings['tts_marca_dagua'])"],
                           cwd=str(base), env=env, check=True,
                           capture_output=True, text=True)
    assert saida.stdout.strip() == "False", "o default sobrescreveu a escolha do dono"


# --- endpoints da UI do #528 (#690) ------------------------------------------
def test_denoise_previa_devolve_wav_sem_escrever(client, auth, voices_tmp, monkeypatch):
    """A prévia devolve o áudio limpo e NÃO toca na voz nem cria `.orig`."""
    wav = _voz_para_limpar(voices_tmp)
    antes = wav.read_bytes()
    monkeypatch.setitem(app._settings, "voice_limpar_motor", "gating")
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32))
    monkeypatch.setattr(app, "_guarda_timbre", lambda a, b, sr: 0.93)
    monkeypatch.setattr(app, "_dnsmos", lambda a, sr: {"ovrl": 3.1, "sig": 3.2,
                                                       "bak": 3.0, "p808": 3.0})
    r = client.post("/api/voices/limpa00001/denoise?previa=true", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    assert r.headers["X-Recusado"] == "0"
    assert r.headers["X-Timbre"] == "0.930"
    assert r.headers["X-Dnsmos-Antes"] == "3.10"
    assert wav.read_bytes() == antes, "a prévia escreveu na voz"
    assert not (voices_tmp / "limpa00001.orig.wav").exists(), "criou o .orig"
    # sem `previa` continua aplicando (contrato antigo intacto)
    corpo = client.post("/api/voices/limpa00001/denoise", headers=auth).json()
    assert corpo["ok"] is True and corpo["recusado"] is False
    assert wav.read_bytes() != antes and (voices_tmp / "limpa00001.orig.wav").exists()


def test_denoise_previa_recusada_devolve_o_original(client, auth, voices_tmp, monkeypatch):
    """Recusa na prévia: WAV do ORIGINAL e o motivo no header."""
    wav = _voz_para_limpar(voices_tmp)
    antes = wav.read_bytes()
    monkeypatch.setitem(app._settings, "voice_limpar_motor", "gating")
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32) * 0.9)
    monkeypatch.setattr(app, "_guarda_timbre", lambda a, b, sr: 0.5)
    monkeypatch.setitem(app._settings, "voice_timbre_min", 0.80)
    r = client.post("/api/voices/limpa00001/denoise?previa=true", headers=auth)
    assert r.status_code == 200 and r.headers["X-Recusado"] == "1"
    assert r.headers["X-Motivo"] == "timbre"
    assert r.content[:4] == b"RIFF"
    assert wav.read_bytes() == antes and not (voices_tmp / "limpa00001.orig.wav").exists()


def test_speaker_avalia_mede_sem_perfil(client, auth, monkeypatch):
    """O medidor responde as réguas e não toca em perfil nenhum."""
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".vozav": Path("f.wav"))
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 1.2)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 4.0)
    tocados = []
    monkeypatch.setattr(app, "_speaker_save", lambda p: tocados.append(1))
    r = client.post("/api/speaker/avalia", headers=auth,
                    files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["ok"] is False and corpo["fala_s"] == 1.2 and corpo["snr_db"] == 4.0
    assert len(corpo["problemas"]) == 2 and tocados == []
    # amostra boa passa
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 5.0)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 30.0)
    corpo = client.post("/api/speaker/avalia", headers=auth,
                        files={"audio": ("a.wav", b"x", "audio/wav")}).json()
    assert corpo["ok"] is True and corpo["problemas"] == []


def test_avalia_aceita_chave_de_uso(client, monkeypatch):
    """É uso, como o resto de `/api/speaker/*`: chave `use` alcança."""
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".vozav": Path("f.wav"))
    monkeypatch.setattr(app, "_vad_fala_s", lambda p: 4.0)
    monkeypatch.setattr(app, "_snr_estimado", lambda p: 30.0)
    segredo = app._apikeys["keys"][0]["secret"]
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    try:
        r = client.post("/api/speaker/avalia", headers={"X-API-Key": segredo},
                        files={"audio": ("a.wav", b"x", "audio/wav")})
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()
    assert r.status_code == 200


# --- `passo_ms` do filtro de comando (#577) -----------------------------------
# Está na whitelist com piso/teto e o handler o lê (`_live_suspeita_barge`); sem
# estas células, apagar a linha da whitelist passava com a suíte verde.
def _passo_gravado(client, auth, valor):
    r = client.post("/api/settings", headers=auth, json={
        "live_barge_filtros": {"palavras_corte": {"passo_ms": valor}}})
    assert r.status_code == 200, r.text
    return r.json()["live_barge_filtros"]["palavras_corte"].get("passo_ms")


def test_passo_ms_tem_piso_e_teto(client, auth):
    """`passo_ms` é clampado em 50–1000 como o handler espera."""
    try:
        assert _passo_gravado(client, auth, 9) == 50, "piso não aplicado"
        assert _passo_gravado(client, auth, 9999) == 1000, "teto não aplicado"
        assert _passo_gravado(client, auth, 300) == 300, "valor válido não passou"
    finally:
        client.post("/api/settings", headers=auth, json={"live_barge_filtros": {}})


def test_passo_ms_ausente_mantem_o_que_estava(client, auth):
    """Filtro sem `passo_ms` não inventa campo (o handler usa o default dele)."""
    try:
        client.post("/api/settings", headers=auth, json={
            "live_barge_filtros": {"palavras_corte": {"passo_ms": 400}}})
        r = client.post("/api/settings", headers=auth, json={
            "live_barge_filtros": {"palavras_corte": {"palavras": ["basta"]}}})
        assert r.status_code == 200, r.text
        assert "passo_ms" not in r.json()["live_barge_filtros"]["palavras_corte"]
    finally:
        client.post("/api/settings", headers=auth, json={"live_barge_filtros": {}})


def test_handler_le_o_passo_ms_do_settings(monkeypatch):
    """O elo com o HANDLER: `_live_suspeita_barge` tira o passo do settings.

    Sem esta célula a whitelist podia gravar um campo que ninguém lê."""
    passos = []

    class _Motor:
        config = type("C", (), {"barge_decisao_ms": 10})()

        def audio_suspeito(self):
            return b""

        def playback_restante_ms(self):
            return 5000

    class _Timer:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(app.threading, "Timer", _Timer)
    monkeypatch.setattr(app, "_live_transcreve_trecho", lambda s, a: "para")
    monkeypatch.setattr(app, "_live_decide_barge",
                        lambda s, a, fala, *resto, **kw: passos.append(1))
    # o Evento de parada dublado guarda a ESPERA pedida: é nela que o passo
    # aparece (0,05 s com `passo_ms: 50`; 0,15 s seria o default do handler)
    esperas = []

    _EventoReal = threading.Event

    class _Parar(_EventoReal):               # herda: o próprio Thread usa Event
        def wait(self, t=None):
            if t is None:
                return False
            esperas.append(round(t, 3))
            return t > 0                     # 0,0 na 1ª volta; depois encerra

    monkeypatch.setattr(app.threading, "Event", lambda: _Parar())
    monkeypatch.setitem(app._settings, "live_barge_filtros",
                        {"palavras_corte": {"passo_ms": 50}})
    sess = {"id": "s1", "engine": _Motor(), "barge_suspeito": {"audio": b""}}
    app._live_suspeita_barge(sess, type("E", (), {"audio": b"\x00\x00", "fala_ms": 300})())
    for _ in range(50):
        if 0.05 in esperas:
            break
        time.sleep(0.02)
    assert 0.05 in esperas, f"o handler não leu o passo do settings: {esperas}"


def test_denoise_troca_o_wav_de_forma_atomica(client, auth, voices_tmp, monkeypatch):
    """Quem lê durante o `denoise` vê o arquivo ANTIGO inteiro, não um pela metade.

    Risco 5 do gate #690: prévia (leitura) e aplicar (escrita) ao mesmo tempo. A
    escrita vai para um temporário e entra com `os.replace`; gravar por cima deixava
    o leitor com cabeçalho truncado."""
    import soundfile as _sf

    wav = _voz_para_limpar(voices_tmp)
    antes = wav.read_bytes()
    inode_antes = wav.stat().st_ino
    monkeypatch.setitem(app._settings, "voice_limpar_motor", "gating")
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32))
    monkeypatch.setattr(app, "_guarda_timbre", lambda a, b, sr: 0.95)
    vistas = []
    real = _sf.write

    def _espia(destino, *a, **k):
        # no instante em que a cadeia GRAVA (no temporário), alguém lê o FINAL
        if str(destino) != str(wav) and Path(str(destino)).suffix == ".wav":
            vistas.append(wav.read_bytes())
        return real(destino, *a, **k)

    monkeypatch.setattr(_sf, "write", _espia)
    assert client.post("/api/voices/limpa00001/denoise", headers=auth).status_code == 200
    assert vistas, "a cadeia não gravou no destino final"
    # a leitura concorrente tem de ser um WAV COMPLETO (o antigo, neste instante)
    assert vistas[0] == antes, "leitor concorrente viu arquivo parcial"
    # e o final é OUTRO inode: gravar por cima mantém o mesmo (trunca no lugar) e
    # é justamente o que deixa leitor concorrente com arquivo pela metade
    assert wav.stat().st_ino != inode_antes, "o `.wav` foi sobrescrito no lugar"
    assert _sf.read(str(wav))[0].size > 0


def test_live_ws_fechar_avisa_session_fechada_nao_ociosa(ws_client, live_limpo,
                                                         monkeypatch):
    """#628: task de envio morta por outro motivo NÃO é "ociosa".

    Antes `fechar` saía como `session_ttl` com o texto "sessão ociosa por Ns" e o
    cliente RETOMAVA — a coluna IA mentia e a retomada era de um caso que não é
    vencimento. Aqui a marca é posta SEM mexer no `visto`, então o único motivo
    possível é o `fechar`."""
    with ws_client.websocket_connect("/api/live/ws") as ws:
        sid = _setup_ok(ws)["session_id"]
        with app._live_lock:
            app._live_sessions[sid]["fechar"] = True   # visto RECENTE de propósito
        aviso = ws.receive_json()
        assert aviso["code"] == "session_fechada", aviso
        assert "ociosa" not in aviso["message"], aviso["message"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_temporarios_de_voices_nao_ficam_para_sempre(client, auth, voices_tmp,
                                                     monkeypatch):
    """Órfão do `denoise`/`.orig` em `voices/` é podado, e não vaza na listagem.

    O temporário fica em `voices/` de propósito (mesmo FS para o `os.replace`
    atômico do #690). Se o processo cair entre o write e o replace, ele sobra —
    sem esta poda ficaria para sempre e a listagem por `*.json` não o veria."""
    import os as _os

    _voz_para_limpar(voices_tmp)          # a voz de verdade, com o .json
    orfao = voices_tmp / ".tmp-tts-denoise-limpa00001-deadbeef.wav"
    orfao.write_bytes(b"RIFF....")
    recente = voices_tmp / ".tmp-tts-orig-limpa00001-cafebabe"
    recente.write_bytes(b"RIFF....")
    velho = time.time() - 7 * 3600
    _os.utime(orfao, (velho, velho))
    # pela entrada PÚBLICA: cobra também a fiação em `_auto_cleanup_once`
    assert app._auto_cleanup_once() >= 1, "a poda não está ligada na limpeza"
    assert not orfao.exists() and recente.exists(), "podou o recente ou deixou o velho"
    # e a listagem não os mostra como voz
    corpo = client.get("/api/voices", headers=auth).json()
    vozes = corpo if isinstance(corpo, list) else corpo["voices"]
    ids = [v["id"] for v in vozes]
    assert "limpa00001" in ids, ids
    assert not [i for i in ids if i.startswith(".")], ids


def test_denoise_temporarios_de_duas_chamadas_nao_colidem(client, auth, voices_tmp,
                                                         monkeypatch):
    """Duas aplicações usam temporários DISTINTOS para a mesma voz.

    Com nome fixo (`.tmp-tts-denoise-<id>.wav`) a segunda escrevia por cima do temporário
    da primeira e uma das trocas publicava arquivo pela metade."""
    import soundfile as _sf

    _voz_para_limpar(voices_tmp)
    monkeypatch.setitem(app._settings, "voice_limpar_motor", "gating")
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32))
    monkeypatch.setattr(app, "_guarda_timbre", lambda a, b, sr: 0.95)
    temporarios = []
    real = _sf.write

    def _espia(destino, *a, **k):
        nome = Path(str(destino)).name
        if nome.startswith(".tmp-tts-"):
            temporarios.append(nome)
        return real(destino, *a, **k)

    monkeypatch.setattr(_sf, "write", _espia)
    assert client.post("/api/voices/limpa00001/denoise", headers=auth).status_code == 200
    assert client.post("/api/voices/limpa00001/denoise", headers=auth).status_code == 200
    assert len(temporarios) == 2 and temporarios[0] != temporarios[1], temporarios
    assert not list(voices_tmp.glob(".tmp-tts-*")), "sobrou temporário"


def test_poda_de_voices_nao_casa_nada_que_nao_seja_temporario(voices_tmp):
    """O glob da poda é o ponto mais sensível: ele APAGA em `voices/`.

    Nada de voz, meta, `.orig` real do #443 nem nome de usuário com ponto pode
    casar. A lista é versionada aqui de propósito: apontar para o `voices/` do dono
    faria a célula depender da máquina (nem existe em worktree limpo)."""
    import glob as _glob

    prefixo = app._TMP_VOICES_PREFIX
    assert prefixo == ".tmp-tts-", "o prefixo mudou: revise o glob da poda"
    padrao = prefixo + "*"
    # nomes representativos: id simples, com ponto, o `.orig` do #443, meta, os
    # temporários do esquema ANTIGO e um arquivo qualquer do usuário
    intactos = ["abc.wav", "abc.json", "abc.orig.wav", "voz.com.ponto.wav",
                "vd-narrador.wav", "vd-narrador.json", "voz.da.familia.wav",
                ".orig-abc", ".denoise-abc", "nota.txt"]
    for nome in intactos:
        assert not _glob.fnmatch.fnmatch(nome, padrao), f"{nome} casaria a poda"
    temporarios = ["%sdenoise-abc-1234.wav" % prefixo, "%sorig-abc-1234" % prefixo]
    for nome in temporarios:
        assert _glob.fnmatch.fnmatch(nome, padrao), f"{nome} não casaria a poda"



def test_poda_nao_apaga_temporario_recente(voices_tmp):
    """Temporário em uso (mtime de agora) sobrevive à poda de 6 h."""
    novo = voices_tmp / (app._TMP_VOICES_PREFIX + "denoise-x-aaaa.wav")
    novo.write_bytes(b"RIFF....")
    app._limpa_temporarios_de_voices()
    assert novo.exists(), "a poda comeu um temporário recém-criado"


def test_orig_guardado_uma_vez_so_com_duas_aplicacoes(client, auth, voices_tmp,
                                                      monkeypatch):
    """O `.orig` é criado UMA vez e guarda o áudio de ANTES, mesmo em corrida."""
    wav = _voz_para_limpar(voices_tmp)
    original = wav.read_bytes()
    monkeypatch.setitem(app._settings, "voice_limpar_motor", "gating")
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32))
    monkeypatch.setattr(app, "_guarda_timbre", lambda a, b, sr: 0.95)
    assert client.post("/api/voices/limpa00001/denoise", headers=auth).status_code == 200
    assert client.post("/api/voices/limpa00001/denoise", headers=auth).status_code == 200
    orig = voices_tmp / "limpa00001.orig.wav"
    assert orig.read_bytes() == original, "o `.orig` não guardou o áudio de antes"


def test_orig_reserva_atomica_em_corrida(voices_tmp, monkeypatch):
    """Duas cópias concorrentes: o `.orig` fica com a PRIMEIRA, não com a última.

    `exists()` antes da cópia não basta — dois pedidos simultâneos veem `False`,
    os dois copiam e o último `replace` vence, guardando áudio já limpo. A reserva
    `O_EXCL` é o que resolve; aqui o `exists()` é forçado a mentir para simular a
    corrida."""
    wav = voices_tmp / "abc.wav"
    wav.write_bytes(b"ORIGINAL")
    orig = voices_tmp / "abc.orig.wav"
    real_exists = Path.exists

    def _exists_mentiroso(self):
        return False if self.name.endswith(".orig.wav") else real_exists(self)

    monkeypatch.setattr(Path, "exists", _exists_mentiroso)
    app._guarda_original("abc")            # 1ª: guarda ORIGINAL
    wav.write_bytes(b"JA-LIMPO")
    app._guarda_original("abc")            # 2ª (concorrente): não pode sobrescrever
    monkeypatch.setattr(Path, "exists", real_exists)
    assert orig.read_bytes() == b"ORIGINAL", orig.read_bytes()


def test_poda_em_disco_preserva_voz_meta_e_orig_e_leva_o_temp_velho(client, auth,
                                                                   voices_tmp):
    """Com fixture no disco: a poda APAGA só o temporário, e só o velho.

    O glob conferido por nome não basta — aqui os arquivos existem de verdade e
    passam pela poda. `<id>.wav`, `<id>.json` e o `<id>.orig.wav` do #443 têm de
    sobreviver; o temporário antigo some e o recente fica.

    Voz, meta e `.orig` nascem VELHOS (7 h) de propósito: recém-criados eles
    sobreviveriam por idade e um glob errado que os casasse passaria verde. Velhos,
    só sobrevivem se o nome estiver fora do padrão."""
    import os as _os

    wav = _voz_para_limpar(voices_tmp)
    orig = voices_tmp / "limpa00001.orig.wav"
    orig.write_bytes(b"RIFF....orig")
    velho = voices_tmp / (app._TMP_VOICES_PREFIX + "denoise-limpa00001-aaaa.wav")
    velho.write_bytes(b"RIFF....")
    recente = voices_tmp / (app._TMP_VOICES_PREFIX + "denoise-limpa00001-bbbb.wav")
    recente.write_bytes(b"RIFF....")
    quando = time.time() - 7 * 3600
    for arq in (velho, wav, voices_tmp / "limpa00001.json", orig):
        _os.utime(arq, (quando, quando))
    assert app._auto_cleanup_once() >= 1
    assert wav.exists() and (voices_tmp / "limpa00001.json").exists(), "comeu a voz"
    assert orig.exists(), "comeu o .orig real do #443"
    assert not velho.exists(), "o temporário velho ficou"
    assert recente.exists(), "comeu temporário em uso"


def test_apagar_voz_leva_os_temporarios_dela(client, auth, voices_tmp):
    """O temporário órfão da voz sai no DELETE — e não o de id com prefixo igual."""
    _voz_para_limpar(voices_tmp)
    meu = voices_tmp / (app._TMP_VOICES_PREFIX + "denoise-limpa00001-aaaa.wav")
    meu.write_bytes(b"RIFF....")
    outro = voices_tmp / (app._TMP_VOICES_PREFIX + "denoise-limpa00001x-bbbb.wav")
    outro.write_bytes(b"RIFF....")
    assert client.delete("/api/voices/limpa00001", headers=auth).status_code == 200
    assert not meu.exists(), "o temporário da voz ficou órfão"
    assert outro.exists(), "levou o temporário de uma voz de nome parecido"


# --- #721: estado do dono fora do repositório ---------------------------------
def test_state_dir_vence_por_ambiente(tmp_path):
    """`TTS_STATE_DIR` manda: é o que o conftest usa para não tocar o real."""
    import subprocess

    env = {**os.environ, "TTS_STATE_DIR": str(tmp_path / "estado"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_SETTINGS_PATH", "TTS_APIKEYS_PATH", "TTS_LEGACY_APIKEY_PATH",
                "TTS_SPEAKER_PATH"):
        env.pop(var, None)
    app_dir = Path(__file__).resolve().parent.parent
    saida = subprocess.run(
        [sys.executable, "-c", "import app; print(app.SETTINGS_PATH.parent)"],
        cwd=str(app_dir), env=env, check=True, capture_output=True, text=True)
    raiz_saida = Path(saida.stdout.strip().splitlines()[-1]).resolve()
    assert raiz_saida == (tmp_path / "estado").resolve(), saida.stdout
    assert raiz_saida != app_dir.resolve(), "estado continuou no repositório"


def test_estado_migra_por_copia_e_nunca_move(tmp_path):
    """1ª partida: COPIA raiz → STATE_DIR (0600/0700) e deixa o original."""
    raiz = tmp_path / "repo"
    destino = tmp_path / "estado"
    raiz.mkdir()
    for nome, conteudo in (("settings.json", '{"speed": 1.1}'), (".apikey", "chave\n"),
                           (".apikeys.json", '{"keys": []}'),
                           (".speaker-profiles.json", "{}")):
        (raiz / nome).write_text(conteudo)
    app._migra_estado_para_fora_do_repo(base=raiz, destino=destino)
    for nome, conteudo in (("settings.json", '{"speed": 1.1}'), (".apikey", "chave\n"),
                           (".apikeys.json", '{"keys": []}'),
                           (".speaker-profiles.json", "{}")):
        assert (destino / nome).is_file(), f"não copiou {nome}"
        assert (destino / nome).read_text() == conteudo, f"copiou errado {nome}"
        assert (raiz / nome).is_file(), f"APAGOU o original {nome}"   # nunca move
        assert (destino / nome).stat().st_mode & 0o777 == 0o600, f"modo de {nome}"


def test_estado_nao_sobrescreve_destino_existente(tmp_path):
    """Destino que já existe manda: a raiz não vence na 2ª partida."""
    raiz = tmp_path / "repo"
    destino = tmp_path / "estado"
    raiz.mkdir()
    destino.mkdir()
    (raiz / "settings.json").write_text('{"origem": "raiz"}')
    (destino / "settings.json").write_text('{"origem": "estado"}')
    app._migra_estado_para_fora_do_repo(base=raiz, destino=destino)
    assert "estado" in (destino / "settings.json").read_text()
    assert (raiz / "settings.json").exists()


def test_worktree_usa_estado_local_e_nunca_o_real(tmp_path):
    """Fora da raiz principal, o padrão é `<árvore>/.state-local` (#721).

    Sem isso, todo harness num worktree (live_ws, suítes .sh, uvicorn) escreveria no
    estado REAL do dono — o problema que o card fecha, por outra porta."""
    import subprocess

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_STATE_DIR", "TTS_SETTINGS_PATH", "TTS_APIKEYS_PATH",
                "TTS_LEGACY_APIKEY_PATH", "TTS_SPEAKER_PATH"):
        env.pop(var, None)
    app_dir = Path(__file__).resolve().parent.parent
    saida = subprocess.run(
        [sys.executable, "-c",
         "import app; print(app._arbore_e_a_principal(), app.STATE_DIR)"],
        cwd=str(app_dir), env=env, check=True, capture_output=True, text=True)
    # vale em qualquer árvore: worktree/cópia → `.state-local`; raiz principal →
    # o diretório de aplicativo (a célula do worktree FALSO cobre o caso estrito)
    principal, estado = saida.stdout.strip().split(maxsplit=1)
    if principal == "False":
        assert estado == str(app_dir / ".state-local"), estado
    else:
        assert str(app_dir) not in estado, estado


def test_copia_sem_git_usa_estado_local(tmp_path, monkeypatch):
    """Cópia da árvore SEM `.git` (é o que as suítes fazem em /tmp) usa local.

    Se caísse no Application Support, todo freeze/probe que sobe servidor de uma
    cópia escreveria no estado REAL do dono."""
    copia = tmp_path / "copia-da-arvore"
    copia.mkdir()
    monkeypatch.setattr(app, "BASE", copia)
    monkeypatch.delenv("TTS_STATE_DIR", raising=False)
    assert app._arbore_e_a_principal() is False
    assert app._dir_de_estado_padrao() == copia / ".state-local"


def test_worktree_falso_resolve_para_estado_local(tmp_path, monkeypatch):
    """Árvore com `.git` ARQUIVO (worktree) usa `.state-local`, nunca o real."""
    falsa = tmp_path / "wt-falso"
    falsa.mkdir()
    (falsa / ".git").write_text("gitdir: /qualquer/.git/worktrees/x\n")
    monkeypatch.setattr(app, "BASE", falsa)
    monkeypatch.delenv("TTS_STATE_DIR", raising=False)
    assert app._arbore_e_a_principal() is False
    assert app._dir_de_estado_padrao() == falsa / ".state-local"


def test_migracao_nao_copia_para_estado_local(tmp_path):
    """Worktree/cópia não migra: `.state-local` nasce do que a árvore tiver."""
    raiz = tmp_path / "repo"
    raiz.mkdir()
    (raiz / "settings.json").write_text('{"x": 1}')
    destino = raiz / ".state-local"
    app._migra_estado_para_fora_do_repo(base=raiz, destino=destino)
    assert not (destino / "settings.json").exists()


def test_migracao_nao_copia_metadados_do_original(tmp_path):
    """A cópia é só de CONTEÚDO: nada de metadados/flag do original.

    O original vai de propósito com modo 400 e mtime velho, sem chflags em lugar
    nenhum: se a migração preservasse metadados (`copy2`), o destino nasceria
    somente-leitura e o app não conseguiria salvar. O destino tem de sair 0600,
    GRAVÁVEL e com mtime NOVO."""
    raiz = tmp_path / "repo"
    destino = tmp_path / "estado"
    raiz.mkdir()
    origem = raiz / "settings.json"
    origem.write_text('{"x": 1}')
    origem.chmod(0o400)
    velho = time.time() - 3 * 3600
    os.utime(origem, (velho, velho))
    app._migra_estado_para_fora_do_repo(base=raiz, destino=destino)
    alvo = destino / "settings.json"
    assert alvo.read_text() == '{"x": 1}'
    assert alvo.stat().st_mode & 0o777 == 0o600, "modo do destino não é 0600"
    assert alvo.stat().st_mtime > velho + 60, "veio o mtime do original (metadado)"
    alvo.write_text('{"x": 2}')                       # gravável de verdade
    assert alvo.read_text() == '{"x": 2}'


def test_sessao_nao_e_semeada_com_o_settings_do_dono():
    """O estado da sessão é o de DEFAULTS, não uma cópia do dono (#721).

    Antes o conftest copiava `app.SETTINGS_PATH` para o tmp da sessão; quando esse
    caminho era o arquivo do dono, cada rodada deixava uma cópia com a chave
    secreta em /tmp (191 medidas). Agora o caminho é o `STATE_DIR` em tmp e o
    arquivo nasce dos defaults: `chat_api_key` vazio é a marca de "sem cópia"."""
    import app

    assert app.SETTINGS_PATH.is_absolute()
    assert str(app.BASE) not in str(app.SETTINGS_PATH), \
        f"o estado da sessão está na árvore: {app.SETTINGS_PATH}"
    assert not app._settings.get("chat_api_key"), \
        "os settings da sessão vieram do dono (têm chave secreta)"


def test_raiz_principal_usa_o_aplicativo(tmp_path, monkeypatch):
    """O outro lado da regra: `.git` DIRETÓRIO = raiz principal = Application Support."""
    raiz = tmp_path / "raiz"
    (raiz / ".git").mkdir(parents=True)
    monkeypatch.setattr(app, "BASE", raiz)
    monkeypatch.delenv("TTS_STATE_DIR", raising=False)
    assert app._arbore_e_a_principal() is True
    assert app._dir_de_estado_padrao() != raiz / ".state-local"


def test_rollback_do_restore_nao_devolve_arquivo_imutavel(tmp_path, monkeypatch):
    """Snapshot/restauração não podem viajar a FLAG de imutabilidade (#721).

    Modo 0400 não defende: `copy2` copia o modo, mas o `chmod` seguinte conserta e
    a célula passa. Com `chflags uchg` o `copy2` leva a flag, o `chmod` do destino
    dá EPERM e a escrita do app falha — é o caso real do settings do dono. A flag
    fica só nos arquivos desta célula (tmp) e sai no `finally`."""
    import stat as _stat

    import app as _app

    if not hasattr(os, "chflags"):
        pytest.skip("sem chflags (não-macOS)")

    vivo = tmp_path / "vivo"
    vivo.mkdir()
    monkeypatch.setattr(app, "SETTINGS_PATH", vivo / "settings.json")
    monkeypatch.setattr(app, "APIKEYS_PATH", vivo / ".apikeys.json")
    monkeypatch.setattr(app, "SPEAKER_PATH", vivo / ".speaker-profiles.json")
    monkeypatch.setattr(app, "VOICES_DIR", tmp_path / "sem-voices")
    alvo = vivo / "settings.json"
    alvo.write_text('{"x": 1}')
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    copia = rollback / "settings.json"
    os.chflags(alvo, _stat.UF_IMMUTABLE)
    try:
        _app._snapshot_estado(rollback)
        assert copia.read_text() == '{"x": 1}'
        assert not copia.stat().st_flags & _stat.UF_IMMUTABLE, \
            "a flag viajou para o snapshot"
        assert copia.stat().st_mode & 0o777 == 0o600, "snapshot não saiu 0600"
        os.chflags(alvo, 0)               # equivale ao app poder salvar de novo
        alvo.write_text('{"x": 2}')
        os.chflags(alvo, _stat.UF_IMMUTABLE)
        _app._restaura_estado(rollback)
        assert alvo.read_text() == '{"x": 1}'
        assert not alvo.stat().st_flags & _stat.UF_IMMUTABLE, \
            "a restauração devolveu o arquivo imutável"
        assert alvo.stat().st_mode & 0o777 == 0o600, "restauração não deixou 0600"
        alvo.write_text('{"x": 3}')       # gravável de verdade, sem tirar flag
        assert alvo.read_text() == '{"x": 3}'
    finally:
        for arq in (alvo, copia):
            if arq.exists():
                os.chflags(arq, 0)


def test_import_nao_cai_com_diretorio_de_estado_sem_permissao(tmp_path):
    """Pai somente-leitura: o import avisa e segue em vez de explodir (#721).

    Sem os `try`, um `TTS_STATE_DIR` dentro de diretório sem escrita derrubava o
    app no import."""
    import subprocess

    pai = tmp_path / "pai"
    pai.mkdir()
    pai.chmod(0o500)
    env = {**os.environ, "TTS_STATE_DIR": str(pai / "estado"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_SETTINGS_PATH", "TTS_APIKEYS_PATH", "TTS_LEGACY_APIKEY_PATH",
                "TTS_SPEAKER_PATH"):
        env.pop(var, None)
    app_dir = Path(__file__).resolve().parent.parent
    try:
        saida = subprocess.run([sys.executable, "-c", "import app; print('ok')"],
                               cwd=str(app_dir), env=env, capture_output=True, text=True)
    finally:
        pai.chmod(0o700)
    assert saida.returncode == 0, saida.stderr[-1500:]
    assert saida.stdout.strip().endswith("ok")
    # o aviso vai para STDERR (stdout do import fica só com o que a sonda imprime)
    assert "não criei o pai" in saida.stderr and "estado" in saida.stderr


def test_log_da_migracao_vai_para_stderr(tmp_path, capsys):
    """O log da migração é LOG: stderr, com o stdout do import limpo (#744).

    Sondas leem o stdout de `import app` como lista de caminhos; um `[estado] …` ali
    vira caminho inválido. Chamada DIRETA (sem subprocesso com `TTS_STATE_DIR`
    vazio, que copiaria o settings do dono para o tmp): estado sintético em tmp."""
    raiz = tmp_path / "repo"
    destino = tmp_path / "estado"
    raiz.mkdir()
    (raiz / "settings.json").write_text('{"sintetico": true}')
    app._migra_estado_para_fora_do_repo(base=raiz, destino=destino)
    saida = capsys.readouterr()
    assert saida.out == "", f"o log vazou para o stdout: {saida.out!r}"
    assert "[estado] copiado settings.json" in saida.err, saida.err
    assert (destino / "settings.json").is_file()      # e a cópia aconteceu


# --- #777: prefixo duplo não pode furar a auth -------------------------------
_PREFIXOS_DO_PROXY = ("", "/ttsproxy", "/ttsproxy/ttsproxy", "//", "/ttsproxy//ttsproxy",
                      "/ttsproxy/./", "/ttsproxy/%2E")


def _rotas_http():
    """Toda rota HTTP que o app declara (o alvo do teste paramétrico)."""
    from starlette.routing import Mount, Route

    vistas = set()
    for rota in app.app.routes:
        if isinstance(rota, Mount) or not isinstance(rota, Route):
            continue
        caminho = getattr(rota, "path", "")
        metodos = getattr(rota, "methods", None) or set()
        if not caminho or caminho in vistas:
            continue
        vistas.add(caminho)
        yield caminho, metodos


def test_lan_sem_chave_nao_passa_por_prefixo_nenhum(monkeypatch, auth_freio_folgado):
    """LAN sem chave: nenhum prefixo do proxy pode devolver 2xx/3xx (#777).

    Percorre `app.routes`: para cada rota HTTP, todo prefixo tem de dar 401/403/404/
    405 — nunca 200 nem redirect. Antes o prefixo duplo saía como `/ttsproxy/api/…`
    e escapava da lista de protegidos."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    cliente = TestClient(app.app, raise_server_exceptions=False,
                         client=("203.0.113.99", 50000))
    publicas = app._CAMINHOS_PUBLICOS
    escaparam = []
    for caminho, metodos in _rotas_http():
        if caminho in publicas:
            continue
        for prefixo in _PREFIXOS_DO_PROXY:
            for metodo in sorted(metodos or {"GET"}):
                if metodo in ("HEAD", "OPTIONS"):
                    continue
                alvo = f"{prefixo}{caminho}" or "/"
                r = cliente.request(metodo, alvo)
                if r.status_code not in (401, 403, 404, 405):
                    escaparam.append((metodo, alvo, r.status_code))
    assert not escaparam, f"passaram sem chave: {escaparam[:15]}"


def test_loopback_e_lan_com_chave_seguem_funcionando_com_e_sem_prefixo(monkeypatch, auth_freio_folgado):
    """A correção não pode quebrar cliente legítimo: loopback e chave válida."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    chave = app._primary_api_key()
    local = TestClient(app.app, raise_server_exceptions=False,
                       client=("127.0.0.1", 50000))
    de_fora = TestClient(app.app, raise_server_exceptions=False,
                         client=("203.0.113.99", 50000))
    for prefixo in ("", "/ttsproxy"):
        assert local.get(f"{prefixo}/api/status").status_code == 200, prefixo
        assert local.get(f"{prefixo}/health").status_code == 200, prefixo
        r = de_fora.get(f"{prefixo}/api/status", headers={"X-API-Key": chave})
        assert r.status_code == 200, (prefixo, r.status_code)


def test_publicos_continuam_abertos_na_lan(monkeypatch):
    """A UI e o health não podem exigir chave: é o que o navegador da LAN busca."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    cliente = TestClient(app.app, raise_server_exceptions=False,
                         client=("203.0.113.99", 50000))
    for caminho in ("/", "/health", "/live-worklet.js"):
        for prefixo in ("", "/ttsproxy"):
            assert cliente.get(f"{prefixo}{caminho}").status_code == 200, (prefixo, caminho)


def test_caminho_roteado_normaliza_antes_de_decidir():
    """A decisão não pode depender da FORMA do path (#777)."""
    from starlette.requests import Request

    def roteado(caminho):
        escopo = {"type": "http", "path": caminho, "method": "GET", "headers": []}
        return app._caminho_roteado(Request(escopo))

    # o prefixo do proxy sai UMA vez; o resto normaliza para o mesmo path
    for cru in ("/api/status", "//api//status", "/./api/status", "/api/status/",
                "/ttsproxy/api/status", "/ttsproxy//api/status", "/ttsproxy/./api/status"):
        assert roteado(cru).rstrip("/") == "/api/status", cru
    # prefixo DUPLO: sobra um `/ttsproxy` — é o path que o roteador também vê
    for cru in ("/ttsproxy/ttsproxy/api/status", "/ttsproxy//ttsproxy//api/status"):
        assert roteado(cru).rstrip("/") == "/ttsproxy/api/status", cru


def test_prefixo_duplo_vira_404_e_nao_rota_servida(monkeypatch):
    """O prefixo é removido UMA vez (#777): o duplo sobra e não casa a rota.

    Sem chave dá 401 (a auth decide antes); COM a chave o roteador mostra o
    contrato: 404. Se o middleware voltar a reescrever o `scope["path"]`, o
    Starlette tira o `root_path` de novo e o duplo passa a casar (200)."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    chave = app._primary_api_key()
    fora = TestClient(app.app, raise_server_exceptions=False,
                      client=("203.0.113.99", 50000))
    assert fora.get("/ttsproxy/ttsproxy/api/status").status_code == 401
    r = fora.get("/ttsproxy/ttsproxy/api/status", headers={"X-API-Key": chave})
    assert r.status_code == 404, r.status_code
    # e o caminho normalizado (prefixo uma vez) segue servindo
    assert fora.get("/ttsproxy/api/status", headers={"X-API-Key": chave}).status_code == 200


# --- #779: DNS rebinding — Host fora da allowlist não fala com a API ---------
def _cliente_loopback_host(host, monkeypatch):
    """Socket loopback (é o que o rebinding usa) com um `Host` escolhido."""
    from starlette.testclient import TestClient as _TC

    class _ComHost(_TC):
        def request(self, method, url, **kw):
            cab = dict(kw.pop("headers", None) or {})
            cab.setdefault("host", host)
            return super().request(method, url, headers=cab, **kw)

    return _ComHost(app.app, raise_server_exceptions=False,
                    client=("127.0.0.1", 50000))


def test_dns_rebinding_host_desconhecido_nao_herda_isenção(monkeypatch):
    """`evil.tld:7860` no loopback não lê segredo nem cria chave (#779)."""
    c = _cliente_loopback_host("evil.tld:7860", monkeypatch)
    for cab in ({}, {"Origin": "http://evil.tld:7860"}):
        assert c.get("/api/apikeys?reveal=1", headers=cab).status_code == 421, cab
        assert c.post("/api/apikeys", headers=cab,
                      json={"name": "invasor"}).status_code == 421, cab
        assert c.post("/api/shutdown", headers=cab).status_code == 421, cab
        assert c.get("/api/status", headers=cab).status_code == 421, cab
    # síntese: 421 (nem chega a validar corpo)
    assert c.post("/v1/audio/speech", json={}).status_code == 421


def test_hosts_da_maquina_seguem_passando(monkeypatch):
    """Os nomes da própria máquina e os configurados continuam servindo."""
    for host in ("127.0.0.1:7860", "localhost:7860", "[::1]:7860", "testserver"):
        c = _cliente_loopback_host(host, monkeypatch)
        assert c.get("/api/status").status_code == 200, host
    monkeypatch.setattr(app, "_ALLOWED_HOSTS", ("tts.the-dudes.com",))
    c = _cliente_loopback_host("tts.the-dudes.com", monkeypatch)
    assert c.get("/api/status").status_code == 200
    # e o `.local` da máquina também (Bonjour)
    assert app._host_permitido("mac-do-dono.local")


def test_allowlist_de_host_vem_do_ambiente(monkeypatch):
    monkeypatch.setenv("TTS_ALLOWED_HOSTS", "a.exemplo, B.exemplo:8443")
    import importlib

    recarregado = importlib.reload(app)
    try:
        assert "a.exemplo" in recarregado._ALLOWED_HOSTS
        assert "b.exemplo" in recarregado._ALLOWED_HOSTS
        assert recarregado._host_permitido("a.exemplo")
        assert not recarregado._host_permitido("evil.tld")
    finally:
        monkeypatch.delenv("TTS_ALLOWED_HOSTS", raising=False)
        importlib.reload(app)


# --- #780: origem estrangeira não lê a API sem chave -------------------------
_EVIL = "https://evil.example"


def _gets_de_api():
    from starlette.routing import Mount, Route

    for rota in app.app.routes:
        if isinstance(rota, Mount) or not isinstance(rota, Route):
            continue
        caminho = getattr(rota, "path", "")
        metodos = getattr(rota, "methods", set()) or set()
        if caminho.startswith(("/api/", "/v1/")) and "GET" in metodos:
            yield caminho


def test_get_de_origem_estrangeira_exige_chave_e_nao_ecoa_cors(monkeypatch, auth_freio_folgado):
    """Página de terceiro no loopback não lê nada sem chave, e sem `ACAO` (#780).

    O `ACAO: *` era o que deixava a página LER lista de vozes, saídas e a amostra
    da voz do dono. Sem chave: 401/403 e nenhum `access-control-allow-origin`."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("127.0.0.1", 50000))
    escaparam = []
    for caminho in _gets_de_api():
        alvo = caminho.replace("{", "").replace("}", "")
        r = c.get(alvo, headers={"Origin": _EVIL})
        if r.status_code not in (401, 403, 404, 405):
            escaparam.append((alvo, r.status_code))
        if r.headers.get("access-control-allow-origin"):
            escaparam.append((alvo, "ACAO"))
    assert not escaparam, f"vazaram sem chave: {escaparam[:10]}"


def test_sem_origin_ou_same_origin_segue_livre(monkeypatch):
    """curl/SDK/bookmark (sem `Origin`) e a própria UI não pagam por isso."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("127.0.0.1", 50000))
    assert c.get("/api/status").status_code == 200
    # same-origin é o `Host` da requisição (`testserver` no TestClient): passa sem
    # `ACAO` (o navegador não precisa de CORS)
    r = c.get("/api/status", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert not r.headers.get("access-control-allow-origin")   # same-origin não usa


def test_origem_listada_em_cors_le_com_chave(monkeypatch):
    """Quem o dono autorizou em `TTS_CORS_ORIGINS` continua lendo (com chave)."""
    chave = app._primary_api_key()
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("127.0.0.1", 50000))
    origem = app._ORIGENS_PERMITIDAS[0]
    r = c.get("/api/status", headers={"Origin": origem, "X-API-Key": chave})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origem


# --- #781: WS não herda a isenção do loopback com Origin de fora -------------
_EVIL_WS = "http://evil.tld"


def test_ws_origem_estrangeira_no_loopback_exige_credencial(tickets_limpos,
                                                             monkeypatch):
    """Site de terceiro não abre sessão Live só por estar no loopback (#781).

    O navegador não aplica CORS a WebSocket: sem esta checagem, qualquer página
    aberta no Mac fala com a voz do dono e ocupa o teto de sessões."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    c = _ws_cliente("127.0.0.1")
    with c.websocket_connect("/api/live/ws", headers={"Origin": _EVIL_WS}) as ws:
        aviso = ws.receive_json()
        assert aviso["code"] == "unauthorized", aviso
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403, exc.value.code
    assert app._origens_barradas, "a recusa não foi contabilizada"


def test_ws_origem_estrangeira_com_ticket_abre(tickets_limpos, monkeypatch):
    """Com ticket válido a página de fora abre normalmente."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    t = app._live_ticket_emite()
    c = _ws_cliente("127.0.0.1")
    with c.websocket_connect(f"/api/live/ws?ticket={t}",
                            headers={"Origin": _EVIL_WS}) as ws:
        assert _setup_ok(ws)["type"] == "ready"


def test_ws_sem_origin_e_same_origin_seguem_abertos(ws_client, monkeypatch):
    """Cliente não-browser (sem `Origin`) e a própria UI não pagam por isso."""
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws)["type"] == "ready"
    with ws_client.websocket_connect(
            "/api/live/ws", headers={"Origin": "http://testserver"}) as ws:
        assert _setup_ok(ws)["type"] == "ready"


# --- #796: deliver/abort NÃO podem esperar a fila segurando o `_gen_lock` -----
def _jobs_isolados_falsos(monkeypatch, tmp_path, atrasos):
    """`Popen` falso: cada job escreve `done` no status depois do seu atraso."""
    import json as _json
    import subprocess as _sub

    class _PopenFalso:
        def __init__(self, argv, **kw):
            self.pid = 4242
            self.returncode = None
            cfg = _json.loads(Path(argv[2]).read_text())
            self._status = Path(cfg["status_path"])
            self._atraso = atrasos.pop(0)
            threading.Thread(target=self._roda, daemon=True).start()

        def _roda(self):
            time.sleep(self._atraso)
            self._status.write_text(_json.dumps(
                {"status": "done", "pieces": 1, "total": 1,
                 "output": {"id": "abc", "duration": 0.5}}))
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(_sub, "Popen", _PopenFalso)
    monkeypatch.setattr(app, "_unload_local_tts", lambda: None)
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(app, "_schedule_job_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(app, "_jobs_hist_salva", lambda: None)
    monkeypatch.setitem(app._settings, "speech_queue", True)
    monkeypatch.setitem(app._settings, "speech_queue_gap_s", 0.0)
    monkeypatch.setattr(app, "_SPEECH_FILA_ESPERA_S", 30.0)


def _job_falso(job_id):
    app._jobs[job_id] = {"id": job_id, "text": "oi", "status": "running",
                         "pieces": 0, "total": 1}
    return app._speech_queue_begin(job_id)


def test_fila_nao_trava_quando_ticket_maior_pega_o_lock_antes(monkeypatch, tmp_path):
    """Ticket 1 não pode esperar a fila SEGURANDO o `_gen_lock` (#796).

    Ordem que trava o código antigo: o ticket 1 pega o lock e chama `deliver`, que
    espera o ticket 0 — dono do lock, ainda gerando. Ninguém solta nada."""
    _jobs_isolados_falsos(monkeypatch, tmp_path, atrasos=[0.6, 0.05])
    be = {"id": "omnivoice", "family": "omnivoice", "meta": {"label": "Omni"}}
    t0 = _job_falso("j0")
    t1 = _job_falso("j1")
    erros = []

    def _roda(job_id, token):
        try:
            app._run_tts_job_isolated(job_id, "oi", "voz-x", tmp_path / "v.wav",
                                      "pt", {}, be, sq=token)
        except Exception as exc:  # noqa: BLE001
            erros.append(exc)

    # o de ticket MAIOR primeiro: é ele que pega o lock
    th1 = threading.Thread(target=_roda, args=("j1", t1), daemon=True)
    th1.start()
    time.sleep(0.1)
    th0 = threading.Thread(target=_roda, args=("j0", t0), daemon=True)
    th0.start()
    for th in (th0, th1):
        th.join(timeout=10)
        assert not th.is_alive(), "a fila travou (deadlock do #796)"
    assert not erros, erros
    assert app._jobs["j0"]["status"] == "done" and app._jobs["j1"]["status"] == "done"


def test_fila_pula_ticket_de_job_que_morreu(monkeypatch, tmp_path):
    """Ticket órfão (job fora de running/queued) não segura a fila."""
    _jobs_isolados_falsos(monkeypatch, tmp_path, atrasos=[0.05])
    be = {"id": "omnivoice", "family": "omnivoice", "meta": {"label": "Omni"}}
    _job_falso("morto")
    app._jobs["morto"]["status"] = "error"        # dono do ticket 0 sumiu
    token = _job_falso("vivo")
    app._run_tts_job_isolated("vivo", "oi", "voz-x", tmp_path / "v.wav", "pt", {},
                              be, sq=token)
    assert app._jobs["vivo"]["status"] == "done", app._jobs["vivo"].get("error")


# --- #798: fechar uma sessão Live não pode congelar as outras ----------------
@pytest.fixture()
def servidor_vivo():
    """Sobe o app num uvicorn de verdade (porta livre) — o loop é o de produção."""
    import socket as _socket

    import uvicorn

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    cfg = uvicorn.Config(app.app, host="127.0.0.1", port=porta, log_level="error",
                         lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    limite = time.time() + 15
    while time.time() < limite and not srv.started:
        time.sleep(0.02)
    assert srv.started, "o servidor de teste não subiu"
    try:
        yield f"127.0.0.1:{porta}"
    finally:
        srv.should_exit = True
        th.join(timeout=10)


def _ws_setup(url, headers=None):
    import json as _json

    import websockets.sync.client as _wsc

    ws = _wsc.connect(url, additional_headers=headers or {})
    ws.send(_json.dumps({"type": "setup", "vad": {}, "voz": "vd-narrador"}))
    pronto = _json.loads(ws.recv())
    assert pronto.get("type") == "ready", pronto
    return ws


def test_fechar_sessao_lenta_nao_congela_o_health(servidor_vivo, monkeypatch):
    """`close()` lento não pode segurar o event loop (#798).

    Dublê o `close` do pipe para demorar 3 s; durante o fechamento, `/health` tem de
    responder em milissegundos. No código antigo (close síncrono no `finally`) o
    health só voltava depois do dublê."""
    import httpx

    libera = threading.Event()

    class _PipeLento:
        def __init__(self):
            self.fechado = False

        def close(self):
            self.fechado = True
            libera.wait(10)          # segura o fechamento até o teste mandar

    lento = _PipeLento()
    # o prewarm real trocaria o `pipe` dublado: ele vira o próprio pipe da sessão
    monkeypatch.setattr(app, "_live_pipe_start",
                        lambda sess: sess.__setitem__("pipe", lento))
    url = f"ws://{servidor_vivo}/api/live/ws"
    ws = _ws_setup(url)
    sess = list(app._live_sessions.values())[0]
    t0 = time.monotonic()
    ws.close()                        # dispara o `finally` do handler
    while not lento.fechado and time.monotonic() - t0 < 8:
        time.sleep(0.02)
    assert lento.fechado, (f"o fechamento nem começou (fechar={sess.get('fechar')}, "
                           f"no_registro={sess in app._live_sessions.values()})")
    # o loop tem de estar LIVRE com o close ainda pendurado
    r = httpx.get(f"http://{servidor_vivo}/health", timeout=2.0)
    demora = time.monotonic() - t0
    libera.set()
    assert r.status_code == 200
    assert demora < 2.0, f"o health esperou o fechamento ({demora:.2f}s)"


# --- #813: extensão só vale por ORIGEM EXATA na lista ------------------------
_EXT = "chrome-extension://abcdefghijklmnop"          # a que o ambiente lista
_EXT_FORA = "chrome-extension://zzzzzzzzzzzzzzzz"     # nunca listada


def test_extensao_listada_le_com_acao_dela(monkeypatch):
    """Extensão autorizada lê o reveal E recebe `ACAO` da própria origem (#813).

    O defeito era o par: auth dizia "da casa" (200) e o CORS não a listava, então o
    navegador bloqueava a leitura. O middleware copia a lista na criação, então a
    célula checa também que a origem está no `allow_origins` montado."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    assert _EXT in app._ORIGENS_PERMITIDAS, "a variável de ambiente não foi lida"
    c = _cliente_local()
    r = c.get("/api/apikeys?reveal=1", headers={"Origin": _EXT})
    assert r.status_code == 200
    assert any(k.get("secret") for k in r.json()["keys"])
    assert r.headers.get("access-control-allow-origin") == _EXT


def test_extensao_fora_da_lista_nao_le(monkeypatch):
    """Extensão NÃO listada é origem estrangeira: 403 e sem `ACAO`."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    c = _cliente_local()
    r = c.get("/api/apikeys?reveal=1", headers={"Origin": _EXT_FORA})
    assert r.status_code == 403, r.status_code
    assert not r.headers.get("access-control-allow-origin")
    # a resposta devolve a origem para o dono COPIAR no hint/variável
    assert _EXT_FORA in _json.dumps(r.json())


def test_extensao_por_origem_exata_nao_por_prefixo(monkeypatch):
    """`chrome-extension://` sozinho não basta; e o CORS inclui a lista."""
    assert app._origem_confiavel("chrome-extension://qualquer-um", _req_falso()) is False
    monkeypatch.setattr(app, "_ORIGENS_PERMITIDAS",
                        tuple(app._ORIGENS_PERMITIDAS) + (_EXT,))
    assert app._origem_confiavel(_EXT, _req_falso()) is True
    assert app._origem_confiavel(_EXT + "/", _req_falso()) is True      # normaliza
    assert app._origem_confiavel(_EXT + "x", _req_falso()) is False     # exato


def test_ws_herda_a_allowlist_de_extensao(tickets_limpos, monkeypatch):
    """O WS usa a mesma decisão: extensão listada abre, não listada leva 4403."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    c = _ws_cliente("127.0.0.1")
    with c.websocket_connect("/api/live/ws", headers={"Origin": _EXT_FORA}) as ws:
        aviso = ws.receive_json()
        assert aviso["code"] == "unauthorized", aviso
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403
    c2 = _ws_cliente("127.0.0.1")
    with c2.websocket_connect("/api/live/ws", headers={"Origin": _EXT}) as ws:
        assert _setup_ok(ws)["type"] == "ready"


def _req_falso(chave=None, host="127.0.0.1:7860"):
    from starlette.requests import Request

    cab = [(b"host", host.encode())]
    if chave:
        cab.append((b"x-api-key", chave.encode()))
    return Request({"type": "http", "path": "/api/x", "method": "GET",
                    "headers": cab, "scheme": "http"})


def test_porta_local_fora_da_lista_nao_le(monkeypatch):
    """`http://localhost:<porta>` só vale se a PORTA estiver na lista (#789).

    Antes, qualquer porta de loopback era "da casa" — um servidor qualquer na 3999
    lia o `reveal` com as chaves em claro."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    c = _cliente_local()
    r = c.get("/api/apikeys?reveal=1", headers={"Origin": "http://localhost:3999"})
    assert r.status_code == 403, r.status_code
    assert not r.headers.get("access-control-allow-origin")
    # listada (a 5198 é o default do cliente de dev) → lê e recebe o ACAO dela
    ok = c.get("/api/apikeys?reveal=1", headers={"Origin": "http://localhost:5198"})
    assert ok.status_code == 200
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5198"
    assert any(k.get("secret") for k in ok.json()["keys"])


def test_loopback_mesma_porta_do_servidor_passa(monkeypatch):
    """Loopback na MESMA porta do servidor é a própria UI (#789).

    `127.0.0.1:7860` chamado por `localhost:7860` é a mesma máquina e o mesmo
    serviço — passar só por igualdade de string era sorte de grafia."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    from starlette.requests import Request

    def req(host):
        return Request({"type": "http", "path": "/api/x", "method": "GET",
                        "headers": [(b"host", host.encode())], "scheme": "http"})

    assert app._origem_confiavel("http://127.0.0.1:7860", req("localhost:7860")) is True
    assert app._origem_confiavel("http://localhost:7860", req("127.0.0.1:7860")) is True
    # porta diferente, mesmo host: outro serviço, não passa
    assert app._origem_confiavel("http://localhost:3999", req("localhost:7860")) is False
    # host de fora, mesma porta: também não
    assert app._origem_confiavel("http://evil.tld:7860", req("localhost:7860")) is False


# --- #790: teto de corpo ANTES do spool --------------------------------------
def test_corpo_acima_do_teto_e_recusado_antes_de_espolpar(monkeypatch):
    """413 pelo `Content-Length`, sem o formparser tocar o disco (#790).

    O parser espolava o corpo inteiro e só então a rota pedia admin/tamanho: 80 MB
    anônimos viravam arquivo para terminar em 403."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("203.0.113.99", 50000))
    teto = app._TETO_CORPO_PADRAO
    r = c.post("/api/voices", headers={"Content-Length": str(teto + 1),
                                       "Content-Type": "multipart/form-data; boundary=x"},
               content=b"")
    assert r.status_code == 413, r.status_code
    assert "teto" in r.json()["detail"].lower()
    # JSON tem teto menor (8 MB): 9 MB já não cabe
    r = c.post("/api/settings", headers={"Content-Length": str(9 * 1024 * 1024),
                                         "Content-Type": "application/json"},
               content=b"")
    assert r.status_code == 413
    # rota de zip grande aceita o que o teto padrão recusaria
    r = c.post("/api/restore", headers={"Content-Length": str(200 * 1024 * 1024),
                                        "Content-Type": "multipart/form-data; boundary=x"},
               content=b"")
    assert r.status_code != 413, r.status_code


def test_use_em_rota_admin_nao_le_o_corpo(monkeypatch):
    """Chave `use` em `/api/restore` dá 403 antes de qualquer leitura (#790)."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("203.0.113.99", 50000))
    chave = app._primary_api_key()
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    try:
        r = c.post("/api/restore", headers={"X-API-Key": chave,
                                            "Content-Length": str(80 * 1024 * 1024),
                                            "Content-Type": "multipart/form-data; boundary=x"},
                   content=b"")
        assert r.status_code == 403, r.status_code
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()


def test_youtube_audio_entra_no_teto_pesado():
    assert app._rate_limit_for("/api/youtube-audio") == app._RATE_HEAVY


def test_pool_de_fechamento_cobre_o_teto_de_sessoes(monkeypatch):
    """Uma vaga por sessão: item que ainda não começou seria cancelado calado (#798).

    `wait_for` cancela o future e o `_WorkItem.run` do CPython desiste antes de
    rodar quando já está cancelado — com pool menor que o teto, fechamentos somem."""
    assert app._LIVE_FECHAMENTO_POOL._max_workers >= app._LIVE_MAX_SESSIONS


def test_prazo_estourado_avisa_no_stderr(servidor_vivo, monkeypatch, capsys):
    """Estourar o prazo tem de aparecer: senão o órfão não fica em lugar nenhum."""
    import httpx

    libera = threading.Event()

    class _PipeTravado:
        def close(self):
            libera.wait(30)

    lento = _PipeTravado()
    monkeypatch.setattr(app, "_live_pipe_start",
                        lambda sess: sess.__setitem__("pipe", lento))
    monkeypatch.setattr(app, "_LIVE_FECHAMENTO_TIMEOUT_S", 0.2)
    ws = _ws_setup(f"ws://{servidor_vivo}/api/live/ws")
    ws.close()
    limite = time.time() + 5
    avisou = False
    while time.time() < limite:
        if "passou de" in capsys.readouterr().err:
            avisou = True
            break
        time.sleep(0.05)
    libera.set()
    # o health volta a responder mesmo com o close pendurado
    assert httpx.get(f"http://{servidor_vivo}/health", timeout=2.0).status_code == 200
    assert avisou, "o prazo estourou sem aviso no stderr"


def test_origem_com_porta_invalida_nao_vira_500(monkeypatch):
    """Porta inválida no `Origin` cai para "não confiável", não derruba (#789)."""
    monkeypatch.setattr(app, "_origens_barradas", {})
    c = _cliente_local()
    for origem in ("http://localhost:99999", "http://localhost:abc",
                   "http://[::1]:abc"):
        r = c.get("/api/apikeys?reveal=1", headers={"Origin": origem})
        assert r.status_code == 403, (origem, r.status_code)
    assert app._origem_confiavel("http://localhost:99999", _req_falso()) is False
    assert app._origem_confiavel("http://localhost:abc", _req_falso()) is False


def _multipart_chunked(n_bytes, campo="audio"):
    """Multipart em pedaços, SEM `Content-Length` (httpx manda chunked)."""
    yield b'--X\r\nContent-Disposition: form-data; name="name"\r\n\r\nvoz\r\n'
    yield (f'--X\r\nContent-Disposition: form-data; name="{campo}"; '
           f'filename="a.wav"\r\n\r\n').encode()
    resto = n_bytes
    while resto > 0:
        bloco = min(1024 * 1024, resto)
        yield b"\x00\x01" * (bloco // 2)
        resto -= bloco
    yield b"\r\n--X--\r\n"


def test_corpo_sem_content_length_acima_do_teto_da_413(monkeypatch):
    """Sem `Content-Length` a recusa tem de ser 413, não corpo truncado (#790).

    Antes o stream era cortado e a rota rodava com o corpo pela metade: o parser
    devolvia 400 ("áudio obrigatório") e ninguém sabia do teto."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("203.0.113.99", 50000))
    r = c.post("/api/voices", headers={"Content-Type": "multipart/form-data; boundary=X"},
               content=_multipart_chunked(app._TETO_CORPO_PADRAO + 4 * 1024 * 1024))
    assert r.status_code == 413, (r.status_code, r.text[:120])
    # e abaixo do teto o corpo chega inteiro na rota (400 é o áudio fake inválido,
    # não "áudio obrigatório" — que seria campo faltando por truncamento)
    chave = app._primary_api_key()
    r = c.post("/api/voices", headers={"Content-Type": "multipart/form-data; boundary=X",
                                       "X-API-Key": chave},
               content=_multipart_chunked(64 * 1024))
    assert r.status_code == 400 and "obrigatório" not in r.text, r.text[:120]


def test_teto_grande_nao_e_maior_que_o_limite_interno():
    """O teto do middleware não pode ser decorativo: `restore` recusa antes (#790)."""
    assert app._TETO_CORPO_GRANDE <= app._IMPORT_MAX_TOTAL


def test_fixture_dsh_fecha_cliente_deixado_no_pool(monkeypatch):
    """Trocar de teste não pode abandonar o processo do `fake_acp` (#688/#790).

    `dsh_limpo` zerava a lista sem fechar: cada cliente vazado ficava vivo até o fim
    da sessão (6 medidos, 30+ min)."""
    fechados = []

    class _Cli:
        def close(self):
            fechados.append(1)

    class _Quebrado:
        def close(self):
            raise RuntimeError("já morto")

    monkeypatch.setattr(app, "_chat_dsh_livres", [_Cli(), _Quebrado(), _Cli()])
    _dsh_fecha_pool()
    # fecha os dois vivos e NÃO derruba a suíte no que já estava morto
    assert len(fechados) == 2


def test_quatro_fechamentos_simultaneos_chamam_os_quatro_closes(servidor_vivo,
                                                               monkeypatch):
    """4 sessões fechando JUNTAS: os 4 `close` têm de ser CHAMADOS (#798).

    Pool menor que o teto perde fechamento: `wait_for` cancela o future e o
    `_WorkItem.run` do CPython desiste antes de rodar. A célula do health não via
    isso — aqui se conta as CHAMADAS."""
    import httpx

    chamados = []
    trava = threading.Event()

    class _PipeRegistrador:
        def __init__(self, n):
            self.n = n

        def close(self):
            chamados.append(self.n)
            trava.wait(10)          # segura a vaga: é o cenário de processo pendurado

    monkeypatch.setattr(app, "_LIVE_FECHAMENTO_TIMEOUT_S", 5.0)
    sessoes = []
    for i in range(app._LIVE_MAX_SESSIONS):
        pipe = _PipeRegistrador(i)
        monkeypatch.setattr(app, "_live_pipe_start",
                            lambda sess, _p=pipe: sess.__setitem__("pipe", _p))
        ws = _ws_setup(f"ws://{servidor_vivo}/api/live/ws")
        sessoes.append(ws)
        # o pipe entra por uma thread do prewarm: espera o dublê aparecer
        limite_pipe = time.time() + 5
        while time.time() < limite_pipe:
            if any(p.get("pipe") is pipe for p in app._live_sessions.values()):
                break
            time.sleep(0.02)
        assert any(p.get("pipe") is pipe for p in app._live_sessions.values()), i
    for ws in sessoes:
        ws.close()
    limite = time.time() + 8
    while len(chamados) < len(sessoes) and time.time() < limite:
        time.sleep(0.02)
    trava.set()
    assert len(chamados) == len(sessoes), (
        f"só {len(chamados)} de {len(sessoes)} closes rodaram")
    assert httpx.get(f"http://{servidor_vivo}/health", timeout=2.0).status_code == 200


# --- #785: chave fraca e freio de força bruta --------------------------------
def test_chave_de_ambiente_curta_avisa_e_nao_vira_admin(capsys):
    """`TTS_ROD_API_KEY=ab` autentica, mas NÃO administra (#785).

    Descartá-la abriria a LAN: `_auth_enabled()` ficaria falso. Fraca vira `use`."""
    assert app._chave_de_ambiente("TTS_ROD_API_KEY", "ab") == "ab"
    assert "NÃO vale como admin" in capsys.readouterr().err


def test_chave_de_ambiente_longa_autentica():
    forte = "k" * 40
    assert app._chave_de_ambiente("TTS_ROD_API_KEY", forte) == forte
    assert app._chave_de_ambiente("TTS_ROD_API_KEY", "") == ""


def test_chave_curta_no_ambiente_nao_autentica(monkeypatch):
    """O fio completo: ambiente curto → `_ENV_API_KEY` vazio → não autentica."""
    monkeypatch.setenv("TTS_ROD_API_KEY", "ab")
    import importlib

    recarregado = importlib.reload(app)
    try:
        # continua valendo (senão a LAN cai para aberta) e a auth segue ligada
        assert recarregado._ENV_API_KEY == "ab"
        assert recarregado._key_is_valid("ab") is True
        assert recarregado._auth_enabled() is True
        assert recarregado._ENV_CHAVE_FRACA is True
    finally:
        monkeypatch.delenv("TTS_ROD_API_KEY", raising=False)
        importlib.reload(app)


def test_forca_bruta_toma_429_depois_do_teto(monkeypatch):
    """N falhas do MESMO IP → 429 com `Retry-After` (#785).

    A identidade é o IP de origem: com a chave como identidade cada chute abria um
    balde novo e o 429 nunca vinha."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    monkeypatch.setattr(app, "_AUTH_FALHAS_MAX", 5)
    app._auth_falhas.clear()
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("203.0.113.99", 50000))
    for _ in range(5):
        r = c.get("/api/status", headers={"X-API-Key": "chute"})
        assert r.status_code == 401, r.status_code
    r = c.get("/api/status", headers={"X-API-Key": "chute"})
    assert r.status_code == 429, r.status_code
    assert int(r.headers["retry-after"]) >= 1
    # outro IP não herdou o freio
    outro = TestClient(app.app, raise_server_exceptions=False,
                       client=("203.0.113.98", 50000))
    assert outro.get("/api/status", headers={"X-API-Key": "chute"}).status_code == 401
    app._auth_falhas.clear()


def test_chave_fraca_autentica_mas_nao_administra(monkeypatch):
    """Ponta a ponta: chave fraca de ambiente dá 200 e toma 403 no admin (#785).

    Fechar o furo da chave fraca abrindo a LAN seria pior: ela continua valendo."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    monkeypatch.setattr(app, "_ENV_API_KEY", "ab")
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "")
    monkeypatch.setattr(app, "_ENV_CHAVE_FRACA", True)
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("203.0.113.99", 50000))
    assert c.get("/api/status", headers={"X-API-Key": "ab"}).status_code == 200
    assert c.post("/api/shutdown", headers={"X-API-Key": "ab"}).status_code == 403
    # e com a flag desligada a MESMA chave administra (é a força que decide)
    monkeypatch.setattr(app, "_ENV_CHAVE_FRACA", False)
    assert c.post("/api/shutdown", headers={"X-API-Key": "ab"}).status_code == 200


# --- #786: identidade é admin; `use` não envenena o perfil do dono -----------
_IDENTIDADE = ("speaker_gate", "speaker_threshold", "speaker_far_alvo",
               "speaker_anticlone", "live_so_minha_voz")


def test_campos_de_identidade_sao_admin():
    for campo in _IDENTIDADE:
        assert campo in app._SETTINGS_ADMIN, campo


def test_post_settings_com_identidade_volta_em_admin_ignored(monkeypatch):
    """Chave `use` mexendo em identidade tem de sair em `admin_ignored` (#786)."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("203.0.113.99", 50000))
    chave = app._primary_api_key()
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    try:
        r = c.post("/api/settings", headers={"X-API-Key": chave},
                   json={"speaker_gate": "off", "speaker_threshold": 0.35,
                         "speaker_anticlone": False})
        assert r.status_code == 200, r.text
        assert set(r.json().get("admin_ignored") or []) >= set(_IDENTIDADE) - {
            "speaker_far_alvo", "live_so_minha_voz"}, r.json().get("admin_ignored")
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()


def test_use_nao_sobrescreve_perfil_existente(client, auth, monkeypatch):
    """`use` cadastrando num perfil EXISTENTE toma 403 e não toca nos vetores."""
    import hashlib

    perfis = {"Dono": {"vecs": [[1.0, 0.0]] * 3, "modelo": app.SPEAKER_MODELO_LEGADO,
                       "updated": 0}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_save", lambda p: None)
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, prefix=".voz": Path("f.wav"))
    monkeypatch.setattr(app, "_speaker_avalia",
                        lambda p, v: ([[9.0, 9.0]], []))
    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: [9.0, 9.0])
    antes = hashlib.sha256(_json.dumps(perfis, sort_keys=True).encode()).hexdigest()
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    try:
        r = client.post("/api/speaker/enroll?forcar=true", headers=auth,
                        data={"name": "Dono"},
                        files={"audio": ("a.wav", b"x", "audio/wav")})
        assert r.status_code == 403, r.status_code
        depois = hashlib.sha256(_json.dumps(perfis, sort_keys=True).encode()).hexdigest()
        assert depois == antes, "o perfil mudou mesmo com 403"
        # perfil NOVO continua livre para `use`
        r = client.post("/api/speaker/enroll", headers=auth, data={"name": "Novo"},
                        files={"audio": ("a.wav", b"x", "audio/wav")})
        assert r.status_code == 200, r.text
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()


def test_admin_key_curta_nao_e_demovida(monkeypatch):
    """Assimetria deliberada (#785): só a chave de USO fraca vira `use`.

    Demover também a `TTS_ROD_ADMIN_KEY` deixaria o admin inalcançável pela LAN
    quando ela fosse a única chave."""
    monkeypatch.setattr(app, "_own_ips", lambda: {"203.0.113.10"})
    monkeypatch.setattr(app, "_ENV_API_KEY", "")
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "ab")
    monkeypatch.setattr(app, "_ENV_CHAVE_FRACA", True)
    c = TestClient(app.app, raise_server_exceptions=False,
                   client=("203.0.113.99", 50000))
    assert c.get("/api/status", headers={"X-API-Key": "ab"}).status_code == 200
    assert c.post("/api/shutdown", headers={"X-API-Key": "ab"}).status_code == 200


def test_recusa_do_enroll_vem_antes_de_qualquer_escrita(client, auth, monkeypatch):
    """O 403 não pode tocar no disco: nem upload, nem `pendente`, nem `vecs` (#786).

    A guarda fica ANTES de `_save_audio_upload` e de qualquer `_speaker_save`; aqui
    se conta as duas coisas para o teste não depender só do sha."""
    salvos, uploads = [], []
    perfis = {"Dono": {"vecs": [[1.0, 0.0]] * 3, "modelo": app.SPEAKER_MODELO_LEGADO,
                       "updated": 0}}
    monkeypatch.setattr(app, "_speaker_load", lambda: perfis)
    monkeypatch.setattr(app, "_speaker_save", lambda p: salvos.append(1))
    monkeypatch.setattr(app, "_save_audio_upload",
                        lambda up, prefix=".voz": (uploads.append(1), Path("f.wav"))[1])
    app._apikeys["keys"][0]["role"] = "use"
    app._apikeys_publica()
    try:
        for forcar in ("", "?forcar=true"):
            r = client.post(f"/api/speaker/enroll{forcar}", headers=auth,
                            data={"name": "Dono"},
                            files={"audio": ("a.wav", b"x", "audio/wav")})
            assert r.status_code == 403, (forcar, r.status_code)
        assert salvos == [] and uploads == [], (salvos, uploads)
    finally:
        app._apikeys["keys"][0]["role"] = "admin"
        app._apikeys_publica()


def _post_cru(host, caminho, cabecalhos, corpo_blocos, limite=60):
    """POST HTTP/1.1 por socket CRU — httpx não reproduz o uvicorn no chunked."""
    import socket as _socket

    host_, porta = host.split(":")
    linhas = [f"POST {caminho} HTTP/1.1", f"Host: {host_}"]
    linhas += [f"{k}: {v}" for k, v in cabecalhos.items()]
    linhas += ["Connection: close", "", ""]
    sk = _socket.create_connection((host_, int(porta)), timeout=limite)
    sk.sendall("\r\n".join(linhas).encode())
    dados = b""
    try:
        for bloco in corpo_blocos:
            sk.sendall(bloco)
    except OSError:
        pass                        # servidor pode fechar no meio: é o esperado
    sk.settimeout(limite)
    try:
        while True:
            pedaco = sk.recv(65536)
            if not pedaco:
                break
            dados += pedaco
    except OSError:
        pass
    finally:
        sk.close()
    return dados.split(b"\r\n")[0].decode(errors="replace")


def _multipart_cru(n_bytes, chunked=True):
    """Corpo multipart em blocos; com `chunked`, cada bloco vai com o tamanho."""
    def _e(dados):
        return b"%x\r\n" % len(dados) + dados + b"\r\n" if chunked else dados

    yield _e(b'--X\r\nContent-Disposition: form-data; name="name"\r\n\r\nvoz\r\n')
    yield _e(b'--X\r\nContent-Disposition: form-data; name="audio"; filename="a.wav"'
             b'\r\n\r\n')
    resto = n_bytes
    bloco = b"\x00\x01" * (1024 * 1024 // 2)
    while resto > 0:
        yield _e(bloco)
        resto -= len(bloco)
    yield _e(b"\r\n--X--\r\n")
    if chunked:
        yield b"0\r\n\r\n"


def test_chunked_acima_do_teto_da_413_em_uvicorn_real(servidor_vivo):
    """Corpo CHUNKED acima do teto -> 413 (não 422), medido no uvicorn (#790).

    `TestClient` com gerador não reproduz o uvicorn: por isso socket cru."""
    linha = _post_cru(servidor_vivo, "/api/voices",
                      {"Content-Type": "multipart/form-data; boundary=X",
                       "Transfer-Encoding": "chunked"},
                      _multipart_cru(app._TETO_CORPO_PADRAO + 4 * 1024 * 1024))
    assert "413" in linha, linha


def test_content_length_mentindo_para_menos_da_4xx(servidor_vivo):
    """Header mentindo para menos: h11 delimita o corpo por ele, então o excedente
    vira lixo de protocolo — o contrato é 4xx (nunca 2xx), hoje 400."""
    linha = _post_cru(servidor_vivo, "/api/voices",
                      {"Content-Type": "multipart/form-data; boundary=X",
                       "Content-Length": "1000"},
                      _multipart_cru(4 * 1024 * 1024, chunked=False))
    assert " 4" in linha or "400" in linha, linha


# --- #787: `model` livre por pedido é privilégio de admin --------------------
def _sem_job(monkeypatch):
    chamadas = []
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **k: chamadas.append(a))
    return chamadas


def test_use_com_model_fora_do_catalogo_da_403(client, chave_de_uso, monkeypatch):
    """String livre vira `snapshot_download` de repo arbitrário (#787)."""
    chamadas = _sem_job(monkeypatch)
    for livre in ("atacante/voxcpm-malicioso", "/tmp/modelo-local"):
        r = client.post("/api/tts", headers=chave_de_uso,
                        json={"text": "oi", "model": livre})
        assert r.status_code == 403, (livre, r.status_code, r.text[:120])
        assert "catálogo" in r.text
    assert chamadas == [], "criou job com modelo fora do catálogo"


def test_use_com_id_do_catalogo_passa(client, chave_de_uso, monkeypatch):
    _sem_job(monkeypatch)
    for ok in ("kokoro", "OMNIVOICE"):          # id e caixa alta (normalizada)
        r = client.post("/api/tts", headers=chave_de_uso,
                        json={"text": "oi", "model": ok})
        assert r.status_code == 200, (ok, r.text[:120])


def test_admin_segue_livre_para_modelo_arbitrario(client, auth, monkeypatch):
    """Admin mantém a liberdade: é ele que instala modelo."""
    _sem_job(monkeypatch)
    r = client.post("/api/tts", headers=auth,
                    json={"text": "oi", "model": "atacante/qualquer"})
    assert r.status_code == 200, r.text[:120]


# --- #784: SSRF no /api/tunnel/status ---------------------------------------
def _listener_local():
    """Listener HTTP em 127.0.0.1 que conta os hits e responde `{"ok": true}`."""
    import http.server
    import socketserver
    import threading as _th

    hits = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            corpo = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


def test_use_com_url_na_query_da_403_e_nao_bate_no_destino(client, chave_de_uso):
    srv, hits = _listener_local()
    try:
        porta = srv.server_address[1]
        r = client.get(f"/api/tunnel/status?url=http://127.0.0.1:{porta}",
                       headers=chave_de_uso)
        assert r.status_code == 403, r.status_code
        assert hits == [], f"bateu no destino com chave use: {hits}"
    finally:
        srv.shutdown()


def test_admin_com_destino_privado_da_400(client, auth):
    srv, hits = _listener_local()
    try:
        porta = srv.server_address[1]
        r = client.get(f"/api/tunnel/status?url=http://127.0.0.1:{porta}", headers=auth)
        assert r.status_code == 400, r.text[:120]
        assert "interno" in r.text
        # o 400 diz COMO reativar a checagem (#784)
        assert "public_url" in r.text
        assert hits == [], "seguiu para destino interno"
    finally:
        srv.shutdown()


def test_admin_com_destino_publico_bate_e_devolve_ok(client, auth, monkeypatch):
    """Destino público de mentira: a checagem roda e o resultado volta."""
    vistos = []
    monkeypatch.setattr(app, "_public_proxy_check",
                        lambda u, timeout=6.0: (vistos.append(u), {"ok": True})[1])
    r = client.get("/api/tunnel/status?url=https://exemplo.trycloudflare.com",
                   headers=auth)
    assert r.status_code == 200, r.text[:120]
    assert vistos == ["https://exemplo.trycloudflare.com"]
    assert r.json()["public_check"]["ok"] is True


def test_sem_url_usa_a_configurada(client, auth, monkeypatch):
    vistos = []
    monkeypatch.setattr(app, "_public_proxy_check",
                        lambda u, timeout=6.0: (vistos.append(u), {"ok": True})[1])
    monkeypatch.setitem(app._settings, "public_url", "https://fixo.exemplo")
    assert client.get("/api/tunnel/status", headers=auth).status_code == 200
    assert vistos == ["https://fixo.exemplo"]


def test_host_privado_reconhece_as_faixas():
    for privado in ("127.0.0.1", "::1", "10.1.2.3", "192.168.0.1", "172.16.5.4",
                    "169.254.169.254", "100.64.0.1", "localhost", "mac.local"):
        assert app._host_privado(privado) is True, privado
    for publico in ("8.8.8.8", "abc.trycloudflare.com", "meu.dominio.com.br"):
        assert app._host_privado(publico) is False, publico


def test_host_privado_ipv6_e_formas_de_url():
    """IPv6 (colchetes inclusive), `user@host` e esquema não-HTTP (#784)."""
    for privado in ("::1", "[::1]", "fc00::1", "[fc00::1]", "fe80::1", "[fe80::1]",
                    "::ffff:127.0.0.1"):
        assert app._host_privado(privado) is True, privado
    assert app._public_proxy_check("ftp://exemplo.com")["ok"] is False
    # `user@host:porta` — o `hostname` do urlsplit é o que conta
    from urllib.parse import urlsplit

    assert app._host_privado(urlsplit("http://dono:segredo@127.0.0.1:9").hostname) is True
    assert app._host_privado(urlsplit("http://dono:segredo@8.8.8.8:9").hostname) is False
    # IPv6 na URL, colchetes e porta
    assert app._host_privado(urlsplit("http://[::1]:7860").hostname) is True


def test_redirect_nao_e_seguido():
    """Host público que redireciona para interno não pode furar a checagem.

    Follow desligado: o `ok` fica falso (3xx) em vez de virar 200 do destino."""
    import threading as _th
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Redir(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/health")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Redir)
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 127.0.0.1 é privado: usa-se o caminho direto pela função, que não valida
        r = app._public_proxy_check(f"http://127.0.0.1:{srv.server_address[1]}")
        assert r["privado"] is True
        # e o handler de redirect está instalado no opener
        import urllib.request

        assert any(isinstance(h, app._SemRedirect) for h in
                   urllib.request.build_opener(app._SemRedirect()).handlers)
    finally:
        srv.shutdown()


# --- #799: trabalho bloqueante fora do event loop ----------------------------
def _health_durante(dispara, limite=5.0):
    """Roda `dispara()` numa thread e mede a latência de `/health` no meio."""
    import httpx
    import threading as _th

    pronto = _th.Event()

    def _roda():
        pronto.set()
        dispara()

    t = _th.Thread(target=_roda)
    t.start()
    pronto.wait(2)
    time.sleep(0.15)                 # deixa o pedido entrar no servidor
    t0 = time.monotonic()
    try:
        r = httpx.get(f"http://{_health_durante.host}/health", timeout=limite)
        assert r.status_code == 200
    finally:
        decorrido = time.monotonic() - t0
        t.join(limite)
    return decorrido


_health_durante.host = "127.0.0.1:7860"


def test_speaker_check_nao_segura_o_loop(servidor_vivo, auth, monkeypatch):
    """Extrator de 2 s não pode congelar o servidor (#799)."""
    import httpx

    _health_durante.host = servidor_vivo

    def _lento(*a, **k):
        time.sleep(2.0)
        return None, 0.9, True, "cru"

    monkeypatch.setattr(app, "_speaker_pontua", _lento)
    monkeypatch.setattr(app, "_save_audio_upload", lambda up, p=".vozchk": Path("f.wav"))

    def _pede():
        r = httpx.post(f"http://{servidor_vivo}/api/speaker/check", headers=auth,
                       files={"audio": ("a.wav", b"x" * 64, "audio/wav")}, timeout=10)
        assert r.status_code in (200, 400), r.text[:120]

    latencia = _health_durante(_pede)
    assert latencia < 1.0, f"/health levou {latencia:.2f}s com o extrator ocupado"


def test_import_nao_segura_o_loop(servidor_vivo, auth, monkeypatch):
    """Import grande roda na thread: `/health` responde durante (#799)."""
    import httpx

    _health_durante.host = servidor_vivo

    def _lento(caminho):
        time.sleep(2.0)
        return {"ok": True, "importados": [], "ignorados": [], "renomeados": []}

    monkeypatch.setattr(app, "_importa_vozes_zip", _lento)

    def _pede():
        r = httpx.post(f"http://{servidor_vivo}/api/voices/import", headers=auth,
                       files={"zip_file": ("v.zip", b"PK\x03\x04" + b"\0" * 128,
                                           "application/zip")}, timeout=20)
        assert r.status_code == 200, r.text[:120]

    latencia = _health_durante(_pede)
    assert latencia < 1.0, f"/health levou {latencia:.2f}s durante o import"


def test_rotas_async_nao_chamam_bloqueantes():
    """Guarda-corpo: `async def` daqui não pode voltar a chamar o que bloqueia."""
    import ast as _ast

    proibidos = {"subprocess.run", "subprocess.check_output", "zipfile.ZipFile",
                 "_speaker_pontua", "_speaker_avalia", "_save_audio_upload",
                 "sf.read", "_executa_restore",
                 # leem o snapshot justamente para NÃO pegar o `_apikeys_lock` no
                 # loop: voltar a chamá-los dentro de `async def` desfaz o #799
                 # calado.
                 "_key_role", "_admin_is_allowed"}
    # Exceção NOMEADA, não buraco: o `_TetoDeCorpo.__call__` é ASGI e chama
    # `_admin_is_allowed` ANTES de ler o corpo de propósito (#790) — ali o
    # snapshot é a propriedade certa e a célula de runtime
    # `test_admin_nao_pega_o_apikeys_lock` cobra. Qualquer OUTRO `async def`
    # que chame o nome tem de aparecer.
    # A exceção é por NOME de função: hoje só existe UM `__call__` no arquivo. Se
    # aparecer um segundo, ele herda a tolerância sem ninguém notar — nesse dia,
    # qualificar por escopo (classe) em vez de nome.
    permitido = {("__call__", "_admin_is_allowed")}
    def _chamadas(no):
        """Calls do PRÓPRIO corpo — função interna (que roda na thread) não conta."""
        for filho in _ast.iter_child_nodes(no):
            if isinstance(filho, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if isinstance(filho, _ast.Call):
                yield filho
            yield from _chamadas(filho)

    arvore = _ast.parse(Path(app.__file__).read_text())
    achados = []
    for no in _ast.walk(arvore):
        if not isinstance(no, _ast.AsyncFunctionDef):
            continue
        for filho in _chamadas(no):
            alvo = filho.func
            nome = (f"{alvo.value.id}.{alvo.attr}"
                    if isinstance(alvo, _ast.Attribute) and isinstance(alvo.value, _ast.Name)
                    else getattr(alvo, "id", None))
            if nome in proibidos and (no.name, nome) not in permitido:
                achados.append(f"{no.name}:{nome}")
    assert achados == [], f"chamada bloqueante dentro de async def: {achados}"


def test_admin_nao_pega_o_apikeys_lock(monkeypatch):
    """`_admin_is_allowed`/`_key_role` leem o snapshot, sem o `_apikeys_lock` (#799).

    O caminho de admin é o da LAN e do túnel: se ele pega o lock, o loop volta a
    esperar quem está gravando disco na thread."""
    monkeypatch.setattr(app, "_ADMIN_API_KEY", "")
    monkeypatch.setattr(app, "_ENV_API_KEY", "")
    app._apikeys["keys"][0]["role"] = "admin"
    app._apikeys_publica()
    chave = app._apikeys["keys"][0]["secret"]
    pedido = _req_falso(chave=chave, host="testserver")

    # lock PRESO por outra thread: se o caminho pegar o lock, o teste pendura
    preso = threading.Event()

    def _segura():
        with app._apikeys_lock:
            preso.set()
            time.sleep(1.5)

    t = threading.Thread(target=_segura, daemon=True)
    t.start()
    preso.wait(2)
    t0 = time.monotonic()
    try:
        assert app._key_role(chave) == "admin"
        assert app._admin_is_allowed(pedido) is True
        decorrido = time.monotonic() - t0      # ANTES do join: o join espera o sleep
    finally:
        t.join(2)
    assert decorrido < 1.0, f"pegou o `_apikeys_lock` no caminho de admin ({decorrido:.2f}s)"
    app._apikeys["keys"][0]["role"] = "admin"
    app._apikeys_publica()


def test_troca_de_role_na_mao_sempre_publica():
    """Guarda: escrever `role` direto sem republicar deixa o vizinho vendo o papel
    anterior (#799). Cobre o arquivo inteiro, não um caso."""
    linhas = Path(__file__).read_text().split("\n")
    sem_publicar = [
        i + 1 for i, linha in enumerate(linhas)
        if 'app._apikeys["keys"][0]["role"] =' in linha
        and "_apikeys_publica" not in "\n".join(linhas[i + 1:i + 3])
    ]
    assert sem_publicar == [], f"role trocado sem publicar nas linhas {sem_publicar}"
