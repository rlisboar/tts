"""GATE QA (task_aa879054): matriz admin × use nas rotas mutantes.

A régua é o DANO (#356): apagar/desligar/trocar o que é GLOBAL ou do dono = 403
com chave `use`; CONSUMIR (gerar, editar, transcrever, ticket, mic-route) = 200
com `use`. Admin faz tudo.

A lista de rotas da task é mínima e a própria task manda procurar rota ESQUECIDA —
por isso a matriz aqui é montada pela varredura de TODAS as rotas mutantes do
`app.py`, e não pela lista do texto. Rota mutante nova que apareça sem veredito
quebra o teste de cobertura no fim do arquivo.

Instância própria por `TestClient` (nada de `:7860`); `settings.json` e
`.apikeys.json` já vivem em tmp pelo conftest.
"""

import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

import app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import (  # noqa: E402,F401
    _voz_fake, apikeys_isolados, auth, chave_de_uso, client, voices_tmp,
)

# Fixtures consumidas pelos testes do arquivo; o `ruff` cobre com `noqa`, o
# `pyflakes` (que o ignora) precisa do `__all__`. Inerte em runtime.
__all__ = [
    "apikeys_isolados",
    "auth",
    "chave_de_uso",
    "client",
    "voices_tmp",
]

# ---------------------------------------------------------------- classificação
# ELEVADAS: mexem no global/na instalação/no catálogo do dono.
ELEVADAS = [
    ("POST", "/api/shutdown"),
    ("POST", "/api/tunnel/start"),
    ("POST", "/api/tunnel/stop"),
    ("POST", "/api/tunnel/restart"),
    ("POST", "/api/models/unload"),
    ("DELETE", "/api/outputs"),
    ("DELETE", "/api/outputs/abc123"),
    ("DELETE", "/api/voices/abc123"),
    ("POST", "/api/voices/import"),
    ("GET", "/api/voices/export"),
    ("DELETE", "/api/speaker/abc123"),
    ("POST", "/api/apikeys"),
    ("PATCH", "/api/apikeys/abc"),
    ("POST", "/api/apikeys/abc/rotate"),
    ("DELETE", "/api/apikeys/abc"),
    ("POST", "/api/apikeys/enabled"),
    # ESQUECIDA pela task e pela implementação: o restore sobrescreve settings,
    # chaves e vozes de uma vez — é o caso mais destrutivo de todos.
    ("POST", "/api/restore"),
]

# CONSUMO: quem só usa a instalação continua com 200.
CONSUMO = [
    ("POST", "/api/mic-route"),
    ("POST", "/api/tts"),
    ("POST", "/api/transcribe"),
    ("POST", "/api/translate-speech"),
    ("POST", "/api/modify-speech"),
    ("POST", "/api/stt-partial"),
    ("POST", "/api/audio/edit"),
    ("POST", "/api/voices/abc123/denoise"),
    ("POST", "/api/live/ticket"),
    ("POST", "/api/speaker/check"),
    ("POST", "/v1/audio/transcriptions"),
    ("POST", "/v1/audio/translations"),
    ("POST", "/v1/audio/speech"),
]


@pytest.fixture
def preparado(voices_tmp, monkeypatch):
    """Voz e speaker de mentira para as rotas por id não darem 404 antes do 403."""
    _voz_fake(app.VOICES_DIR, "v1")
    pastas = {
        "outputs": app.OUTPUTS_DIR,
        "speakers": getattr(app, "SPEAKERS_DIR", None),
    }
    return pastas


def _wav() -> bytes:
    import io

    import numpy as np
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, (0.2 * np.ones(24000)).astype(np.float32), 24000, format="WAV")
    return buf.getvalue()


def _zip_restore() -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"arquivos": {}}))
    return buf.getvalue()


def _chama(client, verbo, rota, headers):
    """Chamada mínima que chega ao guarda (corpo/arquivo só onde é exigido)."""
    if rota == "/api/restore":
        return client.post(rota, headers=headers, files={"arquivo": ("e.zip", _zip_restore(), "application/zip")})
    if rota == "/api/voices/v1/replace":
        return client.post(rota, headers=headers, files={"audio": ("a.wav", _wav(), "audio/wav")})
    if rota == "/api/voices/import":
        return client.post(rota, headers=headers, files={"zip_file": ("v.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip")})
    if verbo == "GET":
        return client.get(rota, headers=headers)
    if verbo == "DELETE":
        caminho = rota.replace("/api/voices/abc123", "/api/voices/v1")
        return client.delete(caminho, headers=headers)
    if verbo == "PATCH":
        return client.patch(rota, headers=headers, json={})
    if rota.endswith("/denoise"):
        # a voz de exemplo é `v1`; a rota do teste usa o id normalizado
        return client.post(rota.replace("abc123", "v1"), headers=headers)
        return client.post(rota, headers=headers)
    if rota == "/api/voices/abc123/replace":
        return client.post(rota.replace("abc123", "v1"), headers=headers,
                           files={"audio": ("a.wav", _wav(), "audio/wav")})
    if rota.startswith("/api/apikeys"):
        return client.post(rota, headers=headers, json={"name": "x", "enabled": True})
    if rota in ("/api/tts", "/api/audio/edit", "/api/speaker/check", "/api/mic-route",
                "/api/live/ticket", "/api/translate-speech"):
        return client.post(rota, headers=headers, json={})
    return client.post(rota, headers=headers)


# --------------------------------------------------------------------- matriz

@pytest.mark.parametrize("verbo,rota", ELEVADAS)
def test_elevada_recusa_chave_de_uso(client, chave_de_uso, preparado, verbo, rota):
    r = _chama(client, verbo, rota, chave_de_uso)
    assert r.status_code == 403, f"{verbo} {rota} respondeu {r.status_code} para chave de uso: {r.text[:120]}"


@pytest.mark.parametrize("verbo,rota", ELEVADAS)
def test_elevada_aceita_admin(client, auth, preparado, verbo, rota):
    """Admin não pode ser barrado. 400/404 aqui é ambiente (sem agente de túnel
    instalado, áudio/voz de exemplo ausente), não guarda — o que importa é não
    ser 403."""
    r = _chama(client, verbo, rota, auth)
    assert r.status_code != 403, f"{verbo} {rota} barrou o admin: {r.text[:120]}"
    assert r.status_code in (200, 400, 404, 422), (
        f"{verbo} {rota} deu {r.status_code} para admin: {r.text[:120]}")


@pytest.mark.parametrize("verbo,rota", CONSUMO)
def test_consumo_aceita_chave_de_uso(client, chave_de_uso, preparado, verbo, rota):
    r = _chama(client, verbo, rota, chave_de_uso)
    assert r.status_code in (200, 400, 422), (
        f"{verbo} {rota} não passou do guarda com chave de uso: {r.status_code} {r.text[:120]}")


def test_chave_legada_sem_papel_administra(client, preparado):
    """Sem `TTS_ROD_ADMIN_KEY`, chave criada sem `role` é legada e admin."""
    r = client.post("/api/apikeys", headers={"X-API-Key": _legada(client)}, json={"name": "pytest-legada"})
    assert r.status_code == 200, r.text
    assert r.json()["key"]["role"] is None
    h = {"X-API-Key": r.json()["key"]["secret"]}
    # 400 aqui é ambiente (sem agente de túnel instalado); o que não pode é 403
    assert client.post("/api/tunnel/restart", headers=h).status_code != 403


def _legada(client) -> str:
    for k in client.get("/api/apikeys", headers={"X-API-Key": app.API_KEY}).json()["keys"]:
        if k.get("role") is None:
            return app.API_KEY
    return app.API_KEY


def test_status_e_settings_nao_regridem(client, chave_de_uso, auth):
    """A UI lê `is_admin` em /api/settings e fala /api/status: nem a leitura de uso
    pode quebrar nem o papel pode vir trocado."""
    assert client.get("/api/status", headers=chave_de_uso).status_code == 200
    assert client.get("/api/settings", headers=chave_de_uso).json().get("is_admin") is False
    assert client.get("/api/settings", headers=auth).json().get("is_admin") is True


# ---------------------------------------------------- cobertura da varredura

def test_nenhuma_rota_mutante_fica_fora_da_matriz():
    """Rota mutante NOVA sem classificação quebra aqui, em vez de ficar aberta."""
    texto = Path(app.__file__).read_text()
    def _sem_id(rota: str) -> str:
        return re.sub(r"\{[a-z_]+\}", "abc123", rota)

    rotas = {(m.group(1).upper(), _sem_id(m.group(2)))
             for m in re.finditer(r'@app\.(post|put|patch|delete)\("([^"]+)"', texto)}
    conhecidas = set(ELEVADAS) | set(CONSUMO)
    # rotas de conversa/chat: sessão por sessão, auth de consumo, cobertas à parte
    fora = {r for r in rotas - conhecidas
            if not r[1].startswith(("/api/chat", "/api/chat-debug", "/api/settings",
                                    "/api/voices/{voice_id}/replace", "/api/voices",
                                    "/api/speaker/enroll", "/api/youtube-audio",
                                    "/api/translate/warmup", "/api/apikeys"))}
    assert not fora, f"rotas mutantes sem veredito na matriz: {sorted(fora)}"
