"""TTS-STUDIO — clonagem de voz local com gravação e gerenciamento de vozes.

Servidor FastAPI + backends TTS via MLX (Apple Silicon): OmniVoice, Qwen3-TTS,
Fish S2, Chatterbox, Kokoro, PocketTTS, VoxCPM2, Voxtral, etc.
Tudo local: nenhum áudio ou texto sai da máquina.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import queue
import re
import secrets as _secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import uuid
import wave
from collections import OrderedDict
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket)
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

# onnxruntime (caminho ONNX do VAD) tenta gravar o ID de telemetria em $HOME;
# com HOME não-gravável (sandbox/CI) ele larga um arquivo ":memory:.ses" no CWD.
# O run.sh já exporta isto para o servidor; aqui vale também para pytest, scripts
# e IDE, que carregam o VAD sem passar pelo run.sh. O app é local/offline: a
# telemetria não serve para nada e o warning suja a suíte. Fica ANTES dos
# imports locais porque o próprio onnxruntime lê a var ao ser importado.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

import dsh_client
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

# ---------------------------------------------------------------------------
# Limpeza de boot dos diretórios de job (outputs/.job-*)
#
# Um `.job-*` é trabalho de ALGUMA instância do app — o servidor da máquina, um
# smoke, um `pytest` (a suíte importa app). A limpeza antiga apagava tudo no
# import, então um segundo processo derrubava a geração do servidor vivo
# (`System error` ao escrever o trecho e 404 no status) e ainda matava o worker
# filho dele. Agora só sai o órfão de verdade: dir com dono VIVO fica.
# ---------------------------------------------------------------------------

_JOB_ORFAO_JANELA_S = 300             # sem dono registrado: atividade recente segura


def _pid_cmd(pid) -> str:
    """Cmdline do pid ("" se o processo não existe ou o `ps` não respondeu).

    `ps` pode faltar (sandbox/CI nega o exec): por isso a vida do processo é
    medida com `os.kill(pid, 0)` e a cmdline é só refinamento opcional. E sem
    ela não dá para distinguir pid reciclado — daí o "não respondeu" nunca ser
    tratado como "morto"."""
    try:
        return subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def _pid_vivo(pid) -> bool:
    """True se o processo existe (sem depender do ps)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                       # existe, mas é de outro usuário
    except OSError:
        return False


def _job_owner_pid(job_dir: Path):
    """Pid do processo que criou o job (None em dir de versão anterior)."""
    try:
        return int((job_dir / "owner.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _job_owner_vivo(job_dir: Path) -> bool:
    """True se o dono do job ainda é um processo python vivo desta app.

    Com a cmdline disponível exige "python": sem isso a reciclagem do pid por
    outro programa deixaria um dir velho preso para sempre. Sem `ps`, vale a
    vida do processo (o `owner.pid` é escrito por nós)."""
    pid = _job_owner_pid(job_dir)
    if not _pid_vivo(pid):
        return False
    cmd = _pid_cmd(pid)
    return "python" in cmd.lower() if cmd else True


def _job_dir_ativo(job_dir: Path) -> bool:
    """Dir de job que outra instância pode estar usando AGORA.

    `owner.pid` presente decide sozinho: dono vivo = fica, dono morto = órfão de
    crash e sai na hora (é o que queremos depois de um restart, em vez de deixar
    lixo 5 min). Só o dir SEM dono (versão anterior do app) cai na atividade
    recente — o dono reescreve status.json e cria trechos enquanto gera.
    `owner.pid` fica FORA dos mtimes: escrito uma vez no começo, ele mentiria
    "recente" num dir que já parou (o dir em si já conta a criação)."""
    if _job_owner_pid(job_dir) is not None:
        return _job_owner_vivo(job_dir)
    mtimes = []
    for alvo in (job_dir, job_dir / "status.json", job_dir / "0.wav"):
        try:
            mtimes.append(alvo.stat().st_mtime)
        except OSError:
            pass
    return bool(mtimes) and (time.time() - max(mtimes)) < _JOB_ORFAO_JANELA_S


# Workers filhos têm sessão própria e sobrevivem à morte do pai: recolhe só os
# que ficaram órfãos. A decisão é a MESMA do dir (`_job_dir_ativo`): dir vivo
# mantém o worker, senão o par ficava incoerente (dir preservado, worker morto,
# e quem pollava o worker era a instância antiga). Dir novo = dono vivo decide;
# dir de versão anterior cai na atividade recente.
for _pid_file in OUTPUTS_DIR.glob(".job-*/worker.pid"):
    try:
        _pid = int(_pid_file.read_text().strip())
        _cmd = _pid_cmd(_pid)
        if _cmd and "tts_worker.py" not in _cmd:
            continue                      # pid morto ou reciclado por outro
        if not _cmd and not _pid_vivo(_pid):
            continue                      # sem ps: só segue se o processo existe
        if _job_dir_ativo(_pid_file.parent):
            continue                      # instância viva ainda polla esse worker
        os.killpg(_pid, signal.SIGTERM)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

# trechos parciais de job interrompido não sobrevivem a restart
for _d in OUTPUTS_DIR.glob(".job-*"):
    if _job_dir_ativo(_d):
        continue
    if _d.is_dir():
        shutil.rmtree(_d, ignore_errors=True)
    else:
        _d.unlink(missing_ok=True)        # arquivo solto: rmtree não o alcança

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
    "stt_anti_ruido": True,           # desliga todos os filtros acima (só VAD e tamanho)
    "stt_denoise": True,              # denoise ffmpeg (afftdn) antes de cada transcrição
    "stt_local_engine": "whisper",    # STT local no Mac: whisper | parakeet
    "stt_whisper_repo": "",           # repo HF do Whisper local; vazio = large-v3-turbo
    "stt_beam": 5,                    # beam do STT remoto: 1=rápido, 5=padrão, 8=qualidade
    # Modelos remotos (API OpenAI-compatível). base_url deve terminar em /v1
    # (ex.: http://rtx-host:8000/v1). api_key opcional. Tudo local por padrão.
    # base_url + api_key ficam locais (settings.json é gitignored). Vazio = local.
    "remote_tts": False,              # síntese (OmniVoice) numa máquina remota (ex.: RTX)
    "remote_tts_url": "",             # URL completa do endpoint, ex.: http://rtx-host:8800/tts
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
    # Backend de IA alternativo: o harness `dsh` via ACP (ver dsh_client.py). "openai"
    # (default) = endpoint+chave acima, comportamento INTACTO; "dsh" = processo local
    # com o perfil sem tools. O default do modelo é a rota COM chave deste host (a
    # rota default do catálogo, `deepseek-official`, falha -32603 sem credencial).
    "chat_backend": "openai",
    # Backend SÓ do Live (#176): a recomendação de rota difere entre as duas telas
    # (no Live o dsh dá 1º token ~0,4 s contra 4–11 s do provedor remoto; na
    # Conversa o provedor atual serve). VAZIO = herda `chat_backend`.
    "chat_backend_live": "",
    "chat_dsh_bin": "dsh",            # `dsh` resolvido no PATH (ou caminho absoluto)
    "chat_dsh_profile": dsh_client.DSH_DEFAULT_PROFILE,
    "chat_dsh_model": dsh_client.DSH_DEFAULT_MODEL,   # par opaco JSON ["rota","modelo"]
    "chat_dsh_effort": "off",         # off|low|high|max (off = orçamento do Live)
    # Verificação de locutor (biometria de voz): off | enforce (só vozes
    # cadastradas) | label (transcreve todos e etiqueta quem falou)
    "speaker_gate": "off",
    "speaker_threshold": 0.6,         # rigor: similaridade cosseno mínima (0.35–0.9)
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
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


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
# STT alternativo local: NVIDIA Parakeet TDT 0.6B v3 (multilíngue, ~30x tempo real)
PARAKEET_REPO = os.environ.get("TTS_ROD_PARAKEET", "mlx-community/parakeet-tdt-0.6b-v3")
TRANSLATE_REPO = os.environ.get("TTS_ROD_TRANSLATE", "mlx-community/Qwen2.5-3B-Instruct-4bit")
# código -> nome em inglês (para o prompt de tradução e o lang do OmniVoice)
LANG_DISPLAY = {
    "pt": "Portuguese", "en": "English", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "zh": "Chinese",
    "ru": "Russian", "ko": "Korean", "ar": "Arabic", "nl": "Dutch",
}

# ---------------------------------------------------------------------------
# Chaves de API (multi): protegem /api/* e /v1/* na rede. Loopback e o próprio
# Mac (IP da LAN como origem) não exigem chave. Aceita Authorization: Bearer,
# Authorization: Bearer ou X-API-Key. Persistidas em
# .apikeys.json; migra de .apikey / TTS_ROD_API_KEY. Gestão na UI (Acesso).
# ---------------------------------------------------------------------------
_ENV_API_KEY = (os.environ.get("TTS_ROD_API_KEY") or "").strip()
_ADMIN_API_KEY = (os.environ.get("TTS_ROD_ADMIN_KEY") or "").strip()
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
    # sem chaves gerenciadas o .apikey NÃO é apagado: run.sh e a migração de
    # _load_apikeys() ainda o usam como bootstrap da chave.
    try:
        keys = _apikeys.get("keys") or []
        if keys:
            LEGACY_APIKEY_PATH.write_text(keys[0]["secret"] + "\n")
            try:
                os.chmod(LEGACY_APIKEY_PATH, 0o600)
            except Exception:  # noqa: BLE001
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
    """Auth na rede se enabled e existe ao menos uma chave (arquivo ou env).

    `TTS_ROD_ADMIN_KEY` conta como chave: quem sobe o app só com ela não pode
    ficar com a rede ABERTA (era o caso: `_key_is_valid` não a conhecia e
    `_auth_enabled` só olhava TTS_ROD_API_KEY/chaves gerenciadas)."""
    with _apikeys_lock:
        if not _apikeys.get("enabled", True):
            return False
        if _apikeys.get("keys"):
            return True
    return bool(_ENV_API_KEY or _ADMIN_API_KEY)


def _extract_request_key(request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _secrets_iguais(a: str, b: str) -> bool:
    """compare_digest sem estourar se os tamanhos diferem."""
    if not a or not b:
        return False
    try:
        return _secrets.compare_digest(a, b)
    except (TypeError, ValueError):
        return False


_own_ips_lock = threading.Lock()
_own_ips_cache: tuple[float, frozenset] = (0.0, frozenset())


def _peer_host(request) -> str:
    """IP do cliente TCP (sem o prefixo IPv4-mapeado em IPv6)."""
    host = (request.client.host if request.client else "") or ""
    if host.startswith("::ffff:"):
        host = host[7:]
    return host


def _own_ips() -> set[str]:
    """IPs deste Mac (loopback + interfaces). Cache curto: Wi-Fi muda."""
    global _own_ips_cache
    now = time.monotonic()
    with _own_ips_lock:
        ts, cached = _own_ips_cache
        if cached and (now - ts) < 15:
            return set(cached)
    ips = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            if ip and ip not in ("0.0.0.0", "255.255.255.255", "::"):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("1.1.1.1", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    frozen = frozenset(ips)
    with _own_ips_lock:
        _own_ips_cache = (now, frozen)
    return ips


def _lan_urls() -> list[str]:
    """URLs IPv4 da LAN p/ a UI mostrar 'abra isto no outro dispositivo'."""
    urls = []
    for ip in sorted(_own_ips()):
        if ip in ("127.0.0.1", "::1") or ":" in ip:
            continue
        if ip.startswith("169.254."):
            continue
        urls.append(f"http://{ip}:7860")
    return urls


def _is_local(request) -> bool:
    """Só loopback é considerado local sem autenticação.

    Não isentamos os IPs da própria máquina: em um túnel SSH reverso o
    processo SSH pode abrir a conexão para o IP LAN do Mac usando justamente
    um IP local como origem, o que transformaria uma requisição da internet
    em uma requisição "local".
    """
    host = _peer_host(request)
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    return False


def _key_is_valid(provided: str) -> bool:
    """A chave autentica em /api/* e /v1/*.

    A chave administrativa TAMBÉM vale como chave: ela é a credencial do
    operador, e sem isto o fluxo documentado ("defina TTS_ROD_ADMIN_KEY")
    devolvia 401 para quem só tinha a chave admin — e 403 de admin para quem
    só tinha a chave comum, ou seja, NADA administrava pela rede.
    """
    if not provided:
        return False
    if _ENV_API_KEY and _secrets_iguais(provided, _ENV_API_KEY):
        return True
    if _ADMIN_API_KEY and _secrets_iguais(provided, _ADMIN_API_KEY):
        return True
    with _apikeys_lock:
        return any(_secrets_iguais(provided, k.get("secret") or "")
                   for k in (_apikeys.get("keys") or []))


def _key_role(provided: str) -> str | None:
    """Papel da chave apresentada: "admin" | "use" | None (não é chave gerenciada).

    None = linha antiga, sem o campo — mantém a regra de compatibilidade (uma
    chave válida administra enquanto não existir `TTS_ROD_ADMIN_KEY`)."""
    if not provided:
        return None
    with _apikeys_lock:
        for k in (_apikeys.get("keys") or []):
            if _secrets_iguais(provided, k.get("secret") or ""):
                return (k.get("role") or "").strip().lower() or None
    return None


def _admin_is_allowed(request) -> bool:
    """Admin local, chave administrativa dedicada ou chave com papel `admin`.

    O papel por chave (`role`) é a separação explícita "chave de uso x chave de
    administração" sem depender de variável de ambiente; linha sem `role`
    (arquivo anterior ao campo) preserva a política antiga."""
    if _is_local(request):
        return True
    provided = _extract_request_key(request)
    if _ADMIN_API_KEY and _secrets_iguais(provided, _ADMIN_API_KEY):
        return True
    papel = _key_role(provided)
    if papel == "admin":
        return True
    if papel == "use":
        return False
    # Compatibilidade: chave sem papel e sem chave administrativa configurada
    # continua administrando a instalação, como nas versões anteriores.
    return not _ADMIN_API_KEY and _key_is_valid(provided)


def _normaliza_role(v) -> str | None:
    """`admin` | `use` para a chave; vazio/ausente = None (política legada)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if s not in ("admin", "use"):
        raise HTTPException(400, "role inválido (admin|use)")
    return s


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
        "role": k.get("role") or None,      # None = legado (regra antiga de admin)
    }
    if reveal:
        row["secret"] = k.get("secret") or ""
    return row


_load_apikeys()

# compat: scripts antigos / mic-router que leem API_KEY no módulo
API_KEY = _primary_api_key() or _ENV_API_KEY or None

def _ffmpeg_bin() -> str:
    """ffmpeg do sistema ou, ausente, o binário estático do imageio-ffmpeg
    (já é dependência do projeto) — Macs sem ffmpeg no PATH ficam cobertos."""
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — mantém fallback histórico
        return "/opt/homebrew/bin/ffmpeg"


FFMPEG = _ffmpeg_bin()

app = FastAPI(title="TTS-STUDIO")
_CORS_ORIGINS = [x.strip() for x in os.environ.get("TTS_CORS_ORIGINS", "").split(",") if x.strip()]
# NOTA: o CORS é registrado no FIM do módulo (depois dos @app.middleware) de
# propósito. `add_middleware` empilha na frente, então quem é registrado por
# último fica por FORA — e um 401/429 que sai do middleware de auth precisa
# atravessar o CORS para ganhar os cabeçalhos. Registrado aqui em cima ele
# ficava por dentro: o cliente cross-origin via "Failed to fetch" em vez do
# 401 "chave inválida" (o preflight passava, a resposta real não).

# Limite leve em memória: impede que uma chave/IP monopolize STT, importação ou
# criação de jobs. Não substitui rate limit no nginx, mas protege o servidor
# mesmo quando ele é usado diretamente na LAN.
_rate_lock = threading.Lock()
_rate_hits = defaultdict(deque)
_RATE_WINDOW = 60.0
_RATE_DEFAULT = int(os.environ.get("TTS_RATE_LIMIT", "120"))
_RATE_HEAVY = int(os.environ.get("TTS_HEAVY_RATE_LIMIT", "20"))
_RATE_HEAVY_PATHS = ("/v1/audio/speech", "/api/transcribe",
                     "/api/stt-partial", "/api/translate-speech", "/api/modify-speech",
                     "/api/voices/import")
_RATE_HEAVY_EXATAS = ("/api/tts",)          # o POST que gera; consultar o job não é
# Caminhos que a própria UI polla de propósito (andamento do job, chat, fila do
# mic, status): 120/min derrubava o polling do próprio job numa geração longa e
# aparecia como 429 no log, com o áudio morrendo no meio da frase. Vem ANTES do
# pesado porque /api/tts/jobs/… começa com /api/tts e não é geração.
_RATE_POLL_PATHS = ("/api/status", "/api/mic-route", "/api/tts/jobs/", "/api/chat/",
                    "/api/outputs")
_RATE_POLL = int(os.environ.get("TTS_POLL_RATE_LIMIT", "1200"))


def _rate_limit_for(path: str) -> int:
    if path.startswith(_RATE_POLL_PATHS):
        return _RATE_POLL
    if path in _RATE_HEAVY_EXATAS or path.startswith(_RATE_HEAVY_PATHS):
        return _RATE_HEAVY
    return _RATE_DEFAULT


# Teto de buckets. A identidade é a chave/IP (efêmera) e o path tem id (job,
# voz, saída, sessão): sem nada disso o dict cresce por (chave, path) e o teto
# antigo só varria o que JÁ tinha expirado — uma rajada de paths distintos dentro
# da mesma janela não caberia em lugar nenhum e ficava tudo "vivo".
_RATE_MAX_BUCKETS = max(100, int(os.environ.get("TTS_RATE_MAX_BUCKETS", "10000")))
# Rotas cujo segmento seguinte é um ID — sem isso cada job/voz/saída vira um
# bucket próprio. O LIMITE continua saindo do path cru (`/api/voices/import`
# mantém o teto pesado); o que colapsa é só a CHAVE do bucket.
_RATE_PREFIXOS_ID = ("/api/tts/jobs/", "/api/voices/", "/api/outputs/",
                     "/api/speaker/", "/api/apikeys/", "/api/chat/")
_RATE_SUB_ROTAS = frozenset({"import", "export", "design", "audio", "peaks",
                             "denoise", "replace", "rotate", "pieces", "start",
                             "enroll", "profiles", "check", "enabled"})


def _rate_path_normalizado(path: str) -> str:
    """Path com o ID colapsado em `*` (`/api/tts/jobs/abc/pieces/0` →
    `/api/tts/jobs/*/pieces/*`). Sub-rotas estáticas (`voices/import`) ficam."""
    for pref in _RATE_PREFIXOS_ID:
        if not path.startswith(pref):
            continue
        partes = path[len(pref):].split("/")
        if not partes or not partes[0]:
            return path
        for i in range(len(partes)):
            if partes[i] not in _RATE_SUB_ROTAS:
                partes[i] = "*"
        return pref + "/".join(partes)
    return path


def _rate_poda(now: float) -> None:
    """Mantém `_rate_hits` abaixo do teto (chamar com `_rate_lock` tomado).

    Primeiro descarta o que expirou; se ainda estiver cheio (muitos paths ou
    identidades na MESMA janela) remove os buckets tocados há mais tempo. Drena
    20% de uma vez para o custo do sorted não cair em toda requisição — enquanto
    está abaixo do teto o caminho é uma comparação de int."""
    if len(_rate_hits) <= _RATE_MAX_BUCKETS:
        return
    for chave, bucket in list(_rate_hits.items()):
        if not bucket or now - bucket[-1] >= _RATE_WINDOW:
            _rate_hits.pop(chave, None)
    excesso = len(_rate_hits) - _RATE_MAX_BUCKETS
    if excesso <= 0:
        return
    for chave, _ in sorted(_rate_hits.items(),
                           key=lambda kv: kv[1][-1] if kv[1] else 0.0
                           )[:excesso + _RATE_MAX_BUCKETS // 5]:
        _rate_hits.pop(chave, None)


@app.middleware("http")
async def _rate_limit(request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith(("/api/", "/v1/")):
        return await call_next(request)
    if _is_local(request):
        # O limitador existe para proteger a caixa exposta pela internet. O
        # navegador rodando no próprio Mac é o cliente nativo e polla a API em
        # sub-segundo por design — medir isso contra o teto de 120/min gerava 429
        # interno. Vale só para loopback: pela internet (túnel) a origem é o IP LAN
        # do Mac, que continua limitado.
        return await call_next(request)
    limit = max(0, _rate_limit_for(request.url.path))
    if limit:
        # Usa a chave quando presente para não agrupar todos os clientes atrás
        # do mesmo proxy; nunca registra o segredo, apenas um identificador curto.
        raw_identity = request.headers.get("x-api-key") or request.headers.get("authorization") or _peer_host(request) or "unknown"
        identity = hashlib.sha256(raw_identity.encode("utf-8", "ignore")).hexdigest()[:16]
        now = time.monotonic()
        chave = (identity, _rate_path_normalizado(request.url.path))
        with _rate_lock:
            # a poda vem ANTES de pegar o bucket: assim o bucket desta requisição
            # não corre o risco de ser evituado no mesmo request (deixaria a
            # referência `hits` órfã e a contagem se perderia).
            _rate_poda(now)
            hits = _rate_hits[chave]
            while hits and now - hits[0] >= _RATE_WINDOW:
                hits.popleft()
            if len(hits) >= limit:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Muitas requisições; tente novamente em breve"},
                                    status_code=429, headers={"Retry-After": "60"})
            hits.append(now)
    return await call_next(request)


@app.middleware("http")
async def _exige_chave(request, call_next):
    # somente loopback dispensa chave; qualquer acesso pela LAN exige autenticação
    local = _is_local(request)
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
            return JSONResponse(
                {"detail": "Não autorizado",
                 "hint": "Cole a chave da API. No Mac do servidor: "
                         "Configurações → Acesso → Revelar (ou o terminal do ./run.sh)."},
                status_code=401)
    return await call_next(request)


# Proxy reverso por PATH (ex.: https://dominio/ttsproxy/... atrás de nginx/CF):
# remove o prefixo antes do roteamento — requisições locais (sem prefixo) são
# intocadas. Registrado DEPOIS da auth => roda ANTES: a auth vê /api/... limpo
# e a exigência de chave vale também pelo proxy (origem = IP de LAN, não loopback).
_BASE_PATH = os.environ.get("TTS_ROD_BASE_PATH", "/ttsproxy").rstrip("/")


# CSP pelo HEADER (não pelo `<meta http-equiv>`): o Chromium ignora
# `frame-ancestors` quando a policy vem em meta — a UI podia ser emoldurada
# (clickjacking no botão Revelar da chave) e o console nascia com erro em toda
# carga. Texto IDÊNTICO ao do meta enquanto os dois existirem; o frontend remove o
# meta depois deste header entrar (aí ESTE vira a fonte da verdade).
_CSP_POLICY = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval' "
    "https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' "
    "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src "
    "'self' data: blob:; media-src 'self' data: blob:; connect-src 'self' "
    "http://127.0.0.1:* http://localhost:* https://cdn.jsdelivr.net; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
# /docs e /redoc montam a própria página (CSS do CDN, que a policy da UI não
# cobre) — ficam como sempre foram, sem CSP.
_CSP_SEM_HEADER = ("/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json")
# Paths do SPA: servidos por `index_com_nonce`, que devolve o MESMO index.html com
# `nonce` no `<script>` inline (sem ele, sem 'unsafe-inline', o app não roda).
_SPA_PATHS = ("/", "/index.html")


def _csp_com_nonce(nonce: str | None) -> str:
    """Policy com o nonce DESTA resposta no `script-src` (None = sem nonce)."""
    if not nonce:
        return _CSP_POLICY
    return _CSP_POLICY.replace("script-src 'self'",
                               f"script-src 'self' 'nonce-{nonce}'", 1)


def _index_com_nonce(nonce: str) -> str:
    """index.html com `nonce` nos `<script>` inline (a tag sozinha na linha; as
    duas de CDN ficam como estão — elas são cobertas pelo source do CDN e têm SRI)."""
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    return re.sub(r"(?m)^<script>$", f'<script nonce="{nonce}">', html)


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
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), payment=()")
    if request.url.path not in _CSP_SEM_HEADER:
        # nonce é POR RESPOSTA e quem o cria é a rota do SPA (fica em request.state);
        # nas outras rotas ele não existe e o header sai sem nonce.
        resp.headers.setdefault("Content-Security-Policy",
                                _csp_com_nonce(getattr(request.state, "nonce", None)))
    if (request.headers.get("x-forwarded-proto") or request.url.scheme).lower() == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp

@app.get("/")
@app.get("/index.html")
def index_com_nonce(request: Request):
    """Serve o index.html com nonce no `<script>` inline (CSP sem 'unsafe-inline').

    Fica ANTES do `app.mount("/", StaticFiles(...))`: a rota tem precedência e o
    middleware de CSP lê este nonce do `request.state` para o header da resposta."""
    import secrets as _sec
    nonce = getattr(request.state, "nonce", None)
    if not nonce:
        nonce = _sec.token_urlsafe(16)
        request.state.nonce = nonce
    return HTMLResponse(_index_com_nonce(nonce))


# ---------------------------------------------------------------------------
# Modelo (carregamento preguiçoso — primeira síntese baixa/monta os pesos)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_gen_lock = threading.Lock()  # geração LOCAL (MLX) não é thread-safe; serializa
import contextlib
_NO_LOCK = contextlib.nullcontext()  # remoto pode rodar concorrente (sem serializar)
_model_state = {"status": "idle", "device": None, "model": _settings["model"],
                "error": None, "progress": None, "precision": None, "path": None}


def _model_state_progress(msg):
    """Callback de progresso p/ download/montagem do modelo (common.py)."""
    _model_state.update(progress=msg)


def _current_backend(model: str | None = None) -> dict:
    """Metadados do backend selecionado em settings['model'].

    `model` opcional resolve SEM tocar nas settings — é o que permite validar um
    pedido com `model` no body antes de aplicá-lo globalmente."""
    return resolve_backend(model or _settings.get("model") or "omnivoice")


def _is_omnivoice() -> bool:
    return _current_backend()["family"] == "omnivoice"


def _resolve_model_path(model: str | None = None) -> str:
    """Resolve o modelo efetivo → path/id p/ load_model.

    OmniVoice: monta bf16 local (ou repo fp32). Outros atalhos: repo MLX do catálogo.
    String livre (HF/dir): repassada como está. `model` opcional = modelo do PEDIDO,
    sem depender de `_settings` (usado quando quem pediu não pode trocar o global)."""
    be = _current_backend(model)
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


def _get_model(model: str | None = None):
    """Modelo carregado para `model` (None = o das settings).

    A checagem de cache compara com o modelo EFETIVO do pedido: um cliente que manda
    `model` no body (e não pode trocar o global) carrega o modelo dele, e o próximo
    pedido com outro modelo recarrega — serializado pelo `_gen_lock`."""
    global _model
    modelo = model or _settings["model"]
    with _model_lock:
        prec = str(_settings.get("omni_precision", "bf16")).lower()
        be = _current_backend(modelo)
        if (_model is not None and _model_state.get("model") == modelo
                and _model_state.get("precision") == prec
                and _model_state.get("family") == be["family"]):
            _touch_use("tts")
            return _model
        # troca de modelo/precisão: libera o anterior com agressividade (Metal)
        old = _model
        _model = None
        _conds_cache.clear()
        path = _resolve_model_path(modelo)
        label = be["meta"].get("label") or path
        _model_state.update(
            status="loading", device="mlx", model=modelo,
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
    # `_model_state["model"]` é o ÚLTIMO modelo carregado — verdade sobre a VRAM, não
    # sobre a config: um cliente com `model` no body (chave de uso) carrega o modelo
    # dele sem trocar settings, então o painel passava a mostrar como "config" algo
    # que era um pedido. Aqui vai o valor configurado, ao lado, para desambiguar.
    st["model_settings"] = _settings.get("model")
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
    # Admissão de jobs: sem isto um 429 por teto de ativos era indistinguível de
    # um 429 do limitador de taxa, e a UI não tinha como saber que a geração
    # esperava slot. jobs_active == jobs_active_max = próxima geração recusa.
    st["jobs_active"] = _jobs_ativos()
    st["jobs_active_max"] = _JOBS_ACTIVE_MAX
    st["jobs_history_max"] = _JOBS_MAX
    # Caminho do VAD realmente carregado ("onnx" | "torch-jit"); None = ainda não
    # carregou. Sem isto a única pista de um fallback para o torch seria o print
    # no stderr do servidor — /api/status prova em produção qual caminho está vivo.
    st["vad_backend"] = _vad_backend or None
    # Tamanho do dict do rate limiter (buckets = identidade × path normalizado) e o
    # teto: era invisível e só aparecia como memória crescendo em caixa exposta.
    st["live_sessions"] = len(_live_sessions)
    st["live_historicos"] = len(_live_historico)
    st["live_historicos_max"] = _LIVE_MAX_HISTORICOS
    st["rate_limit_buckets"] = len(_rate_hits)
    st["rate_limit_buckets_max"] = _RATE_MAX_BUCKETS
    with _apikeys_lock:
        st["api_keys_count"] = len(_apikeys.get("keys") or [])
    st["lan_urls"] = _lan_urls()
    return st


# ---------------------------------------------------------------------------
# Gestão de chaves de API (UI: Configurações → Acesso)
# ---------------------------------------------------------------------------

@app.get("/api/apikeys")
def list_apikeys(request: Request, reveal: bool = False):
    """Lista chaves. `reveal=1` devolve o secret se for o Mac ou se a
    requisição já autenticou com uma chave válida."""
    local = _is_local(request)
    tem_chave = _key_is_valid(_extract_request_key(request))
    can_reveal = bool(local) or (_admin_is_allowed(request) and tem_chave)
    do_reveal = bool(reveal) and can_reveal
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
        "can_reveal": can_reveal,
        "local": bool(local),
        "lan_urls": _lan_urls(),
    }


@app.post("/api/apikeys")
def create_apikey(request: Request, payload: dict):
    """Cria chave. Devolve o secret completo uma vez."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
    payload = payload or {}
    name = str(payload.get("name") or "nova chave").strip()[:64] or "nova chave"
    kid = uuid.uuid4().hex[:10]
    secret = _new_api_secret()
    row = {
        "id": kid,
        "name": name,
        "secret": secret,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # role=use: chave que gera fala/transcreve mas não mexe em conexão externa
        # nem lê segredo. Ausente = política antiga (compatibilidade).
        "role": _normaliza_role(payload.get("role")),
    }
    with _apikeys_lock:
        _apikeys.setdefault("keys", []).append(row)
        _save_apikeys()
    global API_KEY
    API_KEY = _primary_api_key() or _ENV_API_KEY or None
    return {"ok": True, "key": _public_key_row(row, reveal=True)}


@app.patch("/api/apikeys/{key_id}")
def rename_apikey(request: Request, key_id: str, payload: dict):
    """Renomeia e/ou troca o papel da chave (admin | use | vazio = legado).

    Corpo parcial: `name` ausente OU vazio mantém o nome atual (o campo em branco
    na UI é "não mexi no nome"), `role` ausente mantém o papel. A UI manda os dois;
    a API pode mandar só um."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
    payload = payload or {}
    if key_id == "__env__":
        raise HTTPException(400, "Chave de ambiente não pode ser renomeada")
    name = str(payload.get("name") or "").strip()[:64]
    novo_role = _normaliza_role(payload["role"]) if "role" in payload else None
    with _apikeys_lock:
        for k in _apikeys.get("keys") or []:
            if k["id"] == key_id:
                if name:                    # vazio/ausente: mantém o que está
                    k["name"] = name
                if "role" in payload:
                    k["role"] = novo_role
                _save_apikeys()
                return {"ok": True, "key": _public_key_row(k)}
    raise HTTPException(404, "Chave não encontrada")


@app.post("/api/apikeys/{key_id}/rotate")
def rotate_apikey(request: Request, key_id: str):
    """Gera novo secret. A chave antiga deixa de valer na hora."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
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
def delete_apikey(request: Request, key_id: str):
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
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
def set_apikeys_enabled(request: Request, payload: dict):
    """Liga/desliga a exigência de chave na rede."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
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
_chat_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8,
                                                       thread_name_prefix="chat-llm")

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
    "O texto final deve conter só o que será falado — sem comentários sobre a conversa. "
    "DEPOIS DE ENTREGAR a conversa continua: pedido ou pergunta NOVA começa um "
    "RASCUNHO NOVO — nunca reenvie um texto já entregue, a não ser que peçam "
    "exatamente o mesmo. Se a fala misturar confirmação com pedido novo (ex.: "
    "'agora envia, e pergunta por que não conectou antes'), o pedido novo é o "
    "próximo rascunho: proponha ele e pergunte se está aprovado."
)


def _chat_provider() -> tuple[str, str, str]:
    """(base_url, model, api_key) efetivos — chat_* com fallback p/ tradução.

    `TTS_CHAT_BASE_URL` / `TTS_CHAT_MODEL` / `TTS_CHAT_API_KEY` (ambiente) têm
    PRECEDÊNCIA sobre as settings: é o que permite teste/smoke apontar para um stub
    SEM gravar no `settings.json` REAL. Motivo (incidente 2026-09-25): o smoke
    gravava via POST /api/settings, o `finally` do restore não roda se o run morre
    — e o snapshot seguinte já era o estado poluído, então o mecanismo não se
    auto-curava. Com env, o arquivo do dono fica intocado por construção.
    A precedência é POR CAMPO: setar só `TTS_CHAT_MODEL` não derruba a base URL."""
    base = (os.environ.get("TTS_CHAT_BASE_URL") or "").strip() \
        or (_settings.get("chat_base_url") or "").strip() \
        or (_settings.get("remote_base_url") or "").strip()
    model = (os.environ.get("TTS_CHAT_MODEL") or "").strip() \
        or (_settings.get("chat_model") or "").strip() \
        or (_settings.get("remote_translate_model") or "gpt-4o-mini")
    key = (os.environ.get("TTS_CHAT_API_KEY") or "").strip() \
        or (_settings.get("chat_api_key") or "").strip() \
        or (_settings.get("remote_api_key") or "").strip()
    if not base:
        raise HTTPException(400, "Provedor de conversa não configurado "
                                 "(Configurações → Rede: Base URL OpenAI-compat)")
    return base.rstrip("/"), model, key


def _chat_llm(messages: list) -> str:
    """Devolve o conteúdo da resposta. Despacha pelo backend (`chat_backend`)."""
    if _chat_backend() == "dsh":
        return _chat_llm_dsh(messages)
    return _chat_llm_openai(messages)


def _chat_llm_openai(messages: list) -> str:
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
            _provedor_marca("chamando")
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                _provedor_marca("ok", getattr(resp, "status", 200))
                dados = json.loads(resp.read())
            return dados["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            _provedor_marca("erro", e.code)
            ultimo_erro = f"HTTP {e.code}"
            if e.code not in (400, 422):
                raise HTTPException(502, f"Provedor de conversa falhou: HTTP {e.code}")
            # 400/422: provedor não aceita reasoning_effort — tenta sem o campo
        except HTTPException:
            raise
        except Exception as e:
            _provedor_marca("timeout" if "timed out" in str(e).lower() else "erro")
            raise HTTPException(502, f"Provedor de conversa falhou: {e}")
    raise HTTPException(502, f"Provedor de conversa falhou: {ultimo_erro}")


# ---------------------------------------------------------------------------
# Backend de IA "dsh" (harness via ACP) — task_129d2183. O `chat_backend` escolhe
# entre o endpoint OpenAI-compat acima e o processo local do dsh. `_chat_provider`
# não muda de assinatura; o caminho openai fica INTACTO. Env por CAMPO, no mesmo
# padrão do `TTS_CHAT_*`: `TTS_CHAT_BACKEND`/`TTS_CHAT_DSH_{BIN,PROFILE,MODEL,EFFORT}`.
# ---------------------------------------------------------------------------
def _chat_backend() -> str:
    """Backend do caminho da CONVERSA (e herança do Live — ver `_chat_backend_live`)."""
    v = (os.environ.get("TTS_CHAT_BACKEND") or "").strip().lower() \
        or str(_settings.get("chat_backend") or "openai").strip().lower()
    return v if v in ("openai", "dsh") else "openai"


def _chat_backend_live() -> str:
    """Backend do caminho do LIVE (#176) — `chat_backend_live` com fallback ao global.

    POR QUE SEPARAR: as duas telas têm recomendações diferentes (o Live ganha muito
    com o dsh; a Conversa vai bem no provedor do dono). Um campo global obrigava a
    escolher entre as duas. Vazio = herda, então quem não mexer nada não muda."""
    v = (os.environ.get("TTS_CHAT_BACKEND_LIVE") or "").strip().lower() \
        or str(_settings.get("chat_backend_live") or "").strip().lower()
    return v if v in ("openai", "dsh") else _chat_backend()


def _chat_dsh_cfg() -> dict:
    """Config do dsh com precedência POR CAMPO do ambiente (setar só um não derruba os outros)."""
    def campo(sufixo: str, setting: str, default: str) -> str:
        return (os.environ.get(f"TTS_CHAT_DSH_{sufixo}") or "").strip() \
            or str(_settings.get(setting) or "").strip() or default
    return {
        "bin": campo("BIN", "chat_dsh_bin", "dsh"),
        "profile": campo("PROFILE", "chat_dsh_profile", dsh_client.DSH_DEFAULT_PROFILE),
        "model": dsh_client.dsh_model_for_turn(
            campo("MODEL", "chat_dsh_model", dsh_client.DSH_DEFAULT_MODEL)),
        "effort": dsh_client.dsh_effort_valido(campo("EFFORT", "chat_dsh_effort", "off")),
    }


_chat_dsh_lock = threading.Lock()
_chat_dsh_livres: list = []          # processos quentes e OCIOSOS: [(chave, cli)]
_chat_dsh_chave: tuple | None = None
_chat_dsh_prewarm_thread: threading.Thread | None = None
_CHAT_DSH_POOL_MAX = 3
# campos cuja mudança invalida o processo quente da Conversa (pre-warm + pool)
_CAMPOS_DHS_BACKEND = ("chat_backend", "chat_backend_live", "chat_dsh_bin",
                       "chat_dsh_profile",
                       "chat_dsh_model", "chat_dsh_effort")


def _chat_dsh_chave_do(cfg: dict) -> tuple:
    return (cfg["bin"], cfg["profile"], cfg["model"], cfg["effort"])


def _dsh_log(msg: str, tag: str = "chat-dsh") -> None:
    """Log do caminho dsh (cliente e pre-warm) sai por STDERR, não pelo stdout.

    O stdout do processo é lido como DADO por quem faz `import app` — o teste de
    telemetria do ORT compara o stdout inteiro de um subprocesso — e o pre-warm
    sobe em thread, então a linha pode sair antes ou depois do fim do import. O
    canal separado deixa o stdout determinístico em vez de dependente da corrida
    (task_6db0e2cc)."""
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


def _chat_dsh_novo(cfg: dict) -> "dsh_client.DshClient":
    return dsh_client.DshClient(
        bin=cfg["bin"], profile=cfg["profile"], model=cfg["model"],
        effort=cfg["effort"], cwd=BASE / "outputs" / ".dsh-cwd",
        on_log=lambda m: _dsh_log(m))


def _chat_dsh_cliente() -> "dsh_client.DshClient":
    """Cliente quente do pool (processo dsh persistente). Fecha o de config ANTIGA.

    A chave (bin/perfil/modelo/effort) viaja CARIMBADA no cliente (`_pool_chave`),
    não no global: entre a entrega e a devolução o `/api/settings` pode ter trocado a
    config e o prewarm da nova já ter mexido no global — carimbar na devolução fazia
    um cliente da config ANTIGA voltar ao pool como se fosse da nova (o próximo
    turno rodava no modelo antigo, calado)."""
    cfg = _chat_dsh_cfg()
    chave = _chat_dsh_chave_do(cfg)
    global _chat_dsh_chave
    with _chat_dsh_lock:
        antigos = [(k, c) for k, c in _chat_dsh_livres if k != chave or not c.alive]
        _chat_dsh_livres[:] = [(k, c) for k, c in _chat_dsh_livres
                               if k == chave and c.alive]
        _chat_dsh_chave = chave
        cli = _chat_dsh_livres.pop()[1] if _chat_dsh_livres else None
    # FORA do lock: `close()` faz `session/close` com timeout de 60 s e um dsh vivo
    # mas mudo não responde — fechando dentro do lock, TODO uso do pool (o próximo
    # turno da Conversa, o resumo do Live) esperava o processo velho morrer.
    for _k, antigo in antigos:
        antigo.close()
    if cli is None:
        cli = _chat_dsh_novo(cfg)
    cli._pool_chave = chave              # a chave viaja COM o cliente
    return cli


def _chat_dsh_devolve(cli: "dsh_client.DshClient") -> None:
    """Devolve ao pool com a chave DA ENTREGA (não a global de agora).

    O `close()` do descarte roda FORA do lock (mesmo motivo do `_chat_dsh_cliente`)."""
    chave = getattr(cli, "_pool_chave", None)
    with _chat_dsh_lock:
        guardar = bool(chave is not None and cli.alive
                       and len(_chat_dsh_livres) < _CHAT_DSH_POOL_MAX)
        if guardar:
            _chat_dsh_livres.append((chave, cli))
    if not guardar:
        cli.close()


def _chat_dsh_prewarm(motivo: str = "") -> threading.Thread | None:
    """Sobe processo + sessão do dsh FORA do 1º turno da Conversa (best-effort).

    Sem custo para quem usa `openai`: nada é spawnado. Nunca bloqueia o chamador
    (thread), nunca derruba nada e nunca muda o backend — falha só vira log. O
    `prewarm()` do cliente é idempotente e o processo volta ao pool quente."""
    global _chat_dsh_prewarm_thread
    if _chat_backend() != "dsh":
        return None
    with _chat_dsh_lock:
        atual = _chat_dsh_prewarm_thread
        if atual is not None and atual.is_alive():
            return atual

    def _rodar() -> None:
        cli = None
        try:
            cli = _chat_dsh_cliente()
            cli.prewarm()
            _dsh_log(f"pre-warm ({motivo}) em {cli.ultimo_boot_ms} ms "
                     f"(sessão {cli.session_id})")
        except Exception as exc:  # noqa: BLE001 — prewarm é otimização
            _dsh_log(f"pre-warm falhou ({motivo}): {exc} — segue")
        finally:
            if cli is not None:
                _chat_dsh_devolve(cli)

    th = threading.Thread(target=_rodar, name="chat-dsh-prewarm", daemon=True)
    with _chat_dsh_lock:
        _chat_dsh_prewarm_thread = th
    th.start()
    return th


def _chat_llm_dsh(messages: list) -> str:
    """`_chat_llm` pelo backend dsh. Modo histórico: a lista vai renderizada num
    prompt (assinatura preservada) — a Conversa não é sensível a latência."""
    cli = _chat_dsh_cliente()
    try:
        return cli.collect(messages)
    except dsh_client.DshError as exc:
        raise HTTPException(502, f"Backend dsh falhou: {exc}")
    finally:
        _chat_dsh_devolve(cli)


# Aquece o dsh no boot do app quando o backend já é dsh. Não bloqueia nada e, com
# `chat_backend=openai`, nem chama `which("dsh")`: nenhum processo é spawnado.
try:
    _chat_dsh_prewarm("startup")
except Exception as exc:  # noqa: BLE001 — prewarm é otimização
    _dsh_log(f"pre-warm inicial falhou: {exc} — segue")


_dsh_models_cache: dict = {}
_DSH_MODELS_TTL_S = 300


def _dsh_bridge_estado() -> dict:
    """Estado do patch do bridge ACP no host (task_159). Nunca levanta.

    O patch vive fora do repo e some em `npm install -g`; sem isto o produto não
    tem como saber que o caminho dsh regrediu para "resposta inteira no fim"."""
    try:
        return dsh_client.estado_bridge(bin=_chat_dsh_cfg()["bin"])
    except Exception as exc:  # noqa: BLE001 — campo informativo não derruba o endpoint
        return {"estado": "unknown", "motivo": f"{type(exc).__name__}: {exc}"}


@app.get("/api/chat/dsh/models")
def chat_dsh_models():
    """Descoberta de modelos/efforts do dsh (o frontend integra a partir daqui)."""
    cfg = _chat_dsh_cfg()
    chave = (cfg["bin"], cfg["profile"])
    agora = time.time()
    if (_dsh_models_cache.get("chave") == chave
            and agora - _dsh_models_cache.get("em", 0) < _DSH_MODELS_TTL_S):
        dados = _dsh_models_cache["dados"]
    else:
        try:
            dados = dsh_client.descobrir_modelos(bin=cfg["bin"], profile=cfg["profile"])
        except dsh_client.DshError as exc:
            raise HTTPException(502, f"descoberta do dsh falhou: {exc}")
        # cacheado JUNTO da descoberta: a tela não paga leitura de arquivo por request
        dados = {"bridge_info": _dsh_bridge_estado(), **dados}
        _dsh_models_cache.update({"dados": dados, "chave": chave, "em": agora})
    bridge = dados.get("bridge_info") or {"estado": "unknown"}
    return {"ok": True, "backend": "dsh",
            **{k: v for k, v in dados.items() if k != "bridge_info"},
            # `bridge` NÃO faz parte do dsh: diz se o HOST tem o patch que entrega
            # deltas (sem ele a resposta vem inteira no fim). A tela usa para avisar.
            "bridge": bridge.get("estado", "unknown"),
            "bridge_detalhe": bridge,
            "current": {"model": cfg["model"], "effort": cfg["effort"]},
            "default_model": dsh_client.DSH_DEFAULT_MODEL,
            "efforts": list(dsh_client.DSH_EFFORTS)}


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


def _chat_worker(sid: str, gen: int = 0) -> None:
    """Chama o LLM em background e atualiza a sessão (status thinking → reply).
    Com teto hard de 150s: provedor pendurado não deixa a sessão em 'thinking'
    para sempre.
    `gen` é a geração do turno: se o humano interrompeu (barge-in) e mandou fala
    nova, a sessão já está em outra geração e este resultado é jogado fora."""
    def _rodar() -> None:
        try:
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if not s or s.get("gen", 0) != gen:
                    return
                hist = s["messages"][:]
                if len(hist) > CHAT_MAX_MSGS + 1:
                    hist = [hist[0]] + hist[-(CHAT_MAX_MSGS + 1):]
            sys_prompt = (_settings.get("chat_system") or "").strip() or CHAT_SYSTEM
            reply = _chat_llm([{"role": "system", "content": sys_prompt}] + hist)
            parsed = _chat_parse(reply)
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if not s or s.get("gen", 0) != gen:
                    return   # interrompido: resposta velha não entra na sessão
                if parsed.get("final") and parsed.get("text"):
                    s["status"], s["text"] = "confirmed", str(parsed["text"])
                    s["reply"] = s["text"]
                    s["last_text"] = s["text"]   # último texto aprovado (persiste entre rodadas)
                    # a ENTREGA também é turno do assistente. Sem isto o histórico
                    # ficava com dois 'user' seguidos e sem registro do que foi
                    # entregue: na rodada seguinte o único rascunho visível era o
                    # antigo e o modelo reconfirmava o MESMO texto.
                    s["messages"].append({
                        "role": "assistant",
                        "content": json.dumps({"final": True, "text": s["text"]},
                                              ensure_ascii=False),
                    })
                else:
                    s["reply"] = str(parsed.get("reply") or "")
                    s["messages"].append({"role": "assistant", "content": s["reply"]})
                s["thinking"] = False
                s["updated"] = time.time()
        except Exception as e:  # noqa: BLE001 — erro vai pra sessão, não derruba o server
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if s and s.get("gen", 0) == gen:
                    s["error"] = str(e)[:200]
                    s["thinking"] = False

    try:
        _chat_executor.submit(_rodar).result(timeout=150)
    except concurrent.futures.TimeoutError:
        with _chat_lock:
            s = _chat_sessions.get(sid)
            if s and s.get("gen", 0) == gen and s.get("thinking"):
                s["error"] = "Provedor de IA demorou demais (150s) — tente de novo"
                s["thinking"] = False


@app.post("/api/chat/start")
def chat_start(payload: dict):
    """Abre sessão p/ decidir, conversando, o texto que o agente vai falar.
    Assíncrono: responde na hora e a fala da IA chega via GET (polling)."""
    objetivo = str((payload or {}).get("objective") or "").strip()
    if not objetivo:
        raise HTTPException(400, "Campo 'objective' obrigatório")
    _chat_dsh_prewarm("chat/start")    # no-op com `openai`
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
                               "gen": 0, "created": now, "updated": now}
    threading.Thread(target=_chat_worker, args=(sid, 0), daemon=True).start()
    return {"session_id": sid, "status": "thinking"}


@app.post("/api/chat/{sid}")
def chat_message(sid: str, payload: dict):
    """Fala nova do humano (transcrição do STT). A resposta da IA chega pelo
    GET — assíncrono p/ não estourar timeouts de proxy/Cloudflare."""
    s = _chat_get(sid)
    msg = str((payload or {}).get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Campo 'message' obrigatório")
    interromper = bool((payload or {}).get("interrupt"))
    with _chat_lock:
        if s.get("thinking") and not interromper:
            raise HTTPException(409, "IA ainda processando a fala anterior")
        interrompido = bool(s.get("thinking"))
        if interrompido:
            # barge-in: a resposta em voo é descartada (o worker vê a geração
            # nova e não escreve nada). A fala anterior ficou sem resposta, então
            # junta com a nova pra não mandar dois 'user' seguidos ao provedor.
            if s["messages"] and s["messages"][-1]["role"] == "user":
                s["messages"][-1]["content"] += "\n" + msg
            else:
                s["messages"].append({"role": "user", "content": msg})
        else:
            s["messages"].append({"role": "user", "content": msg})
        s["gen"] = s.get("gen", 0) + 1
        gen = s["gen"]
        s["status"], s["text"] = "chatting", ""
        s["reply"], s["error"] = "", ""
        s["thinking"] = True
        s["updated"] = time.time()
    threading.Thread(target=_chat_worker, args=(sid, gen), daemon=True).start()
    return {"ok": True, "status": "thinking", "interrupted": interrompido}


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
    if os.environ.get("TTS_CHAT_DEBUG") != "1":
        return {"ok": True, "disabled": True}
    try:
        with open("/tmp/tts-chat-debug.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {json.dumps(payload, ensure_ascii=False)}\n")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


# ── Túnel / acesso pela internet (proxy por path) ──────────────────────────
# Dois caminhos possíveis, independentes: túnel SSH reverso até a VPS
# (tunnel.sh) e conector Cloudflare Tunnel na própria máquina (cloudflare.sh,
# gerenciado remotamente no dashboard). O app só reporta e liga/desliga os
# agentes instalados — nenhum dos dois é obrigatório.
TUNNEL_LABEL = "studio.tts.tunnel"
CF_LABEL = "com.local.cloudflared-tts"


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


def _cf_proc_running() -> bool:
    """Há um conector cloudflared vivo nesta máquina?"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "cloudflared tunnel"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _launchd_loaded(label: str) -> bool | None:
    """LaunchAgent carregado? None se não for macOS/sem launchctl."""
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        return None
    try:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return None


def _tunnel_launchd_loaded() -> bool | None:
    return _launchd_loaded(TUNNEL_LABEL)


def _cf_launchd_loaded() -> bool | None:
    return _launchd_loaded(CF_LABEL)


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
        # o python.org framework build não tem CA store próprio — sem certifi a
        # checagem falha com "unable to get local issuer certificate" e a UI
        # mostra a internet como caída (mesmo bug já tratado em _chat_llm)
        import ssl
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001 — sem certifi, trust store padrão
            ctx = ssl.create_default_context()
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(200)
            lat = round((time.monotonic() - t0) * 1000)
            ok = resp.status == 200 and b'"ok"' in body
            return {"ok": ok, "status_code": resp.status,
                    "latency_ms": lat, "error": None if ok else "resposta inesperada"}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e)[:200]}


@app.get("/api/tunnel/status")
def tunnel_status(url: str = ""):
    """Status do acesso pela internet: agentes instalados/carregados (SSH e
    Cloudflare) e checagem fim a fim da URL pública (passada pela UI)."""
    pub = _public_proxy_check(url) if url.strip() else None
    return {
        "tunnel_running": _tunnel_proc_running(),
        "launchd_loaded": _tunnel_launchd_loaded(),
        "cloudflared_installed": _cf_plist().exists(),
        "cloudflared_running": _cf_proc_running(),
        "cloudflared_loaded": _cf_launchd_loaded(),
        "public_check": pub,
    }


def _tunnel_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{TUNNEL_LABEL}.plist"


def _cf_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{CF_LABEL}.plist"


def _agentes_publicos() -> list[tuple[str, Path]]:
    """Agentes de acesso público instalados nesta máquina (label, plist)."""
    return [(label, plist) for label, plist in
            ((TUNNEL_LABEL, _tunnel_plist()), (CF_LABEL, _cf_plist()))
            if plist.exists()]


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


# Parar/reiniciar o túnel mata justamente o processo que está servindo ESTA
# requisição — a resposta chegaria como 502 (o Cloudflare corta a conexão).
# Por isso esses comandos saem com um atraso, fora do caminho da resposta.
_LAUNCHCTL_ATRASO = 1.5
_launchctl_threads: list = []


def _launchctl_adiado(args: list, atraso: float | None = None) -> None:
    """Roda launchctl em thread, depois de `atraso` segundos (default 1.5)."""
    espera = _LAUNCHCTL_ATRASO if atraso is None else atraso

    def alvo():
        if espera:
            time.sleep(espera)
        try:
            subprocess.run(args, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=alvo, daemon=True)
    _launchctl_threads.append(t)
    t.start()


def _tunnel_exige_macos() -> None:
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        raise HTTPException(400, "Disponível apenas no macOS com o LaunchAgent instalado (tunnel.sh install)")


@app.post("/api/tunnel/restart")
def tunnel_restart():
    """Reinicia os agentes do acesso pela internet (kickstart). Exige macOS."""
    _tunnel_exige_macos()
    agentes = _agentes_publicos()
    if not agentes:
        raise HTTPException(400, "Nenhum agente instalado — rode ./tunnel.sh install ou ./cloudflare.sh install")
    for label, _plist in agentes:
        _launchctl_adiado(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
    return {"ok": True, "msg": "Túnel reiniciado (aguarde ~5s e reavalie o status)"}


@app.post("/api/tunnel/stop")
def tunnel_stop():
    """Desliga o acesso pela internet: bootout dos agentes (o KeepAlive não
    reergue). A rede local continua valendo."""
    _tunnel_exige_macos()
    agentes = _agentes_publicos()
    if not agentes:
        raise HTTPException(400, "Nenhum agente instalado — rode ./tunnel.sh install ou ./cloudflare.sh install")
    for label, _plist in agentes:
        # adiado: o agente que serve esta requisição morre depois de responder
        _launchctl_adiado(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
    return {"ok": True, "msg": "Túnel desligado — a URL pública cai em ~2s"}


@app.post("/api/tunnel/start")
def tunnel_start():
    """Religa o acesso pela internet: bootstrap dos agentes instalados; agente
    já carregado vai de kickstart (bootstrap em carregado dá erro)."""
    _tunnel_exige_macos()
    agentes = _agentes_publicos()
    if not agentes:
        raise HTTPException(400, "Nenhum agente instalado — rode ./tunnel.sh install ou ./cloudflare.sh install")
    for label, plist in agentes:
        if not _launchd_loaded(label):
            _launchctl(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        elif not (_cf_proc_running() if label == CF_LABEL else _tunnel_proc_running()):
            _launchctl_adiado(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
    return {"ok": True, "msg": "Túnel ligado — aguarde ~5s e teste a URL pública"}


@app.post("/api/shutdown")
def shutdown():
    """Desliga o servidor (botão na UI). Reiniciar: dois cliques em TTS-STUDIO.command."""
    def _stop():
        time.sleep(0.4)  # deixa a resposta HTTP voltar antes de sair
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True, "msg": "Servidor desligando…"}


# ---------------------------------------------------------------------------
# /api/settings: separação entre chave de USO e administração
#
# Gerar fala é uma coisa; repontar a instalação para outro provedor (e ler a
# credencial dele) é outra. `_SETTINGS_ADMIN` = campos que só admin altera:
# conexões externas (URLs, chaves, toggles de envio remoto) e escolha de modelo
# (que dispara download/load de repo). Fora da lista: `remote_tts_voice`/
# `remote_tts_extra` (formato do payload, não o destino) e políticas de recurso
# (idle_unload, free_local_on_remote, speech_queue).
#
# Compatibilidade: quem não quer separação nenhuma não muda NADA — sem
# `TTS_ROD_ADMIN_KEY` e sem `role` nas chaves, toda chave válida segue admin.
# ---------------------------------------------------------------------------
_SETTINGS_ADMIN = frozenset({
    "model", "translate_model", "stt_whisper_repo",
    "remote_tts", "remote_translate", "remote_stt",
    "remote_base_url", "remote_api_key", "remote_tts_url",
    "remote_stt_base_url", "remote_stt_key",
    "remote_tts_model", "remote_translate_model", "remote_stt_model",
    "chat_base_url", "chat_model", "chat_api_key",
    "chat_backend", "chat_backend_live", "chat_dsh_bin", "chat_dsh_profile",
    "chat_dsh_model",
    "chat_dsh_effort",
})
_SETTINGS_SECRETS = ("remote_api_key", "remote_stt_key", "chat_api_key")
# marcador da máscara: começa com "•" e nunca é confundido com chave de verdade.
# O POST que devolver a máscara (a UI recarrega o form e reenvia o blob) mantém
# o segredo guardado em vez de gravar "••••1234" por cima dele.
_MASCARA_SETTING = "••••"


def _mascara_setting_secret(v: str) -> str:
    s = str(v or "")
    return _MASCARA_SETTING + (s[-4:] if len(s) >= 4 else "")


def _settings_payload_adm(request, payload: dict) -> tuple[dict, list[str]]:
    """Limpa o payload antes de aplicar: tira máscara de segredo e, para quem não
    é admin, tira os campos administrativos (devolvidos em `ignorados`).

    Não é 403: a UI manda um blob único no "Salvar", então um 403 por causa de um
    campo remote_* travaria também a mudança de `speed`."""
    dados = dict(payload or {})
    for k in _SETTINGS_SECRETS:
        v = dados.get(k)
        if isinstance(v, str) and v.startswith(_MASCARA_SETTING):
            dados.pop(k, None)          # segredo mascarado: mantém o guardado
    if _admin_is_allowed(request):
        return dados, []
    ignorados = []
    for k in list(dados):
        if k in _SETTINGS_ADMIN:
            ignorados.append(k)
            dados.pop(k)
    return dados, ignorados


@app.get("/api/settings")
def get_settings(request: Request):
    st = dict(_settings)
    st["chat_system_default"] = CHAT_SYSTEM  # p/ a UI exibir o preprompt padrão
    st.update(_settings_vista(request))
    return st


def _settings_vista(request) -> dict:
    """Corta o que a requisição não pode VER: segredos de serviços externos.

    Chave de uso (e visitante sem chave admin) não recebe `remote_api_key`,
    `remote_stt_key` nem `chat_api_key` em claro — são credenciais de terceiros,
    não material para gerar fala. Admin/mac continua vendo tudo."""
    adm = _admin_is_allowed(request)
    extra = {"is_admin": adm, "admin_fields": sorted(_SETTINGS_ADMIN)}
    if not adm:
        for k in _SETTINGS_SECRETS:
            if _settings.get(k):
                extra[k] = _mascara_setting_secret(_settings[k])
    return extra


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
def update_settings(request: Request, payload: dict):
    """Aplica as settings de forma ATÔMICA (o corpo faz `_settings[k]=...` campo a
    campo e só grava o disco no fim).

    Sem rollback, o primeiro campo inválido (ex.: `chat_extra` que não é JSON,
    `stt_whisper_repo` sem "whisper") devolvia 400 DEPOIS de aplicar os anteriores
    na RAM, sem nunca chegar ao `_save_settings()` — a config passava a valer na
    hora, sobrevivia ao uso e SUMIA no restart, sem nenhum aviso de que não tinha
    sido gravada. Aqui: falhou, restaura o estado anterior (RAM == disco).

    Chave de uso: campos administrativos são IGNORADOS (não é 403 — a UI manda um
    blob único no "Salvar") e vêm em `admin_ignored`. Máscara de segredo é
    tratada como "mantém o que está guardado".
    """
    payload, ignorados = _settings_payload_adm(request, payload)
    _antes = dict(_settings)
    try:
        _r = _apply_settings(payload)
    except Exception:
        _settings.clear()
        _settings.update(_antes)
        raise
    if str(_antes.get("omni_precision", "")).lower() != str(_settings.get("omni_precision", "")).lower():
        # Troca de dtype só pegaria no próximo load_model; descarrega o modelo com o
        # dtype antigo agora, senão continua servindo (e segurando ~GB de RAM com) ele.
        # Com job em voo não encosta: _gen_lock está tomado e o reload é lazy de qualquer jeito.
        if not any(j.get("status") in ("running", "queued")
                   for j in _jobs_snapshot()):
            _unload_local_models(tts=True, stt=False, mt=False, ser=False)
    # Cópia: `_apply_settings` devolve o dict vivo `_settings` (e `_save_settings`
    # persiste só as chaves de _SETTINGS_DEFAULTS) — não injetar metadado nele.
    out = dict(_r)
    # Mesma vista do GET: quem não é admin não recebe segredo de terceiro em claro
    # nem na resposta do Salvar (a resposta É as settings, não um ack).
    out.update(_settings_vista(request))
    # sempre presente: a UI/storybook não precisa distinguir "sem campo" de "vazio"
    out["admin_ignored"] = sorted(ignorados)
    return out


def _voice_path(v) -> Path | None:
    """Path do WAV de `v` — e só se ele ficar DENTRO de `voices/`.

    `VOICES_DIR / f"{v}.wav"` aceita qualquer coisa: `"../fora"` aponta para um
    arquivo fora do diretório de vozes e ele seguia para o worker/remoto como se
    fosse voz cadastrada.

    O critério segue o ARQUIVO de propósito (decisão do PM na #69): só vale o que
    está DENTRO de `voices/` mesmo depois do symlink resolvido. Assim
    `voices/link.wav -> /fora/x.wav` é recusado (o conteúdo vem de fora) e `../fora`
    também; um symlink para dentro de `voices/` continua valendo. Não uso `_safe_id`:
    ele rejeitaria id exótico já registrado (`<img src=x …>`). None = escapa ou não existe."""
    if not v or not isinstance(v, str):
        return None
    p = VOICES_DIR / f"{v}.wav"
    try:
        if p.resolve().parent != VOICES_DIR.resolve():
            return None
    except OSError:
        return None
    return p if p.exists() else None


def _apply_settings(payload: dict):
    _dsh_antes = tuple(_settings.get(k) for k in _CAMPOS_DHS_BACKEND)
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
        # `_voice_path` (não `exists()` no caminho cru): "../x" não fica gravado como
        # voz padrão e não contamina todo pedido que não manda voz própria
        _settings["default_voice"] = v if _voice_path(v) else None
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
    if "stt_local_engine" in payload:
        eng = str(payload["stt_local_engine"] or "whisper").strip().lower()
        if eng not in ("whisper", "parakeet"):
            raise HTTPException(400, "stt_local_engine inválido (whisper|parakeet)")
        _settings["stt_local_engine"] = eng
    if "stt_whisper_repo" in payload:
        repo = str(payload["stt_whisper_repo"] or "").strip()[:200]
        if repo and (not re.fullmatch(r"[\w.\-/]+", repo) or "whisper" not in repo.lower()):
            raise HTTPException(400, "stt_whisper_repo inválido (esperado repo HF do whisper)")
        _settings["stt_whisper_repo"] = repo
    if "stt_beam" in payload:
        _settings["stt_beam"] = int(_clamp(payload["stt_beam"], 1, 10, 5))
    for chave in ("stt_anti_ruido", "stt_denoise"):
        if chave in payload:
            _settings[chave] = bool(payload[chave])
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
    if "chat_backend" in payload:
        b = str(payload["chat_backend"] or "openai").strip().lower()
        if b not in ("openai", "dsh"):
            raise HTTPException(400, "chat_backend inválido (openai|dsh)")
        _settings["chat_backend"] = b
    if "chat_backend_live" in payload:
        # vazio é VÁLIDO e significa "herda o global" (#176)
        bl = str(payload["chat_backend_live"] or "").strip().lower()
        if bl and bl not in ("openai", "dsh"):
            raise HTTPException(400, "chat_backend_live inválido (vazio=herda|openai|dsh)")
        _settings["chat_backend_live"] = bl
    if "chat_dsh_bin" in payload:
        _settings["chat_dsh_bin"] = str(payload["chat_dsh_bin"] or "dsh").strip()[:200] or "dsh"
    if "chat_dsh_profile" in payload:
        _settings["chat_dsh_profile"] = \
            str(payload["chat_dsh_profile"] or "").strip()[:80] or dsh_client.DSH_DEFAULT_PROFILE
    if "chat_dsh_model" in payload:
        m = str(payload["chat_dsh_model"] or "").strip()[:200]
        if m and not dsh_client.dsh_model_valido(m):
            raise HTTPException(400, 'chat_dsh_model inválido: use o par JSON do catálogo, '
                                     'ex. ["dsflash","deepseek-flash-41"]')
        _settings["chat_dsh_model"] = m or dsh_client.DSH_DEFAULT_MODEL
    if "chat_dsh_effort" in payload:
        e = str(payload["chat_dsh_effort"] or "off").strip().lower()
        if e not in dsh_client.DSH_EFFORTS:
            raise HTTPException(400, "chat_dsh_effort inválido (off|low|high|max)")
        _settings["chat_dsh_effort"] = e
    if "speaker_gate" in payload:
        g = str(payload["speaker_gate"] or "off").strip().lower()
        if g not in ("off", "enforce", "label"):
            raise HTTPException(400, "speaker_gate inválido (off|enforce|label)")
        _settings["speaker_gate"] = g
    if "speaker_threshold" in payload:
        _settings["speaker_threshold"] = _clamp(payload["speaker_threshold"], 0.35, 0.9, 0.6)
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
    if tuple(_settings.get(k) for k in _CAMPOS_DHS_BACKEND) != _dsh_antes:
        # trocou de backend/ajuste do dsh: já sobe o processo em thread, para quem
        # acabou de ligar `dsh` na UI não pagar o boot no 1º turno da Conversa
        _chat_dsh_prewarm("settings")
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
    _write_upload_limited(audio, tmp)
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

    voice_id = _safe_id(voice_id)
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

    STREAM de disco: antes o zip era montado num `BytesIO` e devolvido via
    `getvalue()` — o zip inteiro DUAS vezes na RAM (buffer + cópia do corpo), com
    cada .wav lido por inteiro pelo `z.write`. Com o teto de 512 MB anunciado no
    import isso chega perto de 1 GB de pico numa máquina que já carrega modelo.
    Agora grava num arquivo temporário e o `FileResponse` envia de lá, com o
    unlink no background (o import já usa o mesmo padrão de temporário).
    """
    import zipfile
    from starlette.background import BackgroundTask

    tmp = tempfile.NamedTemporaryFile(prefix=".vozes-", suffix=".zip",
                                      dir=OUTPUTS_DIR, delete=False)
    tmp.close()
    path = Path(tmp.name)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(VOICES_DIR.iterdir()):
                # só wav/json de voz; ignora temporários de upload (.up-*/.rep-*)
                if p.is_file() and p.suffix in (".wav", ".json") and not p.name.startswith("."):
                    z.write(p, f"voices/{p.name}")
    except Exception:  # noqa: BLE001 — não deixar temporário pela metade
        path.unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        media_type="application/zip",
        filename="tts-studio-vozes.zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
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
    import re as _re
    import zipfile

    # Copia em streaming para não depender de UploadFile.size nem carregar o
    # upload inteiro na RAM antes de validar o limite.
    with tempfile.NamedTemporaryFile(prefix=".voice-import-", suffix=".zip",
                                     dir=OUTPUTS_DIR, delete=False) as incoming:
        incoming_path = Path(incoming.name)
        recebido = 0
        try:
            while True:
                bloco = await zip_file.read(1024 * 1024)
                if not bloco:
                    break
                recebido += len(bloco)
                if recebido > _IMPORT_MAX_TOTAL:
                    raise HTTPException(400, "Zip grande demais (máx. 512 MB)")
                incoming.write(bloco)
        except Exception:
            incoming_path.unlink(missing_ok=True)
            raise

    try:
        zf = zipfile.ZipFile(incoming_path)
    except zipfile.BadZipFile as exc:
        incoming_path.unlink(missing_ok=True)
        raise HTTPException(400, "Arquivo não é um zip válido") from exc

    importados, ignorados, renomeados = [], [], []
    total = 0
    preparados = []
    try:
        with zf:
            with tempfile.TemporaryDirectory(prefix=".voice-import-", dir=OUTPUTS_DIR) as staging:
                staging_dir = Path(staging)
                vistos = set()
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
                    chave = nome.lower()
                    if (not _re.fullmatch(r"[A-Za-z0-9_-]+\.(wav|json)", nome)
                            or chave in vistos):
                        ignorados.append(info.filename)
                        continue
                    vistos.add(chave)
                    destino = staging_dir / nome
                    with zf.open(info) as origem, destino.open("wb") as saida:
                        shutil.copyfileobj(origem, saida, length=1024 * 1024)
                    if nome.endswith(".json"):
                        try:
                            dados = json.loads(destino.read_text(encoding="utf-8"))
                            if not isinstance(dados, dict):
                                raise ValueError("metadata deve ser objeto JSON")
                        except Exception:
                            ignorados.append(info.filename)
                            destino.unlink(missing_ok=True)
                            continue
                        # O `id` tem de ser o nome do arquivo: as rotas
                        # (/api/voices/{id}/audio|peaks) remontam o caminho pelo
                        # id com `_safe_id`, que é estrito — um id fora de
                        # [A-Za-z0-9_-] (ex.: `<img src=x onerror=…>`, de backup
                        # feito à mão) deixava a voz na lista e 404 no áudio/peaks,
                        # e um id ausente nem aparecia. Aqui o meta é alinhado ao
                        # stem (que JÁ passou pela regex no nome do arquivo).
                        stem = Path(nome).stem
                        if dados.get("id") != stem:
                            renomeados.append({"arquivo": nome,
                                               "de": dados.get("id"), "para": stem})
                            dados["id"] = stem
                            destino.write_text(
                                json.dumps(dados, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                    preparados.append((nome, destino))

                # Só publica depois de validar TODAS as entradas: falha não
                # deixa restore parcialmente aplicado.
                publicados = []
                backups = {}
                try:
                    for nome, origem in preparados:
                        destino = VOICES_DIR / nome
                        if destino.exists():
                            backup = staging_dir / (".backup-" + nome)
                            shutil.copy2(destino, backup)
                            backups[nome] = backup
                        origem.replace(destino)
                        publicados.append(nome)
                        importados.append(nome)
                except Exception:
                    # Reverte o que já foi publicado caso o filesystem falhe
                    # no meio da troca.
                    for nome in publicados:
                        (VOICES_DIR / nome).unlink(missing_ok=True)
                    for nome, backup in backups.items():
                        if backup.exists():
                            backup.replace(VOICES_DIR / nome)
                    importados.clear()
                    raise
    finally:
        incoming_path.unlink(missing_ok=True)
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
            "orfaos": orfaos[:10], "ignorados": ignorados[:10],
            # ids alinhados ao nome do arquivo (o que faz a UI achar áudio/peaks)
            "renomeados": renomeados[:10]}


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
        dados = _read_upload_limited(audio)
        if not dados:
            raise HTTPException(400, "Áudio vazio")
        a, sr = sf.read(_io.BytesIO(dados), dtype="float32")
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

    voice_id = _safe_id(voice_id)
    wav = VOICES_DIR / f"{voice_id}.wav"
    if not wav.exists():
        raise HTTPException(404, "Voz não encontrada")
    tmp = VOICES_DIR / f".rep-{voice_id}"
    _write_upload_limited(audio, tmp)
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

    voice_id = _safe_id(voice_id)
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
    voice_id = _safe_id(voice_id)
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
_JOBS_MAX = 20                          # teto do histórico (ver _evict_jobs)
# Teto de jobs EM VOO (running/queued) — é o que a admissão conta; acima disto o
# pedido novo leva 429 em vez de empurrar um job ATIVO para fora do histórico.
# Antes não existia: com os _JOBS_MAX já em voo, o evict não achava nenhum job
# terminado para descartar e caía no "último recurso" — apagava o job MAIS
# ANTIGO, que estava rodando (e o .job-* com os trechos), então o cliente perdia
# status e stream enquanto a thread seguia gerando. Padrão 20 = teto prático de
# antes (sem regressão); env TTS_JOBS_ACTIVE_MAX para apertar em máquina pequena.
_JOBS_ACTIVE_MAX = max(1, int(os.environ.get("TTS_JOBS_ACTIVE_MAX", "20")))
# Campos comuns de todo job novo (é o que as rotas de status/trechos consomem).
_JOB_BASE = {"status": "running", "pieces": 0, "total": None, "progress": None,
             "output": None, "error": None}
# Serializa admissão/evict. Endpoint `def` roda no threadpool do FastAPI, então
# checagem+insert concorrentes furavam o teto de ativos; e o `pop` do evict no
# meio de uma iteração alheia mexe no tamanho do dict. Reentrante porque a
# admissão chama helpers que também travam.
_jobs_lock = threading.RLock()

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


def _job_owner_write(pdir: Path) -> None:
    """Marca o dir do job com o pid do processo dono.

    É o que a limpeza de boot de OUTRO processo consulta para não apagar os
    trechos de um job em voo (`_job_dir_ativo`). Falha aqui não derruba o job:
    sem o arquivo o boot cai na regra de atividade recente."""
    try:
        (pdir / "owner.pid").write_text(str(os.getpid()))
    except OSError:
        pass


def _safe_id(v: str) -> str:
    """Valida id vindo da rota antes de montar Path — defesa em profundidade
    contra path traversal em downloads (o roteador já impede '/', isto aqui
    garante mesmo se o roteamento mudar)."""
    if not v or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v):
        raise HTTPException(404, "Id inválido")
    return v


def _jobs_ativos() -> int:
    """Jobs em voo (running/queued) — o que a admissão conta.

    `queued` conta: job parado na fila de falas ainda não entregou nada, então
    continua sendo um ativo que o cliente está esperando.
    """
    with _jobs_lock:
        return sum(1 for j in _jobs.values() if j.get("status") in ("running", "queued"))


def _jobs_snapshot() -> list:
    """Cópia dos jobs sob lock, para quem precisa ITERAR.

    Iterar a view viva (`_jobs.values()`) enquanto outra thread admite/evicta
    estoura "OrderedDict mutated during iteration" — e o `_jobs` foi feito para
    ser mexido de dentro de threads (admissão de rota + evict)."""
    with _jobs_lock:
        return list(_jobs.values())


def _jobs_admit(extra: dict | None = None) -> str:
    """Registra um job novo com admissão controlada; devolve o job_id.

    Estourou o teto de ativos → 429 (mesma família do limitador de taxa, que o
    cliente já sabe exibir e o navegador mostra pelo `detail`), ANTES de criar o
    job e de subir thread: o pedido recusado não deixa nada pela metade.
    `Retry-After` curto porque um slot libera assim que qualquer job termina.

    Checagem + insert acontecem sob `_jobs_lock`: endpoint `def` roda no
    threadpool do FastAPI, então duas requisições passavam juntas pela checagem
    e o teto era furado (medido no gate: teto 2 com 10 admissões simultâneas →
    4 aceitos). Com o lock o teto é exato; o invariante (nunca descartar ativo)
    valia antes e continua valendo.
    """
    with _jobs_lock:
        _jobs_capacity_check()
        job_id = uuid.uuid4().hex[:10]
        _jobs[job_id] = {**_JOB_BASE, **(extra or {})}
        _evict_jobs()
        return job_id


def _jobs_capacity_check():
    """Levanta 429 se não há slot de job ativo livre.

    Chamado cedo nas rotas que fazem STT/tradução antes de criar o job: recusar
    depois do STT inteiro custaria segundos de espera ao cliente para então
    devolver o erro (e o áudio teria de ser reenviado). Como não reserva slot,
    o probe pode passar e o `_jobs_admit` recusar logo depois — quem decide é
    sempre a admissão.
    """
    ativos = _jobs_ativos()
    if ativos >= _JOBS_ACTIVE_MAX:
        raise HTTPException(
            429,
            f"Limite de jobs em andamento atingido ({ativos}/{_JOBS_ACTIVE_MAX}) "
            "— aguarde um job terminar e tente de novo",
            headers={"Retry-After": "5"},
        )


def _evict_jobs():
    """Mantém no máximo _JOBS_MAX jobs no histórico — SEM tocar em job ativo.

    Só jobs terminados (done/error) saem: se todos os _JOBS_MAX estiverem em voo
    (admissão garante ativos ≤ _JOBS_ACTIVE_MAX) o dict fica acima do teto e
    volta a ele conforme os jobs terminam. Descartar um ativo aqui apagaria os
    trechos .job-* e o cliente perderia status/stream no meio da geração, com a
    thread ainda rodando (o bug: o job sumia, o áudio parava).

    Trava o `_jobs_lock` em volta do laço: é chamado da admissão (que já o
    segura — daí o RLock) e de testes, e o `pop` no meio de uma iteração
    concorrente mudaria o tamanho do dict.
    """
    with _jobs_lock:
        while len(_jobs) > _JOBS_MAX:
            alvo = next((jid for jid, j in _jobs.items()
                         if j.get("status") not in ("running", "queued")), None)
            if alvo is None:
                break                   # só ativos em voo: nenhum é descartado
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
            _model_state.update(status="idle", progress=None, model=_settings.get("model"),
                                precision=None, path=None)


def _run_tts_job_isolated(job_id: str, text: str, voice_id: str, voice_path: Path,
                          language: str, omni: dict, be: dict, sq=None,
                          model: str | None = None):
    """Síntese em subprocesso: crash nativo (SIGSEGV) só mata o worker."""
    import subprocess
    import sys

    job = _jobs[job_id]
    pdir = _piece_dir(job_id)
    pdir.mkdir(exist_ok=True)
    _job_owner_write(pdir)          # dono: o boot de outro processo consulta
    status_path = pdir / "status.json"
    cfg_path = pdir / "config.json"
    label = (be.get("meta") or {}).get("label") or be.get("id")
    family = be["family"]
    hold_pieces = sq is not None  # fila ligada: não publica trechos até entregar

    # libera RAM do modelo in-process antes do filho carregar o Qwen/etc.
    _unload_local_tts()
    _model_state.update(
        status="loading", device="mlx-worker", model=model or _settings.get("model"),
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
        "model": model or _settings.get("model") or "omnivoice",
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
            # Registro antes do polling: se o servidor morrer, o próximo
            # startup consegue localizar e encerrar este grupo de processos.
            (pdir / "worker.pid").write_text(str(proc.pid))
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
            (pdir / "worker.pid").unlink(missing_ok=True)

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
                 language: str, omni: dict, model_override: str | None = None,
                 use_queue: bool = True):
    """use_queue=False pula a fila de falas: quem controla o tempo é o cliente.
    A Conversa toca frase a frase no navegador — se o gate também segurasse a
    entrega, a reserva (duração da fala anterior) viraria silêncio em dobro, e
    depois de um barge-in ficaria reservando uma fala que ninguém ouviu."""
    import numpy as np
    import soundfile as sf

    job = _jobs[job_id]
    remote = False
    sq = _speech_queue_begin(job_id) if use_queue else None
    hold_pieces = sq is not None  # com fila: grava trechos mas só libera na entrega
    # `model_override` = modelo DESTE pedido. Vale sem trocar o global: quem não é
    # admin manda `model` no body e gera com ele (ver /api/tts); admin também
    # persiste, então os dois caminhos ficam idênticos aqui.
    try:
        remote = _use_remote_tts()
        be = _current_backend(model_override)
        family = be["family"]
        be_meta = be.get("meta") or {}

        # Qwen/Fish/etc.: processo isolado (não trava/derruba o servidor)
        if not remote and family in _ISOLATED_FAMILIES:
            _run_tts_job_isolated(job_id, text, voice_id, voice_path, language, omni, be,
                                  sq=sq, model=model_override)
            # worker morreu: garante pool Metal limpo no pai + apaga trechos depois
            _release_mlx_memory(aggressive=True)
            st = job.get("status")
            _schedule_job_cleanup(job_id, delay_s=90 if st == "done" else 30)
            return

        chunks = _split_text(_sanitize_text(text), max_chars=_settings["chunk_max_chars"])
        job["total"] = len(chunks)
        pdir = _piece_dir(job_id)
        pdir.mkdir(exist_ok=True)
        _job_owner_write(pdir)      # dono: o boot de outro processo consulta

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
            model = None if remote else _get_model(model_override)
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
def synthesize(request: Request, payload: dict):
    """Aceita o pedido e devolve o job; valida ANTES de mexer em qualquer config.

    `model` no body é o modelo DESTE pedido. Ele também vira config global quando
    quem manda é admin/loopback (é o que a UI espera do seletor: escolheu, ficou
    escolhido); para chave de uso ele vale só no pedido — coerente com `model` ser
    campo admin no /api/settings. E, dos dois jeitos, só DEPOIS do pedido passar:
    antes, um 400 (texto vazio) ou 404 (voz) já tinha trocado `settings['model']`
    na RAM e no disco, com um job que nem rodou.
    """
    text = (payload.get("text") or "").strip()
    if _settings["pre_prompt"]:
        text = f"{_settings['pre_prompt']} {text}".strip()
    language = (payload.get("language") or _settings["language"]).lower()
    # `model` no body: resolvido aqui SEM mutar nada (a família decide as regras de
    # voz/design abaixo, então precisa valer já na validação).
    model_override = None
    if payload.get("model"):
        m = str(payload["model"]).strip()
        if m and m != "__custom__":
            model_override = m
    be = _current_backend(model_override)
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
    elif raw_voice and _voice_path(raw_voice):
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

    # text[:200]: facilita depurar relatos de áudio mudo
    job_id = _jobs_admit({"text": text[:200]})

    # Pedido ACEITO (passou na validação e tem slot). O job já recebe o modelo do
    # pedido explicitamente; o que falta é decidir se ele também vira config GLOBAL:
    # - admin/loopback: sim (contrato do seletor da UI: escolhe e fica escolhido);
    # - chave de USO: não — `model` é campo admin no /api/settings, então a chave de
    #   uso gera com o modelo pedido sem sequestrar a configuração da instalação.
    # `_save_settings` é melhor-esforço: falha de disco não derruba a geração.
    persistido = bool(model_override) and _admin_is_allowed(request)
    if persistido and _settings.get("model") != model_override:
        _settings["model"] = model_override
        try:
            _save_settings()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(
        target=_run_tts_job,
        args=(job_id, text, voice_id, voice_path, language, omni, model_override),
        daemon=True,
    ).start()
    out = {"job_id": job_id, "backend": be.get("id"), "family": family}
    if model_override:
        # verdadeiro = o global agora é esse modelo; falso = valeu só neste pedido
        out["model"] = model_override
        out["model_aplicado_global"] = _settings.get("model") == model_override
    return out


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
    # Temporários de uploads podem sobreviver a timeout/crash. Limpa somente
    # arquivos conhecidos e antigos, nunca outputs finais.
    temp_cutoff = time.time() - 6 * 3600
    for pattern in (".stt-*", ".pstt-*", ".voz-*", ".vozchk-*", ".up-*", ".rep-*"):
        for tmp in OUTPUTS_DIR.glob(pattern):
            try:
                if tmp.is_file() and tmp.stat().st_mtime < temp_cutoff:
                    tmp.unlink(missing_ok=True)
                    removidos += 1
            except OSError:
                pass
    return removidos


def _auto_cleanup_loop():
    while True:
        time.sleep(60)
        try:
            if _settings["auto_cleanup"]:
                _auto_cleanup_once()
            else:
                # Mesmo com histórico permanente, temporários órfãos devem sair.
                temp_cutoff = time.time() - 6 * 3600
                for pattern in (".stt-*", ".pstt-*", ".voz-*", ".vozchk-*", ".up-*", ".rep-*"):
                    for tmp in OUTPUTS_DIR.glob(pattern):
                        try:
                            if tmp.is_file() and tmp.stat().st_mtime < temp_cutoff:
                                tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_auto_cleanup_loop, daemon=True).start()
# idle unload sobe depois que _ser/_mt existem (ver fim do bloco de modelos)


@app.delete("/api/outputs/{out_id}")
def delete_output(out_id: str):
    out_id = _safe_id(out_id)
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
                    # precision/path junto com o status: sem isto /api/status segue
                    # afirmando "ready, precision X, path Y" de um modelo que já
                    # saiu da RAM (unload por ociosidade / troca de precisão).
                    _model_state.update(status="idle", error=None, progress=None,
                                        precision=None, path=None)
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
        if _pk["model"] is not None:
            _pk["model"] = None
            _pk["repo"] = ""
            freed.append("parakeet")
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
            # não descarrega TTS no meio de um job (snapshot: iterar a view viva estoura
            # quando uma rota admite job na mesma hora)
            busy_tts = any(
                j.get("status") == "running" for j in _jobs_snapshot()
            )
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


def _ffmpeg_to_mono16k(audio_path: Path):
    """Decodifica qualquer formato que o FFMPEG entenda (webm/opus do
    MediaRecorder, m4a, mp4, ogg…) direto para float32 mono 16 kHz."""
    import numpy as np

    p = subprocess.run([FFMPEG, "-v", "error", "-i", str(audio_path),
                        "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                       capture_output=True, timeout=120)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace").strip()[:160]
                           or "ffmpeg não decodificou o áudio")
    return np.frombuffer(p.stdout, dtype="<f4").copy()


def _wav_to_mono16k(audio_path: Path):
    """Devolve array float32 mono a 16 kHz.

    Caminho normal: soundfile (sem processo externo), que já é o bastante para
    WAV vindo do navegador — evita o load_audio do whisper e o ffmpeg_read do
    transformers. Quando o libsndfile não reconhece o formato (ele não lê
    matroska: webm/opus do MediaRecorder, m4a, mp4), cai no FFMPEG do projeto
    — que tem o binário do imageio-ffmpeg como reserva, então não depende de
    ffmpeg no PATH. Sem esse fallback, quem chamasse isto com o blob cru
    levava LibsndfileError (ou, via `_vad_tem_fala`, um fail-open silencioso)."""
    import numpy as np
    import soundfile as sf

    try:
        audio, sr = sf.read(str(audio_path), dtype="float32")
    except Exception:  # noqa: BLE001 — formato que o libsndfile não lê: usa ffmpeg
        return _ffmpeg_to_mono16k(audio_path)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if int(sr) != 16000:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(sr), 16000)
        audio = resample_poly(audio, 16000 // g, int(sr) // g)
    return np.ascontiguousarray(audio, dtype=np.float32)


# --- Modelos remotos (API OpenAI-compatível): tradução e transcrição opcionais ---
def _env_base(nome: str) -> str:
    """Base vinda do AMBIENTE (o override do #103/#107). Vazio = sem override."""
    return (os.environ.get(nome) or "").strip()


def _remote_ready() -> bool:
    # base_url basta; api_key é opcional (endpoints em LAN, ex.: RTX, não têm auth)
    return bool(_settings.get("remote_base_url"))


def _use_remote_translate() -> bool:
    """Caminho remoto do tradutor.

    ENV PRIMEIRO e já basta: com `TTS_TRANSLATE_BASE_URL` no ambiente o remoto está
    ativo mesmo com URLs/toggles vazios no settings — senão o override ficava
    INERTE e o cliente caía no local em silêncio (medido no gate #109: 200 com texto
    vazio em vez do erro do provedor). Sem env, a regra antiga (toggle + base)."""
    return bool(_env_base("TTS_TRANSLATE_BASE_URL")) or (
        bool(_settings.get("remote_translate")) and _remote_ready())


def _use_remote_stt() -> bool:
    # idem: env dedicado do STT (ou a base do tradutor) basta para o remoto
    return bool(_env_base("TTS_STT_BASE_URL") or _env_base("TTS_TRANSLATE_BASE_URL")) \
        or (bool(_settings.get("remote_stt")) and bool(
            _settings.get("remote_stt_base_url") or _settings.get("remote_base_url")))


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


def _traducao_remota_cfg() -> tuple[str, str]:
    """(base_url, modelo) do tradutor remoto — env > settings, com erro EXPLICATIVO.

    NÃO passa pelo `_chat_provider` de propósito (decisão do PM no mini-gate): o
    tradutor fala com o 14B do RTX pelo OmniVoice, unificar mudaria a semântica.
    `TTS_TRANSLATE_BASE_URL`/`TTS_TRANSLATE_MODEL` existem para smoke isolado, no
    mesmo padrão do `TTS_CHAT_*`.
    Base vazia antes virava `MissingSchema: Invalid URL '/chat/completions'` cru."""
    base = (os.environ.get("TTS_TRANSLATE_BASE_URL") or "").strip() \
        or (_settings.get("remote_base_url") or "").strip()
    if not base:
        raise HTTPException(400, "Tradutor remoto não configurado — informe a Base URL "
                                 "do provedor (Configurações → Rede) ou defina "
                                 "TTS_TRANSLATE_BASE_URL")
    modelo = (os.environ.get("TTS_TRANSLATE_MODEL") or "").strip() \
        or (_settings.get("remote_translate_model") or "gpt-4o-mini")
    return base.rstrip("/"), modelo


def _translate_remote(text: str, target: str, emotion: str | None = None) -> str:
    import requests

    base, modelo = _traducao_remota_cfg()
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {_settings['remote_api_key']}",
                 "Content-Type": "application/json"},
        json={"model": modelo,
              "temperature": 0.4 if emotion else 0.2,
              "messages": [{"role": "user", "content": _translate_prompt(text, target, emotion)}]},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"tradução remota falhou ({r.status_code}): {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"].strip().strip('"').strip()


def _transcribe_remote(audio_path: Path, language: str | None):
    import requests

    # URL/chave dedicadas do STT, se definidas; senão as compartilhadas (RTX).
    # Mesmo defeito do tradutor se ambas estiverem vazias: mensagem explicativa.
    base = ((os.environ.get("TTS_STT_BASE_URL") or "").strip()
            or (_settings.get("remote_stt_base_url") or "").strip()
            or (_settings.get("remote_base_url") or "").strip())
    if not base:
        raise HTTPException(400, "STT remoto não configurado — informe a URL do "
                                 "provedor (Configurações → Rede) ou defina TTS_STT_BASE_URL")
    base = base.rstrip("/")
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
            files={"file": ("audio.wav", fh, "audio/wav")}, data=data, timeout=(5, 60),
        )
    if not r.ok:
        raise RuntimeError(f"transcrição remota falhou ({r.status_code}): {r.text[:200]}")
    j = r.json()
    return {"text": j.get("text", ""), "language": (j.get("language") or "").strip().lower(),
            "segments": j.get("segments") or []}


_vad_model = None


_vad_backend = ""  # "onnx" | "torch-jit" — caminho realmente carregado


def _vad_load():
    """Silero VAD carregado sob demanda (~1MB, CPU, roda em tempo real).

    Pede o modelo ONNX EXPLICITAMENTE: `load_silero_vad()` sem argumento não
    basta porque o default mudou de versão — no silero-vad 5.x era onnx=True,
    no 6.x é onnx=False. Com 6.x instalado, o app carregava o jit do torch
    mesmo com onnxruntime presente, e o `except` nunca disparava (os dois
    ramos carregavam o mesmo modelo).

    NÃO é "mais leve": medido no M3, o ONNX soma ~30–40 MB de RSS (o silero_vad
    importa torch no topo, então o torch entra nos dois caminhos) e empata em
    velocidade fora do cold start. Fica ONNX por ser o caminho declarado no
    requirements, por não desserializar torchscript e por tornar o fallback
    explícito e logado. Sem onnxruntime o load estoura e o fallback é o jit do
    torch (mesmo resultado, dependência extra)."""
    global _vad_model, _vad_backend
    if _vad_model is None:
        from silero_vad import load_silero_vad
        try:
            _vad_model = load_silero_vad(onnx=True)
            _vad_backend = "onnx"
        except Exception as e:  # noqa: BLE001 — sem onnxruntime: cai no torch
            print(f"[vad] modelo ONNX indisponível ({str(e)[:120]}) — caindo no torch jit",
                  flush=True)
            _vad_backend = "torch-jit"
            _vad_model = load_silero_vad(onnx=False)
    return _vad_model


def _vad_tem_fala(audio_path: Path, minimo_s: float = 0.3) -> bool:
    """Silero VAD: True se o wav tem fala de verdade (não ruído).

    Reamostra para 16 kHz antes do modelo: o Silero só aceita 8/16 kHz e o
    navegador manda WAV a 24 kHz — sem a reamostragem o modelo estourava e o
    `except` devolvia True, ou seja, o guarda anti-alucinação era no-op em todo
    áudio vindo de arquivo (ruído puro passava como fala).
    Em caso de erro no Silero, devolve True (não bloqueia o pipeline)."""
    try:
        import torch
        from silero_vad import get_speech_timestamps
        audio = _wav_to_mono16k(audio_path)          # 16 kHz mono, sr suportado
        speech = get_speech_timestamps(torch.from_numpy(audio), _vad_load(),
                                       sampling_rate=16000, threshold=0.6, speech_pad_ms=100)
        dur_fala = sum((t["end"] - t["start"]) for t in speech) / 16000
        return dur_fala >= minimo_s
    except Exception:  # noqa: BLE001 — Silero indisponível não bloqueia o STT
        return True


def _whisper_repo() -> str:
    """Repo HF do Whisper local: setting (ex. large-v3 completo p/ máxima
    precisão) com fallback p/ o default do env."""
    return (_settings.get("stt_whisper_repo") or "").strip() or WHISPER_REPO


def _transcribe(audio_path: Path, language: str | None = None, allow_remote: bool = True):
    caiu_remoto, erro_remoto = False, ""
    if allow_remote and _use_remote_stt():
        try:
            return _transcribe_remote(audio_path, language)
        except Exception as e:  # noqa: BLE001 — RTX fora do ar cai pro local
            print(f"[stt] remoto indisponível ({str(e)[:120]}) — usando local", flush=True)
            # o cliente precisa distinguir "transcrição vazia" de "o remoto morreu":
            # vai no resultado (as rotas traduzem para header/campo)
            caiu_remoto, erro_remoto = True, str(e)[:200]

    # Silero VAD: se o áudio não tem fala, o STT nem roda (mata alucinação)
    fala = _vad_tem_fala(audio_path)
    if not fala:
        return _com_aviso_remoto({"text": "", "language": "", "segments": []},
                                 caiu_remoto, erro_remoto)

    if (_settings.get("stt_local_engine") or "whisper").lower() == "parakeet":
        return _com_aviso_remoto(_transcribe_parakeet(audio_path), caiu_remoto, erro_remoto)

    import mlx_whisper

    # language fixo (ex.: "pt") melhora muito a precisão em trechos curtos: o
    # whisper deixa de adivinhar o idioma a cada frase. None = auto-detecta.
    lang = language if language and language not in ("auto",) else None
    audio = _wav_to_mono16k(audio_path)
    with _stt_lock:
        # opções que reduzem alucinação: greedy (beam ainda não existe no
        # mlx-whisper) e sem condicionar no texto anterior
        r = mlx_whisper.transcribe(
            audio, path_or_hf_repo=_whisper_repo(), language=lang,
            temperature=0.0, condition_on_previous_text=False,
            # Limiares do PRÓPRIO whisper fazem ele descartar o trecho inteiro: o
            # texto volta vazio e o motivo se perde (medido com "Sim."). Quem julga
            # é o _stt_ok, que devolve a razão — então aqui se pede ao whisper para
            # não deitar nada fora por conta própria.
            no_speech_threshold=0.99, logprob_threshold=-3.0,
            compression_ratio_threshold=9.9,
        )
    del audio
    _touch_use("stt")
    _release_mlx_memory()
    return _com_aviso_remoto(r, caiu_remoto, erro_remoto)


_pk = {"model": None, "repo": ""}


def _com_aviso_remoto(res, caiu: bool, erro: str = ""):
    """Marca o resultado do STT quando o remoto falhou e o local assumiu."""
    if caiu and isinstance(res, dict):
        return {**res, "remote_fallback": True, "remote_error": erro}
    return res


def _aviso_remoto_headers(alvo, r: dict) -> None:
    """Aviso no HEADER (o `/v1/audio/*` também devolve text/srt/vtt, então campo no
    corpo não cobriria tudo). `alvo` é `response.headers` ou o header de um Response."""
    if not r.get("remote_fallback"):
        return
    alvo["X-TTS-Remote-Fallback"] = "1"
    msg = str(r.get("remote_error") or "")[:150]
    if msg:
        alvo["X-TTS-Remote-Error"] = msg.encode("ascii", "replace").decode()


def _transcribe_parakeet(audio_path: Path) -> dict:
    """STT local com NVIDIA Parakeet TDT 0.6B v3 (multilíngue, ~30x tempo real):
    pontuação/acento nativos, timestamps por sentença. Sem probs de no-speech —
    os filtros de alucinação são VAD + tamanho do texto + blacklist (_stt_ok)."""
    from parakeet_mlx import from_pretrained

    with _stt_lock:
        if _pk["model"] is None or _pk["repo"] != PARAKEET_REPO:
            _pk["model"] = from_pretrained(PARAKEET_REPO)
            _pk["repo"] = PARAKEET_REPO
        r = _pk["model"].transcribe(audio_path)
    _touch_use("stt")
    _release_mlx_memory()
    return {"text": (r.text or "").strip(),
            "language": "",
            "segments": [{"start": float(s.start), "end": float(s.end),
                          "text": (s.text or "").strip()} for s in (r.sentences or [])]}


def _stt_ok(r: dict, text: str):
    """Aceita só transcrição que pareça fala real (rejeita ruído/alucinação).

    `stt_anti_ruido=false` desliga tudo: o que continua valendo é o gate de
    silêncio (VAD) e o tamanho do áudio — para quem quer ver também o que o
    Whisper duvidou.
    """
    if not _settings.get("stt_anti_ruido", True):
        return True, ""
    t = text.strip()
    palavras = re.findall(r"[^\W\d_]+", t, flags=re.UNICODE)  # palavras (sem números/símbolos)
    if not t:
        # honesto: fala curta que o whisper descartou chegava aqui como vazio e era
        # reportada como "curto demais", que sugere filtro de tamanho
        return False, "não ouviu fala"
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
        # no_speech_prob sozinho deitava fala curta e boa fora (medido: "Sim."
        # voltava sem texto). Só rejeita se a confiança também estiver fraca.
        if nsp > _settings["stt_max_no_speech"] and alp < _settings["stt_min_logprob"] + 0.5:
            return False, f"sem fala ({nsp:.2f}, confiança {alp:.2f})"
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
                                           path_or_hf_repo=_whisper_repo(), language="pt")
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
    raw = OUTPUTS_DIR / f"{prefix}-{uuid.uuid4().hex[:10]}{suffix}"
    _write_upload_limited(upload, raw)
    wav = OUTPUTS_DIR / f"{prefix}-{uuid.uuid4().hex[:10]}.wav"
    try:
        # trim de silêncio nas pontas + 16k mono: o Whisper alucina em ruído puro —
        # sem isso o "nada" vira "E aí". O denoise espectral (afftdn) é opção: em
        # áudio limpo ele é neutro para a acurácia (medido por WER) e em áudio ruim
        # ajuda, então fica a critério de quem está capturando.
        filtro = "highpass=f=80"
        if _settings.get("stt_denoise", True):
            filtro += ",afftdn=nr=12"
        filtro += (",silenceremove=start_periods=1:start_threshold=-45dB,"
                   "areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse")
        p = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(raw),
                            "-af", filtro, "-ar", "16000", "-ac", "1", str(wav)],
                           capture_output=True, timeout=120, text=True)
        stderr = (p.stderr or "").strip()
        if p.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
            print(f"[stt] ffmpeg ({ctype}, {raw.stat().st_size if raw.exists() else 0}B): {stderr[:160]}", flush=True)
            raise RuntimeError(stderr[:160] or "ffmpeg não produziu saída")
    except HTTPException:
        raise
    except Exception as e:
        raw.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
        raise HTTPException(400, f"Formato de áudio não suportado ({ctype or 'desconhecido'}): {e}") from e
    raw.unlink(missing_ok=True)
    return wav


def _read_upload_limited(upload: UploadFile, default_mb: int = 64) -> bytes:
    """Lê uploads em memória com teto comum para voz, edição e STT."""
    try:
        limite_mb = int(os.environ.get("TTS_MAX_UPLOAD_MB", str(default_mb)))
    except (TypeError, ValueError):
        limite_mb = default_mb
    limite_mb = max(0, min(limite_mb, 2048))
    limite = limite_mb * 1024 * 1024
    dados = upload.file.read(limite + 1)
    if len(dados) > limite:
        raise HTTPException(413, f"Áudio maior que {limite_mb} MB")
    return dados


def _upload_limit_bytes(default_mb: int = 64) -> tuple[int, int]:
    try:
        limite_mb = int(os.environ.get("TTS_MAX_UPLOAD_MB", str(default_mb)))
    except (TypeError, ValueError):
        limite_mb = default_mb
    limite_mb = max(0, min(limite_mb, 2048))
    return limite_mb, limite_mb * 1024 * 1024


def _write_upload_limited(upload: UploadFile, path: Path, default_mb: int = 64) -> None:
    """Copia multipart em blocos, sem materializar o áudio completo na RAM."""
    limite_mb, limite = _upload_limit_bytes(default_mb)
    total = 0
    try:
        with Path(path).open("wb") as out:
            while True:
                bloco = upload.file.read(min(1024 * 1024, limite - total + 1))
                if not bloco:
                    break
                total += len(bloco)
                if total > limite:
                    raise HTTPException(413, f"Áudio maior que {limite_mb} MB")
                out.write(bloco)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    if total == 0:
        Path(path).unlink(missing_ok=True)
        raise HTTPException(400, "Áudio vazio")


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


# Allowlist do /api/youtube-audio. `host.endswith("youtube.com")` puro aceitava
# "evil-youtube.com" e "youtube.com.evil.com" (domínios registráveis por
# terceiros, com outro conteúdo) — exige-se o PONTO antes do domínio.
YT_ALLOWED_ZONE_HOSTS = ("youtube.com", "youtube-nocookie.com")
YT_ALLOWED_EXACT_HOSTS = ("youtu.be",)


def _yt_host_allowed(host: str) -> bool:
    """Só o domínio exato ou um subdomínio DELE (ponto obrigatório antes)."""
    h = (host or "").strip().lower().rstrip(".")      # "youtube.com." é o mesmo host
    if not h:
        return False
    return h in YT_ALLOWED_EXACT_HOSTS or h in YT_ALLOWED_ZONE_HOSTS or any(
        h.endswith("." + d) for d in YT_ALLOWED_ZONE_HOSTS)


def _url_parse(url: str):
    """`urlparse` que não estoura: IPv6 malformado ("http://[::1") levanta
    ValueError no PRÓPRIO urlparse() (e em `.hostname`). None = não parseou."""
    try:
        return urlparse(url or "")
    except ValueError:
        return None


def _yt_url_host(url: str) -> str:
    try:
        return (_url_parse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""


def _yt_final_host(info: object) -> str:
    """Host que o yt-dlp REALMENTE abriu ("" = não deu para saber).

    Um link de redirecionamento (youtube.com/redirect?q=…) faz o yt-dlp cair no
    extractor genérico e baixar de outro site; o host da página final é o único
    lugar onde isso aparece — a URL do STREAM (googlevideo.com) nunca bate com a
    allowlist e não serve para esta checagem."""
    if not isinstance(info, dict):
        return ""
    for key in ("webpage_url", "original_url"):
        host = _yt_url_host(info.get(key) or "")
        if host:
            return host
    entries = info.get("entries")
    if entries:
        for entry in entries:
            if isinstance(entry, dict):
                return _yt_url_host(entry.get("webpage_url") or "")
    return ""


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
                    info = ydl.extract_info(url, download=True)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if not _yt_retryable(exc):
                    raise
                continue
            # Redirecionou para fora da allowlist (ex.: youtube.com/redirect?q=…):
            # não vale insistir nos outros clients, já baixou de outro host.
            fora = _yt_final_host(info)
            if fora and not _yt_host_allowed(fora):
                raise RuntimeError(
                    f"o link saiu do YouTube ({fora}) — use o endereço direto do vídeo "
                    "(youtube.com/… ou youtu.be/…)"
                )
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
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Informe o link do YouTube")
    u = _url_parse(url)
    # `urlparse` LEVANTA em IPv6 malformado ("http://[::1") — antes virava 500 em vez
    # de 400; `u.hostname` também, por isso o host sai do helper (que trata).
    host = _yt_url_host(url)
    if u is None or u.scheme not in ("http", "https") or not _yt_host_allowed(host):
        raise HTTPException(400, "Use um link do YouTube (youtube.com ou youtu.be)")
    # youtube.com/redirect?q=<url>: o yt-dlp segue o q e baixa de onde ele aponta. O
    # host final nem sempre aparece no info do yt-dlp (a checagem pós-download
    # passava batido), então o ALVO é validado aqui: 400 imediato, sem baixar para
    # depois descartar. O bloco pós-download segue como rede de segurança (cadeia).
    if u.path.rstrip("/") == "/redirect":
        alvo = (parse_qs(u.query).get("q") or [""])[0].strip()
        if alvo.startswith("//"):
            alvo = f"{u.scheme}:{alvo}"
        elif alvo.startswith("/"):
            alvo = f"{u.scheme}://{host}{alvo}"
        alvo_host = _yt_url_host(alvo) if alvo else ""
        if alvo_host and not _yt_host_allowed(alvo_host):
            raise HTTPException(
                400,
                f"o link saiu do YouTube ({alvo_host}) — use o endereço direto do vídeo "
                "(youtube.com/… ou youtu.be/…)",
            )
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
    lim = float(_settings.get("speaker_threshold")
                or _SETTINGS_DEFAULTS["speaker_threshold"])
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
def transcribe_audio(response: Response, audio: UploadFile = None,
                     source_lang: str = Form("auto")):
    """Transcrição pura (sem tradução/TTS): áudio -> texto + segmentos. Usa o
    Whisper local ou remoto, conforme as configurações de modelos remotos."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    aviso = {}
    tmp = _save_audio_upload(audio)
    try:
        gate = _speaker_gate_ok(tmp)
        if gate.get("rejected"):
            return {"rejected": True, "reason": gate["reason"], "text": ""}
        r = _transcribe(tmp, language=(source_lang or "auto").lower())
    finally:
        tmp.unlink(missing_ok=True)
    # remoto caiu e o local assumiu: o cliente PRECISA saber (senão "vazio" e
    # "provedor morto" ficam indistinguíveis num 200). Header sempre; campo no corpo
    # porque esta rota é JSON de ponta a ponta.
    _aviso_remoto_headers(response.headers, r)
    aviso = {"remote_fallback": True, "remote_error": r.get("remote_error", "")} \
        if r.get("remote_fallback") else {}
    # filtro anti-alucinação (blacklist "e aí", fragmentos, sem-fala): o
    # /api/transcribe alimenta a Conversa — ruído não vira mensagem
    texto = (r.get("text") or "").strip()
    ok, motivo = _stt_ok(r, texto)
    if not ok:
        return {**aviso, "rejected": True, "reason": motivo, "text": ""}
    segs = r.get("segments") or []
    out = {**aviso, "text": (r.get("text") or "").strip(),
           "language": (r.get("language") or "").strip().lower(),
           "segments": [{"start": float(s.get("start") or 0.0),
                         "end": float(s.get("end") or 0.0),
                         "text": (s.get("text") or "").strip()} for s in segs]}
    if gate.get("speaker"):
        out["speaker"] = gate["speaker"]
        out["speaker_sim"] = gate.get("speaker_sim")
    return out


def _preparar_fala_para_voz(audio: UploadFile, voice_id: str, source_lang: str,
                            emotion_mode: str, instruct: str, msg_voz_design: str,
                            com_gate: bool = False) -> dict:
    """Pipeline comum do tradutor e do modificador de fala.

    Valida a voz (gravada, padrão ou por descrição), aplica o gate de locutor,
    transcreve, filtra ruído/alucinação e idioma de entrada, captura a emoção e
    monta os controles de geração. Ordem dos efeitos: gate -> transcribe ->
    stt_ok -> idioma -> emoção -> unlink do temporário.

    Devolve {"resposta": {...}} quando o áudio é rejeitado (o endpoint só
    devolve esse corpo) ou os intermediários prontos p/ o passo da tradução:
    vid/vpath/gate/src_text/src_lang/lang/omni/emotion/emo_show/emo_err.
    """
    vid = voice_id or _settings["default_voice"]
    # id que ESCAPA de voices/ ('../x') não pode virar caminho do job: resolve para
    # uma voz registrada, como qualquer id desconhecido
    if vid and vid != DESIGN_VOICE_ID and vid not in OMNI_PRESETS and not _voice_path(vid):
        vid = _resolve_voice(vid)
    design = (vid == DESIGN_VOICE_ID)                       # voz por descrição (tags OmniVoice)
    des_instruct = _sanitize_instruct(instruct) if design else ""
    vpath = VOICES_DIR / f"{vid}.wav"
    if design:
        ok = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
        if not ok:
            raise HTTPException(400, msg_voz_design)
    elif not vpath.exists() and vid not in OMNI_PRESETS:
        raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    exp = (source_lang or "auto").lower()
    tmp = _save_audio_upload(audio)
    gate = _speaker_gate_ok(tmp) if com_gate else {}
    if gate.get("rejected"):
        tmp.unlink(missing_ok=True)
        return {"resposta": {"rejected": True, "reason": gate["reason"], "source_text": ""}}
    emo_label, emo_pitch, emo_speed, emo_err = "", "", 1.0, None
    try:
        r = _transcribe(tmp, language=exp)
        src_text = (r.get("text") or "").strip()
        src_lang = (r.get("language") or "").strip().lower()
        ok, motivo = _stt_ok(r, src_text)
        if not ok:
            # não é erro: ruído/silêncio — o cliente apenas ignora e segue ouvindo
            return {"resposta": {"rejected": True, "reason": motivo, "source_text": src_text}}
        # filtro de idioma de entrada: só segue se a fala estiver no idioma escolhido
        if exp not in ("", "auto") and src_lang and src_lang != exp:
            return {"resposta": {"rejected": True,
                                 "reason": f"idioma errado (detectou {src_lang})",
                                 "source_text": src_text, "source_lang": src_lang}}
        # captura de emoção (precisa do áudio ainda em disco)
        modo = (emotion_mode or "off").lower()
        if modo in ("light", "accurate"):
            emo_label, emo_pitch, emo_speed, emo_err = _emotion_instruct(tmp, src_text, modo)
    finally:
        tmp.unlink(missing_ok=True)

    emotivo = bool(emo_label) and emo_label != "neutro"
    omni = _resolve_omni({})
    if design:   # voz por descrição: o instruct pedido define a voz (vence o clone/emoção)
        omni["instruct"] = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
    if emotivo:
        # pitch = tag válida (nudge, mantém o clone); em voice design o pitch da emoção
        # NÃO troca a voz desenhada (mantém o instruct) — a emoção age via velocidade
        # /expressividade + tom do texto. Velocidade = time-stretch clampado 0,5–2,0.
        ep = _sanitize_instruct(emo_pitch)
        if ep and not design:
            omni["instruct"] = ep
        if emo_speed and abs(float(emo_speed) - 1.0) > 1e-3:
            base = float(omni.get("speed") or 1.0)
            omni["speed"] = round(_clamp(base * float(emo_speed), 0.5, 2.0, base), 3)
        # mais expressivo/menos monótono (guidance↓, position_temperature↑)
        omni["guidance_scale"] = round(max(0.5, float(omni.get("guidance_scale") or 2.0) - 0.4), 2)
        omni["position_temperature"] = round(min(20.0, float(omni.get("position_temperature") or 5.0) + 4.0), 1)

    emo_show = None
    if emotivo:
        bits = [b for b in (emo_pitch, f"{omni['speed']}×" if abs(float(omni.get('speed') or 1) - 1) > 1e-3 else "") if b]
        emo_show = emo_label + (f" ({', '.join(bits)})" if bits else "")

    lang = exp if exp not in ("", "auto") else (src_lang or _settings["language"])
    return {"vid": vid, "vpath": vpath, "gate": gate, "src_text": src_text,
            "src_lang": src_lang, "lang": lang, "omni": omni,
            "emotion": emo_label if emotivo else None,
            "emo_show": emo_show, "emo_err": emo_err}


@app.post("/api/translate-speech")
def translate_speech(audio: UploadFile = None, target_lang: str = Form("en"),
                     voice_id: str = Form(""), source_lang: str = Form("auto"),
                     emotion_mode: str = Form("off"), instruct: str = Form("")):
    """fala (áudio) -> transcreve -> traduz -> dispara TTS na voz; devolve textos + job_id."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    _jobs_capacity_check()      # antes do STT+tradução: recusa cedo, não em segundos
    tgt = (target_lang or "en").lower()
    p = _preparar_fala_para_voz(audio, voice_id, source_lang, emotion_mode, instruct,
                                "Voice design vazio — descreva a voz p/ o tradutor",
                                com_gate=True)
    if "resposta" in p:
        return p["resposta"]
    src_text, src_lang, gate = p["src_text"], p["src_lang"], p["gate"]
    # o LLM já traduz no TOM da emoção (pontuação/ênfase) -> prosódia segue o texto
    translation = _translate(src_text, tgt, p["emotion"])

    job_id = _jobs_admit({"text": translation[:200]})
    threading.Thread(
        target=_run_tts_job,
        args=(job_id, translation, p["vid"], p["vpath"], tgt, p["omni"]),
        daemon=True,
    ).start()
    return {"job_id": job_id, "source_text": src_text, "source_lang": src_lang,
            "translation": translation, "target_lang": tgt,
            "emotion": p["emo_show"], "emotion_error": p["emo_err"],
            **({"speaker": gate["speaker"]} if gate.get("speaker") else {})}


@app.post("/api/modify-speech")
def modify_speech(audio: UploadFile = None, voice_id: str = Form(""),
                  source_lang: str = Form("auto"), emotion_mode: str = Form("off"),
                  instruct: str = Form("")):
    """MODIFICADOR: fala -> transcreve -> TTS na voz escolhida, SEM traduzir (mesma
    língua, mesmas palavras). Igual ao tradutor, mas sem o passo do LLM."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    _jobs_capacity_check()      # antes do STT: recusa cedo, não em segundos
    p = _preparar_fala_para_voz(audio, voice_id, source_lang, emotion_mode, instruct,
                                "Voice design vazio — descreva a voz")
    if "resposta" in p:
        return p["resposta"]
    src_text = p["src_text"]
    out_text = src_text                    # SEM tradução: fala o que foi dito
    lang = p["lang"]

    job_id = _jobs_admit({"text": out_text[:200]})
    threading.Thread(target=_run_tts_job, args=(job_id, out_text, p["vid"], p["vpath"],
                                                lang, p["omni"]), daemon=True).start()
    return {"job_id": job_id, "source_text": src_text, "source_lang": p["src_lang"],
            "translation": out_text, "target_lang": lang,
            "emotion": p["emo_show"], "emotion_error": p["emo_err"]}


# ---------------------------------------------------------------------------
# Live (LIVE-1) — WS /api/live/ws: conversa por áudio bidirecional.
#
# Protocolo (contrato de #93 pipeline e #94 UI):
#   cliente→servidor: JSON `setup` (1º frame; voice_id/system_instruction/vad/
#     history), frames BINÁRIOS PCM16 mono 16 kHz (~100 ms), JSON `end_of_speech`,
#     `cancel`, `ping`.
#   servidor→cliente: JSON `ready`{session_id,audio{format,sr}}, `speech_start`,
#     `transcript_user`{text}, `assistant_text`{delta}, `turn_complete`{ms,
#     audio_bytes}, `interrupted`, `error`{code,message}, `prewarm`{ok},
#     `stats`{telemetria, ~4 Hz; ver #118}, `pong`; áudio = frames
#     BINÁRIOS PCM16 mono 24 kHz (formato anunciado no `ready`).
#   `ready`, `prewarm`, `stats` e `pong` são aditivos ao desenho (handshake,
#     pre-warm, telemetria e keepalive); `turno_pendente` (#126) também é:
#     fala com pipeline ocupado é ACUMULADA num pendente (teto 30 s;
#     `cancel` do cliente limpa, barge-in mantém) em vez de descartada.
# Auth: mesma política das rotas (loopback dispensa, o resto exige chave válida).
# Browser não manda header em WebSocket -> `?key=` é aceito aqui (única exceção
# ao "chave só no header"); header também vale para cliente não-browser.
# ---------------------------------------------------------------------------
# LIVE-OBS-1 (#118): telemetria da sessão. Evento `stats` (~4 Hz) e log de
# METADADOS — NUNCA áudio nem texto transcrito/gerado (invariante de privacidade).
# Contrato do evento no comentário da task #118 e no LIVE.md.
_LIVE_STATS_MS = max(0, int(os.environ.get("TTS_LIVE_STATS_MS", "250")))
_live_log = logging.getLogger("live")
if not _live_log.handlers:                    # auto-suficiente: não depende do uvicorn
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [live] %(message)s", "%H:%M:%S"))
    _live_log.addHandler(_h)
    # propagate ligado: o `caplog` dos testes (handler no root) precisa ver; sob o
    # uvicorn o root não tem handler, então o meu handler acima é quem imprime.
    _live_log.propagate = True
_live_log.setLevel(logging.WARNING if os.environ.get("LIVE_LOG") == "0" else logging.INFO)

_provedor_estado = {"estado": "desconhecido", "http": None, "ts": 0.0}


def _provedor_marca(estado: str, http: int | None = None) -> None:
    """Estado da última chamada ao provedor de chat (do PROCESSO, não da sessão).

    É o que torna visível nas telas o 530/timeout que antes só aparecia como 502."""
    _provedor_estado.update({"estado": estado, "http": http, "ts": time.monotonic()})


def _live_log_kv(evento: str, **campos) -> None:
    """Uma linha de metadados: `evento chave=valor …` (nada de áudio/texto)."""
    _live_log.info("%s %s", evento,
                   " ".join(f"{k}={v}" for k, v in campos.items() if v is not None))


def _live_stage(sess: dict, stage: str) -> None:
    """Estágio do turno (`idle|stt|llm|tts`) com o tempo do estágio anterior."""
    if sess.get("st_stage") == stage:
        return
    ini = sess.get("st_stage_ini")
    if ini is not None:
        sess["st_stage_ms"] = int((time.monotonic() - ini) * 1000)
        _live_log_kv("stage_fim", sess=sess["id"], stage=sess.get("st_stage"),
                     ms=sess["st_stage_ms"])
    else:
        sess["st_stage_ms"] = 0
    sess["st_stage"] = stage
    sess["st_stage_ini"] = time.monotonic() if stage != "idle" else None
    if stage != "idle":
        _live_log_kv("stage_inicio", sess=sess["id"], stage=stage)


def _live_observa(sess: dict, obj) -> None:
    """Espia os eventos que SAEM para derivar telemetria (não altera nada)."""
    if not isinstance(obj, dict):
        return
    tipo = obj.get("type")
    if tipo == "error":
        sess["st_erro"] = {"code": obj.get("code"), "stage": sess.get("st_stage"),
                           "ts": time.monotonic()}
        _live_log_kv("erro", sess=sess["id"], code=obj.get("code"),
                     stage=sess.get("st_stage"))
    elif tipo == "transcript_user":
        _live_stage(sess, "llm")
    elif tipo == "turn_complete":
        if obj.get("descartado"):
            _live_log_kv("turno_descartado", sess=sess["id"], motivo="eco/curto")
        _live_stage(sess, "idle")
    elif tipo == "interrupted":
        _live_log_kv("interrupted", sess=sess["id"], turno=obj.get("turno"))
        _live_stage(sess, "idle")


def _live_marca_prov(sess: dict, prob: float) -> None:
    if prob:
        sess["st_prob"] = round(float(prob), 3)


def _live_estado_motor(sess: dict) -> str:
    """Estado para a TELA (deriva o que a FSM não precisa nomear)."""
    if sess.get("falando"):
        return "falando"
    if sess.get("st_stage") == "stt":
        return "fechando"
    eng = sess.get("engine")
    if eng is not None:
        if getattr(eng, "turno_aberto", False):
            return "ouvindo"
        return getattr(getattr(eng, "estado", None), "value", "ocioso")
    return "ouvindo" if sess.get("turno_aberto") else "ocioso"


def _live_stats_ia(sess: dict) -> dict:
    """Backend de IA efetivo da sessão Live (`stats.ia`) — para o painel OBS.

    `pedido` é o que está configurado; `backend` é o que de FATO responde o turno.
    Com `chat_backend=dsh` e o handshake do harness morrendo, o pipeline marca a
    sessão e passa a usar o openai (#146): sem este campo o painel mostraria "dsh"
    enquanto o texto vinha do endpoint."""
    pedido = _chat_backend_live()
    pipe = sess.get("pipe")
    caiu = bool(getattr(pipe, "_dsh_indisponivel", False))
    return {"pedido": pedido, "backend": "openai" if (caiu or pedido != "dsh") else "dsh",
            "fallback": caiu, "motivo": str(getattr(pipe, "_dsh_motivo", "") or "")[:200]}


def _live_stats(sess: dict) -> dict:
    """Payload do evento `stats` (ver contrato na task #118 / LIVE.md)."""
    agora = time.monotonic()
    ultimo = sess.get("st_ultimo_frame")
    eng = sess.get("engine")
    limiares = {}
    if eng is not None:
        try:
            limiares = {"limiar_dbfs": round(eng.limiar_energia_dbfs, 1),
                        "limiar_turno_dbfs": round(eng.limiar_energia_turno_dbfs, 1)}
            st = eng.estatisticas()
            limiares.update({"barge_ativos": st.get("barge_in", 0),
                             "barge_falsos": st.get("barge_falso", 0)})
        except Exception:                     # noqa: BLE001 — telemetria não derruba
            limiares = {}
    erro = sess.get("st_erro")
    ini = sess.get("st_stage_ini")
    dados = {
        "type": "stats", "t_ms": int((agora - sess["t0"]) * 1000),
        "mic": {"frames": sess.get("st_frames", 0), "bytes": sess.get("st_bytes", 0),
                "desde_ultimo_ms": (int((agora - ultimo) * 1000) if ultimo else None),
                "dbfs": sess.get("st_dbfs"), "prob": sess.get("st_prob")},
        "motor": {"estado": _live_estado_motor(sess), **limiares},
        "turno": {"stage": sess.get("st_stage", "idle"),
                  "ms": (int((agora - ini) * 1000) if ini else 0),
                  "n": sess.get("turno", 0), "buffer_bytes": sess.get("buffer_bytes_turno", 0),
                  "t_decisao_ms": sess.get("t_decisao_ms", 0),
                  "pendente": bool(sess.get("turno_pendente")),
                  "pendentes_trechos": sess.get("pendentes_trechos", 0),
                  "pendentes_descartados_ms": sess.get("pendentes_descartados_ms", 0),
                  "pendentes_descartados": sess.get("pendentes_descartados", 0)},
        "playback": {"speaking": bool(sess.get("falando")),
                     "chunks": sess.get("st_chunks", 0), "bytes": sess.get("st_audio_bytes", 0)},
        "sessao": {"idade_s": int(time.time() - sess["criada"]),
                   "ocioso_ms": int((agora - sess["visto"]) * 1000),
                   "ttl_s": _LIVE_TTL_S, "criadas": len(_live_sessions),
                   "historicos": len(_live_historico)},
        # #146: o painel OBS não pode mentir sobre quem respondeu. `pedido` é o
        # backend escolhido (settings/env); `backend` é o EFEITO real — com o dsh
        # caído no handshake a sessão segue no openai e `fallback` fica true.
        "ia": _live_stats_ia(sess),
    }
    if erro:
        dados["erro"] = {"code": erro.get("code"), "stage": erro.get("stage"),
                         "idade_ms": int((agora - erro["ts"]) * 1000)}
    if _provedor_estado["ts"]:
        dados["provedor"] = {"estado": _provedor_estado["estado"],
                             "http": _provedor_estado["http"],
                             "idade_ms": int((agora - _provedor_estado["ts"]) * 1000)}
    return dados


_LIVE_MAX_SESSIONS = max(1, int(os.environ.get("TTS_LIVE_MAX_SESSIONS", "4")))
_LIVE_TTL_S = max(30, int(os.environ.get("TTS_LIVE_TTL_S", "300")))
_LIVE_SETUP_TIMEOUT_S = 10.0
_LIVE_MAX_BUFFER = 2 * 1024 * 1024      # PCM16 16k do turno em curso (~1 min de fala)
# #126: teto do pendente (fala acumulada com o pipeline ocupado). PCM16 mono
# 16 kHz = 32 kB/s; 30 s ~ 960 kB. Estourou, sai o trecho MAIS ANTIGO.
_LIVE_PENDENTE_MAX_S = max(1, int(os.environ.get("TTS_LIVE_PENDENTE_MAX_S", "30")))
_LIVE_PENDENTE_MAX_BYTES = _LIVE_PENDENTE_MAX_S * 32000
_LIVE_MAX_HISTORY = 200
_LIVE_AUDIO_SR = 24000                  # o que o servidor ENVIA (o cliente manda 16k)
_LIVE_TICK_S = 0.2                      # acorda a task de envio p/ checar TTL/fechamento
_live_lock = threading.Lock()
_live_sessions: dict = {}
_live_sweeper_iniciado = False


def _live_erro(codigo: str, mensagem: str) -> dict:
    return {"type": "error", "code": codigo, "message": str(mensagem)[:300]}


# Ticket efêmero (LIVE-1.5): browser não manda header em WebSocket, e a chave em
# query string vaza em access log do uvicorn, log de proxy/túnel e histórico do
# navegador. Então o cliente pede um ticket por HTTP (`POST /api/live/ticket`, auth
# normal no header), troca por `?ticket=` no WS e ele morre no handshake: UM uso,
# 60 s. `?key=` continua aceito (compat) e loopback segue dispensando tudo — em
# loopback o handshake nem chega a consumir o ticket (auth dispensada), então um
# smoke que testa "reuso recusado" precisa conectar pelo IP da LAN, não por 127.0.0.1.
_LIVE_TICKET_TTL_S = 60
_LIVE_TICKET_MAX = 512                  # tickets pendentes (memória limitada)
_live_tickets: dict = {}                # ticket -> expira em (monotonic)


def _live_ticket_limpa(agora: float | None = None) -> None:
    agora = agora if agora is not None else time.monotonic()
    for t, expira in list(_live_tickets.items()):
        if expira <= agora:
            _live_tickets.pop(t, None)


def _live_ticket_emite() -> str:
    with _live_lock:
        _live_ticket_limpa()
        sobra = len(_live_tickets) - _LIVE_TICKET_MAX + 1
        if sobra > 0:                    # os mais antigos saem primeiro
            for t in sorted(_live_tickets, key=_live_tickets.get)[:sobra]:
                _live_tickets.pop(t, None)
        ticket = _secrets.token_urlsafe(24)
        _live_tickets[ticket] = time.monotonic() + _LIVE_TICKET_TTL_S
    return ticket


def _live_ticket_consome(ticket: str) -> bool:
    """UM uso: o ticket some no handshake, mesmo se a sessão cair depois."""
    if not ticket:
        return False
    with _live_lock:
        expira = _live_tickets.pop(ticket, None)
    return expira is not None and expira > time.monotonic()


def _live_autentica(ws) -> bool:
    """MESMA política das rotas: loopback dispensa; o resto precisa de credencial.

    Ordem: `?ticket=` (efêmero, de um uso — o caminho para o browser) → `?key=`
    (compat) → header (cliente não-browser, ex.: script/RN)."""
    if _is_local(ws):
        return True
    if not _auth_enabled():
        return True
    ticket = (ws.query_params.get("ticket") or "").strip()
    if ticket:
        return _live_ticket_consome(ticket)
    chave = (ws.query_params.get("key") or "").strip() or _extract_request_key(ws)
    return _key_is_valid(chave)


@app.post("/api/live/ticket")
def live_ticket():
    """Emite um ticket de um uso para o WS (60 s). Auth normal (header).

    O cliente chama isto, conecta em `/api/live/ws?ticket=<ticket>` e a chave de
    API deixa de aparecer em log/histórico. Loopback pode pular o ticket."""
    return {"ticket": _live_ticket_emite(), "expires_in": _LIVE_TICKET_TTL_S}


def _live_clamp_ms(valor, nome: str, lo: int, hi: int, default: int) -> int:
    if valor is None:
        return default
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise ValueError(f"vad.{nome} precisa ser inteiro (ms)") from None
    return int(min(hi, max(lo, n)))


def _live_valida_setup(msg: dict) -> dict:
    """Valida o `setup` com o rigor dos endpoints HTTP (#23/#33): nada entra por
    confiança, o erro diz o CAMPO e voz desconhecida cai no `_resolve_voice`."""
    if not isinstance(msg, dict):
        raise ValueError("setup precisa ser um objeto JSON")
    voz_pedida = str(msg.get("voice_id") or "").strip()
    voz = voz_pedida
    if not voz:
        voz = _resolve_voice(None)
    elif voz not in (DESIGN_VOICE_ID,) and voz not in OMNI_PRESETS and not _voice_path(voz):
        voz = _resolve_voice(voz)
    sid = str(msg.get("session_id") or "").strip()
    if sid and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
        raise ValueError("session_id inválido (letras, números, _ e -)")
    system = str(msg.get("system_instruction") or "").strip()[:4000]
    vad = msg.get("vad") or {}
    if not isinstance(vad, dict):
        raise ValueError("vad precisa ser um objeto JSON")
    hist = msg.get("history") or []
    if not isinstance(hist, list):
        raise ValueError("history precisa ser uma lista")
    history = []
    for i, h in enumerate(hist[:_LIVE_MAX_HISTORY]):
        if not isinstance(h, dict):
            raise ValueError(f"history[{i}] precisa ser um objeto")
        papel = str(h.get("role") or "").strip().lower()
        if papel not in ("user", "assistant"):
            raise ValueError(f"history[{i}].role precisa ser 'user' ou 'assistant'")
        texto = str(h.get("text") or "").strip()[:4000]
        if texto:
            history.append({"role": papel, "text": texto})
    return {
        "voice_id": voz,
        "voice_id_pedido": voz_pedida,
        "session_id": sid,
        "system": system,
        "vad": {"silence_ms": _live_clamp_ms(vad.get("silence_ms"), "silence_ms", 100, 5000, 600),
                "prefix_ms": _live_clamp_ms(vad.get("prefix_ms"), "prefix_ms", 0, 2000, 100)},
        "history": history,
    }


def _live_sweep() -> list:
    """Fecha sessões ociosas (TTL) e varre o histórico de retomada. A marcação é vista pela task de envio, que é
    quem pode fechar o WS de forma assíncrona."""
    _live_hist_varre()
    agora = time.monotonic()
    with _live_lock:
        vencidas = [s for s, d in _live_sessions.items()
                    if agora - d["visto"] > _LIVE_TTL_S]
        for s in vencidas:
            _live_sessions[s]["vencida"] = True
    return vencidas


def _live_sweeper():
    while True:
        time.sleep(15)
        try:
            _live_sweep()
        except Exception:  # noqa: BLE001 — thread de fundo nunca pode morrer
            pass


def _live_enriquece(sess: dict, obj):
    """Estampa o ESTADO da sessão nos eventos que saem.

    `truncated` no `turn_complete`: o áudio daquele turno bateu no teto do buffer
    e o começo foi mantido (o resto é descartado). O `buffer_bytes` em si é do
    pipeline; até ele emitir, o estado da sessão vai por aqui — e o aviso é
    zerado depois de reportado, para valer por turno.

    `turn_complete`/`interrupted`/`error` também ACORDAM o observador do turno
    pendente (#126): o fechamento — mesmo em erro — é a janela em que a fala
    enfileirada pode abrir, e ela NÃO herda o erro do turno anterior."""
    if isinstance(obj, dict) and obj.get("type") in ("turn_complete", "interrupted",
                                                     "error"):
        if obj.get("type") == "turn_complete":
            if obj.get("descartado"):         # decisão do pipeline (eco x humano)
                sess["descartados"] = sess.get("descartados", 0) + 1
            obj.setdefault("truncated", bool(sess.get("truncado")))
            sess["truncado"] = False          # aviso vale por turno
            sess["buffer"] = bytearray()      # turno fechado: o áudio já foi consumido
            sess["buffer_consumido"] = 0      # (e o marcador volta a zero com ele)
            try:                              # histórico fora da conexão + compressão
                _live_hist_pos_turno(sess)
            except Exception:                 # noqa: BLE001 — nunca derruba o envio
                pass
        if sess.get("turno_pendente"):    # #126: turno fechou (ou ERROU) — o
            _live_acorda_pendente(sess)   # pendente segue e abre quando liberar
    return obj


def _live_janela_turno(sess: dict, aberto: bool) -> None:
    """#167: liga/desliga o TURNO do assistente no motor de turnos.

    A janela de playback do motor (`live_turns`) era dimensionada pela FILA de
    chunks: com um chunk por vez ela fechava `playback_janela_ms` (900 ms) depois do
    último ENVIO, e o onset do humano num VÃO de geração (LLM/TTS; medido até 20 s)
    deixava de ser lido como interrupção — virava turno novo. Aqui a marca segue o
    TURNO: abre no 1º áudio e fecha no evento terminal
    (`turn_complete`/`interrupted`). `set_turno_aberto` é additive no motor: sem
    este call site nada muda (e `TTS_LIVE_BARGE_JANELA_TURNO=0` desliga para A/B)."""
    eng = sess.get("engine")
    fn = getattr(eng, "set_turno_aberto", None)
    if fn is None:
        return
    try:
        fn(bool(aberto))
    except Exception:                    # noqa: BLE001 — motor não pode derrubar o WS
        pass


def _live_envia_json(sess: dict, obj: dict) -> None:
    """Ponto de acoplamento do pipeline (#93): pode ser chamado de QUALQUER thread."""
    if obj.get("type") in ("turn_complete", "interrupted"):
        _live_janela_turno(sess, False)  # #167: turno terminou, fecha a janela
    _live_observa(sess, obj)
    sess["fila"].put(("json", _live_enriquece(sess, obj)))


def _live_envia_audio(sess: dict, pcm: bytes) -> None:
    if pcm:
        _live_janela_turno(sess, True)   # #167: turno do assistente com áudio em voo
        sess["audio_pendente"] = sess.get("audio_pendente", 0) + 1
        sess["fila"].put(("audio", pcm))


def _live_buffer(sess: dict) -> bytes:
    """Áudio do turno em curso (PCM16 16k). Trunca pelo INÍCIO acima do teto."""
    return bytes(sess["buffer"])


try:                                     # motor de turnos do LIVE-3 (#93)
    import live_pipeline as _live_mod
except Exception:                        # noqa: BLE001 — sem o módulo, o stub segura
    _live_mod = None

try:                                     # FSM de turnos do LIVE-2 (#92)
    import live_turns as _live_turns_mod
except Exception:                        # noqa: BLE001 — sem ela, só o PCM cru
    _live_turns_mod = None


def _live_engine_novo(sess: dict):
    """FSM de turnos da sessão (None = sem o módulo: comportamento antigo).

    `prefix_ms`/`silence_ms` vêm do `setup` do cliente (validados lá). A fala do
    TTS entra por `set_speaking()` — é dela que sai o limiar adaptativo do eco."""
    if _live_turns_mod is None:
        return None
    try:
        cfg = _live_turns_mod.Config(
            prefix_ms=sess["vad"]["prefix_ms"],
            silence_ms=sess["vad"]["silence_ms"],
            # #167: janela de barge colada ao turno (desligável p/ A/B no harness)
            barge_janela_turno=os.environ.get(
                "TTS_LIVE_BARGE_JANELA_TURNO", "0") != "0",
            # #167: janela pela duração real do chunk (ver Config.playback_por_duracao)
            playback_por_duracao=os.environ.get(
                "TTS_LIVE_PLAYBACK_DURACAO", "0") != "0")
    except Exception:                    # noqa: BLE001 — config inválida: usa o padrão
        cfg = None
    return _live_turns_mod.TurnEngine(config=cfg)


def _live_cancela(sess: dict, do_cliente: bool = False) -> None:
    """Derruba o turno em curso (barge-in ou `cancel` do cliente).

    `do_cliente` (comando `cancel`) limpa TAMBÉM o pendente: o usuário abortou
    tudo. No barge-in o pendente SEGUE — a fala que interrompeu abre turno
    próprio (ou já está nele) e é respondida depois."""
    sess["cancelado"].set()
    sess["turno"] += 1
    if do_cliente and sess.pop("turno_pendente", None) is not None:
        sess["turno_pendente_barge"] = False
        sess["pendentes_descartados"] = sess.get("pendentes_descartados", 0) + 1
        _live_pendente_zera_contadores(sess)
        _live_log_kv("pend_cancelado", sess=sess["id"])
    _live_log_kv("cancel", sess=sess["id"], turno=sess["turno"])
    pipe = sess.get("pipe")
    if pipe is not None:
        pipe.cancel()
        if not pipe.ocupado:             # com turno rodando, quem avisa é o pipeline
            _live_envia_json(sess, {"type": "interrupted", "turn": sess["turno"]})
    else:
        _live_envia_json(sess, {"type": "interrupted", "turn": sess["turno"]})


_DEBUG_LIVE = os.environ.get("LIVE_DEBUG_TTS") == "1"


def _live_abre_turno(sess: dict, pcm: bytes = b"", barge_in: bool = False) -> None:
    """Abre o turno com o áudio QUE VEIO NO EVENTO (pré-roll já incluído).

    Sem evento (caminho do comando `end_of_speech`), o áudio é o que a sessão
    acumulou — o tamanho fica registrado para o turno e o buffer é zerado quando o
    turno fecha.

    Com turno JÁ aberto (o cliente manda `end_of_speech` logo depois do
    `speech_end` automático do motor), o áudio não é empilhado de novo: o motor
    já entregou esse mesmo trecho no evento. Sem esta guarda, os bytes ficavam no
    buffer do pipeline e envenenavam o turno SEGUINTE (medido: turno 1 com 16 kB
    mudos + 1 kB, e o turno 2 nascendo com o áudio do anterior e truncado).

    O snapshot do buffer só vale o que AINDA não tem dono (`buffer_consumido`:
    bytes já entregues ao turno em curso ou a um pendente) — sem isso, a fala
    enfileirada durante um turno sairia com o áudio do turno ANTES repetido."""
    if not pcm and sess.get("buffer"):
        pcm = bytes(sess["buffer"][sess.get("buffer_consumido", 0):])
        # marcador: estes bytes já têm dono (turno aberto ou pendente)
        sess["buffer_consumido"] = len(sess["buffer"])
    sess["buffer_bytes_turno"] = len(pcm)
    # flag do evento: o pipeline usa para separar ECO de humano (decisão do PM);
    # vai na sessão E como kwarg quando o pipeline aceitar (transição).
    sess["turno_barge_in"] = bool(barge_in)
    pipe = sess.get("pipe")
    if pipe is None:                     # sem pipeline: stub mínimo
        sess["fila"].put(("json", {"type": "turn_complete", "stub": True,
                                   "buffer_bytes": len(pcm)}))
        return
    if _DEBUG_LIVE:
        print(f"[live][dbg] abre_turno pcm={len(pcm)} ocupado={pipe.ocupado} "
              f"turno={sess['turno']}", flush=True)
    if pipe.ocupado:
        if pcm:
            # #126: fala que chega com turno em curso (resposta longa pode levar
            # dezenas de s) não é mais descartada — vira 1 turno pendente e abre
            # quando o pipeline liberar. O dono lia o descarte como "parou de
            # captar". Sem áudio não há o que enfileirar: mantém o aviso antigo.
            _live_guarda_pendente(sess, pcm, barge_in)
        else:
            _live_envia_json(sess, _live_erro("turno_em_curso", "espere o turn_complete"))
        return
    # FALHA ALTO e a sessão sobrevive (padrão do `prewarm`): se o pipeline mudar de
    # assinatura, o cliente VÊ o erro em vez de ficar com turno mudo — o modo
    # fantasma que a muleta antiga (`except TypeError` seguindo sem o flag) escondia.
    try:
        if pcm:
            pipe.push_pcm(pcm, substituir=True)   # é o turno INTEIRO, não um pedaço
        if not pipe.end_of_speech(barge=bool(barge_in)):
            # perdeu a corrida (outro turno abriu entre a checagem e aqui):
            # a fala vira pendente em vez de sumir (mesma política do ocupado)
            if pcm:
                _live_guarda_pendente(sess, pcm, barge_in)
            else:
                _live_envia_json(sess, _live_erro("turno_em_curso", "espere o turn_complete"))
    except Exception as exc:                  # noqa: BLE001 — erro do pipeline, não da sessão
        # turno MORTO: sem `turn_complete`/`interrupted` vindos do pipeline, a janela
        # intra-turno (#167) ficaria aberta até a trava de segurança do motor.
        # Fechar aqui é idempotente (o `set_turno_aberto` do motor aceita repetição).
        _live_janela_turno(sess, False)
        _live_envia_json(sess, _live_erro("pipeline", f"{type(exc).__name__}: {exc}"))


def _live_pendente_zera_contadores(sess: dict) -> None:
    """Zera os contadores do evento `turno_pendente` (`trechos`, `descartados_ms`).

    Eles descrevem o pendente ATUAL: consumido (ou descartado pelo `cancel`), o
    próximo `turno_pendente` começa a contar do zero em vez de herdar o acúmulo
    do anterior."""
    sess["pendentes_trechos"] = 0
    sess["pendentes_descartados_ms"] = 0


def _live_guarda_pendente(sess: dict, pcm: bytes, barge_in: bool) -> None:
    """Acumula o trecho no ÚNICO pendente, para abrir quando o pipeline liberar.

    CONCATENA os pedaços (#126, ajuste do PM): a pessoa costuma completar a
    frase em dois. Teto de duração (`_LIVE_PENDENTE_MAX_S`): estourou, sai o
    MAIS ANTIGO — o fim é o que completa a frase — e o evento avisa `truncado`
    COM QUANTO saiu (`descartados_ms`): o booleano `substituido` não dizia se o
    corte foi de 20 ms ou de 20 s, e o cliente só podia adivinhar.
    O flag de barge-in é pegajoso: qualquer pedaço nascido no playback mantém a
    checagem de eco no turno pendente (transcript que casa com a própria fala
    não vira resposta)."""
    if not pcm:
        return
    novo = (sess.get("turno_pendente") or b"") + pcm
    truncado = len(novo) > _LIVE_PENDENTE_MAX_BYTES
    if truncado:
        cortados = len(novo) - _LIVE_PENDENTE_MAX_BYTES
        novo = novo[-_LIVE_PENDENTE_MAX_BYTES:]
        sess["pendentes_descartados"] = sess.get("pendentes_descartados", 0) + 1
        # 32 bytes = 1 ms (PCM16 mono 16 kHz) — mesma base do teto
        sess["pendentes_descartados_ms"] = (sess.get("pendentes_descartados_ms", 0)
                                            + cortados // 32)
    sess["pendentes_trechos"] = sess.get("pendentes_trechos", 0) + 1  # trechos ACUMULADOS
    sess["turno_pendente"] = novo
    if barge_in:
        sess["turno_pendente_barge"] = True       # pegajoso: eco-check segue valendo
    _live_log_kv("turno_pendente", sess=sess["id"], bytes=len(novo),
                 trechos=sess["pendentes_trechos"], truncado=truncado,
                 descartados_ms=sess.get("pendentes_descartados_ms", 0),
                 barge=bool(sess.get("turno_pendente_barge")))
    # protocolo aditivo: "anotei, respondo já" — o turno em si vem na sequência
    # (speech_start/turn_complete quando o pipeline liberar)
    _live_envia_json(sess, {"type": "turno_pendente", "buffer_bytes": len(novo),
                            "trechos": sess["pendentes_trechos"],
                            "descartados_ms": sess.get("pendentes_descartados_ms", 0),
                            "barge_in": bool(sess.get("turno_pendente_barge")),
                            "truncado": truncado})
    _live_acorda_pendente(sess)


def _live_acorda_pendente(sess: dict) -> None:
    """Garante UM observador por sessão esperando o pipeline liberar.

    Chamado quando o pendente é guardado, quando um turno fecha (o turno em
    curso pode ter acabado) e pelo PRÓPRIO observador moribundo (re-arm no fim
    de `_live_descarrega_pendente`). Nesse último caso a thread ainda consta
    viva — é a current_thread —, então a checagem de `is_alive` não pode bloquear
    quem está saindo de armar o sucessor (gate #126/#127, achado 2: sem isso, a
    fala guardada na janela de morte ficava órfã até o próximo fechamento de
    turno — ou para sempre, com o pipeline livre)."""
    t = sess.get("pend_thread")
    if (t is not None and t is not threading.current_thread()
            and t.is_alive()):
        return
    t = threading.Thread(target=_live_descarrega_pendente, args=(sess,), daemon=True)
    sess["pend_thread"] = t
    t.start()


def _live_descarrega_pendente(sess: dict) -> None:
    """Espera o pipeline liberar e abre o turno pendente (1 no máx.).

    O `pop` decide o vencedor: com vários observadores acordados pelo mesmo
    fechamento, só o primeiro processa. Se o pipeline voltou a ficar ocupado
    (outra fala abriu turno no intervalo), o pendente VOLTA — o fechamento
    desse turno acorda um observador de novo. Daemon: morre com a sessão."""
    pipe = sess.get("pipe")
    if pipe is None:
        return
    try:
        while not sess.get("fechar") and pipe.ocupado:
            time.sleep(0.1)
        pendente = sess.pop("turno_pendente", None)
        barge = bool(sess.pop("turno_pendente_barge", False))
        if not pendente or sess.get("fechar"):
            _live_pendente_zera_contadores(sess)
            return
        # contadores do evento descrevem o pendente ATUAL: preservados no re-arm
        # abaixo (o conteúdo VOLTA) e zerados no fim quando ele é aberto
        trechos = sess.get("pendentes_trechos", 0)
        descartados_ms = sess.get("pendentes_descartados_ms", 0)
        if pipe.ocupado:                  # fechou e abriu outro no intervalo
            sess["turno_pendente"] = pendente
            sess["turno_pendente_barge"] = barge
            sess["pendentes_trechos"] = trechos
            sess["pendentes_descartados_ms"] = descartados_ms
            return
        _live_pendente_zera_contadores(sess)
        _live_abre_turno(sess, pendente, barge_in=barge)
    finally:
        # Fala guardada NA janela de morte deste observador (a guarda viu a
        # thread viva e não armou outra): sem o re-arm ela ficava órfã até o
        # próximo fechamento de turno — ou para sempre, com o pipeline livre
        # (gate #126/#127, achado 2). O `fechar` fora: encerrar a sessão não
        # pode virar nascedouro de observadores.
        if sess.get("turno_pendente") and not sess.get("fechar"):
            _live_acorda_pendente(sess)


def _live_trata_eventos(sess: dict, eventos) -> None:
    """Traduz a FSM (speech_start | speech_end | barge_in) para o protocolo.

    `barge_in` e o `speech_start{barge_in:true}` vêm no MESMO lote: o primeiro
    derruba o turno em curso, o segundo é a única abertura de turno.

    LATÊNCIA: `t_ms` é RETROAGIDO ao início do áudio (é posição no stream); a
    latência real da decisão é `t_decisao_ms`, que fica registrado na sessão — é
    dele que sai o orçamento do LIVE-3 (fim-de-fala → 1º áudio), não do `t_ms.
    DESCARTE: quem decide agora é o PIPELINE (compara o transcript com o texto do
    assistente em reprodução: casar = eco → `turn_complete{descartado:true}`;
    diferir = humano → segue). `curto`/`barge_falso` são dica e não barram mais o
    STT — a bancada mostrou fala REAL de 384 ms marcada como curta e eco puro de
    1280 ms passando como turno fantasma."""
    for ev in eventos:
        try:
            dados = ev.to_json()
        except Exception:                # noqa: BLE001 — evento de outro tipo
            continue
        _live_envia_json(sess, dados)
        _live_marca_prov(sess, getattr(ev, "prob", 0.0))
        if ev.tipo == "barge_in":
            _live_log_kv("barge_in", sess=sess["id"], t_ms=ev.t_ms)
            _live_cancela(sess)
        elif ev.tipo == "speech_start":
            _live_log_kv("speech_start", sess=sess["id"], t_ms=ev.t_ms,
                         prob=getattr(ev, "prob", None))
        elif ev.tipo == "speech_end":
            _live_log_kv("speech_end", sess=sess["id"], t_ms=ev.t_ms,
                         fala_ms=getattr(ev, "fala_ms", None),
                         curto=getattr(ev, "curto", None),
                         barge_falso=getattr(ev, "barge_falso", None),
                         detalhe=getattr(ev, "detalhe", None))
            _live_stage(sess, "stt")
            sess["t_decisao_ms"] = int(getattr(ev, "t_decisao_ms", 0) or 0)
            sess["turno_curto"] = bool(getattr(ev, "curto", False))
            sess["turno_barge_falso"] = bool(getattr(ev, "barge_falso", False))
            _live_abre_turno(sess, bytes(getattr(ev, "audio", b"") or b""),
                             barge_in=bool(getattr(ev, "barge_in", False)))


def _dbfs16(pcm: bytes) -> float:
    """dBFS de um chunk PCM16 — o detector de eco calibra o nível pelo playback."""
    import numpy as np
    if len(pcm) < 2:
        return -120.0
    a = np.frombuffer(pcm[:len(pcm) - (len(pcm) % 2)], dtype="<i2").astype("float32")
    if a.size == 0:
        return -120.0
    rms = float(np.sqrt(float((a * a).mean())))
    return 20.0 * float(np.log10(max(rms, 1e-6) / 32768.0))


# Taxa do áudio do TTS NO FIO (o pipeline publica em `model.sample_rate`, 24 kHz
# para o catálogo atual). O sender só tem os BYTES do chunk — daí a constante aqui:
# é dela que sai a duração real do chunk que dimensiona a janela de playback (#167).
_LIVE_TTS_RATE = 24000


def _live_chunk_ms(pcm: bytes) -> float:
    """Duração REAL do chunk PCM16 (ms) — o backlog de playback do motor (#167).

    O cliente BUFFERIZA: entre o envio e o alto-falante há a fila inteira, então a
    janela de barge tem de cobrir o áudio já mandado. Duração por bytes, sem
    decodificar — o motor só precisa da estimativa de tempo."""
    return len(pcm) / 2 / (_LIVE_TTS_RATE / 1000.0)


def _live_pipe_novo(sess: dict):
    """Pipeline da sessão (None quando o módulo não está disponível -> stub).

    Os callbacks do pipeline caem na MESMA fila da sessão, então a única task que
    escreve no WS continua sendo a de envio (nada de send concorrente).

    DSH-2: com `chat_backend=dsh` a sessão ganha um `DshClient` PRÓPRIO (não o pool
    da Conversa): o `cancel` do barge-in casa por sessão e não vaza entre clientes,
    e o processo é fechado junto com a sessão. Cliente por sessão + `prewarm` no
    `_live_pipe_start` = boot de 17-18 s a frio FORA do turno. Construir o cliente
    é barato (não sobe processo aqui); se algo falhar, a sessão segue no backend
    openai em vez de nascer muda."""
    if _live_mod is None:
        return None
    historico = [{"role": h["role"], "content": h["text"]} for h in sess["history"]]
    dsh = None
    if _chat_backend_live() == "dsh":
        try:
            cfg = _chat_dsh_cfg()
            dsh = dsh_client.DshClient(
                bin=cfg["bin"], profile=cfg["profile"], model=cfg["model"],
                effort=cfg["effort"], cwd=BASE / "outputs" / ".dsh-cwd",
                on_log=lambda m: _dsh_log(m, "live-dsh"))
        except Exception as exc:                 # noqa: BLE001 — cai no openai
            print(f"[live] dsh indisponível ({type(exc).__name__}: {exc}) — "
                  f"sessão no backend openai", flush=True)
    sess["dsh"] = dsh
    return _live_mod.LivePipeline(
        lambda obj: _live_envia_json(sess, obj),
        lambda pcm: _live_envia_audio(sess, pcm),
        voice_id=sess["voice_id"], system=sess["system"] or None, history=historico,
        dsh=dsh)


def _live_pipe_start(sess: dict) -> None:
    """Pre-warm (whisper + TTS frios custam ~5-7 s): fora do caminho do handshake.

    Avisa quando termina (`prewarm`): o 1º turno ANTES disso paga a compilação dos
    kernels dos modelos (medido: 7,3 s de 1º áudio contra 0,7 s depois) — quem
    mede o alvo do MVP precisa saber quando o pipeline está quente."""
    pipe = sess.get("pipe")
    if pipe is None:
        return
    try:
        pipe.start()
        _live_envia_json(sess, {"type": "prewarm", "ok": True})
    except Exception as exc:             # noqa: BLE001 — pre-warm falho não derruba a sessão
        _live_envia_json(sess, {"type": "prewarm", "ok": False,
                                "message": f"{type(exc).__name__}: {exc}"})
        _live_envia_json(sess, _live_erro("prewarm", f"{type(exc).__name__}: {exc}"))


def _live_engine(sess: dict) -> None:
    """STUB do pipeline (o #93 substitui esta função).

    Emite o turno mínimo para o protocolo ser exercitável já: `speech_start` →
    `turn_complete`. O áudio que o #93 gerar sai por `_live_envia_audio`."""
    turno = sess["turno"]
    _live_envia_json(sess, {"type": "speech_start", "turn": turno})
    if sess["cancelado"].is_set():
        return
    _live_envia_json(sess, {"type": "turn_complete", "turn": turno, "stub": True,
                            "audio_bytes": 0, "buffer_bytes": len(sess["buffer"])})


async def _live_sender(sess: dict, ws) -> None:
    """Única task que escreve no WS (evita corrida de send entre turno e eventos)."""
    fila = sess["fila"]
    while True:
        try:
            item = await asyncio.to_thread(fila.get, True, _LIVE_TICK_S)
        except queue.Empty:
            item = None
        except asyncio.CancelledError:
            raise
        if item is None and _LIVE_STATS_MS and sess["visto"]:
            agora = time.monotonic()
            if agora >= sess.get("st_proximo", 0.0):
                sess["st_proximo"] = agora + _LIVE_STATS_MS / 1000.0
                _live_envia_json(sess, _live_stats(sess))
        if item is not None:
            tipo, payload = item
            try:
                if tipo == "json":
                    await ws.send_json(payload)
                else:
                    await ws.send_bytes(payload)
            except Exception:  # noqa: BLE001 — cliente caiu: a limpeza é do endpoint
                return
            if tipo == "audio":
                sess["st_chunks"] += 1
                sess["st_audio_bytes"] += len(payload)
                if sess.get("st_stage") in ("stt", "llm"):   # 1º áudio do turno
                    _live_stage(sess, "tts")
                # V1: o playback é marcado NO ENVIO (nivel do chunk mede o eco);
                # `#94` pode trazer feedback real de playback se o falso barge-in pedir.
                eng = sess.get("engine")
                if eng is not None:
                    if not sess.get("falando"):
                        sess["falando"] = True
                    # #167: o cliente BUFFERIZA e ainda vai tocar este chunk — a
                    # janela de barge tem de cobrir a duração REAL dele. A chamada
                    # vai a CADA chunk (não só no 1º do turno): o backlog do motor
                    # é a SOMA do que foi enviado e não tocou, e alimentá-lo só uma
                    # vez deixava a janela pós-turno com um chunk fixo — onset logo
                    # depois do `turn_complete`, com o cliente ainda tocando o
                    # ÚLTIMO trecho (que é longo), virava turno novo (resíduo
                    # medido). Repetir o `True` é inócuo: a recalibração do eco só
                    # dispara na borda (`não speaking` -> `speaking`).
                    eng.set_speaking(True, nivel_dbfs=_dbfs16(payload),
                                     duracao_ms=_live_chunk_ms(payload))
                    sess["audio_pendente"] = max(0, sess.get("audio_pendente", 1) - 1)
                    if sess["audio_pendente"] == 0:
                        sess["falando"] = False
                        eng.set_speaking(False)
        if sess["vencida"] or sess.get("fechar"):
            try:
                await ws.send_json({"type": "error", "code": "session_ttl",
                                    "message": f"sessão ociosa por {_LIVE_TTL_S}s"})
                await ws.close(code=1000)
            except Exception:  # noqa: BLE001
                pass
            return


# ---------------------------------------------------------------------------
# LIVE-5 (#95) — sessão longa: histórico fora da conexão (resume) + compressão de
# contexto. Desenho no comentário da task; aqui o essencial:
#   · `_live_historico[sid]` guarda msgs/voz/system de uma sessão que caiu, com TTL
#     de retomada e tetos (por registro e total, evicção previsível — lição do #24);
#   · `setup.session_id` retoma (o cliente reconecta e continua o contexto);
#   · acima do limiar, a METADE MAIS ANTIGA vira UM resumo (LLM local, em thread,
#     fora do caminho da latência); o system_instruction fica separado e sempre no
#     prompt. Falha do resumo mantém o histórico cru.
# ---------------------------------------------------------------------------
_LIVE_RESUME_TTL_S = max(60, int(os.environ.get("TTS_LIVE_RESUME_TTL_S", "1800")))
_LIVE_HIST_MAX_MSGS = max(4, int(os.environ.get("TTS_LIVE_HIST_MAX_MSGS", "24")))
_LIVE_HIST_MAX_CHARS = max(1000, int(os.environ.get("TTS_LIVE_HIST_MAX_CHARS", "12000")))
_LIVE_MAX_HISTORICOS = max(1, int(os.environ.get("TTS_LIVE_MAX_HISTORICOS", "32")))
_LIVE_RESUME_MAX_BYTES = 4 * 1024 * 1024
_live_historico: dict = {}          # sid -> {msgs, resumo, voz, system, visto, bytes}
_live_resume_fn = None              # callable(msgs) -> str; default = _chat_llm


def _live_hist_tam(reg: dict) -> int:
    return sum(len(m.get("content") or m.get("text") or "") for m in reg.get("msgs") or [])


def _live_hist_varre(agora: float | None = None) -> list:
    """TTL de retomada + tetos. `visto` só é tocado em uso/conexão — a sessão que
    caiu fica retomável; quem nunca volta expira. Evicção pelo mais antigo."""
    agora = agora if agora is not None else time.monotonic()
    with _live_lock:
        for sid, reg in list(_live_historico.items()):
            if agora - reg.get("visto", 0) > _LIVE_RESUME_TTL_S:
                _live_historico.pop(sid, None)
        while len(_live_historico) > _LIVE_MAX_HISTORICOS:
            mais_antigo = min(_live_historico, key=lambda s: _live_historico[s].get("visto", 0))
            _live_historico.pop(mais_antigo, None)
        while sum(_live_hist_tam(r) for r in _live_historico.values()) > _LIVE_RESUME_MAX_BYTES:
            mais_antigo = min(_live_historico, key=lambda s: _live_historico[s].get("visto", 0))
            _live_historico.pop(mais_antigo, None)
    return list(_live_historico)


def _live_hist_guarda(sess: dict) -> None:
    """Grava o contexto da sessão no registro de retomada (fim de turno/desconexão)."""
    pipe = sess.get("pipe")
    msgs = []
    if pipe is not None and getattr(pipe, "history", None):
        msgs = [{"role": m.get("role"), "content": m.get("content", "")}
                for m in pipe.history if m.get("content")]
    elif sess.get("history"):
        msgs = [{"role": m["role"], "content": m["text"]} for m in sess["history"]]
    if not msgs:
        return
    with _live_lock:
        reg = _live_historico.setdefault(sess["id"], {})
        # geração: o registro é do `sid`, mas quem manda nele é a sessão MAIS NOVA.
        # Sem isto, a sessão antiga (socket zumbi) que morre depois regrava o
        # contexto velho por cima do da sessão que a retomou.
        if reg.get("geracao", 0) > sess.get("geracao", 0):
            return
        reg.update({"msgs": msgs[-_LIVE_HIST_MAX_MSGS:], "visto": time.monotonic(),
                    "geracao": sess.get("geracao", 0),
                    "voz": sess["voice_id"], "system": sess["system"]})
        reg["resumo"] = sess.get("resumo") or reg.get("resumo") or ""
    _live_hist_varre()


def _live_hist_pega(sid: str) -> dict | None:
    """Registro retomável (None = desconhecido/expirado)."""
    if not sid:
        return None
    _live_hist_varre()
    with _live_lock:
        reg = _live_historico.get(sid)
        if reg is None:
            return None
        reg["visto"] = time.monotonic()
        return dict(reg)


def _live_resumidor(sess: dict):
    """Resumidor da compressão do LIVE — o backend EFETIVO da sessão, não o da Conversa.

    POR QUE: `_chat_llm` é o caminho da CONVERSA. Com Live=dsh e Conversa no endpoint
    (a combinação que o #175 recomenda) o resumo saía pelo provedor REMOTO — egress e
    segundos — e, sem endpoint configurado, falhava 400 e o Live NUNCA comprimia (o
    contexto só crescia até reabrir a sessão ACP). Segue o backend EFETIVO
    (`_live_stats_ia`, que já considera o fallback do #146) e, no dsh, usa o POOL: o
    resumo roda em thread no fim do turno e o cliente DA SESSÃO já pode estar no
    próximo prompt (-32602, lição do #162/#170)."""
    if _live_resume_fn is not None:
        return _live_resume_fn
    return _chat_llm_dsh if _live_stats_ia(sess)["backend"] == "dsh" else _chat_llm


def _live_hist_comprime(sess: dict) -> bool:
    """Resume a metade mais antiga (assíncrono, no fim do turno). True = comprimiu."""
    pipe = sess.get("pipe")
    if pipe is None:
        return False
    msgs = list(getattr(pipe, "history", []) or [])
    if len(msgs) <= _LIVE_HIST_MAX_MSGS and \
            sum(len(m.get("content") or "") for m in msgs) <= _LIVE_HIST_MAX_CHARS:
        return False
    meio = max(1, len(msgs) // 2)
    antigas, recentes = msgs[:meio], msgs[meio:]
    try:
        resumidor = _live_resumidor(sess)
        resumo = str(resumidor([
            {"role": "system", "content":
             "Resuma a conversa abaixo em 3-5 linhas, PRESERVANDO nomes, números e "
             "decisões combinadas. Responda só o resumo."},
            *[{"role": m.get("role", "user"), "content": m.get("content", "")} for m in antigas],
        ]) or "").strip()
    except Exception as exc:             # noqa: BLE001 — resumo é otimização
        sess["resumo_erro"] = f"{type(exc).__name__}: {exc}"
        return False
    if not resumo:
        return False
    novo = [{"role": "system", "content": f"Resumo do que já foi dito: {resumo}"}] + recentes
    try:
        pipe.history[:] = novo           # o pipeline usa a MESMA lista
    except Exception:                    # noqa: BLE001 — instância trocada: recria
        pipe.history = novo
    sess["resumo"] = resumo
    return True


def _live_hist_pos_turno(sess: dict) -> None:
    """Fim de turno: guarda o contexto e, se passou do limiar, comprime em thread."""
    _live_hist_guarda(sess)
    if not _live_hist_ja_comprimindo(sess):
        thr = threading.Thread(target=_live_hist_comprime, args=(sess,), daemon=True)
        sess["compr_thread"] = thr
        thr.start()


def _live_hist_ja_comprimindo(sess: dict) -> bool:
    thr = sess.get("compr_thread")
    return bool(thr and thr.is_alive())


@app.websocket("/api/live/ws")
async def live_ws(ws: WebSocket):
    await ws.accept()
    if not _live_autentica(ws):
        await ws.send_json(_live_erro(
            "unauthorized",
            "Sem credencial: pegue um ticket em POST /api/live/ticket e use ?ticket=<t> "
            "(ou ?key=<chave>); loopback dispensa"))
        await ws.close(code=4401)
        return
    _live_sweep()
    # o teto NÃO é checado aqui: entre esta linha e o registro há o `await` do setup
    # (duas conexões passavam juntas e furavam o limite) e só depois do setup se sabe
    # o `session_id` — uma retomada SUBSTITUI a entrada e não pode levar `busy`.
    try:
        primeiro = await asyncio.wait_for(ws.receive(), _LIVE_SETUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        await ws.send_json(_live_erro("setup_timeout", "mande o `setup` primeiro"))
        await ws.close(code=4408)
        return
    if primeiro.get("type") == "websocket.disconnect":
        return
    if "text" not in primeiro:
        await ws.send_json(_live_erro("setup_binario", "o 1º frame é o JSON `setup`"))
        await ws.close(code=4400)
        return
    try:
        cfg = _live_valida_setup(json.loads(primeiro["text"]))
    except json.JSONDecodeError:
        await ws.send_json(_live_erro("setup_json", "setup não é JSON válido"))
        await ws.close(code=4400)
        return
    except HTTPException as exc:
        # `_resolve_voice` levanta HTTPException (404 sem voz gravada) e ela NÃO é
        # ValueError: sem este ramo o handshake morria sem NENHUM frame (o cliente
        # ficava pendurado no receive e não sabia que faltava voz).
        await ws.send_json(_live_erro("setup_invalido", str(exc.detail)))
        await ws.close(code=4400)
        return
    except ValueError as exc:
        await ws.send_json(_live_erro("setup_invalido", str(exc)))
        await ws.close(code=4400)
        return

    retomado = _live_hist_pega(cfg.get("session_id") or "")
    sid = cfg["session_id"] if retomado else uuid.uuid4().hex[:10]
    if retomado:
        # retomada: voz/system do SETUP vencem quando vieram; senão, os guardados
        cfg = {**cfg,
               "voice_id": cfg["voice_id"] if cfg.get("voice_id_pedido") else
                           (retomado.get("voz") or cfg["voice_id"]),
               "system": cfg["system"] or retomado.get("system", ""),
               "history": [{"role": m.get("role", "user"), "text": m.get("content", "")}
                           for m in (retomado.get("msgs") or [])] or cfg["history"]}
    sess = {
        "id": sid, "criada": time.time(), "visto": time.monotonic(),
        # geração: quem nasce depois MANDA no registro de retomada — a sessão antiga
        # (socket zumbi) não pode sobrescrever o contexto da que a retomou.
        "geracao": time.monotonic(),
        "voice_id": cfg["voice_id"], "system": cfg["system"], "vad": cfg["vad"],
        "history": list(cfg["history"]), "buffer": bytearray(),
        "resumo": (retomado or {}).get("resumo") or "",
        "truncado": False, "fila": queue.Queue(), "turno": 0,
        "cancelado": threading.Event(), "vencida": False, "fechar": False,
        "turno_thread": None, "pipe": None, "engine": None,
        "dsh": None,                      # DSH-2: DshClient PRÓPRIO da sessão Live
        "falando": False, "audio_pendente": 0, "buffer_bytes_turno": 0,
        "t_decisao_ms": 0, "descartados": 0, "turno_barge_in": False,
        "turno_curto": False, "turno_barge_falso": False,
        # #126: 1 turno de fala pendente (pipeline ocupado) + o observador dele
        "turno_pendente": None, "turno_pendente_barge": False, "pend_thread": None,
        "pendentes_descartados": 0, "buffer_consumido": 0,
        # contadores do evento `turno_pendente` (o `substituido` booleano virou
        # isto): n de trechos acumulados e ms que saíram no teto do pendente
        "pendentes_trechos": 0, "pendentes_descartados_ms": 0,
        # telemetria (#118): contadores da sessão, sem trabalho extra relevante
        "t0": time.monotonic(), "st_frames": 0, "st_bytes": 0, "st_ultimo_frame": None,
        "st_dbfs": None, "st_prob": None, "st_chunks": 0, "st_audio_bytes": 0,
        "st_erro": None, "st_stage": "idle", "st_stage_ini": None, "st_stage_ms": 0,
        "st_proximo": 0.0,
    }
    sess["engine"] = _live_engine_novo(sess)
    _live_log_kv("abre" if not retomado else "resume", sess=sid,
                 voz=sess["voice_id"], retomado=bool(retomado),
                 history=len(sess["history"]), criadas=len(_live_sessions))
    with _live_lock:
        # teto atômico com a inserção (ver comentário no topo do handler)
        if sid not in _live_sessions and len(_live_sessions) >= _LIVE_MAX_SESSIONS:
            await ws.send_json(_live_erro(
                "busy", f"máximo de {_LIVE_MAX_SESSIONS} sessões simultâneas"))
            await ws.close(code=1013)
            return
        _live_sessions[sid] = sess
    global _live_sweeper_iniciado
    if not _live_sweeper_iniciado:
        _live_sweeper_iniciado = True
        threading.Thread(target=_live_sweeper, daemon=True).start()

    await ws.send_json({"type": "ready", "session_id": sid, "resumed": bool(retomado),
                        "voice_id": sess["voice_id"],
                        "audio": {"format": "pcm16", "sr": _LIVE_AUDIO_SR, "channels": 1},
                        "in_audio": {"format": "pcm16", "sr": 16000, "channels": 1},
                        "vad": sess["vad"]})
    sender = None
    try:
        try:
            # DENTRO do try: um pipeline que não nasce deixava a sessão presa no
            # registry para sempre (sem task de envio, nada a fechava) e o processo
            # dsh já criado órfão. Aqui ele degrada — a sessão continua no ar.
            sess["pipe"] = _live_pipe_novo(sess)
        except Exception as exc:         # noqa: BLE001 — sessão já está no ar
            sess["pipe"] = None
            print(f"[live] pipeline indisponível ({type(exc).__name__}: {exc}) — "
                  f"sessão segue sem pipeline", flush=True)
            _live_envia_json(sess, _live_erro("pipeline", f"{type(exc).__name__}: {exc}"))
        if sess["pipe"] is not None:
            threading.Thread(target=_live_pipe_start, args=(sess,), daemon=True).start()
        sender = asyncio.create_task(_live_sender(sess, ws))
        await _live_loop(sess, ws)
    except WebSocketDisconnect:
        pass
    finally:
        sess["fechar"] = True
        sess["cancelado"].set()
        if sess.get("pipe") is not None:
            try:
                sess["pipe"].close()
            except Exception:            # noqa: BLE001
                pass
        elif sess.get("dsh") is not None:     # pipeline não nasceu: o dsh é órfão
            try:
                sess["dsh"].close()
            except Exception:            # noqa: BLE001
                pass
        _live_log_kv("fecha", sess=sid, idade_s=int(time.time() - sess["criada"]),
                     frames=sess.get("st_frames", 0), bytes=sess.get("st_bytes", 0),
                     chunks=sess.get("st_chunks", 0), turnos=sess.get("turno", 0))
        try:
            _live_hist_guarda(sess)       # retomável pelo session_id
        except Exception:                 # noqa: BLE001
            pass
        sess["fila"].put(None)
        if sender is not None:
            try:
                await asyncio.wait_for(sender, 2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                sender.cancel()
        # SÓ sai do registry quem ainda é o dono da entrada: numa retomada o `sid` é
        # o do cliente, então o pop incondicional da sessão ANTIGA apagava a NOVA
        # (ela ficava fora do sweep/TTL e fora da contagem do teto).
        if _live_sessions.get(sid) is sess:
            with _live_lock:
                _live_sessions.pop(sid, None)


async def _live_loop(sess: dict, ws) -> None:
    """Loop de recepção: binário = áudio do turno, texto = comando do protocolo."""
    while True:
        msg = await ws.receive()
        if msg.get("type") == "websocket.disconnect":
            return
        sess["visto"] = time.monotonic()
        if msg.get("bytes"):
            sess["st_frames"] += 1
            sess["st_bytes"] += len(msg["bytes"])
            sess["st_ultimo_frame"] = time.monotonic()
            sess["st_dbfs"] = round(_dbfs16(bytes(msg["bytes"])), 1)
            eng = sess.get("engine")
            if eng is not None:
                _live_trata_eventos(sess, eng.feed(msg["bytes"]))
            elif sess.get("pipe") is not None:
                sess["pipe"].push_pcm(msg["bytes"])   # sem FSM: pipeline por frame
            buf = sess["buffer"]
            buf.extend(msg["bytes"])
            if len(buf) > _LIVE_MAX_BUFFER:      # mantém o INÍCIO do turno
                del buf[_LIVE_MAX_BUFFER:]
                # consumido em `_live_enriquece` (estampa `truncated` no
                # `turn_complete`); com o pipeline ele também rastreia o próprio
                # flag e o `setdefault` deixa o dele vencer. Este é o ÚNICO
                # emissor quando não há pipeline (stub) ou engine.
                sess["truncado"] = True
            continue
        if not msg.get("text"):
            continue
        try:
            dados = json.loads(msg["text"])
        except json.JSONDecodeError:
            _live_envia_json(sess, _live_erro("json", "frame de texto não é JSON"))
            continue
        tipo = str((dados or {}).get("type") or "")
        if tipo == "ping":
            _live_envia_json(sess, {"type": "pong", "t": dados.get("t")})
        elif tipo == "cancel":
            eng = sess.get("engine")
            if eng is not None:
                eng.cancel()
            _live_cancela(sess, do_cliente=True)   # abortou tudo: pendente sai junto
        elif tipo == "end_of_speech":
            eng = sess.get("engine")
            if eng is not None:          # comando é do cliente: força o fim do turno
                _live_trata_eventos(sess, eng.flush())
                continue
            if sess.get("pipe") is not None:
                _live_abre_turno(sess)               # usa o buffer da sessão
                continue
            if not sess["buffer"]:
                _live_envia_json(sess, _live_erro("sem_audio", "nenhum frame de áudio recebido"))
                continue
            if sess["turno_thread"] and sess["turno_thread"].is_alive():
                _live_envia_json(sess, _live_erro("turno_em_curso", "espere o turn_complete"))
                continue
            sess["cancelado"].clear()
            sess["turno"] += 1
            sess["buffer_bytes_turno"] = len(sess["buffer"])   # p/ observabilidade
            thr = threading.Thread(target=_live_engine, args=(sess,), daemon=True)
            sess["turno_thread"] = thr
            thr.start()
        else:
            _live_envia_json(sess, _live_erro("comando", f"comando desconhecido: {tipo!r}"))

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


def _openai_stt(file, language, response_format, translate, response=None):
    """STT OpenAI-compatível: áudio -> texto (+ segmentos). translate=True traduz p/ inglês.

    `response` é a resposta injetada pela rota: o aviso de fallback do remoto vai no
    HEADER (vale para json/verbose_json/text/srt/vtt; um campo no corpo não cobriria
    os formatos de texto)."""
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
    if response is not None:
        _aviso_remoto_headers(response.headers, r)
    rf = (response_format or "json").lower()
    if rf == "text":
        resp = Response(text + "\n", media_type="text/plain; charset=utf-8")
        _aviso_remoto_headers(resp.headers, r)
        return resp
    if rf == "srt":
        resp = Response(_segs_to_srt(segs), media_type="application/x-subrip; charset=utf-8")
        _aviso_remoto_headers(resp.headers, r)
        return resp
    if rf == "vtt":
        resp = Response(_segs_to_vtt(segs), media_type="text/vtt; charset=utf-8")
        _aviso_remoto_headers(resp.headers, r)
        return resp
    if rf == "verbose_json":
        dur = max((s["end"] for s in segs), default=0.0)
        return {"task": "translate" if translate else "transcribe", "language": lang,
                "duration": round(dur, 3), "text": text,
                "segments": [{"id": i, "start": s["start"], "end": s["end"], "text": s["text"]}
                             for i, s in enumerate(segs)]}
    return {"text": text}


@app.post("/v1/audio/transcriptions")
def openai_transcriptions(response: Response, file: UploadFile = File(...),
                          model: str = Form("whisper-1"), language: str = Form(None),
                          prompt: str = Form(None), response_format: str = Form("json"),
                          temperature: float = Form(0.0)):
    """STT compatível com OpenAI Whisper. response_format: json|text|srt|verbose_json|vtt."""
    return _openai_stt(file, language, response_format, translate=False, response=response)


@app.post("/v1/audio/translations")
def openai_translations(response: Response, file: UploadFile = File(...),
                        model: str = Form("whisper-1"), prompt: str = Form(None),
                        response_format: str = Form("json"), temperature: float = Form(0.0)):
    """STT + tradução p/ inglês (compatível com OpenAI). response_format igual ao de transcriptions."""
    return _openai_stt(file, None, response_format, translate=True, response=response)


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

    # queue=false: cliente controla o tempo da fala (Conversa faz pipeline no
    # navegador). Default true — o contrato do SDK OpenAI não muda.
    q = payload.get("queue", payload.get("speech_queue", _settings.get("speech_queue", True)))
    use_queue = q is not False and str(q).lower() not in ("false", "0", "no")

    # reusa o pipeline de jobs de forma síncrona (histórico incluso)
    job_id = _jobs_admit({"text": text[:200]})
    # Referência PRÓPRIA ao job: assim que a thread termina o job vira
    # descartável e outro pedido pode evictá-lo do _jobs antes da leitura abaixo
    # (o evict dá pop no dict, não invalida o objeto) — sem isto era KeyError/500.
    job = _jobs[job_id]
    # mesma mecânica do /api/tts: MLX exige thread "nova" (stream GPU é
    # thread-local e o threadpool do FastAPI reusa threads sem stream)
    t = threading.Thread(
        target=_run_tts_job,
        args=(job_id, text, voice_id, VOICES_DIR / f"{voice_id}.wav", language, omni),
        kwargs={"use_queue": use_queue},
        daemon=True,
    )
    t.start()
    t.join(timeout=600)
    if t.is_alive():
        raise HTTPException(504, "Síntese excedeu 10 minutos")
    if job["status"] != "done":
        raise HTTPException(500, f"Falha na síntese: {job.get('error')}")

    wav_path = OUTPUTS_DIR / f"{job['output']['id']}.wav"
    # velocidade já aplicada nativamente pelo modelo; aqui ffmpeg só converte o formato
    data, mime = _encode_audio(wav_path, fmt, 1.0)
    return Response(content=data, media_type=mime)


# descarrega TTS/STT/tradutor/SER ociosos (idle_unload_minutes; 0 = off)
threading.Thread(target=_idle_unload_loop, daemon=True).start()

# CORS por ÚLTIMO = camada mais externa (ver nota no topo do módulo): assim
# 401/429 do middleware de auth e erros de rota saem com os cabeçalhos que o
# navegador cross-origin exige — sem eles o fetch vira "Failed to fetch".
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    # `Retry-After` não é header safelisted de CORS: sem expor, cliente
    # cross-origin (o client/ do repo, SDK de fora) lê null e negocia o retry no
    # escuro. Os nossos 429 (admissão de jobs e rate limiter) pedem um tempo
    # explícito — ele tem que chegar a quem vai repetir.
    expose_headers=["Retry-After"],
)

# UI estática (registrada por último para não engolir /api/* e /v1/*)
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
