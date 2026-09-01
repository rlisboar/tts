"""TTS-STUDIO — clonagem de voz local com gravação e gerenciamento de vozes.

Servidor FastAPI + backends TTS via MLX (Apple Silicon): OmniVoice, Qwen3-TTS,
Fish S2, Chatterbox, Kokoro, PocketTTS, VoxCPM2, Voxtral, etc.
Tudo local: nenhum áudio ou texto sai da máquina.
"""

import concurrent.futures
import json
import os
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import OrderedDict
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from backends import generate_with_backend, list_backends, resolve_backend
from common import (CHUNK_SILENCE_S, NATIVE_SPEED_FAMILIES, OMNI_ALIASES,
                    resolve_omni_source, write_json_atomic,
                    apply_audio_fx,
                    atempo_chain as _atempo_chain,
                    biquad as _biquad,
                    fade_edges as _fade_edges,
                    normalize as _normalize,
                    release_mlx_memory as _release_mlx_memory,
                    sanitize_text as _sanitize_text,
                    split_text as _split_text,
                    time_stretch as _time_stretch,
                    trim_tail_silence as _trim_tail_silence,
                    write_wav_concat as _write_wav_concat)

BASE = Path(__file__).resolve().parent
VOICES_DIR = BASE / "voices"
OUTPUTS_DIR = BASE / "outputs"
APIKEYS_PATH = BASE / ".apikeys.json"
SPEAKER_PATH = BASE / ".speaker-profiles.json"   # embeddings de voz (fora do git)
LEGACY_APIKEY_PATH = BASE / ".apikey"
VOICES_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

# trechos parciais de jobs interrompidos não sobrevivem a restart
for _d in OUTPUTS_DIR.glob(".job-*"):
    shutil.rmtree(_d, ignore_errors=True)

# OmniVoice: as conversões MLX publicadas vêm quebradas — app e worker usam o
# dir montado por common.assemble_omnivoice_path (backbone bf16 + audio_tokenizer
# COMPLETO em .omnivoice-bf16/). Outros backends usam o repo MLX direto (backends.py).
# atalho de backends.py (omnivoice, qwen3-0.6b, fish-s2…) ou id/dir MLX livre
MODEL_ID = os.environ.get("TTS_ROD_MODEL", "omnivoice")
# voz "virtual": gera só a partir da descrição textual (instruct), sem ref de clone
DESIGN_VOICE_ID = "__design__"

# ---------------------------------------------------------------------------
# Configurações padrão editáveis no dashboard (persistem em settings.json e
# valem para UI e API; parâmetro explícito na requisição sempre sobrepõe)
# ---------------------------------------------------------------------------
SETTINGS_PATH = BASE / "settings.json"
_SETTINGS_DEFAULTS = {
    "model": MODEL_ID,         # atalho (omnivoice, qwen3-0.6b…) ou id/dir MLX
    "pre_prompt": "",          # texto falado antes de toda geração
    "language": "auto",        # "auto" = OmniVoice detecta o idioma do texto (recomendado)
    "default_voice": None,     # id; None = voz mais recente
    "chunk_max_chars": 140,
    "speed": 1.0,              # velocidade da fala (UI + API); nativa do modelo (preserva o tom)
    "auto_cleanup": False,     # apaga áudios gerados automaticamente
    "auto_cleanup_minutes": 15,
    # OmniVoice — controles de geração (defaults = os da lib)
    "omni_num_steps": 16,             # passos de unmasking (4–64); 16 rápido, 32 qualidade
    "omni_guidance_scale": 2.0,       # força do CFG (0–10): + = mais aderente ao texto/voz
    "omni_class_temperature": 0.0,    # temp. de amostragem de token (0 = greedy/estável)
    "omni_position_temperature": 5.0, # temp. da escolha de posição a revelar (0–20)
    "omni_layer_penalty_factor": 5.0, # penalidade por camada de codebook (0–20)
    "omni_t_shift": 0.1,              # deslocamento do cronograma de difusão (0–1)
    "omni_denoise": True,             # limpa ruído do áudio gerado (config do modelo)
    "omni_preprocess_prompt": True,   # pré-processa o prompt/texto antes de gerar
    "omni_postprocess_output": True,  # pós-processa o áudio de saída
    "omni_audio_chunk_duration": 15.0,   # chunking interno de texto longo: duração (s)
    "omni_audio_chunk_threshold": 30.0,  # chunking interno: limiar p/ dividir (s)
    "omni_instruct": "",              # voice design textual (ex.: "female, low pitch")
    "omni_seed": 42,                  # seed da geração: voz reprodutível (mesmo instruct=mesma voz). -1 = aleatório
    "omni_duration_s": None,          # força duração fixa em s (None = automático)
    "omni_ref_max_s": 10.0,           # quanto da amostra de referência usar (3–30 s)
    "omni_precision": "bf16",         # fp32 (repo F32) | bf16 (montado) | q8 | q4 (quantiza só o backbone)
    # Controles genéricos multi-backend (mapeados em _resolve_omni / generate_with_backend)
    "gen_temperature": 0.8,           # sampling AR (Qwen/Fish/Chatterbox/Pocket/Voxtral)
    "gen_top_p": 0.95,
    "gen_top_k": 50,
    "gen_repetition_penalty": 1.1,
    "gen_max_tokens": 2048,
    "gen_exaggeration": 0.5,          # Chatterbox expressividade
    "gen_cfg_weight": 0.5,            # Chatterbox CFG
    "gen_min_p": 0.05,                # Chatterbox min-p
    "gen_chunk_length": 300,          # Fish S2
    "gen_speaker": "Ryan",            # Qwen3 CustomVoice
    "gen_kokoro_voice": "af_heart",
    "gen_pocket_voice": "alba",
    "gen_voxtral_voice": "casual_male",
    "voice_denoise": True,            # limpa ruído de fundo da amostra ao salvar a voz
    "voice_denoise_strength": 0.7,    # agressividade do spectral gating (0–1)
    # Áudio de saída (pós-geração): ganho + EQ 3 bandas (dB)
    "audio_gain_db": 0.0,             # ganho geral (-15..+15)
    "audio_eq_low_db": 0.0,           # grave (shelf 150 Hz)
    "audio_eq_mid_db": 0.0,           # médio (peak 1.5 kHz)
    "audio_eq_high_db": 0.0,          # agudo (shelf 5 kHz)
    # Tradutor de voz — filtros anti-ruído da transcrição (rejeita alucinação do Whisper)
    "stt_min_words": 1,               # mínimo de palavras p/ aceitar (ignora ruído)
    "stt_min_chars": 2,               # mínimo de caracteres
    "stt_max_no_speech": 0.6,         # rejeita se prob. de "sem fala" acima disto (0–1)
    "stt_min_logprob": -1.0,          # rejeita se confiança média abaixo disto (-5–0)
    "stt_max_compression": 2.4,       # rejeita se repetitivo demais (alucinação) (1–10)
    "stt_beam": 5,                    # beam do STT remoto: 1=rápido, 5=padrão, 8=qualidade
    # Modelos remotos (API OpenAI-compatível). base_url deve terminar em /v1
    # (ex.: http://rtx-host:8000/v1). api_key opcional. Tudo local por padrão.
    # base_url + api_key ficam locais (settings.json é gitignored). Vazio = local.
    "remote_tts": False,              # síntese (OmniVoice) numa máquina remota (ex.: RTX)
    "remote_tts_url": "",             # URL completa do endpoint, ex.: http://192.168.1.50:8800/tts
    "remote_tts_voice": "",           # nome/preset da voz no servidor remoto (vai como `voice`)
    "remote_tts_extra": "",           # JSON com params extras do servidor (speed, num_steps…)
    "remote_tts_model": "tts-1",      # (compat OpenAI) nome do modelo, se o servidor usar
    "remote_translate": False,        # tradução (LLM) via API remota
    "remote_stt": False,              # transcrição (STT) via API remota
    "remote_base_url": "",            # ex.: http://rtx-host:8000/v1
    "remote_api_key": "",             # chave do provedor (guardada localmente; opcional)
    "remote_translate_model": "gpt-4o-mini",
    "remote_stt_model": "whisper-1",
    # STT pode apontar p/ uma API DEDICADA (proxy externo) sem mexer no translate/TTS.
    # vazio = usa remote_base_url/remote_api_key compartilhados.
    "remote_stt_base_url": "",        # ex.: https://api.openai.com/v1 (OpenAI-compatível)
    "remote_stt_key": "",             # chave dessa API de STT (vazio = usa remote_api_key)
    # Conversa (decide o texto por chat com IA). Provedor OpenAI-compatível;
    # vazio = herda remote_base_url/remote_api_key/remote_translate_model.
    "chat_base_url": "",              # ex.: https://api.openai.com/v1
    "chat_model": "",                 # ex.: gpt-4o-mini
    "chat_api_key": "",               # vazio = usa remote_api_key
    "chat_system": "",                # instruções da IA (preprompt); vazio = prompt padrão
    "chat_extra": "",                 # JSON com params extras do LLM (reasoning_effort, top_p…)
    # Verificação de locutor (biometria de voz): off | enforce (só vozes
    # cadastradas) | label (transcreve todos e etiqueta quem falou)
    "speaker_gate": "off",
    "speaker_threshold": 0.75,        # rigor: similaridade cosseno mínima (0.5–0.95)
    "translate_model": "",            # repo MLX do tradutor LOCAL; vazio = padrão (TRANSLATE_REPO)
    "free_local_on_remote": False,    # descarrega o modelo LOCAL correspondente quando o remoto está ativo
    # Memória: descarrega TTS/STT/tradutor/SER após N minutos sem uso (0 = nunca)
    "idle_unload_minutes": 10,
    # Fila de falas: vários sistemas pedem TTS ao mesmo tempo sem sobrepor o áudio.
    # A espera principal = duração real do último áudio entregue; gap é só folga
    # extra (silêncio) entre uma fala e a próxima.
    "speech_queue": True,             # ligado por padrão (vários clientes na rede)
    "speech_queue_gap_s": 0.35,       # folga extra após a duração da fala (0–5 s)
}
_settings = dict(_SETTINGS_DEFAULTS)
if SETTINGS_PATH.exists():
    try:
        salvo = json.loads(SETTINGS_PATH.read_text())
        _settings.update({k: salvo[k] for k in _SETTINGS_DEFAULTS if k in salvo})
    except Exception as exc:  # noqa: BLE001
        # corrompido (crash na escrita): preserva p/ recuperação, segue com defaults
        try:
            SETTINGS_PATH.replace(SETTINGS_PATH.with_name("settings.json.corrupt"))
        except OSError:
            pass
        print(f"⚠ settings.json corrompido — backup em settings.json.corrupt; "
              f"usando defaults: {exc}", flush=True)


def _save_settings():
    """Persiste settings.json com TODAS as chaves conhecidas (defaults + overrides)."""
    # garante booleans/números estáveis (JSON true/false, não null)
    _settings["speech_queue"] = bool(_settings.get("speech_queue"))
    try:
        _settings["speech_queue_gap_s"] = float(_settings.get("speech_queue_gap_s") or 0.35)
    except (TypeError, ValueError):
        _settings["speech_queue_gap_s"] = 0.35
    payload = {k: _settings.get(k, v) for k, v in _SETTINGS_DEFAULTS.items()}
    write_json_atomic(SETTINGS_PATH, payload)


# materializa chaves novas (ex.: speech_queue) no arquivo se ainda não existirem
try:
    _salvo_keys = set()
    if SETTINGS_PATH.exists():
        _salvo_keys = set(json.loads(SETTINGS_PATH.read_text()).keys())
    if "speech_queue" not in _salvo_keys or "speech_queue_gap_s" not in _salvo_keys:
        _save_settings()
except Exception:  # noqa: BLE001
    pass

# Textos maiores são gerados em trechos. OmniVoice é masked-diffusion não-AR (sem o
# problema de EOS do backend antigo), mas dividir permite tocar trecho-a-trecho — a
# fala começa após o 1º trecho, não no fim.
CHUNK_MAX_CHARS = 140

# OmniVoice (masked-diffusion não-AR): passos de unmasking iterativo. 16 = rápido
# (RTF ~0,8 no M3 com ref cacheada), 32 = qualidade (default da lib).
OMNI_STEPS_FAST = 16
OMNI_STEPS_HQ = 32
OMNI_REF_MAX_S = 10.0  # ref >20s é cortada no maior silêncio até este teto

# Vozes padrão do modelo: criadas por "voice design" (descrição `instruct`, sem
# gravação). Na 1ª utilização geramos uma amostra-semente e a salvamos como uma
# voz normal (.wav) — isso ANCORA o timbre para ficar consistente entre trechos.
OMNI_PRESET_SEED = ("Olá, esta é a minha voz. Vou narrar o seu texto com clareza, "
                    "ritmo natural e boa dicção, do começo ao fim.")
OMNI_PRESETS = {
    # instruct usa SÓ o vocabulário fechado do OmniVoice (gender/age/pitch/accent/whisper)
    "vd-narrador": {"name": "Narrador (masc., grave)",     "instruct": "male, middle-aged, low pitch"},
    "vd-locutora": {"name": "Locutora (fem., suave)",      "instruct": "female, moderate pitch"},
    "vd-jovem-m":  {"name": "Jovem (masc., animado)",      "instruct": "male, young adult, high pitch"},
    "vd-jovem-f":  {"name": "Jovem (fem., animada)",       "instruct": "female, young adult, high pitch"},
    "vd-formal":   {"name": "Formal (masc., autoritário)", "instruct": "male, middle-aged, low pitch"},
    "vd-podcast":  {"name": "Podcast (fem., conversa)",    "instruct": "female, young adult, moderate pitch"},
}

# Idioma: o token é injetado cru no MLX (<|lang_start|>{x}<|lang_end|>) — o porte MLX
# NÃO faz o mapeamento nome->código que o upstream faz. Canônico = OmniVoice ID
# (código, ex.: "pt"); "None" = auto-detecção pelo texto (modo recomendado upstream).
_OMNI_LANG_NOMES = {
    "português": "pt", "portugues": "pt", "portuguese": "pt",
    "inglês": "en", "ingles": "en", "english": "en",
    "espanhol": "es", "español": "es", "spanish": "es",
    "francês": "fr", "frances": "fr", "french": "fr",
    "alemão": "de", "alemao": "de", "german": "de",
    "italiano": "it", "italian": "it",
}


def _omni_language(lang) -> str:
    """Resolve o valor de `language` aceito pelo OmniVoice no caminho MLX.

    vazio/"auto"/"none" -> "None" (auto-detecção pelo texto). Nome de idioma ->
    código canônico (OmniVoice ID). Caso contrário, assume que já é um código.
    """
    l = str(lang or "").strip().lower()
    if l in ("", "auto", "none", "null"):
        return "None"
    return _OMNI_LANG_NOMES.get(l, l)


# Tradutor de voz (PoC): STT (mlx-whisper) + tradução (mlx-lm) -> TTS na voz clonada.
WHISPER_REPO = os.environ.get("TTS_ROD_WHISPER", "mlx-community/whisper-large-v3-turbo")
TRANSLATE_REPO = os.environ.get("TTS_ROD_TRANSLATE", "mlx-community/Qwen2.5-3B-Instruct-4bit")
# código -> nome em inglês (para o prompt de tradução e o lang do OmniVoice)
LANG_DISPLAY = {
    "pt": "Portuguese", "en": "English", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "zh": "Chinese",
    "ru": "Russian", "ko": "Korean", "ar": "Arabic", "nl": "Dutch",
}

# ---------------------------------------------------------------------------
# Chaves de API (multi): protegem /api/* e /v1/* na rede. Loopback (127.0.0.1)
# não exige chave. Aceita Authorization: Bearer, X-API-Key ou ?api_key=
# (necessário p/ <audio src> na UI). Persistidas em .apikeys.json; migra de
# .apikey / TTS_ROD_API_KEY. Gestão na UI (Configurações → Acesso).
# ---------------------------------------------------------------------------
_ENV_API_KEY = (os.environ.get("TTS_ROD_API_KEY") or "").strip()
_apikeys_lock = threading.Lock()
_apikeys: dict = {"enabled": True, "keys": []}  # keys: id, name, secret, created_at


def _new_api_secret() -> str:
    return _secrets.token_hex(24)


def _mask_secret(secret: str) -> str:
    s = secret or ""
    if len(s) <= 10:
        return "••••••••"
    return f"{s[:4]}…{s[-4:]}"


def _sync_legacy_apikey_file():
    """Mantém .apikey = 1ª chave gerenciada (compat run.sh / scripts)."""
    try:
        keys = _apikeys.get("keys") or []
        if keys:
            LEGACY_APIKEY_PATH.write_text(keys[0]["secret"] + "\n")
            try:
                os.chmod(LEGACY_APIKEY_PATH, 0o600)
            except Exception:  # noqa: BLE001
                pass
        elif LEGACY_APIKEY_PATH.exists() and not _ENV_API_KEY:
            # sem chaves gerenciadas e sem env: remove legado p/ não reabrir auth fantasma
            pass
    except Exception:  # noqa: BLE001
        pass


def _save_apikeys():
    payload = {
        "enabled": bool(_apikeys.get("enabled", True)),
        "keys": [
            {
                "id": k["id"],
                "name": k.get("name") or "sem nome",
                "secret": k["secret"],
                "created_at": k.get("created_at") or "",
            }
            for k in (_apikeys.get("keys") or [])
            if k.get("secret")
        ],
    }
    write_json_atomic(APIKEYS_PATH, payload)
    try:
        os.chmod(APIKEYS_PATH, 0o600)
    except Exception:  # noqa: BLE001
        pass
    _sync_legacy_apikey_file()


def _load_apikeys():
    """Carrega .apikeys.json; migra .apikey/env; gera chave padrão se vazio."""
    global _apikeys
    keys = []
    enabled = True
    if APIKEYS_PATH.exists():
        try:
            data = json.loads(APIKEYS_PATH.read_text())
            enabled = bool(data.get("enabled", True))
            for raw in data.get("keys") or []:
                sec = str(raw.get("secret") or "").strip()
                if not sec:
                    continue
                keys.append({
                    "id": str(raw.get("id") or uuid.uuid4().hex[:10]),
                    "name": str(raw.get("name") or "chave")[:64],
                    "secret": sec,
                    "created_at": str(raw.get("created_at") or ""),
                })
        except Exception as exc:  # noqa: BLE001
            keys = []
            # corrompido: preserva p/ recuperação — abaixo uma chave nova é gerada
            try:
                APIKEYS_PATH.replace(APIKEYS_PATH.with_name(".apikeys.json.corrupt"))
            except OSError:
                pass
            print(f"⚠ .apikeys.json corrompido — backup em .apikeys.json.corrupt; "
                  f"uma chave nova será gerada: {exc}", flush=True)

    if not keys:
        # migra chave legada (.apikey) ou env
        legacy = ""
        if LEGACY_APIKEY_PATH.exists():
            try:
                legacy = LEGACY_APIKEY_PATH.read_text().strip()
            except Exception:  # noqa: BLE001
                legacy = ""
        seed = legacy or _ENV_API_KEY
        if seed:
            keys.append({
                "id": uuid.uuid4().hex[:10],
                "name": "padrão",
                "secret": seed,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        else:
            keys.append({
                "id": uuid.uuid4().hex[:10],
                "name": "padrão",
                "secret": _new_api_secret(),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        _apikeys = {"enabled": True, "keys": keys}
        _save_apikeys()
        return

    _apikeys = {"enabled": enabled, "keys": keys}
    # se veio só do arquivo antigo sem .apikeys, já salvamos acima; se veio do
    # .apikeys.json, só sincroniza .apikey p/ run.sh
    _sync_legacy_apikey_file()


def _auth_enabled() -> bool:
    """Auth na rede se enabled e existe ao menos uma chave (arquivo ou env)."""
    with _apikeys_lock:
        if not _apikeys.get("enabled", True):
            return False
        if _apikeys.get("keys"):
            return True
    return bool(_ENV_API_KEY)


def _extract_request_key(request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key")
            or request.query_params.get("api_key")
            or "").strip()


def _key_is_valid(provided: str) -> bool:
    if not provided:
        return False
    if _ENV_API_KEY and _secrets.compare_digest(provided, _ENV_API_KEY):
        return True
    with _apikeys_lock:
        return any(_secrets.compare_digest(provided, k.get("secret") or "")
                   for k in (_apikeys.get("keys") or []))


def _primary_api_key() -> str:
    """Chave principal p/ clientes empacotados (mic-router etc.)."""
    with _apikeys_lock:
        keys = _apikeys.get("keys") or []
        if keys:
            return keys[0]["secret"]
    return _ENV_API_KEY or ""


def _public_key_row(k: dict, *, reveal: bool = False) -> dict:
    row = {
        "id": k["id"],
        "name": k.get("name") or "sem nome",
        "masked": _mask_secret(k.get("secret") or ""),
        "created_at": k.get("created_at") or "",
        "readonly": bool(k.get("readonly")),
    }
    if reveal:
        row["secret"] = k.get("secret") or ""
    return row


_load_apikeys()

# compat: scripts antigos / mic-router que leem API_KEY no módulo
API_KEY = _primary_api_key() or _ENV_API_KEY or None

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

app = FastAPI(title="TTS-STUDIO")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _exige_chave(request, call_next):
    # loopback = processo no próprio Mac; chave só para a rede
    local = request.client and request.client.host in ("127.0.0.1", "::1")
    protegido = request.url.path.startswith(("/api/", "/v1/"))
    # docs/openapi revelam o mapa completo da API: abertos no Mac por conveniência,
    # mas exigem chave quando o acesso vem pela internet (proxy)
    if not local and request.url.path in ("/openapi.json", "/docs", "/redoc",
                                          "/docs/oauth2-redirect"):
        protegido = True
    if protegido and request.method != "OPTIONS" and _auth_enabled():
        ok = local or _key_is_valid(_extract_request_key(request))
        if not ok:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Não autorizado"}, status_code=401)
    return await call_next(request)


# Proxy reverso por PATH (ex.: https://dominio/ttsproxy/... atrás de nginx/CF):
# remove o prefixo antes do roteamento — requisições locais (sem prefixo) são
# intocadas. Registrado DEPOIS da auth => roda ANTES: a auth vê /api/... limpo
# e a exigência de chave vale também pelo proxy (origem = IP de LAN, não loopback).
_BASE_PATH = os.environ.get("TTS_ROD_BASE_PATH", "/ttsproxy").rstrip("/")


@app.middleware("http")
async def _strip_base_path(request, call_next):
    if _BASE_PATH and (request.url.path == _BASE_PATH
                       or request.url.path.startswith(_BASE_PATH + "/")):
        # preserva o path ORIGINAL (com prefixo) p/ handlers que precisam
        # reconstruir URLs absolutas (ex.: zip do mic-router)
        request.scope["tts_prefix_path"] = request.url.path
        rest = request.url.path[len(_BASE_PATH):] or "/"
        request.scope["path"] = rest
        request.scope["raw_path"] = rest.encode()
    resp = await call_next(request)
    # HTML sempre revalidado (ETag) — updates da UI chegam no refresh;
    # cabeçalhos de segurança básicos
    if (resp.headers.get("content-type") or "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp

# ---------------------------------------------------------------------------
# Modelo (carregamento preguiçoso — primeira síntese baixa/monta os pesos)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_gen_lock = threading.Lock()  # geração LOCAL (MLX) não é thread-safe; serializa
import contextlib
_NO_LOCK = contextlib.nullcontext()  # remoto pode rodar concorrente (sem serializar)
_model_state = {"status": "idle", "device": None, "model": _settings["model"],
                "error": None, "progress": None}


def _model_state_progress(msg):
    """Callback de progresso p/ download/montagem do modelo (common.py)."""
    _model_state.update(progress=msg)


def _current_backend() -> dict:
    """Metadados do backend selecionado em settings['model']."""
    return resolve_backend(_settings.get("model") or "omnivoice")


def _is_omnivoice() -> bool:
    return _current_backend()["family"] == "omnivoice"


def _resolve_model_path() -> str:
    """Resolve settings['model'] → path/id p/ load_model.

    OmniVoice: monta bf16 local (ou repo fp32). Outros atalhos: repo MLX do catálogo.
    String livre (HF/dir): repassada como está.
    """
    be = _current_backend()
    if be["family"] == "omnivoice" and (
            be["is_shortcut"] or str(be["path"]).strip().lower() in OMNI_ALIASES):
        return resolve_omni_source(_settings, BASE, progress=_model_state_progress)
    return be["path"]


def _quantize_backbone(model, bits: int):
    """Quantiza in-place SÓ o backbone (transformer) p/ q8/q4 — reduz RAM e acelera
    matmul. NÃO toca no audio_tokenizer (codec, preserva timbre) nem nos audio_heads
    (vocab 1025, não divisível pelo group_size). Camadas com última dim não múltipla
    de 64 são puladas pelo predicate (ficam em bf16). Só OmniVoice."""
    import mlx.nn as nn

    gs = 64

    def pred(_path, module):
        w = getattr(module, "weight", None)
        return w is not None and w.ndim >= 2 and w.shape[-1] % gs == 0

    if not hasattr(model, "backbone"):
        return
    nn.quantize(model.backbone, group_size=gs, bits=bits, class_predicate=pred)


def _get_model():
    global _model
    with _model_lock:
        prec = str(_settings.get("omni_precision", "bf16")).lower()
        be = _current_backend()
        if (_model is not None and _model_state.get("model") == _settings["model"]
                and _model_state.get("precision") == prec
                and _model_state.get("family") == be["family"]):
            _touch_use("tts")
            return _model
        # troca de modelo/precisão: libera o anterior com agressividade (Metal)
        old = _model
        _model = None
        _conds_cache.clear()
        path = _resolve_model_path()
        label = be["meta"].get("label") or path
        _model_state.update(
            status="loading", device="mlx", model=_settings["model"],
            precision=prec, family=be["family"], path=path,
            progress=f"carregando {label}…",
            backend_id=be.get("id"), backend_label=be["meta"].get("label"),
        )
        try:
            del old
            _release_mlx_memory(aggressive=True)
            from mlx_audio.tts.utils import load_model

            _model = load_model(path)
            # quantização in-place só faz sentido no OmniVoice (backbone próprio)
            if be["family"] == "omnivoice" and prec in ("q8", "q4"):
                import mlx.core as mx
                _model_state.update(progress=f"quantizando backbone p/ {prec}…")
                _quantize_backbone(_model, 8 if prec == "q8" else 4)
                mx.eval(_model.parameters())
            _model_state.update(status="ready", error=None, progress=None)
            _touch_use("tts")
            return _model
        except Exception as exc:  # noqa: BLE001
            _model = None
            _model_state.update(status="error", error=str(exc), progress=None)
            raise


# ref_tokens por voz custam ~1,5s para preparar; cache LRU evita repetir.
# Chave inclui mtime (regravação invalida a voz). Cache pequeno: cada cond
# guarda tensores MLX (prompt acústico+semântico) que ficam na RAM/Metal.
_conds_cache: "OrderedDict[tuple, object]" = OrderedDict()
_CONDS_CACHE_MAX = 4

# Último uso de cada motor (timestamp) — idle unload descarrega o ocioso.
_last_use = {"tts": 0.0, "stt": 0.0, "mt": 0.0, "ser": 0.0}


def _touch_use(*keys: str):
    now = time.time()
    for k in keys:
        _last_use[k] = now


def _cond_for(model, voice_id: str, voice_path: Path):
    ref_max = _clamp(_settings["omni_ref_max_s"], 3.0, 30.0, OMNI_REF_MAX_S)
    key = (voice_id, voice_path.stat().st_mtime_ns, round(ref_max, 1))
    cached = _conds_cache.get(key)
    if cached is not None:
        _conds_cache.move_to_end(key)
        return cached

    # ref_tokens (acústico + semântico) da amostra; reusados em toda geração.
    # ref_text=None aqui mantém a amostra curta (corta só acima de 20s) — ref curta
    # clona melhor e mais rápido. A transcrição da voz vai ao generate() (ref_text),
    # que é onde de fato melhora a clonagem.
    from mlx_audio.tts.models.omnivoice.utils import create_voice_clone_prompt

    cond = create_voice_clone_prompt(
        str(voice_path), ref_text=None,
        tokenizer=model.audio_tokenizer, max_duration_s=ref_max,
    )
    _conds_cache[key] = cond
    while len(_conds_cache) > _CONDS_CACHE_MAX:
        _conds_cache.popitem(last=False)
    return cond


def _generate_chunk(model, text: str, language: str, conds, ref_text, omni: dict,
                    ref_audio: str | None = None, family: str | None = None,
                    meta: dict | None = None, sr: int | None = None):
    """Gera um trecho com o adapter da família do backend ativo."""
    o = omni or {}
    be_family = family or _current_backend()["family"]
    be_meta = meta if meta is not None else _current_backend().get("meta") or {}
    # OmniVoice: passa o language cru (generate_with_backend resolve "None"/códigos).
    # Outros: passa o valor da UI (pt/en/auto).
    audio = generate_with_backend(
        model, be_family, text,
        language=language,
        ref_audio=ref_audio,
        ref_text=ref_text,
        ref_tokens=conds,
        omni=o,
        meta=be_meta,
    )
    # velocidade: time-stretch só se o backend NÃO aplicou speed nativo
    # (senão fish/chatterbox/qwen ficavam com velocidade²). Com sr, usa o
    # atempo do ffmpeg (qualidade >> phase vocoder em fala).
    speed = float(o.get("speed") or 1.0)
    if abs(speed - 1.0) > 1e-3 and be_family not in NATIVE_SPEED_FAMILIES:
        audio = _time_stretch(audio, speed, sr)
    return audio


def _denoise_audio(audio, sr: int, strength: float = 0.7):
    """Limpa ruído de fundo estacionário (hiss/zumbido/AC) da amostra de voz por
    spectral gating: estima o perfil de ruído nos quadros mais silenciosos e
    subtrai por banda, com piso e suavização p/ evitar 'musical noise'. Passa-alta
    em 70 Hz tira rumble. strength 0..1 = agressividade. numpy/scipy, sem deps."""
    import numpy as np
    from scipy.ndimage import uniform_filter
    from scipy.signal import butter, istft, sosfilt, stft

    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size < sr // 5:                       # < 0.2s: nada a fazer
        return x
    s = float(max(0.0, min(1.0, strength)))
    if s <= 0.0:
        return x

    # passa-alta 70 Hz (rumble/AC) antes da subtração espectral
    sos = butter(2, 70.0 / (sr / 2), btype="high", output="sos")
    x = sosfilt(sos, x).astype(np.float32)

    nperseg = 1024
    nover = nperseg * 3 // 4
    f, t, Z = stft(x, fs=sr, nperseg=nperseg, noverlap=nover)
    mag, phase = np.abs(Z), np.angle(Z)

    # quadros mais silenciosos (20% de menor energia) = estimativa do ruído por banda
    energy = mag.mean(axis=0)
    cut = np.percentile(energy, 20)
    noise = mag[:, energy <= cut]
    if noise.shape[1] < 4:
        noise = mag
    n_mean = noise.mean(axis=1, keepdims=True)
    n_std = noise.std(axis=1, keepdims=True)

    beta = 1.0 + 1.6 * s                        # sobre-subtração 1.0..2.6
    floor = 0.18 * (1.0 - s) + 0.04             # piso residual 0.22..0.04
    n_est = n_mean + 1.5 * n_std
    gain = 1.0 - beta * n_est / (mag + 1e-8)
    gain = np.clip(gain, floor, 1.0)
    gain = uniform_filter(gain, size=(2, 3))    # suaviza em freq/tempo

    Z2 = gain * mag * np.exp(1j * phase)
    _, y = istft(Z2, fs=sr, nperseg=nperseg, noverlap=nover)
    y = np.asarray(y, dtype=np.float32)

    peak = float(np.abs(y).max() or 0.0)
    if peak > 0.99:                             # evita clip pós-processo
        y *= 0.99 / peak
    return y


def _anomalo(audio, sr: int, chunk: str, speed: float = 1.0) -> bool:
    """Geração descarrilada = inaudível ou curta demais para o texto.

    OmniVoice é masked-diffusion não-AR (sem teto de tokens nem EOS frágil): a
    duração é estimada internamente e varia mais legitimamente, então só
    truncamento grosseiro e áudio inaudível pedem nova tentativa. Com speed>1
    o time-stretch encurta o áudio — o limiar acompanha, senão velocidade alta
    falsifica 'truncamento' e dispara retry (com jitter de seed) à toa.
    """
    import numpy as np

    if float(np.sqrt(np.mean(audio**2))) < 0.01:  # inaudível
        return True
    return len(audio) / sr < len(chunk) / 45 / max(1.0, float(speed or 1.0))


def _apply_audio_fx(audio, sr: int):
    """EQ/ganho das settings (ver common.apply_audio_fx) — worker e remoto
    recebem os mesmos valores via cfg/omni."""
    return apply_audio_fx(
        audio, sr,
        g_low=float(_settings.get("audio_eq_low_db", 0.0)),
        g_mid=float(_settings.get("audio_eq_mid_db", 0.0)),
        g_high=float(_settings.get("audio_eq_high_db", 0.0)),
        gain_db=float(_settings.get("audio_gain_db", 0.0)),
    )


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Vozes: voices/<id>.wav + voices/<id>.json
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Liveness p/ monitoramento externo — sem auth, sem detalhe interno."""
    return {"ok": True}


def _git_version() -> str:
    """Hash curto do commit em execução — cai para 'dev' fora de um repo."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True,
            text=True, timeout=3, cwd=BASE,
        ).stdout.strip() or "dev"
    except Exception:  # noqa: BLE001
        return "dev"


_VERSION = _git_version()


@app.get("/api/status")
def status():
    st = dict(_model_state)
    st["version"] = _VERSION
    try:
        be = _current_backend()
        st.setdefault("family", be["family"])
        st.setdefault("backend_id", be["id"])
        st["backend_label"] = be["meta"].get("label")
    except Exception:  # noqa: BLE001
        pass
    # fila de falas: útil p/ clientes e UI verem se há espera
    st["speech_queue"] = bool(_settings.get("speech_queue"))
    st["speech_queue_depth"] = _speech_gate.depth if st["speech_queue"] else 0
    st["speech_queue_free_in"] = round(_speech_gate.free_in, 1) if st["speech_queue"] else 0.0
    st["speech_queue_last_duration"] = (
        round(_speech_gate.last_duration, 2) if st["speech_queue"] else 0.0
    )
    st["api_auth_enabled"] = _auth_enabled()
    with _apikeys_lock:
        st["api_keys_count"] = len(_apikeys.get("keys") or [])
    return st


# ---------------------------------------------------------------------------
# Gestão de chaves de API (UI: Configurações → Acesso)
# ---------------------------------------------------------------------------

@app.get("/api/apikeys")
def list_apikeys(request: Request, reveal: bool = False):
    """Lista chaves. `reveal=1` devolve o secret completo (só localhost)."""
    local = request.client and request.client.host in ("127.0.0.1", "::1")
    do_reveal = bool(reveal) and bool(local)
    with _apikeys_lock:
        rows = [_public_key_row(k, reveal=do_reveal) for k in (_apikeys.get("keys") or [])]
        enabled = bool(_apikeys.get("enabled", True))
    env_row = None
    if _ENV_API_KEY:
        # só lista o env se não estiver já entre as chaves gerenciadas
        with _apikeys_lock:
            already = any(k.get("secret") == _ENV_API_KEY for k in (_apikeys.get("keys") or []))
        if not already:
            env_row = _public_key_row({
                "id": "__env__",
                "name": "TTS_ROD_API_KEY (ambiente)",
                "secret": _ENV_API_KEY,
                "created_at": "",
                "readonly": True,
            }, reveal=do_reveal)
    return {
        "enabled": enabled,
        "auth_active": _auth_enabled(),
        "keys": rows,
        "env_key": env_row,
        "can_reveal": bool(local),
    }


@app.post("/api/apikeys")
def create_apikey(payload: dict):
    """Cria chave. Devolve o secret completo uma vez."""
    payload = payload or {}
    name = str(payload.get("name") or "nova chave").strip()[:64] or "nova chave"
    kid = uuid.uuid4().hex[:10]
    secret = _new_api_secret()
    row = {
        "id": kid,
        "name": name,
        "secret": secret,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _apikeys_lock:
        _apikeys.setdefault("keys", []).append(row)
        _save_apikeys()
    global API_KEY
    API_KEY = _primary_api_key() or _ENV_API_KEY or None
    return {"ok": True, "key": _public_key_row(row, reveal=True)}


@app.patch("/api/apikeys/{key_id}")
def rename_apikey(key_id: str, payload: dict):
    payload = payload or {}
    name = str(payload.get("name") or "").strip()[:64]
    if not name:
        raise HTTPException(400, "Nome vazio")
    if key_id == "__env__":
        raise HTTPException(400, "Chave de ambiente não pode ser renomeada")
    with _apikeys_lock:
        for k in _apikeys.get("keys") or []:
            if k["id"] == key_id:
                k["name"] = name
                _save_apikeys()
                return {"ok": True, "key": _public_key_row(k)}
    raise HTTPException(404, "Chave não encontrada")


@app.post("/api/apikeys/{key_id}/rotate")
def rotate_apikey(key_id: str):
    """Gera novo secret. A chave antiga deixa de valer na hora."""
    if key_id == "__env__":
        raise HTTPException(400, "Chave de ambiente não pode ser rotacionada pela UI")
    with _apikeys_lock:
        for k in _apikeys.get("keys") or []:
            if k["id"] == key_id:
                k["secret"] = _new_api_secret()
                k["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_apikeys()
                global API_KEY
                API_KEY = _primary_api_key() or _ENV_API_KEY or None
                return {"ok": True, "key": _public_key_row(k, reveal=True)}
    raise HTTPException(404, "Chave não encontrada")


@app.delete("/api/apikeys/{key_id}")
def delete_apikey(key_id: str):
    if key_id == "__env__":
        raise HTTPException(400, "Chave de ambiente não pode ser apagada pela UI")
    with _apikeys_lock:
        keys = _apikeys.get("keys") or []
        kept = [k for k in keys if k["id"] != key_id]
        if len(kept) == len(keys):
            raise HTTPException(404, "Chave não encontrada")
        _apikeys["keys"] = kept
        _save_apikeys()
    global API_KEY
    API_KEY = _primary_api_key() or _ENV_API_KEY or None
    return {"ok": True, "remaining": len(kept), "auth_active": _auth_enabled()}


@app.post("/api/apikeys/enabled")
def set_apikeys_enabled(payload: dict):
    """Liga/desliga a exigência de chave na rede."""
    payload = payload or {}
    if "enabled" not in payload:
        raise HTTPException(400, "Campo 'enabled' obrigatório")
    with _apikeys_lock:
        _apikeys["enabled"] = bool(payload["enabled"])
        _save_apikeys()
        enabled = bool(_apikeys["enabled"])
    return {"ok": True, "enabled": enabled, "auth_active": _auth_enabled()}


@app.get("/api/backends")
def api_backends():
    """Catálogo de backends TTS disponíveis (atalhos da UI + metadados)."""
    current = _current_backend()
    return {
        "current": current["id"],
        "current_family": current["family"],
        "current_path": current["path"],
        "backends": list_backends(),
    }


# Estado do roteador de microfone: "app" = a chamada ouve o TTS (voz do app);
# "real" = a chamada ouve o mic real -> o navegador silencia o TTS pra não somar.
# Em memória; volta a "app" quando o servidor reinicia (sem estado preso).
_mic_route = {"mode": "app"}


@app.get("/api/mic-route")
def get_mic_route():
    return _mic_route


@app.post("/api/mic-route")
async def set_mic_route(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    mode = (data or {}).get("mode")
    if mode not in ("app", "real"):
        raise HTTPException(400, "mode deve ser 'app' ou 'real'")
    _mic_route["mode"] = mode
    return _mic_route


@app.get("/api/client/mic-router")
def download_mic_router(request: Request):
    """Empacota o cliente roteador de microfone (client/) num .zip pra download.

    Injeta um config.json com o ENDEREÇO deste servidor (o header Host = como o
    navegador chegou aqui, já é o IP/hostname certo p/ a outra máquina) + a chave
    da API, pra o cliente avisar o modo sem config manual. Sob proxy por path
    (ex.: /ttsproxy), o prefixo vem no próprio path e é preservado na URL.
    """
    import io
    import zipfile

    src = BASE / "client"
    if not src.is_dir():
        raise HTTPException(404, "Cliente não encontrado")

    host = request.headers.get("host") or "127.0.0.1:7860"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    url_path = request.scope.get("tts_prefix_path") or request.url.path
    sufixo = "/api/client/mic-router"
    prefixo = url_path[: -len(sufixo)] if url_path.endswith(sufixo) else ""
    cfg = {"server_url": f"{scheme}://{host}{prefixo}", "api_key": _primary_api_key() or ""}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            # só os fontes; nada de venv/caches/config antigo
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            if rel.parts and rel.parts[0] in (".venv", "__pycache__"):
                continue
            if p.suffix == ".pyc" or rel.name == "config.json":
                continue
            z.write(p, Path("mic-router") / rel)
        z.writestr("mic-router/config.json", json.dumps(cfg, indent=2))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tts-studio-mic-router.zip"'},
    )


# ── Conversa: decide por voz/chat o texto que o agente vai falar ───────────
# Consumo principal: The Dudes (agentes) — mas a tela de teste usa o mesmo contrato.
# Sessões stateful: start → posts até status "confirmed" → text.
CHAT_TTL = 3600          # sessão expira 1h sem uso
CHAT_MAX_MSGS = 60       # teto de mensagens enviadas ao LLM por rodada
_chat_sessions: dict = {}
_chat_lock = threading.Lock()

CHAT_SYSTEM = (
    "Você ajuda a decidir o TEXTO FINAL que será falado por um agente de voz (TTS). "
    "Converse em português, curto e objetivo: entenda o objetivo, faça perguntas de "
    "esclarecimento, proponha rascunhos e incorpore o feedback. É CONVERSA POR VOZ: "
    "responda em no máximo 2 frases curtas, como num papo por WhatsApp. FLUXO "
    "OBRIGATÓRIO: 1) proponha o rascunho e pergunte se está aprovado; 2) só marque "
    "final=true quando o usuário disser EXPLICITAMENTE para enviar (ex.: 'pode mandar', "
    "'envia', 'aprovado') DEPOIS de você ter proposto um rascunho. Comentários, ajustes e "
    "frases ambíguas NÃO são confirmação — nesses casos responda APENAS com JSON: "
    "{\"final\": false, \"reply\": \"<sua mensagem conversacional>\"}. "
    "Confirmação explícita → APENAS com JSON: "
    "{\"final\": true, \"text\": \"<texto aprovado, pronto p/ fala>\"}. "
    "O texto final deve conter só o que será falado — sem comentários sobre a conversa."
)


def _chat_provider() -> tuple[str, str, str]:
    """(base_url, model, api_key) efetivos — chat_* com fallback p/ tradução."""
    base = (_settings.get("chat_base_url") or "").strip() \
        or (_settings.get("remote_base_url") or "").strip()
    model = (_settings.get("chat_model") or "").strip() \
        or (_settings.get("remote_translate_model") or "gpt-4o-mini")
    key = (_settings.get("chat_api_key") or "").strip() \
        or (_settings.get("remote_api_key") or "").strip()
    if not base:
        raise HTTPException(400, "Provedor de conversa não configurado "
                                 "(Configurações → Rede: Base URL OpenAI-compat)")
    return base.rstrip("/"), model, key


def _chat_llm(messages: list) -> str:
    """Chama /chat/completions do provedor e devolve o conteúdo da resposta."""
    import urllib.request
    base, model, key = _chat_provider()
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (compatible; tts-studio/1.0)"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # JSON extra do usuário sobrepõe/adiciona (reasoning_effort, top_p…)
    try:
        extra = json.loads(_settings.get("chat_extra") or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except (ValueError, TypeError):
        extra = {}

    def _corpo(effort: str | None) -> bytes:
        corpo = {"model": model, "messages": messages, "temperature": 0.4, **extra}
        if effort and "reasoning_effort" not in corpo:  # GLM/vLLM queimam tokens em reasoning
            corpo["reasoning_effort"] = effort
        return json.dumps(corpo).encode()

    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — sem certifi, segue com o trust store padrão
        ctx = ssl.create_default_context()

    ultimo_erro = ""
    # com reasoning_effort fixado pelo usuário não há retry (corpo idêntico não muda nada)
    for effort in ((None,) if "reasoning_effort" in extra else ("low", None)):
        req = urllib.request.Request(f"{base}/chat/completions", data=_corpo(effort),
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                dados = json.loads(resp.read())
            return dados["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            ultimo_erro = f"HTTP {e.code}"
            if e.code not in (400, 422):
                raise HTTPException(502, f"Provedor de conversa falhou: HTTP {e.code}")
            # 400/422: provedor não aceita reasoning_effort — tenta sem o campo
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Provedor de conversa falhou: {e}")
    raise HTTPException(502, f"Provedor de conversa falhou: {ultimo_erro}")


def _chat_parse(conteudo: str) -> dict:
    """Extrai o JSON da resposta (tolerante a ```json e texto solto)."""
    txt = (conteudo or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", txt, re.S)
    if m:
        txt = m.group(1)
    try:
        d = json.loads(txt)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    i, j = (conteudo or "").find("{"), (conteudo or "").rfind("}")
    if 0 <= i < j:
        try:
            d = json.loads(conteudo[i:j + 1])
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {"final": False, "reply": (conteudo or "").strip()}


def _chat_get(sid: str) -> dict:
    with _chat_lock:
        s = _chat_sessions.get(sid)
        if not s:
            raise HTTPException(404, "Sessão de conversa não encontrada (expirou?)")
        if time.time() - s["updated"] > CHAT_TTL:
            _chat_sessions.pop(sid, None)
            raise HTTPException(404, "Sessão de conversa expirou")
        s["updated"] = time.time()
        return s


def _chat_purge(now: float) -> None:
    for sid in [k for k, d in _chat_sessions.items() if now - d["updated"] > CHAT_TTL]:
        _chat_sessions.pop(sid, None)


def _chat_worker(sid: str) -> None:
    """Chama o LLM em background e atualiza a sessão (status thinking → reply).
    Com teto hard de 150s: provedor pendurado não deixa a sessão em 'thinking'
    para sempre."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def _rodar() -> None:
        try:
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if not s:
                    return
                hist = s["messages"][:]
                if len(hist) > CHAT_MAX_MSGS + 1:
                    hist = [hist[0]] + hist[-(CHAT_MAX_MSGS + 1):]
            sys_prompt = (_settings.get("chat_system") or "").strip() or CHAT_SYSTEM
            reply = _chat_llm([{"role": "system", "content": sys_prompt}] + hist)
            parsed = _chat_parse(reply)
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if not s:
                    return
                if parsed.get("final") and parsed.get("text"):
                    s["status"], s["text"] = "confirmed", str(parsed["text"])
                    s["reply"] = s["text"]
                    s["last_text"] = s["text"]   # último texto aprovado (persiste entre rodadas)
                else:
                    s["reply"] = str(parsed.get("reply") or "")
                    s["messages"].append({"role": "assistant", "content": s["reply"]})
                s["thinking"] = False
                s["updated"] = time.time()
        except Exception as e:  # noqa: BLE001 — erro vai pra sessão, não derruba o server
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if s:
                    s["error"] = str(e)[:200]
                    s["thinking"] = False

    try:
        ex.submit(_rodar).result(timeout=150)
    except concurrent.futures.TimeoutError:
        with _chat_lock:
            s = _chat_sessions.get(sid)
            if s and s.get("thinking"):
                s["error"] = "Provedor de IA demorou demais (150s) — tente de novo"
                s["thinking"] = False
    finally:
        ex.shutdown(wait=False)


@app.post("/api/chat/start")
def chat_start(payload: dict):
    """Abre sessão p/ decidir, conversando, o texto que o agente vai falar.
    Assíncrono: responde na hora e a fala da IA chega via GET (polling)."""
    objetivo = str((payload or {}).get("objective") or "").strip()
    if not objetivo:
        raise HTTPException(400, "Campo 'objective' obrigatório")
    contexto = str((payload or {}).get("context") or "").strip()
    sid = uuid.uuid4().hex[:12]
    msgs = [{"role": "user",
             "content": f"Objetivo: {objetivo}" + (f"\nContexto: {contexto}" if contexto else "")}]
    now = time.time()
    with _chat_lock:
        _chat_purge(now)
        _chat_sessions[sid] = {"objective": objetivo, "context": contexto,
                               "messages": msgs, "status": "chatting", "text": "",
                               "reply": "", "thinking": True, "error": "", "last_text": "",
                               "created": now, "updated": now}
    threading.Thread(target=_chat_worker, args=(sid,), daemon=True).start()
    return {"session_id": sid, "status": "thinking"}


@app.post("/api/chat/{sid}")
def chat_message(sid: str, payload: dict):
    """Fala nova do humano (transcrição do STT). A resposta da IA chega pelo
    GET — assíncrono p/ não estourar timeouts de proxy/Cloudflare."""
    s = _chat_get(sid)
    msg = str((payload or {}).get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Campo 'message' obrigatório")
    with _chat_lock:
        if s.get("thinking"):
            raise HTTPException(409, "IA ainda processando a fala anterior")
        s["messages"].append({"role": "user", "content": msg})
        s["status"], s["text"] = "chatting", ""
        s["reply"], s["error"] = "", ""
        s["thinking"] = True
        s["updated"] = time.time()
    threading.Thread(target=_chat_worker, args=(sid,), daemon=True).start()
    return {"ok": True, "status": "thinking"}


@app.get("/api/chat/{sid}")
def chat_get(sid: str):
    s = _chat_get(sid)
    with _chat_lock:
        status = "thinking" if s.get("thinking") else s["status"]
        return {"session_id": sid, "status": status, "text": s["text"],
                "reply": s.get("reply", ""), "error": s.get("error", ""),
                "last_text": s.get("last_text", ""),
                "objective": s["objective"],
                "messages": [{"role": m["role"], "content": m["content"]} for m in s["messages"]]}


@app.delete("/api/chat/{sid}")
def chat_delete(sid: str):
    with _chat_lock:
        removida = _chat_sessions.pop(sid, None)
    if removida is None:
        raise HTTPException(404, "Sessão de conversa não encontrada")
    return {"ok": True}


@app.post("/api/chat-debug")
def chat_debug(payload: dict):
    """Telemetria temporária do fluxo Conversa no navegador (diagnóstico)."""
    try:
        with open("/tmp/tts-chat-debug.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {json.dumps(payload, ensure_ascii=False)}\n")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


# ── Túnel / acesso pela internet (proxy por path) ──────────────────────────
TUNNEL_LABEL = "studio.tts.tunnel"


def _tunnel_proc_running() -> bool:
    """Há um processo ssh do túnel (-R 127.0.0.1:7860) vivo nesta máquina?"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", r"ssh .*-R 127\.0\.0\.1:7860"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _tunnel_launchd_loaded() -> bool | None:
    """LaunchAgent do túnel carregado? None se não for macOS/sem launchctl."""
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        return None
    try:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{TUNNEL_LABEL}"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return None


def _public_proxy_check(url: str, timeout: float = 6.0) -> dict:
    """Checa a URL pública do proxy a partir daqui (ida e volta completa:
    Mac → VPS → túnel → Mac). Responde {ok, status_code, latency_ms, error}."""
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL inválida (use http/https)"}
    try:
        import urllib.request
        req = urllib.request.Request(f"{url}/health", method="GET")
        req.add_header("User-Agent", "tts-studio-tunnel-check")
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(200)
            lat = round((time.monotonic() - t0) * 1000)
            ok = resp.status == 200 and b'"ok"' in body
            return {"ok": ok, "status_code": resp.status,
                    "latency_ms": lat, "error": None if ok else "resposta inesperada"}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e)[:200]}


@app.get("/api/tunnel/status")
def tunnel_status(url: str = ""):
    """Status do acesso pela internet: processo do túnel, LaunchAgent e
    checagem fim a fim da URL pública (passada pela UI)."""
    pub = _public_proxy_check(url) if url.strip() else None
    return {
        "tunnel_running": _tunnel_proc_running(),
        "launchd_loaded": _tunnel_launchd_loaded(),
        "public_check": pub,
    }


def _tunnel_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{TUNNEL_LABEL}.plist"


def _launchctl(args: list, timeout: int = 10) -> None:
    """launchctl com tratamento comum: erro -> HTTPException 500."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, text=True)
        if r.returncode != 0:
            raise HTTPException(500, f"launchctl: {(r.stderr or r.stdout or 'erro').strip()[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Falha no launchctl: {e}")


def _tunnel_exige_macos() -> None:
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        raise HTTPException(400, "Disponível apenas no macOS com o LaunchAgent instalado (tunnel.sh install)")


@app.post("/api/tunnel/restart")
def tunnel_restart():
    """Reinicia o LaunchAgent do túnel (kickstart). Exige macOS + agente."""
    _tunnel_exige_macos()
    _launchctl(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{TUNNEL_LABEL}"])
    return {"ok": True, "msg": "Túnel reiniciado (aguarde ~5s e reavalie o status)"}


@app.post("/api/tunnel/stop")
def tunnel_stop():
    """Desliga o acesso pela internet: faz bootout do LaunchAgent (o processo
    ssh morre e o KeepAlive não reergue). A rede local continua valendo."""
    _tunnel_exige_macos()
    _launchctl(["launchctl", "bootout", f"gui/{os.getuid()}/{TUNNEL_LABEL}"])
    return {"ok": True, "msg": "Túnel desligado — URL pública fora do ar"}


@app.post("/api/tunnel/start")
def tunnel_start():
    """Religa o acesso pela internet: bootstrap do LaunchAgent."""
    _tunnel_exige_macos()
    if not _tunnel_plist().exists():
        raise HTTPException(400, "LaunchAgent não instalado — rode ./tunnel.sh install")
    _launchctl(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(_tunnel_plist())])
    return {"ok": True, "msg": "Túnel ligado — aguarde ~5s e teste a URL pública"}


@app.post("/api/shutdown")
def shutdown():
    """Desliga o servidor (botão na UI). Reiniciar: dois cliques em TTS-STUDIO.command."""
    def _stop():
        time.sleep(0.4)  # deixa a resposta HTTP voltar antes de sair
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True, "msg": "Servidor desligando…"}


@app.get("/api/settings")
def get_settings():
    st = dict(_settings)
    st["chat_system_default"] = CHAT_SYSTEM  # p/ a UI exibir o preprompt padrão
    return st


def _clamp(value, lo, hi, default):
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return default


def _resolve_duration_s(v, default):
    """None/vazio/0 = duração automática; senão clampa em 0,5–60 s."""
    if v in (None, "", 0, "0"):
        return None
    try:
        return min(60.0, max(0.5, float(v)))
    except (TypeError, ValueError):
        return default


# Vocabulário FECHADO do instruct do OmniVoice (igual ao _resolve_instruct do modelo).
# Qualquer item fora disto quebra a geração no servidor (clone vira voz default),
# então sanitizamos antes de enviar — emoção em texto livre é descartada.
_OMNI_INSTRUCT_VALID = {
    "male", "female", "child", "teenager", "young adult", "middle-aged", "elderly",
    "very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch", "whisper",
    "american accent", "british accent", "australian accent", "canadian accent", "indian accent",
    "japanese accent", "korean accent", "portuguese accent", "russian accent", "chinese accent",
}


def _sanitize_instruct(s) -> str:
    """Mantém só tags válidas do OmniVoice (descarta texto livre/emoção)."""
    if not s:
        return ""
    seen, out = set(), []
    for tok in str(s).split(","):
        t = tok.strip().lower()
        if t in _OMNI_INSTRUCT_VALID and t not in seen:
            seen.add(t)
            out.append(t)
    return ", ".join(out)


def _resolve_omni(payload: dict, family: str | None = None) -> dict:
    """Resolve controles de geração p/ todos os backends.

    Campos OmniVoice (num_steps, guidance…) + gen_* multi-backend (temperature,
    top_p, exaggeration, speakers…). instruct: tags fechadas só no OmniVoice.
    `family` opcional — quando o request sobrescreve o modelo.
    """
    fam = family or _current_backend()["family"]
    raw_instruct = payload["instruct"] if payload.get("instruct") is not None \
        else _settings.get("omni_instruct", "")
    if fam == "omnivoice":
        instruct = _sanitize_instruct(raw_instruct)
    else:
        instruct = str(raw_instruct or "").strip()[:500]

    def _pick(key, setting_key, lo, hi, default, as_int=False):
        """payload[key] > settings[setting_key] > default, com clamp."""
        if key in payload and payload[key] is not None:
            v = payload[key]
        elif setting_key in payload and payload[setting_key] is not None:
            v = payload[setting_key]
        else:
            v = _settings.get(setting_key, default)
        n = _clamp(v, lo, hi, default)
        return int(n) if as_int else n

    return {
        # OmniVoice / VoxCPM2
        "num_steps": _pick("num_steps", "omni_num_steps", 4, 64, 16, as_int=True),
        "guidance_scale": _pick("guidance_scale", "omni_guidance_scale", 0.0, 10.0, 2.0),
        "class_temperature": _pick("class_temperature", "omni_class_temperature", 0.0, 2.0, 0.0),
        "position_temperature": _pick("position_temperature", "omni_position_temperature", 0.0, 20.0, 5.0),
        "layer_penalty_factor": _pick("layer_penalty_factor", "omni_layer_penalty_factor", 0.0, 20.0, 5.0),
        "t_shift": _pick("t_shift", "omni_t_shift", 0.0, 1.0, 0.1),
        "denoise": bool(payload["denoise"]) if "denoise" in payload else _settings.get("omni_denoise", True),
        "preprocess_prompt": bool(payload["preprocess_prompt"]) if "preprocess_prompt" in payload else _settings.get("omni_preprocess_prompt", True),
        "postprocess_output": bool(payload["postprocess_output"]) if "postprocess_output" in payload else _settings.get("omni_postprocess_output", True),
        "audio_chunk_duration": _pick("audio_chunk_duration", "omni_audio_chunk_duration", 1.0, 60.0, 15.0),
        "audio_chunk_threshold": _pick("audio_chunk_threshold", "omni_audio_chunk_threshold", 5.0, 120.0, 30.0),
        "instruct": instruct,
        "duration_s": (_resolve_duration_s(payload["duration_s"], _settings["omni_duration_s"])
                       if "duration_s" in payload else _settings["omni_duration_s"]),
        "speed": _clamp(payload.get("speed"), 0.25, 4.0, _settings["speed"]),
        "seed": int(payload["seed"]) if str(payload.get("seed", "")).lstrip("-").isdigit()
                else int(_settings.get("omni_seed", 42)),
        # multi-backend
        "temperature": _pick("temperature", "gen_temperature", 0.0, 2.0, 0.8),
        "top_p": _pick("top_p", "gen_top_p", 0.05, 1.0, 0.95),
        "top_k": _pick("top_k", "gen_top_k", 0, 500, 50, as_int=True),
        "repetition_penalty": _pick("repetition_penalty", "gen_repetition_penalty", 1.0, 2.5, 1.1),
        "max_tokens": _pick("max_tokens", "gen_max_tokens", 64, 8192, 2048, as_int=True),
        "exaggeration": _pick("exaggeration", "gen_exaggeration", 0.0, 2.0, 0.5),
        "cfg_weight": _pick("cfg_weight", "gen_cfg_weight", 0.0, 1.0, 0.5),
        "min_p": _pick("min_p", "gen_min_p", 0.0, 0.5, 0.05),
        "chunk_length": _pick("chunk_length", "gen_chunk_length", 50, 600, 300, as_int=True),
        "speaker": str(payload.get("speaker") or payload.get("gen_speaker")
                       or _settings.get("gen_speaker") or "Ryan"),
        "kokoro_voice": str(payload.get("kokoro_voice") or payload.get("gen_kokoro_voice")
                            or _settings.get("gen_kokoro_voice") or "af_heart"),
        "pocket_voice": str(payload.get("pocket_voice") or payload.get("gen_pocket_voice")
                            or _settings.get("gen_pocket_voice") or "alba"),
        "voxtral_voice": str(payload.get("voxtral_voice") or payload.get("gen_voxtral_voice")
                             or _settings.get("gen_voxtral_voice") or "casual_male"),
    }


@app.post("/api/settings")
def update_settings(payload: dict):
    if "model" in payload:
        m = str(payload["model"] or "").strip()
        if m:
            _settings["model"] = m  # carregado (e baixado/montado) na próxima geração
    if "pre_prompt" in payload:
        _settings["pre_prompt"] = str(payload["pre_prompt"] or "").strip()[:500]
    if "language" in payload:
        _settings["language"] = str(payload["language"] or "auto").lower()[:16]
    if "default_voice" in payload:
        v = payload["default_voice"]
        _settings["default_voice"] = v if v and (VOICES_DIR / f"{v}.wav").exists() else None
    if "chunk_max_chars" in payload:
        _settings["chunk_max_chars"] = int(_clamp(payload["chunk_max_chars"], 60, 200, 140))
    if "speed" in payload:
        _settings["speed"] = _clamp(payload["speed"], 0.25, 4.0, 1.0)
    if "auto_cleanup" in payload:
        _settings["auto_cleanup"] = bool(payload["auto_cleanup"])
    if "auto_cleanup_minutes" in payload:
        _settings["auto_cleanup_minutes"] = int(_clamp(payload["auto_cleanup_minutes"], 1, 1440, 15))
    if "omni_num_steps" in payload:
        _settings["omni_num_steps"] = int(_clamp(payload["omni_num_steps"], 4, 64, 16))
    if "omni_guidance_scale" in payload:
        _settings["omni_guidance_scale"] = _clamp(payload["omni_guidance_scale"], 0.0, 10.0, 2.0)
    if "omni_class_temperature" in payload:
        _settings["omni_class_temperature"] = _clamp(payload["omni_class_temperature"], 0.0, 2.0, 0.0)
    if "omni_position_temperature" in payload:
        _settings["omni_position_temperature"] = _clamp(payload["omni_position_temperature"], 0.0, 20.0, 5.0)
    if "omni_layer_penalty_factor" in payload:
        _settings["omni_layer_penalty_factor"] = _clamp(payload["omni_layer_penalty_factor"], 0.0, 20.0, 5.0)
    if "omni_t_shift" in payload:
        _settings["omni_t_shift"] = _clamp(payload["omni_t_shift"], 0.0, 1.0, 0.1)
    for chave in ("omni_denoise", "omni_preprocess_prompt", "omni_postprocess_output"):
        if chave in payload:
            _settings[chave] = bool(payload[chave])
    if "omni_audio_chunk_duration" in payload:
        _settings["omni_audio_chunk_duration"] = _clamp(payload["omni_audio_chunk_duration"], 1.0, 60.0, 15.0)
    if "omni_audio_chunk_threshold" in payload:
        _settings["omni_audio_chunk_threshold"] = _clamp(payload["omni_audio_chunk_threshold"], 5.0, 120.0, 30.0)
    if "omni_instruct" in payload:
        _settings["omni_instruct"] = str(payload["omni_instruct"] or "").strip()[:300]
    if "omni_seed" in payload:
        try:
            _settings["omni_seed"] = max(-1, min(2**31 - 1, int(payload["omni_seed"])))
        except (TypeError, ValueError):
            pass
    if "omni_duration_s" in payload:
        _settings["omni_duration_s"] = _resolve_duration_s(payload["omni_duration_s"], None)
    if "omni_ref_max_s" in payload:
        _settings["omni_ref_max_s"] = _clamp(payload["omni_ref_max_s"], 3.0, 30.0, 10.0)
    if "omni_precision" in payload:
        p = str(payload["omni_precision"] or "bf16").lower()
        _settings["omni_precision"] = p if p in ("fp32", "bf16", "q8", "q4") else "bf16"
    # multi-backend
    if "gen_temperature" in payload:
        _settings["gen_temperature"] = _clamp(payload["gen_temperature"], 0.0, 2.0, 0.8)
    if "gen_top_p" in payload:
        _settings["gen_top_p"] = _clamp(payload["gen_top_p"], 0.05, 1.0, 0.95)
    if "gen_top_k" in payload:
        _settings["gen_top_k"] = int(_clamp(payload["gen_top_k"], 0, 500, 50))
    if "gen_repetition_penalty" in payload:
        _settings["gen_repetition_penalty"] = _clamp(payload["gen_repetition_penalty"], 1.0, 2.5, 1.1)
    if "gen_max_tokens" in payload:
        _settings["gen_max_tokens"] = int(_clamp(payload["gen_max_tokens"], 64, 8192, 2048))
    if "gen_exaggeration" in payload:
        _settings["gen_exaggeration"] = _clamp(payload["gen_exaggeration"], 0.0, 2.0, 0.5)
    if "gen_cfg_weight" in payload:
        _settings["gen_cfg_weight"] = _clamp(payload["gen_cfg_weight"], 0.0, 1.0, 0.5)
    if "gen_min_p" in payload:
        _settings["gen_min_p"] = _clamp(payload["gen_min_p"], 0.0, 0.5, 0.05)
    if "gen_chunk_length" in payload:
        _settings["gen_chunk_length"] = int(_clamp(payload["gen_chunk_length"], 50, 600, 300))
    if "gen_speaker" in payload:
        _settings["gen_speaker"] = str(payload["gen_speaker"] or "Ryan").strip()[:64]
    if "gen_kokoro_voice" in payload:
        _settings["gen_kokoro_voice"] = str(payload["gen_kokoro_voice"] or "af_heart").strip()[:64]
    if "gen_pocket_voice" in payload:
        _settings["gen_pocket_voice"] = str(payload["gen_pocket_voice"] or "alba").strip()[:64]
    if "gen_voxtral_voice" in payload:
        _settings["gen_voxtral_voice"] = str(payload["gen_voxtral_voice"] or "casual_male").strip()[:64]
    if "voice_denoise" in payload:
        _settings["voice_denoise"] = bool(payload["voice_denoise"])
    if "voice_denoise_strength" in payload:
        _settings["voice_denoise_strength"] = _clamp(payload["voice_denoise_strength"], 0.0, 1.0, 0.7)
    if "audio_gain_db" in payload:
        _settings["audio_gain_db"] = _clamp(payload["audio_gain_db"], -15.0, 15.0, 0.0)
    for chave, lim in (("audio_eq_low_db", 12.0), ("audio_eq_mid_db", 12.0), ("audio_eq_high_db", 12.0)):
        if chave in payload:
            _settings[chave] = _clamp(payload[chave], -lim, lim, 0.0)
    if "stt_min_words" in payload:
        _settings["stt_min_words"] = int(_clamp(payload["stt_min_words"], 0, 10, 1))
    if "stt_min_chars" in payload:
        _settings["stt_min_chars"] = int(_clamp(payload["stt_min_chars"], 0, 40, 2))
    if "stt_max_no_speech" in payload:
        _settings["stt_max_no_speech"] = _clamp(payload["stt_max_no_speech"], 0.0, 1.0, 0.6)
    if "stt_min_logprob" in payload:
        _settings["stt_min_logprob"] = _clamp(payload["stt_min_logprob"], -5.0, 0.0, -1.0)
    if "stt_max_compression" in payload:
        _settings["stt_max_compression"] = _clamp(payload["stt_max_compression"], 1.0, 10.0, 2.4)
    if "stt_beam" in payload:
        _settings["stt_beam"] = int(_clamp(payload["stt_beam"], 1, 10, 5))
    for chave in ("remote_tts", "remote_translate", "remote_stt"):
        if chave in payload:
            _settings[chave] = bool(payload[chave])
    if "remote_tts_url" in payload:
        _settings["remote_tts_url"] = str(payload["remote_tts_url"] or "").strip()[:300]
    if "remote_tts_voice" in payload:
        _settings["remote_tts_voice"] = str(payload["remote_tts_voice"] or "").strip()[:120]
    if "remote_tts_extra" in payload:
        _settings["remote_tts_extra"] = str(payload["remote_tts_extra"] or "").strip()[:2000]
    if "remote_base_url" in payload:
        _settings["remote_base_url"] = str(payload["remote_base_url"] or "").strip()[:300]
    if "remote_api_key" in payload:
        _settings["remote_api_key"] = str(payload["remote_api_key"] or "").strip()[:300]
    if "remote_stt_base_url" in payload:
        _settings["remote_stt_base_url"] = str(payload["remote_stt_base_url"] or "").strip()[:300]
    if "remote_stt_key" in payload:
        _settings["remote_stt_key"] = str(payload["remote_stt_key"] or "").strip()[:300]
    if "chat_base_url" in payload:
        _settings["chat_base_url"] = str(payload["chat_base_url"] or "").strip()[:300]
    if "chat_model" in payload:
        _settings["chat_model"] = str(payload["chat_model"] or "").strip()[:80]
    if "chat_api_key" in payload:
        _settings["chat_api_key"] = str(payload["chat_api_key"] or "").strip()[:300]
    if "chat_system" in payload:
        _settings["chat_system"] = str(payload["chat_system"] or "").strip()[:4000]
    if "chat_extra" in payload:
        txt = str(payload["chat_extra"] or "").strip()[:2000]
        if txt:  # valida: precisa ser JSON objeto
            try:
                if not isinstance(json.loads(txt), dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise HTTPException(400, "Extras do LLM: informe um JSON válido de objeto, ex. {\"reasoning_effort\": \"low\"}")
        _settings["chat_extra"] = txt
    if "speaker_gate" in payload:
        g = str(payload["speaker_gate"] or "off").strip().lower()
        if g not in ("off", "enforce", "label"):
            raise HTTPException(400, "speaker_gate inválido (off|enforce|label)")
        _settings["speaker_gate"] = g
    if "speaker_threshold" in payload:
        _settings["speaker_threshold"] = _clamp(payload["speaker_threshold"], 0.5, 0.95, 0.75)
    for chave in ("remote_tts_model", "remote_translate_model", "remote_stt_model"):
        if chave in payload:
            _settings[chave] = str(payload[chave] or "").strip()[:120]
    if "translate_model" in payload:   # repo MLX do tradutor local (recarrega sob demanda)
        _settings["translate_model"] = str(payload["translate_model"] or "").strip()[:120]
    if "free_local_on_remote" in payload:
        _settings["free_local_on_remote"] = bool(payload["free_local_on_remote"])
    if "idle_unload_minutes" in payload:
        # 0 = nunca descarrega por ociosidade; máx. 24 h
        _settings["idle_unload_minutes"] = int(_clamp(payload["idle_unload_minutes"], 0, 1440, 10))
    if "speech_queue" in payload:
        _settings["speech_queue"] = bool(payload["speech_queue"])
    if "speech_queue_gap_s" in payload:
        _settings["speech_queue_gap_s"] = _clamp(payload["speech_queue_gap_s"], 0.0, 5.0, 0.35)
    _save_settings()
    _autofree_local()                  # se ligado, descarrega já os locais agora redundantes
    return _settings


@app.get("/api/voices")
def list_voices():
    voices = []
    existentes = set()
    for meta_file in sorted(VOICES_DIR.glob("*.json")):
        try:
            m = json.loads(meta_file.read_text())
            if not isinstance(m, dict) or not m.get("id"):
                continue
        except Exception:  # noqa: BLE001 — meta corrompido não pode derrubar a API
            continue
        voices.append(m)
        existentes.add(m["id"])
    voices.sort(key=lambda v: v.get("created_at", ""), reverse=True)
    # presets ainda não materializados entram como entradas virtuais ao final
    for pid, p in OMNI_PRESETS.items():
        if pid not in existentes:
            voices.append({"id": pid, "name": p["name"], "preset": True,
                           "materialized": False, "instruct": p["instruct"],
                           "duration": 0, "created_at": ""})
    return voices


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "on", "yes", "sim")


@app.post("/api/voices/design")
def save_design_voice(payload: dict):
    """Materializa uma voz de VOICE DESIGN (instruct + seed) numa voz SALVA/nomeada.

    Gera uma amostra-semente com a descrição+seed e a salva como voz de referência
    (clone), igual aos presets. A voz passa a aparecer no /api/voices e a ser usável
    por id/nome (inclusive na API), com timbre estável (clonagem da amostra).
    """
    import numpy as np
    import soundfile as sf

    name = (payload.get("name") or "").strip()
    instruct = _sanitize_instruct(payload.get("instruct") if payload.get("instruct")
                                  else _settings.get("omni_instruct") or "")
    if not name:
        raise HTTPException(400, "name obrigatório")
    if not instruct:
        raise HTTPException(400, "instruct obrigatório (descrição da voz, ex.: 'female, young adult, high pitch')")
    seed = (int(payload["seed"]) if str(payload.get("seed", "")).lstrip("-").isdigit()
            else int(_settings.get("omni_seed", 42)))

    omni = {"num_steps": OMNI_STEPS_HQ, "guidance_scale": _settings["omni_guidance_scale"],
            "class_temperature": _settings["omni_class_temperature"],
            "position_temperature": _settings["omni_position_temperature"],
            "layer_penalty_factor": _settings["omni_layer_penalty_factor"],
            "t_shift": _settings["omni_t_shift"], "instruct": instruct,
            "duration_s": None, "speed": 1.0, "seed": seed}

    remote = _use_remote_tts()
    sr = 24000
    try:
        with (_NO_LOCK if remote else _gen_lock):
            if remote:
                audio = _tts_remote_chunk(OMNI_PRESET_SEED, _settings["language"], omni, sr, None)
            else:
                model = _get_model()
                sr = getattr(model, "sample_rate", 24000)
                audio = _generate_chunk(model, OMNI_PRESET_SEED, _settings["language"],
                                        None, None, omni, sr=sr)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Falha ao gerar a amostra do design: {repr(e)[:200]}")

    audio = _normalize(_trim_tail_silence(np.asarray(audio, dtype=np.float32), sr))
    voice_id = uuid.uuid4().hex[:10]
    sf.write(str(VOICES_DIR / f"{voice_id}.wav"), audio, sr, subtype="PCM_16")
    meta = {"id": voice_id, "name": name, "from_design": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": round(len(audio) / sr, 1), "ref_text": OMNI_PRESET_SEED,
            "instruct": instruct, "seed": seed}
    write_json_atomic(VOICES_DIR / f"{voice_id}.json", meta)
    return meta


@app.post("/api/voices")
def create_voice(name: str = Form(...), audio: UploadFile = None, ref_text: str = Form(""),
                 denoise: str = Form("1"), denoise_strength: str = Form("")):
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    import soundfile as sf

    voice_id = uuid.uuid4().hex[:10]
    wav_path = VOICES_DIR / f"{voice_id}.wav"
    tmp = VOICES_DIR / f".up-{voice_id}"
    tmp.write_bytes(audio.file.read())
    try:
        data, sr = sf.read(str(tmp), dtype="float32")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "Áudio inválido")
    tmp.unlink(missing_ok=True)

    # limpa o ruído de fundo NA FONTE: a amostra salva (e os ref_tokens do clone)
    # passam a ser a versão limpa
    do_denoise = _truthy(denoise)
    try:
        strg = float(denoise_strength)
    except (TypeError, ValueError):
        strg = float(_settings.get("voice_denoise_strength", 0.7))
    strg = _clamp(strg, 0.0, 1.0, 0.7)
    if do_denoise:
        data = _denoise_audio(data, sr, strg)
    sf.write(str(wav_path), data, sr, subtype="PCM_16")

    duration = _wav_duration(wav_path)
    if duration < 3:
        wav_path.unlink(missing_ok=True)
        raise HTTPException(400, f"Gravação muito curta ({duration}s). Mínimo 3s, ideal 10–30s.")

    meta = {
        "id": voice_id,
        "name": name.strip() or voice_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": duration,
        "denoised": bool(do_denoise),
    }
    # transcrição opcional da amostra: clonagem do OmniVoice fica mais estável
    if ref_text.strip():
        meta["ref_text"] = ref_text.strip()[:500]
    write_json_atomic(VOICES_DIR / f"{voice_id}.json", meta)
    return meta


@app.post("/api/voices/{voice_id}/denoise")
def denoise_voice(voice_id: str, payload: dict = None):
    """Limpa o ruído de fundo de uma voz JÁ existente, sobrescrevendo o .wav. O
    mtime muda -> o cache de ref_tokens invalida sozinho na próxima geração."""
    import soundfile as sf

    path = VOICES_DIR / f"{voice_id}.wav"
    if not path.exists():
        raise HTTPException(404, "Voz não encontrada")
    strg = _clamp((payload or {}).get("strength"), 0.0, 1.0,
                  float(_settings.get("voice_denoise_strength", 0.7)))
    data, sr = sf.read(str(path), dtype="float32")
    data = _denoise_audio(data, sr, strg)
    sf.write(str(path), data, sr, subtype="PCM_16")
    _conds_cache.clear()
    duration = _wav_duration(path)
    jp = VOICES_DIR / f"{voice_id}.json"
    if jp.exists():
        try:
            m = json.loads(jp.read_text())
            m["duration"] = duration
            m["denoised"] = True
            write_json_atomic(jp, m)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "duration": duration}


@app.get("/api/voices/{voice_id}/audio")
def voice_audio(voice_id: str):
    path = VOICES_DIR / f"{_safe_id(voice_id)}.wav"
    if not path.exists():
        raise HTTPException(404, "Voz não encontrada")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/voices/export")
def export_voices():
    """Backup: zip com todas as vozes (.wav + .json). Botão '⬇ Backup' na UI.

    voices/ é gitignored e é o dado mais valioso do app (gravações + presets
    materializados) — sem isso, apagar o dir perde as vozes para sempre.
    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(VOICES_DIR.iterdir()):
            # só wav/json de voz; ignora temporários de upload (.up-*/.rep-*)
            if p.is_file() and p.suffix in (".wav", ".json") and not p.name.startswith("."):
                z.write(p, f"voices/{p.name}")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tts-studio-vozes.zip"'},
    )


# teto do TOTAL descomprimido no import (zip bomba: o limite por entrada não
# protege contra muitas entradas grandes)
_IMPORT_MAX_TOTAL = 512 * 1024 * 1024


@app.post("/api/voices/import")
async def import_voices(zip_file: UploadFile = File(...)):
    """Restaura um backup gerado por /api/voices/export (zip com .wav + .json).

    Sobrescreve vozes com o mesmo id (é um restore). Só extrai .wav/.json com
    nome seguro (achata subpastas); o resto do zip é ignorado. Arquivos órfãos
    (wav sem json ou vice-versa) entram, mas são avisados no response.
    """
    import io
    import re as _re
    import zipfile

    # teto do arquivo RECEBIDO (read() carrega tudo na RAM; o cap de conteúdo
    # descomprimido vem depois)
    if zip_file.size and zip_file.size > _IMPORT_MAX_TOTAL:
        raise HTTPException(400, "Zip grande demais (máx. 512 MB)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(await zip_file.read()))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Arquivo não é um zip válido")
    importados, ignorados = [], []
    total = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            total += info.file_size
            if total > _IMPORT_MAX_TOTAL:
                raise HTTPException(400, "Zip grande demais (total descomprimido > 512 MB)")
            if info.file_size > 100 * 1024 * 1024:
                ignorados.append(info.filename)
                continue
            nome = Path(info.filename).name
            if not _re.fullmatch(r"[A-Za-z0-9_-]+\.(wav|json)", nome):
                ignorados.append(info.filename)
                continue
            (VOICES_DIR / nome).write_bytes(zf.read(info))
            importados.append(nome)
    if not importados:
        raise HTTPException(400, "Nenhuma voz (.wav/.json) encontrada no zip")
    # órfãos: .wav sem .json (voz some da lista) ou .json sem .wav (clonagem quebra)
    wav_stems = {Path(n).stem for n in importados if n.endswith(".wav")}
    json_stems = {Path(n).stem for n in importados if n.endswith(".json")}
    orfaos = sorted((wav_stems - json_stems) | (json_stems - wav_stems))
    # restore pode ter sobrescrito amostras em uso -> invalida o cache de refs
    _conds_cache.clear()
    return {"ok": True, "importados": len(importados),
            "vozes": len(wav_stems & json_stems),
            "orfaos": orfaos[:10], "ignorados": ignorados[:10]}


def _eq_custom(audio, sr, low_db, mid_db, high_db):
    import numpy as np
    from scipy.signal import sosfilt

    y = np.asarray(audio, dtype=np.float32)
    bands = []
    if abs(float(low_db)) >= 0.05:
        bands.append(_biquad("lowshelf", 150.0, float(low_db), sr))
    if abs(float(mid_db)) >= 0.05:
        bands.append(_biquad("peak", 1500.0, float(mid_db), sr, 1.0))
    if abs(float(high_db)) >= 0.05:
        bands.append(_biquad("highshelf", 5000.0, float(high_db), sr))
    for sos in bands:
        y = sosfilt(np.array([sos], dtype=np.float64), y).astype(np.float32)
    return y.astype(np.float32)


@app.post("/api/audio/edit")
def audio_edit(audio: UploadFile = File(...), op: str = Form(...)):
    """Aplica UMA operação de edição num WAV e devolve o WAV processado.
    op (JSON): {type: trim|cut|normalize|denoise|gain|fade|eq, ...params}."""
    import io as _io
    import json as _json

    import numpy as np
    import soundfile as sf

    try:
        a, sr = sf.read(_io.BytesIO(audio.file.read()), dtype="float32")
    except Exception:
        raise HTTPException(400, "Áudio inválido")
    if a.ndim > 1:
        a = a.mean(axis=1)
    try:
        o = _json.loads(op)
    except Exception:
        raise HTTPException(400, "op inválido (JSON)")
    kind = o.get("type")
    dur = len(a) / sr if sr else 0.0

    if kind == "trim":                       # mantém só a seleção
        i0 = max(0, int(float(o.get("start", 0.0)) * sr))
        i1 = min(len(a), int(float(o.get("end", dur)) * sr))
        if i1 - i0 < int(0.05 * sr):
            raise HTTPException(400, "Seleção muito curta (mín. 50ms)")
        a = a[i0:i1]
    elif kind == "cut":                       # remove a seleção
        i0 = max(0, int(float(o.get("start", 0.0)) * sr))
        i1 = min(len(a), int(float(o.get("end", 0.0)) * sr))
        a = np.concatenate([a[:i0], a[i1:]])
        if a.size < int(0.05 * sr):
            raise HTTPException(400, "Sobrou áudio de menos")
    elif kind == "normalize":
        a = _normalize(a)
    elif kind == "denoise":
        a = _denoise_audio(a, sr, _clamp(o.get("strength", 0.7), 0.0, 1.0, 0.7))
    elif kind == "gain":
        g = float(10 ** (_clamp(o.get("db", 0.0), -24.0, 24.0, 0.0) / 20.0))
        s, e = o.get("start"), o.get("end")
        if s is not None and e is not None:          # ganho só na seleção (envelope com rampa)
            i0 = max(0, int(float(s) * sr))
            i1 = min(len(a), int(float(e) * sr))
            if i1 > i0:
                env = np.ones(len(a), dtype=np.float32)
                env[i0:i1] = g
                ramp = min(int(0.006 * sr), (i1 - i0) // 2)   # 6ms cross-fade nas bordas (sem click)
                if ramp > 0:
                    env[i0:i0 + ramp] = np.linspace(1.0, g, ramp, dtype=np.float32)
                    env[i1 - ramp:i1] = np.linspace(g, 1.0, ramp, dtype=np.float32)
                a = (a * env).astype(np.float32)
        else:
            a = (a * g).astype(np.float32)
    elif kind == "fade":
        a = _fade_edges(a, sr, _clamp(o.get("ms", 12.0), 0.0, 1000.0, 12.0))
    elif kind == "eq":
        a = _eq_custom(a, sr, o.get("low", 0.0), o.get("mid", 0.0), o.get("high", 0.0))
    else:
        raise HTTPException(400, f"op desconhecido: {kind}")

    a = np.asarray(a, dtype=np.float32)
    # trava: soft-limit (linear até 0.98, tanh acima) -> só dobra os picos que
    # estouram; NÃO reescala o áudio inteiro (boost num trecho não baixa o resto).
    over = np.abs(a) > 0.98
    if over.any():
        s = np.sign(a); mag = np.abs(a)
        mag_lim = 0.98 + 0.02 * np.tanh((mag - 0.98) / 0.02)
        a = np.where(over, s * mag_lim, a).astype(np.float32)
    buf = _io.BytesIO()
    sf.write(buf, a, sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"X-Duration": f"{len(a)/sr:.3f}" if sr else "0"})


@app.post("/api/voices/{voice_id}/replace")
def replace_voice_audio(voice_id: str, audio: UploadFile = File(...)):
    """Substitui o áudio de uma voz existente (mantém o id) — usado pelo editor."""
    import soundfile as sf

    wav = VOICES_DIR / f"{voice_id}.wav"
    if not wav.exists():
        raise HTTPException(404, "Voz não encontrada")
    tmp = VOICES_DIR / f".rep-{voice_id}"
    tmp.write_bytes(audio.file.read())
    try:
        a, sr = sf.read(str(tmp), dtype="float32")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "Áudio inválido")
    tmp.unlink(missing_ok=True)
    if a.ndim > 1:
        a = a.mean(axis=1)
    sf.write(str(wav), a, sr, subtype="PCM_16")
    dur = _wav_duration(wav)
    if dur < 1:
        raise HTTPException(400, f"Áudio muito curto ({dur}s)")
    jp = VOICES_DIR / f"{voice_id}.json"
    if jp.exists():
        try:
            m = json.loads(jp.read_text())
            m["duration"] = dur
            # denoised NÃO é marcado: o áudio veio do editor, sem denoise garantido
            write_json_atomic(jp, m)
        except Exception:  # noqa: BLE001
            pass
    _conds_cache.clear()                      # invalida o clone em cache p/ esta voz
    return {"ok": True, "duration": dur}


@app.get("/api/voices/{voice_id}/peaks")
def voice_peaks(voice_id: str, n: int = 160):
    """Picos normalizados (0..1) p/ desenhar o waveform da voz salva."""
    import numpy as np
    import soundfile as sf

    path = VOICES_DIR / f"{voice_id}.wav"
    if not path.exists():
        raise HTTPException(404, "Voz não encontrada")
    n = int(_clamp(n, 20, 600, 160))
    a, _sr = sf.read(str(path), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    if a.size == 0:
        return {"peaks": [0.0] * n}
    buckets = np.array_split(np.abs(a), n)
    peaks = np.array([float(b.max()) if b.size else 0.0 for b in buckets])
    mx = float(peaks.max()) or 1.0
    return {"peaks": [round(p, 4) for p in (peaks / mx).tolist()]}


@app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str):
    removed = False
    for ext in ("wav", "json"):
        path = VOICES_DIR / f"{voice_id}.{ext}"
        if path.exists():
            path.unlink()
            removed = True
    if not removed:
        raise HTTPException(404, "Voz não encontrada")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Síntese: outputs/<id>.wav + outputs/<id>.json
# ---------------------------------------------------------------------------


# Jobs de síntese: o servidor gera trecho a trecho e o navegador toca cada
# trecho assim que fica pronto — a fala começa após o 1º trecho, não no fim.
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_MAX = 20

# ---------------------------------------------------------------------------
# Fila de falas (speech_queue): gate serial por ticket.
# - Vários jobs podem GERAR em paralelo (quando remoto / após liberar gen_lock).
# - Só um job ENTREGA por vez, na ordem de chegada.
# - Após entregar, bloqueia a próxima pela duração REAL do WAV (+ folga).
# - Trechos (pieces) só contam no job depois da entrega — endpoint também barra.
# ---------------------------------------------------------------------------

def _wav_duration_precise(path: Path) -> float:
    """Duração em segundos a partir do WAV (precisão de frames; 0 se falhar)."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate() or 0
            if rate <= 0:
                return 0.0
            return w.getnframes() / float(rate)
    except Exception:  # noqa: BLE001
        return 0.0


def _pieces_duration(job_id: str) -> float:
    """Soma a duração dos trechos .wav do job (fallback se o final ainda não existe)."""
    pdir = _piece_dir(job_id)
    if not pdir.is_dir():
        return 0.0
    total = 0.0
    for p in sorted(pdir.glob("*.wav")):
        if not p.stem.isdigit():
            continue
        total += _wav_duration_precise(p)
    return total


def _resolve_speech_duration(duration_s=None, job_id: str | None = None,
                             output_meta: dict | None = None) -> float:
    """Duração da fala: prefere o maior valor confiável (WAV final / trechos / meta)."""
    candidates: list[float] = []

    def _add(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if f > 0:
            candidates.append(f)

    _add(duration_s)
    if output_meta:
        _add(output_meta.get("duration"))
        oid = output_meta.get("id")
        if oid:
            _add(_wav_duration_precise(OUTPUTS_DIR / f"{oid}.wav"))
    if job_id:
        _add(_pieces_duration(job_id))
    return max(candidates) if candidates else 0.0


class _SpeechGate:
    """Gate FIFO de entrega: ticket + espera pela duração da fala anterior."""

    def __init__(self):
        self._cv = threading.Condition()
        self._ticket_seq = 0
        self._next_ticket = 0
        self._free_at = 0.0
        self._last_dur = 0.0
        self._depth = 0

    @property
    def depth(self) -> int:
        with self._cv:
            return int(self._depth)

    @property
    def free_in(self) -> float:
        with self._cv:
            return max(0.0, self._free_at - time.time())

    @property
    def last_duration(self) -> float:
        with self._cv:
            return float(self._last_dur)

    def begin(self, job_id: str):
        if not _settings.get("speech_queue"):
            return None
        with self._cv:
            ticket = self._ticket_seq
            self._ticket_seq += 1
            self._depth += 1
            free_in = max(0.0, self._free_at - time.time())
            last = self._last_dur
            ahead = ticket - self._next_ticket
        token = {"ticket": ticket, "job_id": job_id, "done": False}
        job = _jobs.get(job_id)
        if job is not None:
            job["queued"] = True
            job["status"] = "queued"
            job["queue_ticket"] = ticket
            if ahead > 0 or free_in > 0.05:
                job["progress"] = {
                    "stage": (
                        f"na fila (#{ahead + 1}"
                        + (f", ~{free_in:.0f}s" if free_in > 0.05 else "")
                        + (f", última {last:.1f}s" if last > 0 else "")
                        + ")…"
                    ),
                }
            else:
                job["progress"] = {"stage": "na fila de falas…"}
        return token

    def deliver(self, token, duration_s: float = 0.0,
                output_meta: dict | None = None) -> None:
        if not token or token.get("done"):
            return
        job_id = token.get("job_id") or ""
        job = _jobs.get(job_id)
        try:
            gap = max(0.0, min(5.0, float(_settings.get("speech_queue_gap_s") or 0.35)))
        except (TypeError, ValueError):
            gap = 0.35

        dur = _resolve_speech_duration(duration_s, job_id=job_id, output_meta=output_meta)
        if dur <= 0 and job is not None:
            text = (job.get("text") or "") if isinstance(job.get("text"), str) else ""
            if text:
                dur = max(0.8, min(120.0, len(text) / 14.0))
        # margem mínima: evita overlap por latência de rede/player do cliente
        if dur > 0:
            dur = max(dur, 0.4)

        with self._cv:
            # 1) ordem FIFO estrita
            while token["ticket"] != self._next_ticket:
                if job is not None:
                    pos = token["ticket"] - self._next_ticket + 1
                    job["progress"] = {"stage": f"na fila (posição {max(1, pos)})…"}
                self._cv.wait(timeout=0.4)

            # 2) espera o fim da fala anterior (duração medida na entrega anterior)
            while True:
                wait = self._free_at - time.time()
                if wait <= 0:
                    break
                if job is not None:
                    job["progress"] = {
                        "stage": (
                            f"aguardando fim da fala anterior "
                            f"(~{wait:.0f}s; durou {self._last_dur:.1f}s)…"
                        ),
                    }
                self._cv.wait(timeout=min(0.4, max(0.05, wait)))

            now = time.time()
            self._free_at = now + dur + gap
            self._last_dur = dur
            self._next_ticket += 1
            self._depth = max(0, self._depth - 1)
            token["done"] = True
            token["duration_s"] = dur
            if job is not None:
                job.pop("queued", None)
                job["speech_duration_s"] = round(dur, 3)
            self._cv.notify_all()

    def abort(self, token) -> None:
        """Erro: avança o ticket sem reservar tempo de áudio."""
        if not token or token.get("done"):
            return
        job_id = token.get("job_id") or ""
        with self._cv:
            while token["ticket"] != self._next_ticket:
                self._cv.wait(timeout=0.4)
            self._next_ticket += 1
            self._depth = max(0, self._depth - 1)
            token["done"] = True
            self._cv.notify_all()
        job = _jobs.get(job_id)
        if job is not None:
            job.pop("queued", None)


_speech_gate = _SpeechGate()


def _speech_queue_begin(job_id: str):
    return _speech_gate.begin(job_id)


def _speech_queue_deliver(token, duration_s: float = 0.0,
                          output_meta: dict | None = None) -> None:
    _speech_gate.deliver(token, duration_s=duration_s, output_meta=output_meta)


def _speech_queue_abort(token) -> None:
    _speech_gate.abort(token)


def _piece_dir(job_id: str) -> Path:
    return OUTPUTS_DIR / f".job-{_safe_id(job_id)}"


def _safe_id(v: str) -> str:
    """Valida id vindo da rota antes de montar Path — defesa em profundidade
    contra path traversal em downloads (o roteador já impede '/', isto aqui
    garante mesmo se o roteamento mudar)."""
    if not v or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v):
        raise HTTPException(404, "Id inválido")
    return v


def _evict_jobs():
    """Mantém no máximo _JOBS_MAX jobs no histórico.

    Evict prefere jobs terminados (done/error): apagar trechos de um job em
    execução/na fila deixaria o cliente sem stream. Running só sai na força
    (todos os 20 em voo) — comportamento antigo, último recurso.
    """
    while len(_jobs) > _JOBS_MAX:
        alvo = next((jid for jid, j in _jobs.items()
                     if j.get("status") not in ("running", "queued")),
                    next(iter(_jobs), None))
        if alvo is None:
            break
        _jobs.pop(alvo, None)
        shutil.rmtree(_piece_dir(alvo), ignore_errors=True)


def _voice_ref_text(voice_id: str):
    """Transcrição opcional da amostra (campo 'ref_text' no JSON da voz).

    Com a transcrição a clonagem fica mais estável; se ausente, a lib
    auto-transcreve com Whisper na 1ª geração da voz (mais lento, baixa o ASR).
    """
    try:
        meta = json.loads((VOICES_DIR / f"{voice_id}.json").read_text())
        return (meta.get("ref_text") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _materialize_preset(model, sr: int, pid: str):
    """Gera a amostra-semente de uma voz padrão (voice design) e a salva como voz.

    Roda uma vez por preset; o .wav resultante ancora o timbre (a clonagem por
    ref_tokens passa a valer para essa voz, mantendo-a consistente entre trechos).
    """
    import numpy as np
    import soundfile as sf

    p = OMNI_PRESETS[pid]
    audio = np.concatenate([
        np.array(r.audio, dtype=np.float32)
        for r in model.generate(
            text=OMNI_PRESET_SEED, instruct=p["instruct"], language="None",
            num_steps=OMNI_STEPS_HQ, guidance_scale=2.0, class_temperature=0.0,
        )
    ])
    audio = _normalize(_trim_tail_silence(audio, sr))
    sf.write(VOICES_DIR / f"{pid}.wav", audio, sr, subtype="PCM_16")
    meta = {
        "id": pid, "name": p["name"], "preset": True, "materialized": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": round(len(audio) / sr, 1),
        "ref_text": OMNI_PRESET_SEED, "instruct": p["instruct"],
    }
    write_json_atomic(VOICES_DIR / f"{pid}.json", meta)


# Famílias que rodam em PROCESSO FILHO (SIGSEGV do Metal/Qwen não derruba o servidor).
# OmniVoice e PocketTTS ficam in-process (estáveis + mais rápidos no 2º request).
_ISOLATED_FAMILIES = frozenset({
    "qwen3_tts", "qwen3_custom", "qwen3_design",
    "fish", "chatterbox", "voxcpm2", "voxtral_tts",
    "kokoro", "moss_nano", "indextts", "generic",
})


def _unload_local_tts():
    """Libera o modelo MLX do processo principal (antes de spawnar worker)."""
    global _model
    with _model_lock:
        _model = None
        _conds_cache.clear()
        _release_mlx_memory(aggressive=True)
        if _model_state.get("status") != "error":
            _model_state.update(status="idle", progress=None, model=_settings.get("model"))


def _run_tts_job_isolated(job_id: str, text: str, voice_id: str, voice_path: Path,
                          language: str, omni: dict, be: dict, sq=None):
    """Síntese em subprocesso: crash nativo (SIGSEGV) só mata o worker."""
    import subprocess
    import sys

    job = _jobs[job_id]
    pdir = _piece_dir(job_id)
    pdir.mkdir(exist_ok=True)
    status_path = pdir / "status.json"
    cfg_path = pdir / "config.json"
    label = (be.get("meta") or {}).get("label") or be.get("id")
    family = be["family"]
    hold_pieces = sq is not None  # fila ligada: não publica trechos até entregar

    # libera RAM do modelo in-process antes do filho carregar o Qwen/etc.
    _unload_local_tts()
    _model_state.update(
        status="loading", device="mlx-worker", model=_settings.get("model"),
        family=family, progress=f"worker: {label}…",
        backend_id=be.get("id"), backend_label=label,
    )
    job.update(status="running",
               progress={"stage": f"iniciando worker ({label})…",
                         "backend": be.get("id"), "family": family})

    cfg = {
        "job_id": job_id,
        "text": text,
        "voice_id": voice_id,
        "voice_path": str(voice_path) if voice_path else "",
        "language": language,
        "omni": omni,
        "model": _settings.get("model") or "omnivoice",
        "settings": {
            "chunk_max_chars": _settings.get("chunk_max_chars", 140),
            "omni_ref_max_s": _settings.get("omni_ref_max_s", 10.0),
            "omni_precision": _settings.get("omni_precision", "bf16"),
            # FX de saída (o worker aplica o mesmo EQ/ganho do caminho local)
            "audio_gain_db": _settings.get("audio_gain_db", 0.0),
            "audio_eq_low_db": _settings.get("audio_eq_low_db", 0.0),
            "audio_eq_mid_db": _settings.get("audio_eq_mid_db", 0.0),
            "audio_eq_high_db": _settings.get("audio_eq_high_db", 0.0),
        },
        "piece_dir": str(pdir),
        "outputs_dir": str(OUTPUTS_DIR),
        "voices_dir": str(VOICES_DIR),
        "base_dir": str(BASE),
        "status_path": str(status_path),
    }
    write_json_atomic(cfg_path, cfg)
    py = str(BASE / ".venv-mlx" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    worker = str(BASE / "tts_worker.py")

    log_path = pdir / "worker.log"
    # serializa workers (um modelo pesado por vez no Metal)
    with _gen_lock:
        log_f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        try:
            proc = subprocess.Popen(
                [py, worker, str(cfg_path)],
                cwd=str(BASE),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # grupo próprio: kill limpo se preciso
            )
        except Exception as exc:  # noqa: BLE001
            log_f.close()
            _speech_queue_abort(sq)
            job.update(status="error", error=f"falha ao iniciar worker: {exc}", progress=None)
            _model_state.update(status="idle", progress=None)
            return

        # poll status do filho enquanto gera (UI recebe pieces progressivos;
        # com fila de falas só publica trechos na entrega)
        last_pieces = 0

        def _finish_ok(st: dict) -> None:
            out = st.get("output") or {}
            pieces = int(st["pieces"]) if st.get("pieces") is not None else last_pieces
            if st.get("total") is not None:
                job["total"] = int(st["total"])
            if sq is not None:
                job["progress"] = {"stage": "aguardando vez na fila…"}
                _speech_queue_deliver(sq, out.get("duration"), output_meta=out)
            job["pieces"] = pieces
            job.update(status="done", output=out, progress=None, error=None)
            _model_state.update(status="ready", progress=None,
                                device="mlx-worker",
                                model=_settings.get("model"),
                                family=family,
                                backend_id=be.get("id"),
                                backend_label=label)

        def _finish_err(msg: str) -> None:
            _speech_queue_abort(sq)
            job.update(status="error", error=msg, progress=None, pieces=last_pieces)
            _model_state.update(status="idle", progress=None, error=None)

        try:
            while True:
                rc = proc.poll()
                if status_path.exists():
                    try:
                        st = json.loads(status_path.read_text())
                        if st.get("pieces") is not None:
                            last_pieces = int(st["pieces"])
                            if not hold_pieces:
                                job["pieces"] = last_pieces
                        if st.get("total") is not None:
                            job["total"] = int(st["total"])
                        if st.get("progress") is not None and not hold_pieces:
                            job["progress"] = st["progress"]
                            stage = (st["progress"] or {}).get("stage")
                            if stage:
                                _model_state["progress"] = stage
                        elif st.get("progress") is not None and hold_pieces:
                            # ainda gera: mostra estágio sem liberar trechos
                            stage = (st["progress"] or {}).get("stage")
                            if stage:
                                job["progress"] = {"stage": stage, "backend": be.get("id"),
                                                   "family": family}
                                _model_state["progress"] = stage
                        if st.get("status") == "done" and st.get("output"):
                            try:
                                proc.wait(timeout=30)
                            except Exception:  # noqa: BLE001
                                pass
                            _finish_ok(st)
                            return
                        if st.get("status") == "error":
                            try:
                                proc.wait(timeout=10)
                            except Exception:  # noqa: BLE001
                                pass
                            _finish_err(st.get("error") or "erro no worker")
                            return
                    except Exception:  # noqa: BLE001
                        pass
                if rc is not None:
                    break
                time.sleep(0.35)
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                log_f.close()
            except Exception:  # noqa: BLE001
                pass

        # processo saiu sem status done
        rc = proc.returncode
        err_tail = ""
        try:
            if log_path.exists():
                err_tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        except Exception:  # noqa: BLE001
            pass
        if status_path.exists():
            try:
                st = json.loads(status_path.read_text())
                if st.get("status") == "done" and st.get("output"):
                    _finish_ok(st)
                    return
                if st.get("status") == "error":
                    _finish_err(st.get("error") or "erro no worker")
                    return
            except Exception:  # noqa: BLE001
                pass
        if rc == -11 or rc == 139:  # SIGSEGV
            msg = ("Worker crashou (SIGSEGV / Metal-MLX) ao gerar com "
                   f"{label}. O servidor continua no ar — tente qwen3-0.6b, "
                   "PocketTTS ou OmniVoice, ou reinicie e tente de novo.")
        elif rc and rc < 0:
            msg = f"Worker morto por sinal {-rc} ({label}). Servidor OK."
        else:
            msg = f"Worker saiu com código {rc} ({label})."
        if err_tail:
            msg += " | " + err_tail.replace("\n", " ")[:300]
        _finish_err(msg)


def _run_tts_job(job_id: str, text: str, voice_id: str, voice_path: Path,
                 language: str, omni: dict, model_override: str | None = None):
    import numpy as np
    import soundfile as sf

    job = _jobs[job_id]
    remote = False
    sq = _speech_queue_begin(job_id)
    hold_pieces = sq is not None  # com fila: grava trechos mas só libera na entrega
    # model_override já deve ter sido aplicado em settings pelo /api/tts
    try:
        remote = _use_remote_tts()
        be = _current_backend()
        family = be["family"]
        be_meta = be.get("meta") or {}

        # Qwen/Fish/etc.: processo isolado (não trava/derruba o servidor)
        if not remote and family in _ISOLATED_FAMILIES:
            _run_tts_job_isolated(job_id, text, voice_id, voice_path, language, omni, be, sq=sq)
            # worker morreu: garante pool Metal limpo no pai + apaga trechos depois
            _release_mlx_memory(aggressive=True)
            st = job.get("status")
            _schedule_job_cleanup(job_id, delay_s=90 if st == "done" else 30)
            return

        chunks = _split_text(_sanitize_text(text), max_chars=_settings["chunk_max_chars"])
        job["total"] = len(chunks)
        pdir = _piece_dir(job_id)
        pdir.mkdir(exist_ok=True)

        # remoto: sem lock global -> jobs concorrentes (o servidor RTX paraleliza);
        # local: serializa load+gen no _gen_lock (evita 2 modelos MLX em paralelo / segfault).
        with (_NO_LOCK if remote else _gen_lock):
            started = time.time()
            job["status"] = "running"
            if not remote:
                job["progress"] = {
                    "stage": f"carregando {be['meta'].get('label') or be['id']}…",
                    "backend": be.get("id"), "family": family,
                }
                _model_state["progress"] = job["progress"]["stage"]
            model = None if remote else _get_model()
            sr = 24000 if remote else int(getattr(model, "sample_rate", 24000) or 24000)
            silence = np.zeros(int(CHUNK_SILENCE_S * sr), dtype=np.float32)
            # voz de VOICE DESIGN salva: REGENERA do instruct+seed (determinístico) em
            # vez de clonar a amostra -> é EXATAMENTE a voz projetada, sem drift de clone.
            is_design = voice_id == DESIGN_VOICE_ID
            jpath = voice_path.with_suffix(".json")
            if not is_design and jpath.exists():
                try:
                    _vm = json.loads(jpath.read_text())
                    if _vm.get("from_design"):
                        is_design = True
                        omni = {**omni, "instruct": _vm.get("instruct") or omni.get("instruct"),
                                "seed": _vm.get("seed", omni.get("seed"))}
                except Exception:  # noqa: BLE001
                    pass
            # voz padrão ainda não materializada: cria a amostra-semente 1x (só OmniVoice)
            conds = None
            ref_text = None
            ref_audio = None
            rvoice = None
            if remote:
                # voz de design: ignora a "Voz remota" fixa (o timbre vem do instruct+seed).
                # senão: campo "Voz remota" fixo OU sobe a voz local e usa o nome dela
                rvoice = None if is_design else ((_settings.get("remote_tts_voice") or "").strip() or None)
                if not rvoice and not is_design and voice_path.exists():
                    job["progress"] = {"stage": "enviando voz ao servidor remoto…"}
                    rvoice = _ensure_remote_voice(voice_id, voice_path)
                    if not rvoice:
                        # upload falhou (rede/endpoint): o job segue com a voz
                        # PADRÃO do servidor remoto — avisar, senão parece voz errada
                        job["warning"] = ("voz local não pôde ser enviada ao "
                                          "servidor remoto — usando voz padrão")
                        job["progress"] = {"stage": "⚠ " + job["warning"]}
            elif is_design:
                ref_text = None       # sem ref de clone -> o timbre vem só do instruct
                conds = None
            elif family == "omnivoice":
                if voice_id in OMNI_PRESETS and not voice_path.exists():
                    job["progress"] = {"stage": "criando voz padrão…"}
                    _materialize_preset(model, sr, voice_id)
                ref_text = _voice_ref_text(voice_id)
                conds = _cond_for(model, voice_id, voice_path)
            else:
                # demais backends: passam o .wav da voz (se houver) como ref_audio
                if voice_path.exists():
                    ref_audio = str(voice_path)
                    ref_text = _voice_ref_text(voice_id)
                elif family in ("kokoro", "qwen3_custom", "pocket_tts", "voxtral_tts"):
                    # preset interno do modelo — sem amostra
                    pass
                elif family in ("qwen3_design",) or be_meta.get("voice_design"):
                    pass  # instruct basta
                elif voice_id in OMNI_PRESETS and family == "omnivoice":
                    pass
                else:
                    # backend exige clone mas não há sample: tenta mesmo assim (preset)
                    pass
            for i, chunk in enumerate(chunks):
                job["progress"] = {
                    "current": i + 1, "total": len(chunks),
                    "backend": be.get("id"), "family": family,
                }
                # trecho sem pontuação terminal (quebra por vírgula) ganha ponto
                if chunk[-1] not in ".!?…":
                    chunk = chunk.rstrip(" ,;:") + "."
                if remote:
                    # servidor remoto gera; sem retry local
                    audio = _tts_remote_chunk(chunk, language, omni, sr, rvoice)
                else:
                    for tentativa in (1, 2):
                        o_try = omni
                        if tentativa > 1:
                            o_try = dict(omni)
                            # 2ª tentativa: com seed fixa + temp 0 (greedy) a
                            # regeneração reproduz o MESMO áudio anômalo. Jitter
                            # só em voz de CLONE (timbre vem da ref, não do
                            # seed); em voice design o seed ancora o timbre e
                            # não pode mudar entre trechos.
                            if not is_design:
                                seed = o_try.get("seed")
                                if seed is not None and int(seed) >= 0:
                                    o_try["seed"] = int(seed) + tentativa
                                if not float(o_try.get("class_temperature") or 0):
                                    o_try["class_temperature"] = 0.4
                                if float(o_try.get("temperature") or 0) < 0.5:
                                    o_try["temperature"] = 0.7
                        audio = _generate_chunk(
                            model, chunk, language, conds, ref_text, o_try,
                            ref_audio=ref_audio, family=family, meta=be_meta, sr=sr,
                        )
                        if not _anomalo(audio, sr, chunk, speed=float(omni.get("speed") or 1.0)):
                            break
                        job["retries"] = job.get("retries", 0) + 1
                        # retry: limpa tensores intermediários do generate falho
                        _release_mlx_memory()
                audio = _fade_edges(_apply_audio_fx(_normalize(_trim_tail_silence(audio, sr)), sr), sr)
                if i < len(chunks) - 1:
                    audio = np.concatenate([audio, silence])
                sf.write(pdir / f"{i}.wav", audio, sr, subtype="PCM_16")
                del audio
                if not hold_pieces:
                    job["pieces"] = i + 1  # publica só depois do arquivo no disco
                # MLX acumula blocos no pool Metal; sem clear a RAM sobe a cada trecho
                if not remote:
                    _release_mlx_memory()
                    _touch_use("tts")
        elapsed = round(time.time() - started, 1)

        # arquivo final do histórico = exatamente o que foi tocado (stream, sem concat total)
        out_id = uuid.uuid4().hex[:10]
        duration = _write_wav_concat(pdir, len(chunks), OUTPUTS_DIR / f"{out_id}.wav", sr)
        meta = {
            "id": out_id,
            "text": text,
            "voice_id": voice_id,
            "language": _omni_language(language) if family == "omnivoice" else language,
            "backend": be.get("id"),
            "family": family,
            "num_steps": int(omni.get("num_steps") or OMNI_STEPS_FAST),
            "guidance_scale": omni.get("guidance_scale"),
            "class_temperature": omni.get("class_temperature"),
            "instruct": omni.get("instruct") or "",
            "chunks": len(chunks),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": duration,
            "elapsed": elapsed,
        }
        write_json_atomic(OUTPUTS_DIR / f"{out_id}.json", meta)
        # fila: espera o fim da fala anterior (duração real do último WAV) antes de entregar
        if sq is not None:
            job["progress"] = {"stage": "aguardando vez na fila…"}
            _speech_queue_deliver(sq, duration, output_meta=meta)
            job["pieces"] = len(chunks)
        job.update(status="done", output=meta)
        # trechos já gravados no final: limpa após graça p/ o cliente terminar o stream
        _schedule_job_cleanup(job_id, delay_s=90)
    except Exception as exc:  # noqa: BLE001
        _speech_queue_abort(sq)
        job.update(status="error", error=str(exc))
        _schedule_job_cleanup(job_id, delay_s=30)
    finally:
        job["progress"] = None
        if _model_state.get("status") != "error":
            _model_state["progress"] = None
        if not remote:
            _release_mlx_memory(aggressive=True)


@app.post("/api/tts")
def synthesize(payload: dict):
    text = (payload.get("text") or "").strip()
    if _settings["pre_prompt"]:
        text = f"{_settings['pre_prompt']} {text}".strip()
    language = (payload.get("language") or _settings["language"]).lower()
    # model no body: aplica na hora (e grava em settings p/ UI/API ficarem alinhados)
    model_override = None
    if payload.get("model"):
        m = str(payload["model"]).strip()
        if m and m != "__custom__":
            model_override = m
            if _settings.get("model") != m:
                _settings["model"] = m
                try:
                    _save_settings()
                except Exception:  # noqa: BLE001
                    pass
    be = _current_backend()
    family = be["family"]
    be_meta = be.get("meta") or {}
    omni = _resolve_omni(payload, family=family)
    if not text:
        raise HTTPException(400, "Texto vazio")
    if len(text) > 5000:
        raise HTTPException(400, "Texto longo demais (máx. 5000 caracteres)")
    # preset internos (sem sample) ou design
    no_sample_ok = family in (
        "kokoro", "qwen3_custom", "qwen3_design", "pocket_tts", "voxtral_tts",
    ) or be_meta.get("voice_design")
    raw_voice = payload.get("voice_id") or _settings["default_voice"]
    if raw_voice == DESIGN_VOICE_ID or (isinstance(raw_voice, str)
            and raw_voice.strip().lower() in (DESIGN_VOICE_ID, "design")):
        voice_id = DESIGN_VOICE_ID
    elif raw_voice and (VOICES_DIR / f"{raw_voice}.wav").exists():
        voice_id = raw_voice
    elif raw_voice in OMNI_PRESETS:
        voice_id = raw_voice
    elif no_sample_ok and not raw_voice:
        voice_id = DESIGN_VOICE_ID  # backend com voz interna
    else:
        # clone backends: resolve por nome/id ou cai na voz mais recente
        try:
            voice_id = _resolve_voice(raw_voice)
        except HTTPException:
            if no_sample_ok:
                voice_id = DESIGN_VOICE_ID
            else:
                raise

    voice_path = VOICES_DIR / f"{voice_id}.wav"
    if voice_id == DESIGN_VOICE_ID:
        if not (omni.get("instruct") or "").strip() and not be_meta.get("voice_design") \
                and family not in ("qwen3_design", "qwen3_custom", "kokoro", "pocket_tts"):
            # design sem instruct em OmniVoice: usa instruct das settings se houver
            if family == "omnivoice" and not (omni.get("instruct") or "").strip():
                raise HTTPException(400, "Voice design vazio — descreva a voz no campo instruct")
    elif _use_remote_tts():
        pass  # o servidor remoto valida/mapeia a voz
    elif not voice_path.exists() and voice_id not in OMNI_PRESETS:
        if no_sample_ok:
            pass
        else:
            raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    job_id = uuid.uuid4().hex[:10]
    _jobs[job_id] = {"status": "running", "pieces": 0, "total": None,
                     "progress": None, "output": None, "error": None,
                     "text": text[:200]}  # facilita depurar relatos de áudio mudo
    _evict_jobs()

    threading.Thread(
        target=_run_tts_job,
        args=(job_id, text, voice_id, voice_path, language, omni, model_override),
        daemon=True,
    ).start()
    return {"job_id": job_id, "backend": be.get("id"), "family": family}


@app.get("/api/tts/jobs/{job_id}")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado")
    return job


@app.get("/api/tts/jobs/{job_id}/pieces/{index}")
def job_piece(job_id: str, index: int):
    """Só devolve trecho se o job já liberou `pieces` (fila de falas respeitada)."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado")
    # com fila: pieces fica 0 até a entrega — impede stream/cliente de tocar cedo
    try:
        liberados = int(job.get("pieces") or 0)
    except (TypeError, ValueError):
        liberados = 0
    if index < 0 or index >= liberados:
        raise HTTPException(404, "Trecho ainda não liberado")
    path = _piece_dir(job_id) / f"{index}.wav"
    if not path.exists():
        raise HTTPException(404, "Trecho não encontrado")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/outputs")
def list_outputs():
    outputs = []
    for f in OUTPUTS_DIR.glob("*.json"):
        try:
            o = json.loads(f.read_text())
        except Exception:  # noqa: BLE001 — meta corrompido não pode derrubar a API
            continue
        if isinstance(o, dict):
            outputs.append(o)
    outputs.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    return outputs


@app.get("/api/outputs/{out_id}/audio")
def output_audio(out_id: str):
    path = OUTPUTS_DIR / f"{_safe_id(out_id)}.wav"
    if not path.exists():
        raise HTTPException(404, "Áudio não encontrado")
    return FileResponse(path, media_type="audio/wav", filename=f"tts-studio-{out_id}.wav")


@app.delete("/api/outputs")
def delete_all_outputs():
    removidos = 0
    for meta in OUTPUTS_DIR.glob("*.json"):
        meta.with_suffix(".wav").unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        removidos += 1
    return {"ok": True, "removidos": removidos}


def _auto_cleanup_once():
    """Apaga áudios gerados mais antigos que o limite configurado."""
    limite = time.time() - _settings["auto_cleanup_minutes"] * 60
    removidos = 0
    for meta in OUTPUTS_DIR.glob("*.json"):
        if meta.stat().st_mtime < limite:
            meta.with_suffix(".wav").unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            removidos += 1
    # diretórios de jobs (sínteses longas): a limpeza de *.json/*.wav não os
    # alcança e órfãos se acumulam. Pula jobs ativos (apagar trechos no meio
    # da síntese quebraria o stream) e o resto sai por idade.
    for d in OUTPUTS_DIR.glob(".job-*"):
        if not d.is_dir() or d.stat().st_mtime >= limite:
            continue
        jid = d.name[len(".job-"):]
        j = _jobs.get(jid)
        if j and (j.get("status") == "running" or j.get("status") == "queued"):
            continue
        shutil.rmtree(d, ignore_errors=True)
        removidos += 1
    return removidos


def _auto_cleanup_loop():
    while True:
        time.sleep(60)
        try:
            if _settings["auto_cleanup"]:
                _auto_cleanup_once()
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_auto_cleanup_loop, daemon=True).start()
# idle unload sobe depois que _ser/_mt existem (ver fim do bloco de modelos)


@app.delete("/api/outputs/{out_id}")
def delete_output(out_id: str):
    removed = False
    for ext in ("wav", "json"):
        path = OUTPUTS_DIR / f"{out_id}.{ext}"
        if path.exists():
            path.unlink()
            removed = True
    if not removed:
        raise HTTPException(404, "Áudio não encontrado")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tradutor de voz (PoC): fala -> texto (STT) -> tradução -> fala traduzida na
# voz clonada (TTS). STT = mlx-whisper; tradução = mlx-lm (LLM local).
# ---------------------------------------------------------------------------

_stt_lock = threading.Lock()      # mlx-whisper não é thread-safe; serializa
_mt = {"model": None, "tok": None, "repo": None}
_mt_lock = threading.Lock()


def _mt_repo() -> str:
    return (_settings.get("translate_model") or "").strip() or TRANSLATE_REPO


def _unload_local_models(tts=True, stt=True, mt=True, ser=True) -> dict:
    """Libera RAM descarregando os modelos LOCAIS (MLX/torch). Cada flag controla um motor."""
    global _model
    freed = []
    if tts:
        with _gen_lock:
            with _model_lock:
                if _model is not None:
                    _model = None
                    freed.append("tts")
                _conds_cache.clear()
                if _model_state.get("status") != "error":
                    _model_state.update(status="idle", error=None, progress=None)
    if mt:
        with _mt_lock:
            if _mt.get("model") is not None:
                _mt["model"] = _mt["tok"] = None
                _mt["repo"] = None
                freed.append("tradutor")
    if stt:                                  # mlx-whisper cacheia o modelo em ModelHolder (classe)
        try:
            from mlx_whisper.transcribe import ModelHolder
            if ModelHolder.model is not None:
                ModelHolder.model = None
                ModelHolder.model_path = None
                freed.append("whisper")
        except Exception:  # noqa: BLE001
            pass
    if ser:
        with _ser_lock:
            if _ser.get("clf") is not None:
                _ser["clf"] = None
                freed.append("ser")
    _release_mlx_memory(aggressive=True)
    # pipeline SER / transformers às vezes puxa torch — tenta soltar cache MPS/CPU
    try:
        import torch
        if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    return {"unloaded": freed}


def _schedule_job_cleanup(job_id: str, delay_s: float = 90):
    """Remove .job-* (trechos WAV) depois da graça — cliente já tem o áudio final."""
    def _clean():
        time.sleep(max(5.0, delay_s))
        shutil.rmtree(_piece_dir(job_id), ignore_errors=True)
    threading.Thread(target=_clean, daemon=True).start()


def _idle_unload_loop():
    """Descarrega motores ociosos após idle_unload_minutes (0 = desligado)."""
    while True:
        time.sleep(30)
        try:
            mins = float(_settings.get("idle_unload_minutes") or 0)
            if mins <= 0:
                continue
            limit = mins * 60.0
            now = time.time()
            # não descarrega TTS no meio de um job
            busy_tts = any(
                j.get("status") == "running" for j in _jobs.values()
            ) if _jobs else False
            tts_idle = (not busy_tts) and _last_use["tts"] > 0 and (now - _last_use["tts"]) >= limit
            stt_idle = _last_use["stt"] > 0 and (now - _last_use["stt"]) >= limit
            mt_idle = _last_use["mt"] > 0 and (now - _last_use["mt"]) >= limit
            ser_idle = _last_use["ser"] > 0 and (now - _last_use["ser"]) >= limit
            if not (tts_idle or stt_idle or mt_idle or ser_idle):
                continue
            # só descarrega o que realmente está carregado e ocioso
            need_tts = tts_idle and _model is not None
            need_stt = stt_idle
            need_mt = mt_idle and _mt.get("model") is not None
            need_ser = ser_idle and _ser.get("clf") is not None
            # whisper: ModelHolder pode ter modelo sem _last_use se nunca tocou — ok
            if need_tts or need_stt or need_mt or need_ser:
                r = _unload_local_models(
                    tts=need_tts, stt=need_stt, mt=need_mt, ser=need_ser,
                )
                if r.get("unloaded"):
                    for k, flag in (("tts", need_tts), ("stt", need_stt),
                                    ("mt", need_mt), ("ser", need_ser)):
                        if flag:
                            _last_use[k] = 0.0
        except Exception:  # noqa: BLE001
            pass


def _autofree_local():
    """Se a opção estiver ligada, descarrega os locais cujo remoto está ativo."""
    if not _settings.get("free_local_on_remote"):
        return
    _unload_local_models(
        tts=_use_remote_tts(), stt=_use_remote_stt(),
        mt=_use_remote_translate(), ser=False,
    )


# alucinações comuns do Whisper em silêncio/ruído (pt + en). Comparadas após
# normalizar (lower + tira pontuação/aspas das pontas), então cobrem variações.
_STT_BLACKLIST = {
    "obrigado", "obrigada", "tchau", "valeu", "fim", "the end",
    "thank you", "thank you very much", "thanks for watching", "you", "bye", "okay", "ok",
    "legendas pela comunidade amara.org", "amara.org", "subtitles by the amara.org community",
    "♪", "...", ".", "music", "música", "applause", "aplausos",
    # interjeições/fragmentos curtos típicos de ruído (match é da frase INTEIRA)
    "e aí", "e ai", "aí", "hum", "hmm", "uhum", "ãhã", "ã", "ahn", "eh",
    "uh", "uhn", "um", "ó", "ahã", "mm", "mhm", "thanks",
}


def _wav_to_mono16k(audio_path: Path):
    """Lê WAV (vindo do navegador) via soundfile e devolve array float32 mono a
    16 kHz. Evita o load_audio do whisper e o ffmpeg_read do transformers — ambos
    dependem do binário externo `ffmpeg`, ausente em muitos Macs."""
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(str(audio_path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if int(sr) != 16000:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(sr), 16000)
        audio = resample_poly(audio, 16000 // g, int(sr) // g)
    return np.ascontiguousarray(audio, dtype=np.float32)


# --- Modelos remotos (API OpenAI-compatível): tradução e transcrição opcionais ---
def _remote_ready() -> bool:
    # base_url basta; api_key é opcional (endpoints em LAN, ex.: RTX, não têm auth)
    return bool(_settings.get("remote_base_url"))


def _use_remote_translate() -> bool:
    return bool(_settings.get("remote_translate")) and _remote_ready()


def _use_remote_stt() -> bool:
    # ativo se houver URL dedicada de STT OU a base compartilhada
    return bool(_settings.get("remote_stt")) and bool(
        _settings.get("remote_stt_base_url") or _settings.get("remote_base_url"))


def _use_remote_tts() -> bool:
    return bool(_settings.get("remote_tts")) and bool(_settings.get("remote_tts_url"))


def _remote_origin() -> str:
    """scheme://host:port da URL do TTS remoto (p/ os endpoints /voices)."""
    from urllib.parse import urlsplit

    u = urlsplit((_settings.get("remote_tts_url") or "").strip())
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


_remote_voice_cache: dict = {}   # (origin, voice_id, mtime) -> nome remoto


def _ensure_remote_voice(voice_id: str, voice_path: Path):
    """Garante que a voz local exista no servidor remoto (sobe 1x por mtime).
    Devolve o nome remoto (sanitizado) ou None se não der."""
    import re as _re

    import requests

    origin = _remote_origin()
    if not origin or not voice_path.exists():
        return None
    name = _re.sub(r"[^a-zA-Z0-9_-]", "_", _voice_display_name(voice_id))[:64] or "voz"
    key = (origin, name, voice_path.stat().st_mtime_ns)
    if _remote_voice_cache.get(voice_id) == key:
        return name
    headers = {}
    if _settings.get("remote_api_key"):
        headers["Authorization"] = f"Bearer {_settings['remote_api_key']}"
    try:
        with open(voice_path, "rb") as fh:
            r = requests.post(f"{origin}/voices", headers=headers,
                              files={"audio": ("voz.wav", fh, "audio/wav")},
                              data={"name": name, "ref_text": _voice_ref_text(voice_id) or ""},
                              timeout=120)
        if r.ok:
            _remote_voice_cache[voice_id] = key
            return r.json().get("voice", name)
    except Exception:  # noqa: BLE001
        pass
    return None


def _voice_display_name(voice_id: str) -> str:
    try:
        return json.loads((VOICES_DIR / f"{voice_id}.json").read_text()).get("name") or voice_id
    except Exception:  # noqa: BLE001
        return voice_id


def _tts_remote_chunk(text: str, language: str, omni: dict, sr: int = 24000, voice: str = None):
    """Encaminha um trecho ao servidor de TTS remoto (ex.: OmniVoice numa RTX) e
    devolve o áudio como array float32 no sample-rate local. Manda os params do
    OmniVoice configurados + text/language (+ voice se houver clone remoto)."""
    import io

    import numpy as np
    import requests
    import soundfile as sf

    headers = {"Content-Type": "application/json"}
    if _settings.get("remote_api_key"):
        headers["Authorization"] = f"Bearer {_settings['remote_api_key']}"
    # OmniVoice (masked-diffusion): params canônicos do generate
    url = _settings["remote_tts_url"].strip()
    body = {
        "num_steps": int(omni.get("num_steps") or OMNI_STEPS_FAST),
        "guidance_scale": omni.get("guidance_scale", 2.0),
        "class_temperature": omni.get("class_temperature", 0.0),
        "position_temperature": omni.get("position_temperature", 5.0),
        "layer_penalty_factor": omni.get("layer_penalty_factor", 5.0),
        "t_shift": omni.get("t_shift", 0.1),
        "speed": 1.0,   # servidor gera na duração natural; velocidade vira time-stretch local
    }
    # params extras do OmniVoiceGenerationConfig (o server RTX faz whitelist)
    for k in ("denoise", "preprocess_prompt", "postprocess_output",
              "audio_chunk_duration", "audio_chunk_threshold"):
        if omni.get(k) is not None:
            body[k] = omni[k]
    if (omni.get("instruct") or "").strip():
        body["instruct"] = omni["instruct"]
    if omni.get("seed") is not None and int(omni["seed"]) >= 0:
        body["seed"] = int(omni["seed"])
    if omni.get("duration_s") is not None:
        body["duration_s"] = omni["duration_s"]
    if voice:
        body["voice"] = voice
    body["text"] = text
    lang_name = LANG_DISPLAY.get((language or "").lower())
    if lang_name and "language" not in body:
        body["language"] = lang_name
    # JSON extra do usuário sobrepõe/adiciona
    try:
        extra = json.loads(_settings.get("remote_tts_extra") or "{}")
        if isinstance(extra, dict):
            body.update(extra)
    except (ValueError, TypeError):
        pass
    # 1 retry p/ falha transitória (rede/Wi-Fi/5xx): um erro no trecho 20 de 30
    # não pode matar o job inteiro
    r = None
    for tentativa in (1, 2):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300)
        except requests.RequestException as exc:
            r = None
            if tentativa == 2:
                raise RuntimeError(f"TTS remoto inacessível (2 tentativas): {exc}") from exc
            time.sleep(2.0)
            continue
        if r.status_code < 500 or tentativa == 2:
            break
        time.sleep(2.0)   # 5xx: servidor ocupado/carregando modelo — tenta de novo
    if r is None or not r.ok:
        raise RuntimeError(f"TTS remoto falhou ({getattr(r, 'status_code', '?')}): "
                           f"{getattr(r, 'text', '')[:200]}")
    data, src_sr = sf.read(io.BytesIO(r.content), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if int(src_sr) != sr:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(int(src_sr), sr)
        data = resample_poly(data, sr // g, int(src_sr) // g)
    data = np.asarray(data, dtype=np.float32)
    # velocidade: time-stretch (preserva tom, não pula palavras), igual ao local
    speed = float(omni.get("speed") or 1.0)
    if abs(speed - 1.0) > 1e-3:
        data = _time_stretch(data, speed, sr)
    return data


# rótulo PT da emoção -> palavra inglesa p/ o prompt do LLM
_EMO_EN = {
    "alegre": "happy and upbeat", "triste": "sad and downcast", "raiva": "angry and intense",
    "medo": "fearful and tense", "surpresa": "surprised and excited", "calmo": "calm and gentle",
    "desgosto": "displeased", "suave": "soft and gentle", "intenso": "intense and firm",
    "animado": "lively and enthusiastic", "ágil": "lively",
}


def _translate_prompt(text: str, target: str, emotion: str | None = None) -> str:
    nome = LANG_DISPLAY.get(target, target)
    if target == "pt":   # evita PT-europeu ("está a cair") — fixa o alvo no Brasil
        nome = "Brazilian Portuguese (português do Brasil, registro coloquial brasileiro)"
    emo_en = _EMO_EN.get((emotion or "").lower()) if emotion and emotion != "neutro" else None
    base = (
        f"You are an expert {nome} translator and localizer. Render the text into "
        f"natural, idiomatic {nome} exactly as a native speaker would say it out loud — "
        f"translate the MEANING and intent, never word-for-word. Use native phrasing, "
        f"idioms, contractions and the same register and tone; rephrase anything that "
        f"would sound literal, stiff or translated. Keep proper names. Do not add or omit "
        f"information. Output ONLY the {nome} translation — no quotes, no notes, no original."
    )
    if emo_en:
        base += (f" Word it so it sounds {emo_en} when spoken aloud "
                 f"(punctuation, emphasis, natural interjections) without changing the meaning.")
    return f"{base}\n\nText: {text}"


def _translate_remote(text: str, target: str, emotion: str | None = None) -> str:
    import requests

    base = _settings["remote_base_url"].rstrip("/")
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {_settings['remote_api_key']}",
                 "Content-Type": "application/json"},
        json={"model": _settings.get("remote_translate_model") or "gpt-4o-mini",
              "temperature": 0.4 if emotion else 0.2,
              "messages": [{"role": "user", "content": _translate_prompt(text, target, emotion)}]},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"tradução remota falhou ({r.status_code}): {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"].strip().strip('"').strip()


def _transcribe_remote(audio_path: Path, language: str | None):
    import requests

    # URL/chave dedicadas do STT, se definidas; senão as compartilhadas (RTX)
    base = (_settings.get("remote_stt_base_url") or _settings["remote_base_url"]).rstrip("/")
    key = _settings.get("remote_stt_key") or _settings.get("remote_api_key") or ""
    data = {"model": _settings.get("remote_stt_model") or "whisper-1",
            "response_format": "verbose_json",
            "beam_size": int(_settings.get("stt_beam", 5))}   # qualidade↔velocidade
    if language and language not in ("auto",):
        data["language"] = language
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with open(audio_path, "rb") as fh:
        r = requests.post(
            f"{base}/audio/transcriptions",
            headers=headers,
            files={"file": ("audio.wav", fh, "audio/wav")}, data=data, timeout=120,
        )
    if not r.ok:
        raise RuntimeError(f"transcrição remota falhou ({r.status_code}): {r.text[:200]}")
    j = r.json()
    return {"text": j.get("text", ""), "language": (j.get("language") or "").strip().lower(),
            "segments": j.get("segments") or []}


_vad_model = None


def _vad_load():
    """Silero VAD carregado sob demanda (~1MB, CPU, roda em tempo real)."""
    global _vad_model
    if _vad_model is None:
        from silero_vad import load_silero_vad
        _vad_model = load_silero_vad()
    return _vad_model


def _vad_tem_fala(audio_path: Path, minimo_s: float = 0.3) -> bool:
    """Silero VAD: True se o wav tem fala de verdade (não ruído).
    Em caso de erro no Silero, devolve True (não bloqueia o pipeline)."""
    try:
        import soundfile as sf
        import torch
        from silero_vad import get_speech_timestamps
        audio, sr = sf.read(str(audio_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        speech = get_speech_timestamps(torch.from_numpy(audio), _vad_load(),
                                       sampling_rate=sr, threshold=0.6, speech_pad_ms=100)
        dur_fala = sum((t["end"] - t["start"]) for t in speech) / sr
        return dur_fala >= minimo_s
    except Exception:  # noqa: BLE001 — Silero indisponível não bloqueia o STT
        return True


def _transcribe(audio_path: Path, language: str | None = None, allow_remote: bool = True):
    if allow_remote and _use_remote_stt():
        return _transcribe_remote(audio_path, language)

    # Silero VAD: se o áudio não tem fala, o Whisper nem roda (mata alucinação)
    fala = _vad_tem_fala(audio_path)
    if not fala:
        return {"text": "", "language": "", "segments": []}

    import mlx_whisper

    # language fixo (ex.: "pt") melhora muito a precisão em trechos curtos: o
    # whisper deixa de adivinhar o idioma a cada frase. None = auto-detecta.
    lang = language if language and language not in ("auto",) else None
    audio = _wav_to_mono16k(audio_path)
    with _stt_lock:
        # opções que reduzem alucinação: greedy, sem condicionar no texto anterior,
        # e os limiares de no-speech / confiança / repetição configuráveis
        r = mlx_whisper.transcribe(
            audio, path_or_hf_repo=WHISPER_REPO, language=lang,
            temperature=0.0, condition_on_previous_text=False,
            no_speech_threshold=_settings["stt_max_no_speech"],
            logprob_threshold=_settings["stt_min_logprob"],
            compression_ratio_threshold=_settings["stt_max_compression"],
        )
    del audio
    _touch_use("stt")
    _release_mlx_memory()
    return r


def _stt_ok(r: dict, text: str):
    """Aceita só transcrição que pareça fala real (rejeita ruído/alucinação)."""
    t = text.strip()
    palavras = re.findall(r"[^\W\d_]+", t, flags=re.UNICODE)  # palavras (sem números/símbolos)
    if len(t) < _settings["stt_min_chars"]:
        return False, "curto demais"
    if len(palavras) < _settings["stt_min_words"]:
        return False, "sem palavras"
    if t.lower().strip(" .!?…\"'") in _STT_BLACKLIST:
        return False, "alucinação comum"
    segs = r.get("segments") or []
    if segs:
        nsp = max((s.get("no_speech_prob", 0.0) for s in segs), default=0.0)
        alp = min((s.get("avg_logprob", 0.0) for s in segs), default=0.0)
        cr = max((s.get("compression_ratio", 0.0) for s in segs), default=0.0)
        if nsp > _settings["stt_max_no_speech"]:
            return False, f"sem fala ({nsp:.2f})"
        if alp < _settings["stt_min_logprob"]:
            return False, f"baixa confiança ({alp:.2f})"
        if cr > _settings["stt_max_compression"]:
            return False, f"repetitivo ({cr:.2f})"
    return True, ""


_NOTE_RE = re.compile(
    r"^[\(\[\*]*\s*(note|nota|obs\b|observa|alternativ|"
    r"a (more|better) (natural|idiomatic|colloquial|common|literal)\b|"
    r"uma (forma|maneira|vers[aã]o) mais (natural|comum|idiom|coloquial))", re.I)


def _clean_translation(s: str) -> str:
    """Tira notas/alternativas/preâmbulos que o LLM às vezes anexa (senão o TTS fala isso)."""
    s = (s or "").strip().strip('"').strip()
    s = s.split("\n\n", 1)[0].strip()          # corta bloco extra após linha em branco (nota/alternativa)
    linhas = []
    for ln in s.split("\n"):
        if _NOTE_RE.match(ln.strip()):          # linha de nota inline -> para aqui
            break
        linhas.append(ln)
    return "\n".join(linhas).strip().strip('"').strip()


def _translate(text: str, target: str, emotion: str | None = None) -> str:
    if _use_remote_translate():
        return _clean_translation(_translate_remote(text, target, emotion))

    from mlx_lm import generate, load

    repo = _mt_repo()
    with _mt_lock:
        if _mt["model"] is None or _mt.get("repo") != repo:   # troca de modelo -> recarrega
            _mt["model"], _mt["tok"] = load(repo)
            _mt["repo"] = repo
        model, tok = _mt["model"], _mt["tok"]
        msgs = [{"role": "user", "content": _translate_prompt(text, target, emotion)}]
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
        out = generate(model, tok, prompt=prompt, max_tokens=512, verbose=False)
    _touch_use("mt")
    _release_mlx_memory()
    return _clean_translation(out)


# --- Captura de emoção -> alavancas que o OmniVoice TEM, sem quebrar o clone:
#     pitch (tag VÁLIDA do instruct) + velocidade (time-stretch, preserva o timbre).
#     Mapa: label -> (rótulo p/ exibir, tag de pitch, fator de velocidade).
_ser = {"clf": None}
_ser_lock = threading.Lock()
SER_REPO = os.environ.get("TTS_ROD_SER", "superb/wav2vec2-base-superb-er")
_SER_EMO = {
    "hap": ("alegre", "high pitch", 1.12), "happy": ("alegre", "high pitch", 1.12),
    "ang": ("raiva", "high pitch", 1.08), "angry": ("raiva", "high pitch", 1.08),
    "sad": ("triste", "low pitch", 0.88), "sadness": ("triste", "low pitch", 0.88),
    "neu": ("neutro", "", 1.0), "neutral": ("neutro", "", 1.0), "calm": ("calmo", "low pitch", 0.95),
    "fear": ("medo", "high pitch", 1.08), "fearful": ("medo", "high pitch", 1.08),
    "disgust": ("desgosto", "low pitch", 0.95), "surprise": ("surpresa", "very high pitch", 1.12),
}


def _prosody(path: Path) -> dict:
    """Pistas acústicas baratas: volume, dinâmica e duração."""
    import numpy as np
    import soundfile as sf

    a, sr = sf.read(str(path), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    dur = max(0.1, len(a) / sr)
    w = max(1, int(0.025 * sr))
    en = np.array([float(np.sqrt(np.mean(a[i:i + w] ** 2)))
                   for i in range(0, max(1, len(a) - w), w)]) if len(a) > w else np.array([0.0])
    return {"rms": float(np.sqrt(np.mean(a ** 2))), "dyn": float(np.std(en)), "dur": dur}


def _emotion_light(path: Path, text: str):
    """Prosódia (energia + ritmo) -> (rótulo, pitch, velocidade). Determinística."""
    p = _prosody(path)
    rate = len(text) / p["dur"]
    forte, fraco, expr = p["rms"] > 0.16, p["rms"] < 0.06, p["dyn"] > 0.05
    rapido, lento = rate > 16, rate < 9
    pitch = "high pitch" if (forte and expr) else ("low pitch" if fraco else "")
    speed = 1.10 if rapido else (0.90 if lento else 1.0)
    if forte and expr:
        lbl = "animado"
    elif forte:
        lbl = "intenso"
    elif fraco:
        lbl = "suave"
    elif rapido:
        lbl = "ágil"
    elif lento:
        lbl = "calmo"
    else:
        lbl = "neutro"
    return (lbl, pitch, speed)


def _emotion_accurate(path: Path):
    """Modelo SER (wav2vec2) -> categoria -> (rótulo, pitch, velocidade), com gate."""
    audio = _wav_to_mono16k(path)  # array 16 kHz -> sem ffmpeg_read do transformers
    with _ser_lock:
        if _ser["clf"] is None:
            from transformers import pipeline
            _ser["clf"] = pipeline("audio-classification", model=SER_REPO)
        res = _ser["clf"]({"raw": audio, "sampling_rate": 16000}, top_k=None)
    del audio
    _touch_use("ser")
    if not res:
        return ("neutro", "", 1.0)
    top = max(res, key=lambda x: x.get("score", 0.0))
    lab = str(top.get("label", "")).lower()
    # baixa confiança ou neutro -> não força emoção (evita falso "alegre/raiva")
    if top.get("score", 0.0) < 0.5 or lab in ("neu", "neutral"):
        return ("neutro", "", 1.0)
    return _SER_EMO.get(lab, ("neutro", "", 1.0))


def _emotion_instruct(path: Path, text: str, mode: str):
    """Retorna (rótulo, pitch_tag, fator_velocidade, erro). mode: off|light|accurate."""
    try:
        if mode == "light":
            lbl, pitch, spd = _emotion_light(path, text)
            return (lbl, pitch, spd, None)
        if mode == "accurate":
            lbl, pitch, spd = _emotion_accurate(path)
            return (lbl, pitch, spd, None)
    except Exception as exc:  # noqa: BLE001
        return ("", "", 1.0, str(exc))
    return ("", "", 1.0, None)


@app.post("/api/translate/warmup")
def translate_warmup():
    """Carrega whisper + LLM de tradução em background — chamado quando o usuário
    abre/começa o tradutor, p/ os modelos ficarem quentes antes da 1ª frase."""
    # não aquece o que vai pro remoto quando "liberar local" está ligado
    skip_stt = _settings.get("free_local_on_remote") and _use_remote_stt()
    skip_mt = _settings.get("free_local_on_remote") and _use_remote_translate()

    def _warm():
        if not skip_stt:
            try:
                import numpy as np
                import mlx_whisper
                with _stt_lock:
                    mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32),
                                           path_or_hf_repo=WHISPER_REPO, language="pt")
                _touch_use("stt")
                _release_mlx_memory()
            except Exception:  # noqa: BLE001
                pass
        if not skip_mt:
            try:
                from mlx_lm import load
                repo = _mt_repo()
                with _mt_lock:
                    if _mt["model"] is None or _mt.get("repo") != repo:
                        _mt["model"], _mt["tok"] = load(repo)
                        _mt["repo"] = repo
                _touch_use("mt")
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=_warm, daemon=True).start()
    return {"ok": True}


@app.post("/api/models/unload")
def unload_models(payload: dict = None):
    """Descarrega modelos LOCAIS (MLX) p/ liberar RAM. Sem corpo = todos os locais.
    {"only_remote": true} = só os que têm remoto ativo."""
    p = payload or {}
    if p.get("only_remote"):
        r = _unload_local_models(tts=_use_remote_tts(), stt=_use_remote_stt(), mt=_use_remote_translate())
    else:
        r = _unload_local_models()
    return r


def _save_audio_upload(upload: UploadFile, prefix: str = ".stt") -> Path:
    """Grava um UploadFile de áudio e devolve um WAV 16k mono limpo pro STT:
    decodifica qualquer formato via ffmpeg + denoise espectral (afftdn) +
    trim de silêncio nas pontas — o Whisper alucina em ruído puro."""
    ctype = (upload.content_type or "").split(";")[0].strip().lower()
    por_ctype = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
                 "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
                 "audio/flac": ".flac", "audio/wav": ".wav", "audio/x-wav": ".wav",
                 "audio/wave": ".wav"}
    nome = (upload.filename or "").lower()
    suffix = por_ctype.get(ctype)
    if not suffix:
        for ext, sfx in (".webm", ".webm"), (".m4a", ".m4a"), (".mp4", ".m4a"), \
                         (".mp3", ".mp3"), (".ogg", ".ogg"), (".flac", ".flac"), (".wav", ".wav"):
            if nome.endswith(ext):
                suffix = sfx
                break
        else:
            suffix = ".wav"
    # cap de tamanho: leitura limitada (sem confiar em Content-Length) —
    # evita encher disco/memória com upload gigante de quem tem a chave
    limite_mb = int(os.environ.get("TTS_MAX_UPLOAD_MB", "600"))
    dados = upload.file.read(limite_mb * 1024 * 1024 + 1)
    if len(dados) > limite_mb * 1024 * 1024:
        raise HTTPException(413, f"Áudio maior que {limite_mb} MB")
    if not dados:
        raise HTTPException(400, "Áudio vazio")
    raw = OUTPUTS_DIR / f"{prefix}-{uuid.uuid4().hex[:10]}{suffix}"
    raw.write_bytes(dados)
    wav = OUTPUTS_DIR / f"{prefix}-{uuid.uuid4().hex[:10]}.wav"
    try:
        # denoise espectral (afftdn) + trim de silêncio nas pontas + 16k mono:
        # o Whisper alucina em ruído puro — sem isso o "nada" vira "E aí"
        filtro = ("highpass=f=80,afftdn=nr=12,"
                  "silenceremove=start_periods=1:start_threshold=-45dB,"
                  "areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse")
        p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw),
                            "-af", filtro, "-ar", "16000", "-ac", "1", str(wav)],
                           capture_output=True, timeout=120, text=True)
        stderr = (p.stderr or "").strip()
        if p.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
            print(f"[stt] ffmpeg ({ctype}, {len(dados)}B): {stderr[:160]}", flush=True)
            raise RuntimeError(stderr[:160] or "ffmpeg não produziu saída")
    except HTTPException:
        raise
    except Exception as e:
        raw.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
        raise HTTPException(400, f"Formato de áudio não suportado ({ctype or 'desconhecido'}): {e}") from e
    raw.unlink(missing_ok=True)
    return wav


@app.post("/api/stt-partial")
def stt_partial(audio: UploadFile = None, source_lang: str = Form("auto")):
    """Transcrição parcial e rápida (sem tradução/TTS) — usada para mostrar as
    palavras na tela enquanto o usuário ainda fala. Sem filtro anti-ruído: é só
    prévia ao vivo, a versão final vem do /api/translate-speech."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    tmp = _save_audio_upload(audio, ".pstt")
    try:
        r = _transcribe(tmp, language=(source_lang or "auto").lower())
    except Exception:  # noqa: BLE001 — prévia: nunca derruba a UI
        return {"text": ""}
    finally:
        tmp.unlink(missing_ok=True)
    return {"text": (r.get("text") or "").strip(),
            "language": (r.get("language") or "").strip().lower()}


def _parse_time(v) -> "float | None":
    """Aceita segundos (12.5), MM:SS (1:23) ou HH:MM:SS (1:02:03). None se vazio."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        if ":" in s:
            sec = 0.0
            for part in s.split(":"):
                sec = sec * 60 + float(part)
            return sec
        return float(s)
    except (ValueError, TypeError):
        return None


def _yt_retryable(exc: BaseException) -> bool:
    """403/SABR: o client InnerTube escolhido devolveu URL que o CDN recusa.
    Outros erros (vídeo privado, indisponível) não valem retry."""
    msg = str(exc).lower()
    return any(s in msg for s in ("403", "forbidden", "unable to download video data",
                                  "sign in to confirm"))


def _youtube_audio(url: str, start_s: float, end_s: float) -> bytes:
    """Baixa o áudio do YouTube (yt-dlp) e recorta [start,end] em WAV 24k mono com
    o ffmpeg estático (imageio-ffmpeg) — não depende de ffmpeg do sistema."""
    import glob
    import subprocess
    import tempfile

    import imageio_ffmpeg
    import yt_dlp

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    d = tempfile.mkdtemp(prefix="yt-")
    try:
        outtmpl = os.path.join(d, "src.%(ext)s")
        # YouTube SABR (2026-08): android_vr/web_safari devolvem URL sem stream
        # direto e o CDN responde 403. yt-dlp >= 2026.08.19 troca o default
        # (visionos); se ainda 403, cai para android/mweb/ios.
        # ejs:github resolve os desafios JS (n/sig) — sem isso faltam formatos.
        client_attempts = (None, ["android"], ["mweb"], ["ios"])
        last_err = None
        srcs = []
        for clients in client_attempts:
            for leftover in glob.glob(os.path.join(d, "src.*")):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "socket_timeout": 30,
                "max_filesize": 300 * 1024 * 1024,
                "retries": 3,
                "remote_components": ["ejs:github"],
            }
            if clients:
                opts["extractor_args"] = {"youtube": {"player_client": clients}}
            # vídeos que exigem login ("sign in to confirm"): cookies exportados
            # do navegador (formato Netscape) via TTS_ROD_YT_COOKIES=/caminho.txt
            cookies = (os.environ.get("TTS_ROD_YT_COOKIES") or "").strip()
            if cookies:
                ck = Path(cookies).expanduser()
                if ck.exists():
                    opts["cookiefile"] = str(ck)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if not _yt_retryable(exc):
                    raise
                continue
            srcs = glob.glob(os.path.join(d, "src.*"))
            if srcs:
                break
            last_err = RuntimeError("nada baixado (vídeo indisponível ou maior que o limite)")
        else:
            raise last_err or RuntimeError("nada baixado (vídeo indisponível ou maior que o limite)")
        out = os.path.join(d, "out.wav")
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start_s}", "-t", f"{end_s - start_s}", "-i", srcs[0],
               "-ar", "24000", "-ac", "1", "-f", "wav", out]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"ffmpeg: {p.stderr.decode()[:200]}")
        if os.path.getsize(out) < 2000:   # ~vazio: trecho fora da duração do vídeo
            raise RuntimeError("trecho vazio — o tempo de fim passou da duração do vídeo?")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@app.post("/api/youtube-audio")
def youtube_audio(payload: dict):
    """Extrai um trecho de áudio de um link do YouTube -> WAV 24k mono. Usado como
    fonte de voz (treino) e de transcrição."""
    from urllib.parse import urlparse

    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Informe o link do YouTube")
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not (
            host == "youtu.be" or host.endswith(("youtube.com", "youtube-nocookie.com"))):
        raise HTTPException(400, "Use um link do YouTube (youtube.com ou youtu.be)")
    start = max(0.0, _parse_time(payload.get("start")) or 0.0)
    end = _parse_time(payload.get("end"))
    if end is None or end <= start:
        raise HTTPException(400, "Informe início e fim (fim maior que início)")
    if end - start > 600:
        raise HTTPException(400, "Trecho longo demais (máx. 10 min)")
    try:
        data = _youtube_audio(url, start, end)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).strip()[:240]
        if _yt_retryable(exc):
            raise HTTPException(
                400,
                "YouTube recusou o download (403). Atualize o yt-dlp "
                "(pip install -U 'yt-dlp>=2026.08.19') e tente de novo.",
            ) from exc
        raise HTTPException(400, f"Falha ao extrair do YouTube: {msg}") from exc
    return Response(content=data, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Verificação de locutor (biometria de voz, opcional): cadastra perfis com
# embeddings (Resemblyzer/GE2E, CPU) e usa no gate do STT — só vozes
# autorizadas são transcritas, ou etiqueta quem falou.

SPEAKER_MAX_SAMPLES = 5      # embeddings por perfil (média robuza o timbre)
_speaker_lock = threading.Lock()
_speaker_encoder = None      # VoiceEncoder carregado sob demanda


def _speaker_embed(wav_path: Path) -> list[float] | None:
    """Embedding 256-d do wav 16k mono. None = lib ausente/áudio curto demais."""
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
        global _speaker_encoder
        if _speaker_encoder is None:
            _speaker_encoder = VoiceEncoder()
        f32 = preprocess_wav(wav_path)  # carrega 16k mono + trim VAD próprio
        if len(f32) < 16000 * 0.6:      # < 0.6s não gera embedding confiável
            return None
        return [float(x) for x in _speaker_encoder.embed_utterance(f32)]
    except Exception as e:  # noqa: BLE001 — sem o gate não derruba o STT
        print(f"[speaker] embedding falhou: {e}", flush=True)
        return None


def _cos_sim(a, b) -> float:
    import math
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    return num / (da * db) if da and db else 0.0


def _speaker_load() -> dict:
    try:
        d = json.loads(SPEAKER_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — arquivo ausente/corrompido = vazio
        return {}


def _speaker_save(perfis: dict) -> None:
    SPEAKER_PATH.write_text(json.dumps(perfis))
    try:
        SPEAKER_PATH.chmod(0o600)
    except OSError:
        pass


def _speaker_identify(wav_path: Path) -> tuple[str | None, float]:
    """Quem fala no wav: (nome, melhor_sim) ou (None, melhor_sim) se ninguém."""
    emb = _speaker_embed(wav_path)
    if not emb:
        return None, 0.0
    melhor, sim = None, 0.0
    for nome, p in _speaker_load().items():
        for vec in p.get("vecs", []):
            s = _cos_sim(emb, vec)
            if s > sim:
                melhor, sim = nome, s
    return melhor, sim


def _speaker_gate_ok(wav_path: Path) -> dict:
    """Aplica o gate configurado. Devolve dict p/ enriquecer a resposta:
    rejected=True bloqueia; speaker=<nome> etiqueta o locutor."""
    gate = (_settings.get("speaker_gate") or "off").lower()
    if gate == "off":
        return {}
    perfis = _speaker_load()
    if not perfis:
        return {}  # sem perfis cadastrados: gate inerte
    emb = _speaker_embed(wav_path)
    if not emb:
        # sem embedding (áudio curto/erro): enforce rejeita, label segue
        return ({"rejected": True, "reason": "áudio curto p/ identificar a voz"}
                if gate == "enforce" else {})
    melhor, sim = None, 0.0
    for nome, p in perfis.items():
        for vec in p.get("vecs", []):
            s = _cos_sim(emb, vec)
            if s > sim:
                melhor, sim = nome, s
    lim = float(_settings.get("speaker_threshold") or 0.75)
    autorizado = melhor is not None and sim >= lim
    out: dict = {}
    if gate == "enforce" and not autorizado:
        out["rejected"] = True
        out["reason"] = (f"voz não autorizada (melhor: {melhor or 'ninguém'} "
                         f"{sim:.2f} < {lim:.2f})" if melhor else
                         f"voz não reconhecida ({sim:.2f} < {lim:.2f})")
    if gate == "label" and autorizado:
        out["speaker"] = melhor
        out["speaker_sim"] = round(sim, 3)
    return out


@app.post("/api/speaker/enroll")
async def speaker_enroll(name: str = Form(...), audio: UploadFile = None):
    """Cadastra/atualiza um perfil de voz com o áudio enviado (8–30 s de fala)."""
    nome = (name or "").strip()[:40]
    if not nome:
        raise HTTPException(400, "Nome do perfil obrigatório")
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    tmp = _save_audio_upload(audio, ".voz")
    try:
        emb = _speaker_embed(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if not emb:
        raise HTTPException(400, "Não deu para extrair a voz — grave 8s+ falando")
    with _speaker_lock:
        perfis = _speaker_load()
        p = perfis.get(nome, {"vecs": [], "updated": 0})
        vecs = p.get("vecs", [])
        vecs.append(emb)
        p["vecs"] = vecs[-SPEAKER_MAX_SAMPLES:]
        p["updated"] = time.time()
        perfis[nome] = p
        _speaker_save(perfis)
    return {"ok": True, "name": nome, "samples": len(p["vecs"])}


@app.get("/api/speaker/profiles")
def speaker_profiles():
    with _speaker_lock:
        perfis = _speaker_load()
    return [{"name": n, "samples": len(p.get("vecs", [])),
             "updated": p.get("updated", 0)} for n, p in sorted(perfis.items())]


@app.delete("/api/speaker/{name}")
def speaker_delete(name: str):
    with _speaker_lock:
        perfis = _speaker_load()
        if perfis.pop(name, None) is None:
            raise HTTPException(404, "Perfil não encontrado")
        _speaker_save(perfis)
    return {"ok": True}


@app.post("/api/speaker/check")
async def speaker_check(audio: UploadFile = None):
    """Diagnóstico: quem é a voz do áudio e a similaridade (sem gate)."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    tmp = _save_audio_upload(audio, ".vozchk")
    try:
        emb = _speaker_embed(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if not emb:
        raise HTTPException(400, "Áudio curto demais p/ identificar (grave 8s+)")
    melhor, sim = None, 0.0
    for nome, p in _speaker_load().items():
        for vec in p.get("vecs", []):
            s = _cos_sim(emb, vec)
            if s > sim:
                melhor, sim = nome, s
    return {"speaker": melhor, "sim": round(sim, 3)}


@app.post("/api/transcribe")
def transcribe_audio(audio: UploadFile = None, source_lang: str = Form("auto")):
    """Transcrição pura (sem tradução/TTS): áudio -> texto + segmentos. Usa o
    Whisper local ou remoto, conforme as configurações de modelos remotos."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    tmp = _save_audio_upload(audio)
    try:
        gate = _speaker_gate_ok(tmp)
        if gate.get("rejected"):
            return {"rejected": True, "reason": gate["reason"], "text": ""}
        r = _transcribe(tmp, language=(source_lang or "auto").lower())
    finally:
        tmp.unlink(missing_ok=True)
    segs = r.get("segments") or []
    out = {"text": (r.get("text") or "").strip(),
           "language": (r.get("language") or "").strip().lower(),
           "segments": [{"start": float(s.get("start") or 0.0),
                         "end": float(s.get("end") or 0.0),
                         "text": (s.get("text") or "").strip()} for s in segs]}
    if gate.get("speaker"):
        out["speaker"] = gate["speaker"]
        out["speaker_sim"] = gate.get("speaker_sim")
    return out


@app.post("/api/translate-speech")
def translate_speech(audio: UploadFile = None, target_lang: str = Form("en"),
                     voice_id: str = Form(""), source_lang: str = Form("auto"),
                     emotion_mode: str = Form("off"), instruct: str = Form("")):
    """fala (áudio) -> transcreve -> traduz -> dispara TTS na voz; devolve textos + job_id."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    tgt = (target_lang or "en").lower()
    vid = voice_id or _settings["default_voice"]
    design = (vid == DESIGN_VOICE_ID)                       # voz por descrição (tags OmniVoice)
    des_instruct = _sanitize_instruct(instruct) if design else ""
    vpath = VOICES_DIR / f"{vid}.wav"
    if design:
        ok = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
        if not ok:
            raise HTTPException(400, "Voice design vazio — descreva a voz p/ o tradutor")
    elif not vpath.exists() and vid not in OMNI_PRESETS:
        raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    tmp = _save_audio_upload(audio)
    gate = _speaker_gate_ok(tmp)
    if gate.get("rejected"):
        tmp.unlink(missing_ok=True)
        return {"rejected": True, "reason": gate["reason"], "source_text": ""}
    emo_label, emo_pitch, emo_speed, emo_err = "", "", 1.0, None
    try:
        r = _transcribe(tmp, language=(source_lang or "auto").lower())
        src_text = (r.get("text") or "").strip()
        src_lang = (r.get("language") or "").strip().lower()
        ok, motivo = _stt_ok(r, src_text)
        if not ok:
            # não é erro: ruído/silêncio — o cliente apenas ignora e segue ouvindo
            return {"rejected": True, "reason": motivo, "source_text": src_text}
        # filtro de idioma de entrada: só segue se a fala estiver no idioma escolhido
        exp = (source_lang or "auto").lower()
        if exp not in ("", "auto") and src_lang and src_lang != exp:
            return {"rejected": True, "reason": f"idioma errado (detectou {src_lang})",
                    "source_text": src_text, "source_lang": src_lang}
        # captura de emoção (precisa do áudio ainda em disco)
        modo = (emotion_mode or "off").lower()
        if modo in ("light", "accurate"):
            emo_label, emo_pitch, emo_speed, emo_err = _emotion_instruct(tmp, src_text, modo)
    finally:
        tmp.unlink(missing_ok=True)

    emotivo = bool(emo_label) and emo_label != "neutro"
    # 1) o LLM já traduz no TOM da emoção (pontuação/ênfase) -> prosódia segue o texto
    translation = _translate(src_text, tgt, emo_label if emotivo else None)
    omni = _resolve_omni({})
    if design:   # voz por descrição: o instruct do tradutor define a voz (vence o clone/emoção)
        omni["instruct"] = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
    if emotivo:
        # 2) pitch = tag válida (nudge, mantém o clone); 3) velocidade = time-stretch
        #    em voice design o pitch da emoção NÃO troca a voz desenhada (mantém o instruct);
        #    a emoção ainda atua via velocidade/expressividade + tom do texto traduzido.
        ep = _sanitize_instruct(emo_pitch)
        if ep and not design:
            omni["instruct"] = ep
        if emo_speed and abs(float(emo_speed) - 1.0) > 1e-3:
            base = float(omni.get("speed") or 1.0)
            omni["speed"] = round(_clamp(base * float(emo_speed), 0.5, 2.0, base), 3)
        # 4) mais expressivo/menos monótono (guidance↓, position_temperature↑)
        omni["guidance_scale"] = round(max(0.5, float(omni.get("guidance_scale") or 2.0) - 0.4), 2)
        omni["position_temperature"] = round(min(20.0, float(omni.get("position_temperature") or 5.0) + 4.0), 1)

    job_id = uuid.uuid4().hex[:10]
    _jobs[job_id] = {"status": "running", "pieces": 0, "total": None,
                     "progress": None, "output": None, "error": None,
                     "text": translation[:200]}
    _evict_jobs()
    threading.Thread(
        target=_run_tts_job,
        args=(job_id, translation, vid, vpath, tgt, omni),
        daemon=True,
    ).start()
    emo_show = None
    if emo_label and emo_label != "neutro":
        bits = [b for b in (emo_pitch, f"{omni['speed']}×" if abs(float(omni.get('speed') or 1) - 1) > 1e-3 else "") if b]
        emo_show = emo_label + (f" ({', '.join(bits)})" if bits else "")
    return {"job_id": job_id, "source_text": src_text, "source_lang": src_lang,
            "translation": translation, "target_lang": tgt,
            "emotion": emo_show, "emotion_error": emo_err,
            **({"speaker": gate["speaker"]} if gate.get("speaker") else {})}


@app.post("/api/modify-speech")
def modify_speech(audio: UploadFile = None, voice_id: str = Form(""),
                  source_lang: str = Form("auto"), emotion_mode: str = Form("off"),
                  instruct: str = Form("")):
    """MODIFICADOR: fala -> transcreve -> TTS na voz escolhida, SEM traduzir (mesma
    língua, mesmas palavras). Igual ao tradutor, mas sem o passo do LLM."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    vid = voice_id or _settings["default_voice"]
    design = (vid == DESIGN_VOICE_ID)
    des_instruct = _sanitize_instruct(instruct) if design else ""
    vpath = VOICES_DIR / f"{vid}.wav"
    if design:
        ok = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
        if not ok:
            raise HTTPException(400, "Voice design vazio — descreva a voz")
    elif not vpath.exists() and vid not in OMNI_PRESETS:
        raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    tmp = _save_audio_upload(audio)
    emo_label, emo_pitch, emo_speed, emo_err = "", "", 1.0, None
    try:
        r = _transcribe(tmp, language=(source_lang or "auto").lower())
        src_text = (r.get("text") or "").strip()
        src_lang = (r.get("language") or "").strip().lower()
        ok, motivo = _stt_ok(r, src_text)
        if not ok:
            return {"rejected": True, "reason": motivo, "source_text": src_text}
        exp = (source_lang or "auto").lower()
        if exp not in ("", "auto") and src_lang and src_lang != exp:
            return {"rejected": True, "reason": f"idioma errado (detectou {src_lang})",
                    "source_text": src_text, "source_lang": src_lang}
        modo = (emotion_mode or "off").lower()
        if modo in ("light", "accurate"):
            emo_label, emo_pitch, emo_speed, emo_err = _emotion_instruct(tmp, src_text, modo)
    finally:
        tmp.unlink(missing_ok=True)

    out_text = src_text                                    # SEM tradução: fala o que foi dito
    lang = exp if exp not in ("", "auto") else (src_lang or _settings["language"])
    emotivo = bool(emo_label) and emo_label != "neutro"
    omni = _resolve_omni({})
    if design:
        omni["instruct"] = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
    if emotivo:
        ep = _sanitize_instruct(emo_pitch)
        if ep and not design:
            omni["instruct"] = ep
        if emo_speed and abs(float(emo_speed) - 1.0) > 1e-3:
            base = float(omni.get("speed") or 1.0)
            omni["speed"] = round(_clamp(base * float(emo_speed), 0.5, 2.0, base), 3)
        omni["guidance_scale"] = round(max(0.5, float(omni.get("guidance_scale") or 2.0) - 0.4), 2)
        omni["position_temperature"] = round(min(20.0, float(omni.get("position_temperature") or 5.0) + 4.0), 1)

    job_id = uuid.uuid4().hex[:10]
    _jobs[job_id] = {"status": "running", "pieces": 0, "total": None, "progress": None,
                     "output": None, "error": None, "text": out_text[:200]}
    _evict_jobs()
    threading.Thread(target=_run_tts_job, args=(job_id, out_text, vid, vpath, lang, omni), daemon=True).start()
    emo_show = None
    if emo_label and emo_label != "neutro":
        bits = [b for b in (emo_pitch, f"{omni['speed']}×" if abs(float(omni.get('speed') or 1) - 1) > 1e-3 else "") if b]
        emo_show = emo_label + (f" ({', '.join(bits)})" if bits else "")
    return {"job_id": job_id, "source_text": src_text, "source_lang": src_lang,
            "translation": out_text, "target_lang": lang, "emotion": emo_show, "emotion_error": emo_err}


# ---------------------------------------------------------------------------
# API compatível com OpenAI (POST /v1/audio/speech) — funciona com o SDK da
# OpenAI e clientes xAI/Grok apontando base_url para http://127.0.0.1:7860/v1
# ---------------------------------------------------------------------------

# formato -> (args do ffmpeg, content-type)
_AUDIO_FORMATS = {
    "mp3": (["-f", "mp3", "-b:a", "128k"], "audio/mpeg"),
    "wav": (["-f", "wav"], "audio/wav"),
    "flac": (["-f", "flac"], "audio/flac"),
    "aac": (["-f", "adts", "-c:a", "aac"], "audio/aac"),
    "opus": (["-f", "ogg", "-c:a", "libopus"], "audio/ogg"),
    "pcm": (["-f", "s16le", "-ar", "24000", "-ac", "1"], "audio/pcm"),
}


def _resolve_voice(voice) -> str:
    """Aceita id ou nome; desconhecida cai na voz padrão do dashboard ou na mais recente."""
    # voz virtual de VOICE DESIGN: gera só do instruct (sem clone). Aceita
    # "__design__" ou "design" -> a API pode pedir uma voz projetada por texto.
    if voice and str(voice).strip().lower() in (DESIGN_VOICE_ID, "design"):
        return DESIGN_VOICE_ID
    voices = list_voices()
    if not voices:
        raise HTTPException(404, "Nenhuma voz gravada — grave uma na UI primeiro")
    for v in voices:
        if voice and (v["id"] == voice or v["name"].lower() == str(voice).lower()):
            return v["id"]
    padrao = _settings["default_voice"]
    if padrao and any(v["id"] == padrao for v in voices):
        return padrao
    return voices[0]["id"]  # mais recente


def _encode_audio(wav_path: Path, fmt: str, speed: float) -> tuple[bytes, str]:
    args, mime = _AUDIO_FORMATS[fmt]
    if fmt == "wav" and abs(speed - 1.0) < 1e-3:
        return wav_path.read_bytes(), mime
    cmd = [FFMPEG, "-v", "error", "-i", str(wav_path)]
    if abs(speed - 1.0) >= 1e-3:
        cmd += ["-filter:a", _atempo_chain(speed)]
    cmd += args + ["pipe:1"]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise HTTPException(500, f"Conversão de áudio falhou: {proc.stderr.decode()[:200]}")
    return proc.stdout, mime


@app.get("/v1/models")
def openai_models():
    agora = int(time.time())
    return {"object": "list", "data": [
        {"id": m, "object": "model", "created": agora, "owned_by": "tts-studio"}
        for m in ("tts-1", "tts-1-hd", "whisper-1")
    ]}


def _ts_srt(x: float, sep: str = ",") -> str:
    h = int(x // 3600); mm = int((x % 3600) // 60); s = x % 60
    return f"{h:02d}:{mm:02d}:{s:06.3f}".replace(".", sep)


def _segs_to_srt(segs) -> str:
    return "\n".join(f"{i}\n{_ts_srt(s['start'])} --> {_ts_srt(s['end'])}\n{s['text']}\n"
                     for i, s in enumerate(segs, 1))


def _segs_to_vtt(segs) -> str:
    body = "\n".join(f"{_ts_srt(s['start'], '.')} --> {_ts_srt(s['end'], '.')}\n{s['text']}\n" for s in segs)
    return "WEBVTT\n\n" + body


def _openai_stt(file, language, response_format, translate):
    """STT OpenAI-compatível: áudio -> texto (+ segmentos). translate=True traduz p/ inglês."""
    if file is None:
        raise HTTPException(400, "Campo 'file' obrigatório")
    tmp = _save_audio_upload(file)
    try:
        r = _transcribe(tmp, language=(language or "auto").lower())
    finally:
        tmp.unlink(missing_ok=True)
    text = (r.get("text") or "").strip()
    lang = (r.get("language") or "").strip().lower()
    segs = [{"start": float(s.get("start") or 0.0), "end": float(s.get("end") or 0.0),
             "text": (s.get("text") or "").strip()} for s in (r.get("segments") or [])]
    if translate and text:                       # /translations -> inglês (agrega; srt/vtt ficam no original)
        text = _translate(text, "en")
    rf = (response_format or "json").lower()
    if rf == "text":
        return Response(text + "\n", media_type="text/plain; charset=utf-8")
    if rf == "srt":
        return Response(_segs_to_srt(segs), media_type="application/x-subrip; charset=utf-8")
    if rf == "vtt":
        return Response(_segs_to_vtt(segs), media_type="text/vtt; charset=utf-8")
    if rf == "verbose_json":
        dur = max((s["end"] for s in segs), default=0.0)
        return {"task": "translate" if translate else "transcribe", "language": lang,
                "duration": round(dur, 3), "text": text,
                "segments": [{"id": i, "start": s["start"], "end": s["end"], "text": s["text"]}
                             for i, s in enumerate(segs)]}
    return {"text": text}


@app.post("/v1/audio/transcriptions")
def openai_transcriptions(file: UploadFile = File(...), model: str = Form("whisper-1"),
                          language: str = Form(None), prompt: str = Form(None),
                          response_format: str = Form("json"), temperature: float = Form(0.0)):
    """STT compatível com OpenAI Whisper. response_format: json|text|srt|verbose_json|vtt."""
    return _openai_stt(file, language, response_format, translate=False)


@app.post("/v1/audio/translations")
def openai_translations(file: UploadFile = File(...), model: str = Form("whisper-1"),
                        prompt: str = Form(None), response_format: str = Form("json"),
                        temperature: float = Form(0.0)):
    """STT + tradução p/ inglês (compatível com OpenAI). response_format igual ao de transcriptions."""
    return _openai_stt(file, None, response_format, translate=True)


@app.post("/v1/audio/speech")
def openai_speech(payload: dict):
    # NOTA: síncrono por design — o SDK OpenAI espera o áudio na resposta. O
    # t.join() abaixo segura 1 thread do threadpool do Starlette (40 por
    # default) até 10 min por request; a fila de falas serializa clientes em
    # série, então o pool não esgota no uso normal (LAN).
    text = (payload.get("input") or "").strip()
    if not text:
        raise HTTPException(400, "Campo 'input' vazio")
    if len(text) > 5000:
        raise HTTPException(400, "Texto longo demais (máx. 5000 caracteres)")

    if _settings["pre_prompt"]:
        text = f"{_settings['pre_prompt']} {text}".strip()
    fmt = payload.get("response_format", "mp3")
    if fmt not in _AUDIO_FORMATS:
        raise HTTPException(400, f"response_format inválido. Suportados: {', '.join(_AUDIO_FORMATS)}")
    voice_id = _resolve_voice(payload.get("voice"))
    language = (payload.get("language") or _settings["language"]).lower()
    omni = _resolve_omni(payload)  # inclui speed -> aplicado nativamente pelo modelo
    if voice_id == DESIGN_VOICE_ID and not (omni.get("instruct") or "").strip():
        raise HTTPException(400, "voice='__design__' exige 'instruct' (descrição da voz) — "
                                 "ex.: 'female, young adult, high pitch' — ou defina omni_instruct nas settings")
    # tts-1-hd força mais passos de difusão (qualidade); senão vale o padrão/override
    if str(payload.get("model", "tts-1")).endswith("-hd") and "num_steps" not in payload:
        omni["num_steps"] = OMNI_STEPS_HQ

    # reusa o pipeline de jobs de forma síncrona (histórico incluso)
    job_id = uuid.uuid4().hex[:10]
    _jobs[job_id] = {"status": "running", "pieces": 0, "total": None,
                     "progress": None, "output": None, "error": None,
                     "text": text[:200]}
    # mesma mecânica do /api/tts: MLX exige thread "nova" (stream GPU é
    # thread-local e o threadpool do FastAPI reusa threads sem stream)
    t = threading.Thread(
        target=_run_tts_job,
        args=(job_id, text, voice_id, VOICES_DIR / f"{voice_id}.wav", language, omni),
        daemon=True,
    )
    t.start()
    t.join(timeout=600)
    if t.is_alive():
        raise HTTPException(504, "Síntese excedeu 10 minutos")
    job = _jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(500, f"Falha na síntese: {job.get('error')}")

    wav_path = OUTPUTS_DIR / f"{job['output']['id']}.wav"
    # velocidade já aplicada nativamente pelo modelo; aqui ffmpeg só converte o formato
    data, mime = _encode_audio(wav_path, fmt, 1.0)
    return Response(content=data, media_type=mime)


# descarrega TTS/STT/tradutor/SER ociosos (idle_unload_minutes; 0 = off)
threading.Thread(target=_idle_unload_loop, daemon=True).start()

# UI estática (registrada por último para não engolir /api/* e /v1/*)
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
