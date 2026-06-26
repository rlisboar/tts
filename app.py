"""TTS-Rod — clonagem de voz local com gravação e gerenciamento de vozes.

Servidor FastAPI + OmniVoice (Xiaomi/k2-fsa) quantizado em MLX (Apple Silicon).
Tudo local: nenhum áudio ou texto sai da máquina.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
import wave
from collections import OrderedDict
from pathlib import Path
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
VOICES_DIR = BASE / "voices"
OUTPUTS_DIR = BASE / "outputs"
VOICES_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

# trechos parciais de jobs interrompidos não sobrevivem a restart
for _d in OUTPUTS_DIR.glob(".job-*"):
    shutil.rmtree(_d, ignore_errors=True)

# OmniVoice é o único backend. As conversões MLX publicadas vêm quebradas: o repo
# "-bf16" tem o audio_tokenizer sem o encoder semântico (HuBERT) e o "-4bit" não
# quantiza no load_model. Montamos um dir local = backbone bf16 + audio_tokenizer
# COMPLETO do repo sem sufixo (que traz o HuBERT). Feito uma vez em .omnivoice-bf16/.
OMNI_BACKBONE_REPO = os.environ.get("TTS_ROD_OMNI_BACKBONE", "mlx-community/OmniVoice-bf16")
OMNI_TOKENIZER_REPO = os.environ.get("TTS_ROD_OMNI_TOKENIZER", "mlx-community/OmniVoice")
OMNI_ASSEMBLED = BASE / ".omnivoice-bf16"
# "omnivoice" (default) = monta o bf16 acima; ou um id/dir MLX de OmniVoice já pronto
MODEL_ID = os.environ.get("TTS_ROD_MODEL", "omnivoice")
OMNI_ALIASES = {"", "omnivoice", "omni", "omnivoice-bf16"}

# ---------------------------------------------------------------------------
# Configurações padrão editáveis no dashboard (persistem em settings.json e
# valem para UI e API; parâmetro explícito na requisição sempre sobrepõe)
# ---------------------------------------------------------------------------
SETTINGS_PATH = BASE / "settings.json"
_SETTINGS_DEFAULTS = {
    "model": MODEL_ID,         # "omnivoice" ou um id/dir MLX de OmniVoice
    "pre_prompt": "",          # texto falado antes de toda geração
    "language": "auto",        # "auto" = OmniVoice detecta o idioma do texto (recomendado)
    "default_voice": None,     # id; None = voz mais recente
    "chunk_max_chars": 140,
    "speed": 1.0,              # só API /v1 (conversão ffmpeg)
    "auto_cleanup": False,     # apaga áudios gerados automaticamente
    "auto_cleanup_minutes": 15,
    # OmniVoice — controles de geração (defaults = os da lib)
    "omni_num_steps": 16,             # passos de unmasking (4–64); 16 rápido, 32 qualidade
    "omni_guidance_scale": 2.0,       # força do CFG (0–10): + = mais aderente ao texto/voz
    "omni_class_temperature": 0.0,    # temp. de amostragem de token (0 = greedy/estável)
    "omni_position_temperature": 5.0, # temp. da escolha de posição a revelar (0–20)
    "omni_layer_penalty_factor": 5.0, # penalidade por camada de codebook (0–20)
    "omni_t_shift": 0.1,              # deslocamento do cronograma de difusão (0–1)
    "omni_instruct": "",              # voice design textual (ex.: "female, low pitch")
    "omni_duration_s": None,          # força duração fixa em s (None = automático)
    "omni_ref_max_s": 10.0,           # quanto da amostra de referência usar (3–30 s)
    # Tradutor de voz — filtros anti-ruído da transcrição (rejeita alucinação do Whisper)
    "stt_min_words": 1,               # mínimo de palavras p/ aceitar (ignora ruído)
    "stt_min_chars": 2,               # mínimo de caracteres
    "stt_max_no_speech": 0.6,         # rejeita se prob. de "sem fala" acima disto (0–1)
    "stt_min_logprob": -1.0,          # rejeita se confiança média abaixo disto (-5–0)
    "stt_max_compression": 2.4,       # rejeita se repetitivo demais (alucinação) (1–10)
}
_settings = dict(_SETTINGS_DEFAULTS)
if SETTINGS_PATH.exists():
    try:
        salvo = json.loads(SETTINGS_PATH.read_text())
        _settings.update({k: salvo[k] for k in _SETTINGS_DEFAULTS if k in salvo})
    except Exception:  # noqa: BLE001
        pass


def _save_settings():
    SETTINGS_PATH.write_text(json.dumps(_settings, ensure_ascii=False, indent=2))

# Textos maiores são gerados em trechos. OmniVoice é masked-diffusion não-AR (sem o
# problema de EOS do backend antigo), mas dividir permite tocar trecho-a-trecho — a
# fala começa após o 1º trecho, não no fim.
CHUNK_MAX_CHARS = 140
CHUNK_SILENCE_S = 0.25

# O modelo sai com volume baixo (RMS ~0,10); normaliza para nível de fala.
TARGET_RMS = 0.15
PEAK_LIMIT = 0.95

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
    "vd-narrador": {"name": "Narrador (masc., grave)",     "instruct": "male, deep, warm, calm narrator, clear articulation"},
    "vd-locutora": {"name": "Locutora (fem., suave)",      "instruct": "female, soft, warm, friendly, natural"},
    "vd-jovem-m":  {"name": "Jovem (masc., animado)",      "instruct": "young male, energetic, upbeat, expressive"},
    "vd-jovem-f":  {"name": "Jovem (fem., animada)",       "instruct": "young female, bright, cheerful, lively"},
    "vd-formal":   {"name": "Formal (masc., autoritário)", "instruct": "male, serious, authoritative, formal, measured"},
    "vd-podcast":  {"name": "Podcast (fem., conversa)",    "instruct": "female, conversational, relaxed, engaging podcast host"},
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

# Chave de API: protege /api/* e /v1/*. Aceita Authorization: Bearer,
# X-API-Key ou ?api_key= (necessário para <audio src> na UI, que não envia header).
API_KEY = os.environ.get("TTS_ROD_API_KEY")
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

app = FastAPI(title="TTS-Rod")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _exige_chave(request, call_next):
    protegido = request.url.path.startswith(("/api/", "/v1/"))
    if API_KEY and protegido and request.method != "OPTIONS":
        # loopback = processo no próprio Mac; chave só para a rede
        local = request.client and request.client.host in ("127.0.0.1", "::1")
        ok = (local
              or request.headers.get("authorization") == f"Bearer {API_KEY}"
              or request.headers.get("x-api-key") == API_KEY
              or request.query_params.get("api_key") == API_KEY)
        if not ok:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Não autorizado"}, status_code=401)
    return await call_next(request)

# ---------------------------------------------------------------------------
# Modelo (carregamento preguiçoso — primeira síntese baixa/monta os pesos)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_gen_lock = threading.Lock()  # geração não é thread-safe; serializa
_model_state = {"status": "idle", "device": None, "model": _settings["model"],
                "error": None, "progress": None}


def _assemble_omnivoice_path() -> str:
    """Monta (uma vez) o dir local que conserta a conversão bf16 do OmniVoice.

    backbone bf16 (sem o audio_tokenizer quebrado) + audio_tokenizer completo
    (com HuBERT) do repo sem sufixo, ligados por symlink em .omnivoice-bf16/.
    """
    from huggingface_hub import snapshot_download

    pronto = ((OMNI_ASSEMBLED / "model.safetensors").exists()
              and (OMNI_ASSEMBLED / "audio_tokenizer" / "model.safetensors").exists())
    if pronto:
        return str(OMNI_ASSEMBLED)
    _model_state.update(progress="baixando OmniVoice (backbone + tokenizer)…")
    backbone = Path(snapshot_download(OMNI_BACKBONE_REPO, ignore_patterns=["audio_tokenizer/*"]))
    tokrepo = Path(snapshot_download(OMNI_TOKENIZER_REPO, allow_patterns=["audio_tokenizer/*"]))
    OMNI_ASSEMBLED.mkdir(exist_ok=True)
    for f in backbone.iterdir():
        if f.name == "audio_tokenizer":
            continue
        dst = OMNI_ASSEMBLED / f.name
        if not dst.exists():
            os.symlink(f.resolve(), dst)
    atok = OMNI_ASSEMBLED / "audio_tokenizer"
    if not atok.exists():
        os.symlink((tokrepo / "audio_tokenizer").resolve(), atok)
    _model_state.update(progress=None)
    return str(OMNI_ASSEMBLED)


def _resolve_model_path() -> str:
    """'omnivoice' (alias) -> monta o bf16; senão usa o id/dir MLX informado."""
    if str(_settings["model"] or "").strip().lower() in OMNI_ALIASES:
        return _assemble_omnivoice_path()
    return _settings["model"]


def _get_model():
    global _model
    with _model_lock:
        if _model is not None and _model_state.get("model") == _settings["model"]:
            return _model
        # troca de modelo no dashboard: descarrega o atual e carrega o novo
        _model = None
        _conds_cache.clear()
        _model_state.update(status="loading", device="mlx", model=_settings["model"])
        try:
            import gc

            gc.collect()
            from mlx_audio.tts.utils import load_model

            _model = load_model(_resolve_model_path())
            _model_state.update(status="ready", error=None)
            return _model
        except Exception as exc:  # noqa: BLE001
            _model_state.update(status="error", error=str(exc))
            raise


# ref_tokens por voz custam ~1,5s para preparar; cache LRU evita repetir.
# Chave inclui mtime (regravação invalida a voz).
_conds_cache: "OrderedDict[tuple, object]" = OrderedDict()
_CONDS_CACHE_MAX = 8


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


def _sanitize_text(text: str) -> str:
    """Limpeza leve para o tokenizer multilíngue (Qwen3) do OmniVoice.

    O Qwen3 lida com acentos, pontuação rica e colchetes (tags não-verbais como
    [laughter]); só normalizamos forma Unicode, expandimos símbolos com leitura
    natural e garantimos pontuação terminal estável.
    """
    # acentos digitados em forma decomposta (NFD, comum no macOS) viram o composto
    text = unicodedata.normalize("NFC", text)
    # símbolos com leitura natural em pt
    text = (text.replace("%", " por cento").replace("&", " e ")
                .replace("+", " mais ").replace("°", " graus ")
                .replace("=", " igual a ").replace("/", " ou "))
    text = re.sub(r"\s{2,}", " ", text).strip()
    # final sem pontuação terminal desestabiliza a duração estimada
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def _split_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Divide por sentenças (e vírgulas, em último caso) em trechos de até max_chars."""

    def pack(parts: list[str]) -> list[str]:
        out, cur = [], ""
        for p in parts:
            if cur and len(cur) + len(p) + 1 > max_chars:
                out.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}".strip()
        if cur:
            out.append(cur)
        return out

    def burst(c: str) -> list[str]:
        return pack(re.split(r"(?<=[,;:])\s+", c)) if len(c) > max_chars else [c]

    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]
    if not sentences:
        return []
    # 1ª sentença fica sozinha: trecho menor → a fala começa mais cedo
    final = burst(sentences[0])
    for c in pack(sentences[1:]):
        final.extend(burst(c))
    final = [c for c in final if c.strip()]
    # trecho minúsculo (ex.: "Disparo:" sobrando de ponto órfão) desestabiliza
    # o modelo — funde com o vizinho
    merged: list[str] = []
    for c in final:
        if merged and (len(c) < 15 or len(merged[-1]) < 15) \
                and len(merged[-1]) + len(c) + 1 <= max_chars:
            merged[-1] = f"{merged[-1]} {c}"
        else:
            merged.append(c)
    return merged


def _generate_chunk(model, text: str, language: str, conds, ref_text, omni: dict):
    import numpy as np

    # masked-diffusion não-AR: duração estimada internamente, sem teto de tokens
    # nem risco de "correr até o fim". conds são os ref_tokens cacheados; todos os
    # controles vêm do dict omni (settings/override por requisição).
    o = omni or {}
    results = model.generate(
        text=text,
        ref_tokens=conds,
        ref_text=ref_text,
        language=_omni_language(language),
        num_steps=int(o.get("num_steps") or OMNI_STEPS_FAST),
        guidance_scale=o.get("guidance_scale", 2.0),
        class_temperature=o.get("class_temperature", 0.0),
        position_temperature=o.get("position_temperature", 5.0),
        layer_penalty_factor=o.get("layer_penalty_factor", 5.0),
        t_shift=o.get("t_shift", 0.1),
        instruct=o.get("instruct") or "None",
        duration_s=o.get("duration_s"),
    )
    pieces = [np.array(r.audio, dtype=np.float32) for r in results]
    return np.concatenate(pieces)


def _trim_tail_silence(audio, sr: int, limiar: float = 0.006, pad_s: float = 0.3):
    """Corta cauda silenciosa (sobra típica de geração que estourou o teto)."""
    import numpy as np

    win = int(0.05 * sr)
    fim = len(audio)
    while fim > win:
        if float(np.sqrt(np.mean(audio[fim - win:fim] ** 2))) >= limiar:
            break
        fim -= win
    return audio[:min(len(audio), fim + int(pad_s * sr))]


def _anomalo(audio, sr: int, chunk: str) -> bool:
    """Geração descarrilada = inaudível ou curta demais para o texto.

    OmniVoice é masked-diffusion não-AR (sem teto de tokens nem EOS frágil): a
    duração é estimada internamente e varia mais legitimamente, então só
    truncamento grosseiro e áudio inaudível pedem nova tentativa.
    """
    import numpy as np

    if float(np.sqrt(np.mean(audio**2))) < 0.01:  # inaudível
        return True
    return len(audio) / sr < len(chunk) / 45  # truncamento grosseiro


def _normalize(audio, target_rms: float = TARGET_RMS, peak_limit: float = PEAK_LIMIT):
    import numpy as np

    rms = float(np.sqrt(np.mean(audio**2)))
    if rms > 1e-6:
        audio = audio * (target_rms / rms)
    peak = float(np.abs(audio).max())
    if peak > peak_limit:
        audio = audio * (peak_limit / peak)
    return audio


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Vozes: voices/<id>.wav + voices/<id>.json
# ---------------------------------------------------------------------------


@app.get("/api/status")
def status():
    return _model_state


@app.post("/api/shutdown")
def shutdown():
    """Desliga o servidor (botão na UI). Reiniciar: dois cliques em TTS-Rod.command."""
    def _stop():
        time.sleep(0.4)  # deixa a resposta HTTP voltar antes de sair
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True, "msg": "Servidor desligando…"}


@app.get("/api/settings")
def get_settings():
    return _settings


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


def _resolve_omni(payload: dict) -> dict:
    """Resolve os controles do OmniVoice: override no request, senão settings."""
    return {
        "num_steps": int(_clamp(payload.get("num_steps"), 4, 64, _settings["omni_num_steps"])),
        "guidance_scale": _clamp(payload.get("guidance_scale"), 0.0, 10.0, _settings["omni_guidance_scale"]),
        "class_temperature": _clamp(payload.get("class_temperature"), 0.0, 2.0, _settings["omni_class_temperature"]),
        "position_temperature": _clamp(payload.get("position_temperature"), 0.0, 20.0, _settings["omni_position_temperature"]),
        "layer_penalty_factor": _clamp(payload.get("layer_penalty_factor"), 0.0, 20.0, _settings["omni_layer_penalty_factor"]),
        "t_shift": _clamp(payload.get("t_shift"), 0.0, 1.0, _settings["omni_t_shift"]),
        "instruct": (str(payload["instruct"]).strip()[:300] if payload.get("instruct")
                     else _settings["omni_instruct"]),
        "duration_s": (_resolve_duration_s(payload["duration_s"], _settings["omni_duration_s"])
                       if "duration_s" in payload else _settings["omni_duration_s"]),
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
    if "omni_instruct" in payload:
        _settings["omni_instruct"] = str(payload["omni_instruct"] or "").strip()[:300]
    if "omni_duration_s" in payload:
        _settings["omni_duration_s"] = _resolve_duration_s(payload["omni_duration_s"], None)
    if "omni_ref_max_s" in payload:
        _settings["omni_ref_max_s"] = _clamp(payload["omni_ref_max_s"], 3.0, 30.0, 10.0)
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
    _save_settings()
    return _settings


@app.get("/api/voices")
def list_voices():
    voices = []
    existentes = set()
    for meta_file in sorted(VOICES_DIR.glob("*.json")):
        m = json.loads(meta_file.read_text())
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


@app.post("/api/voices")
def create_voice(name: str = Form(...), audio: UploadFile = None, ref_text: str = Form("")):
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    voice_id = uuid.uuid4().hex[:10]
    wav_path = VOICES_DIR / f"{voice_id}.wav"
    wav_path.write_bytes(audio.file.read())

    duration = _wav_duration(wav_path)
    if duration < 3:
        wav_path.unlink()
        raise HTTPException(400, f"Gravação muito curta ({duration}s). Mínimo 3s, ideal 10–30s.")

    meta = {
        "id": voice_id,
        "name": name.strip() or voice_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": duration,
    }
    # transcrição opcional da amostra: clonagem do OmniVoice fica mais estável
    if ref_text.strip():
        meta["ref_text"] = ref_text.strip()[:500]
    (VOICES_DIR / f"{voice_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


@app.get("/api/voices/{voice_id}/audio")
def voice_audio(voice_id: str):
    path = VOICES_DIR / f"{voice_id}.wav"
    if not path.exists():
        raise HTTPException(404, "Voz não encontrada")
    return FileResponse(path, media_type="audio/wav")


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


def _piece_dir(job_id: str) -> Path:
    return OUTPUTS_DIR / f".job-{job_id}"


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
    (VOICES_DIR / f"{pid}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def _run_tts_job(job_id: str, text: str, voice_id: str, voice_path: Path,
                 language: str, omni: dict):
    import numpy as np
    import soundfile as sf

    job = _jobs[job_id]
    try:
        model = _get_model()
        sr = getattr(model, "sample_rate", 24000)
        chunks = _split_text(_sanitize_text(text), max_chars=_settings["chunk_max_chars"])
        silence = np.zeros(int(CHUNK_SILENCE_S * sr), dtype=np.float32)
        job["total"] = len(chunks)
        pdir = _piece_dir(job_id)
        pdir.mkdir(exist_ok=True)

        with _gen_lock:
            started = time.time()
            # voz padrão ainda não materializada: cria a amostra-semente 1x
            if voice_id in OMNI_PRESETS and not voice_path.exists():
                job["progress"] = {"stage": "criando voz padrão…"}
                _materialize_preset(model, sr, voice_id)
            ref_text = _voice_ref_text(voice_id)
            conds = _cond_for(model, voice_id, voice_path)
            for i, chunk in enumerate(chunks):
                job["progress"] = {"current": i + 1, "total": len(chunks)}
                # trecho sem pontuação terminal (quebra por vírgula) ganha ponto
                if chunk[-1] not in ".!?…":
                    chunk = chunk.rstrip(" ,;:") + "."
                for tentativa in (1, 2):
                    audio = _generate_chunk(model, chunk, language, conds, ref_text, omni)
                    if not _anomalo(audio, sr, chunk):
                        break
                    job["retries"] = job.get("retries", 0) + 1
                audio = _normalize(_trim_tail_silence(audio, sr))
                if i < len(chunks) - 1:
                    audio = np.concatenate([audio, silence])
                sf.write(pdir / f"{i}.wav", audio, sr, subtype="PCM_16")
                job["pieces"] = i + 1  # publica só depois do arquivo no disco
        elapsed = round(time.time() - started, 1)

        # arquivo final do histórico = exatamente o que foi tocado
        full = np.concatenate([sf.read(pdir / f"{i}.wav", dtype="float32")[0]
                               for i in range(len(chunks))])
        out_id = uuid.uuid4().hex[:10]
        sf.write(OUTPUTS_DIR / f"{out_id}.wav", full, sr, subtype="PCM_16")
        meta = {
            "id": out_id,
            "text": text,
            "voice_id": voice_id,
            "language": _omni_language(language),
            "num_steps": int(omni.get("num_steps") or OMNI_STEPS_FAST),
            "guidance_scale": omni.get("guidance_scale"),
            "class_temperature": omni.get("class_temperature"),
            "instruct": omni.get("instruct") or "",
            "chunks": len(chunks),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": round(len(full) / sr, 1),
            "elapsed": elapsed,
        }
        (OUTPUTS_DIR / f"{out_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        job.update(status="done", output=meta)
    except Exception as exc:  # noqa: BLE001
        job.update(status="error", error=str(exc))
    finally:
        job["progress"] = None
        _model_state["progress"] = None


@app.post("/api/tts")
def synthesize(payload: dict):
    text = (payload.get("text") or "").strip()
    if _settings["pre_prompt"]:
        text = f"{_settings['pre_prompt']} {text}".strip()
    voice_id = payload.get("voice_id") or _settings["default_voice"]
    language = (payload.get("language") or _settings["language"]).lower()
    omni = _resolve_omni(payload)
    if not text:
        raise HTTPException(400, "Texto vazio")
    if len(text) > 5000:
        raise HTTPException(400, "Texto longo demais (máx. 5000 caracteres)")

    voice_path = VOICES_DIR / f"{voice_id}.wav"
    if not voice_path.exists() and voice_id not in OMNI_PRESETS:
        raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    job_id = uuid.uuid4().hex[:10]
    _jobs[job_id] = {"status": "running", "pieces": 0, "total": None,
                     "progress": None, "output": None, "error": None,
                     "text": text[:200]}  # facilita depurar relatos de áudio mudo
    while len(_jobs) > _JOBS_MAX:
        old_id, _ = _jobs.popitem(last=False)
        shutil.rmtree(_piece_dir(old_id), ignore_errors=True)

    threading.Thread(
        target=_run_tts_job,
        args=(job_id, text, voice_id, voice_path, language, omni),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@app.get("/api/tts/jobs/{job_id}")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado")
    return job


@app.get("/api/tts/jobs/{job_id}/pieces/{index}")
def job_piece(job_id: str, index: int):
    path = _piece_dir(job_id) / f"{index}.wav"
    if job_id not in _jobs or not path.exists():
        raise HTTPException(404, "Trecho não encontrado")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/outputs")
def list_outputs():
    outputs = [json.loads(f.read_text()) for f in OUTPUTS_DIR.glob("*.json")]
    outputs.sort(key=lambda o: o["created_at"], reverse=True)
    return outputs


@app.get("/api/outputs/{out_id}/audio")
def output_audio(out_id: str):
    path = OUTPUTS_DIR / f"{out_id}.wav"
    if not path.exists():
        raise HTTPException(404, "Áudio não encontrado")
    return FileResponse(path, media_type="audio/wav", filename=f"tts-rod-{out_id}.wav")


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
_mt = {"model": None, "tok": None}
_mt_lock = threading.Lock()


# alucinações comuns do Whisper em silêncio/ruído (pt + en)
_STT_BLACKLIST = {
    "obrigado", "obrigada", "obrigado.", "tchau", "valeu", "fim", "the end",
    "thank you", "thanks for watching", "you", "bye", "bye.", "okay", "ok",
    "legendas pela comunidade amara.org", "amara.org", "subtitles by the amara.org community",
    "♪", "...", ".", "music", "música", "applause", "aplausos",
}


def _transcribe(audio_path: Path):
    import mlx_whisper

    with _stt_lock:
        # opções que reduzem alucinação: greedy, sem condicionar no texto anterior,
        # e os limiares de no-speech / confiança / repetição configuráveis
        r = mlx_whisper.transcribe(
            str(audio_path), path_or_hf_repo=WHISPER_REPO,
            temperature=0.0, condition_on_previous_text=False,
            no_speech_threshold=_settings["stt_max_no_speech"],
            logprob_threshold=_settings["stt_min_logprob"],
            compression_ratio_threshold=_settings["stt_max_compression"],
        )
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


def _translate(text: str, target: str) -> str:
    from mlx_lm import generate, load

    with _mt_lock:
        if _mt["model"] is None:
            _mt["model"], _mt["tok"] = load(TRANSLATE_REPO)
        model, tok = _mt["model"], _mt["tok"]
        nome = LANG_DISPLAY.get(target, target)
        msgs = [{"role": "user", "content":
                 f"Translate the text below into {nome}. Output ONLY the translation, "
                 f"no quotes, no explanations, keep it natural.\n\nText: {text}"}]
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
        out = generate(model, tok, prompt=prompt, max_tokens=512, verbose=False)
    return out.strip().strip('"').strip()


# --- Captura de emoção: vira `instruct` (voice design) do OmniVoice ---
_ser = {"clf": None}
_ser_lock = threading.Lock()
SER_REPO = os.environ.get("TTS_ROD_SER", "superb/wav2vec2-base-superb-er")
_SER_MAP = {
    "hap": "happy, cheerful, upbeat", "happy": "happy, cheerful, upbeat",
    "ang": "angry, intense, tense", "angry": "angry, intense, tense",
    "sad": "sad, subdued, soft", "sadness": "sad, subdued, soft",
    "neu": "neutral, calm", "neutral": "neutral, calm", "calm": "calm, relaxed",
    "fear": "fearful, anxious, tense", "fearful": "fearful, anxious, tense",
    "disgust": "disgusted, cold", "surprise": "surprised, excited",
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


def _emotion_light(path: Path, text: str) -> str:
    """Heurística de prosódia -> adjetivos de tom (intensidade/energia). Determinística."""
    p = _prosody(path)
    rate = len(text) / p["dur"]
    forte, fraco, expr = p["rms"] > 0.16, p["rms"] < 0.06, p["dyn"] > 0.05
    rapido, lento = rate > 16, rate < 9
    adj = []
    if forte and expr:
        adj += ["energetic", "expressive"]
    elif forte:
        adj += ["intense", "firm"]
    elif fraco:
        adj += ["soft", "gentle"]
    if rapido:
        adj.append("lively")
    elif lento:
        adj.append("calm")
    if not adj:
        adj = ["neutral", "natural"]
    vistos, out = set(), []
    for a in adj:
        if a not in vistos:
            vistos.add(a)
            out.append(a)
    return ", ".join(out[:4])


def _emotion_accurate(path: Path) -> str:
    """Modelo SER (wav2vec2, PyTorch) -> categoria -> adjetivos, com gate de confiança."""
    with _ser_lock:
        if _ser["clf"] is None:
            from transformers import pipeline
            _ser["clf"] = pipeline("audio-classification", model=SER_REPO)
        res = _ser["clf"](str(path), top_k=None)
    if not res:
        return "neutral, calm"
    top = max(res, key=lambda x: x.get("score", 0.0))
    lab = str(top.get("label", "")).lower()
    # baixa confiança ou neutro -> não força emoção (evita falso "happy/angry")
    if top.get("score", 0.0) < 0.5 or lab in ("neu", "neutral"):
        return "neutral, calm"
    return _SER_MAP.get(lab, lab)


def _emotion_instruct(path: Path, text: str, mode: str):
    """Retorna (instruct, erro). mode: off | light | accurate."""
    try:
        if mode == "light":
            return _emotion_light(path, text), None
        if mode == "accurate":
            return _emotion_accurate(path), None
    except Exception as exc:  # noqa: BLE001
        return "", str(exc)
    return "", None


@app.post("/api/translate-speech")
def translate_speech(audio: UploadFile = None, target_lang: str = Form("en"),
                     voice_id: str = Form(""), source_lang: str = Form("auto"),
                     emotion_mode: str = Form("off")):
    """fala (áudio) -> transcreve -> traduz -> dispara TTS na voz; devolve textos + job_id."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    tgt = (target_lang or "en").lower()
    vid = voice_id or _settings["default_voice"]
    vpath = VOICES_DIR / f"{vid}.wav"
    if not vpath.exists() and vid not in OMNI_PRESETS:
        raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    tmp = OUTPUTS_DIR / f".stt-{uuid.uuid4().hex[:10]}"
    tmp.write_bytes(audio.file.read())
    emo, emo_err = "", None
    try:
        r = _transcribe(tmp)
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
        # captura de emoção (precisa do áudio ainda em disco) -> vira instruct
        modo = (emotion_mode or "off").lower()
        if modo in ("light", "accurate"):
            emo, emo_err = _emotion_instruct(tmp, src_text, modo)
    finally:
        tmp.unlink(missing_ok=True)

    translation = _translate(src_text, tgt)
    omni = _resolve_omni({})
    if emo:
        omni["instruct"] = emo  # reproduz a emoção detectada na voz traduzida

    job_id = uuid.uuid4().hex[:10]
    _jobs[job_id] = {"status": "running", "pieces": 0, "total": None,
                     "progress": None, "output": None, "error": None,
                     "text": translation[:200]}
    while len(_jobs) > _JOBS_MAX:
        old_id, _ = _jobs.popitem(last=False)
        shutil.rmtree(_piece_dir(old_id), ignore_errors=True)
    threading.Thread(
        target=_run_tts_job,
        args=(job_id, translation, vid, vpath, tgt, omni),
        daemon=True,
    ).start()
    return {"job_id": job_id, "source_text": src_text, "source_lang": src_lang,
            "translation": translation, "target_lang": tgt,
            "emotion": emo or None, "emotion_error": emo_err}


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


def _atempo_chain(speed: float) -> str:
    fatores = []
    while speed > 2.0:
        fatores.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        fatores.append(0.5)
        speed /= 0.5
    fatores.append(speed)
    return ",".join(f"atempo={f:g}" for f in fatores)


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
        {"id": m, "object": "model", "created": agora, "owned_by": "tts-rod"}
        for m in ("tts-1", "tts-1-hd")
    ]}


@app.post("/v1/audio/speech")
def openai_speech(payload: dict):
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
    speed = _clamp(payload.get("speed"), 0.25, 4.0, _settings["speed"])
    voice_id = _resolve_voice(payload.get("voice"))
    language = (payload.get("language") or _settings["language"]).lower()
    omni = _resolve_omni(payload)
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
    data, mime = _encode_audio(wav_path, fmt, speed)
    return Response(content=data, media_type=mime)


# UI estática (registrada por último para não engolir /api/* e /v1/*)
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
