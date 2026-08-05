"""Catálogo de backends TTS locais (mlx-audio), controles por família e adaptador
unificado de geração.

settings["model"] = atalho (ex.: "qwen3-0.6b") ou id/dir HF livre.
"""

from __future__ import annotations

from typing import Any, Optional

# ---------------------------------------------------------------------------
# Controles por família — UI consome isso via GET /api/backends
# type: range | number | checkbox | select | text
# setting: chave em settings.json
# ---------------------------------------------------------------------------

def _r(setting, label, *, min_v, max_v, step, default, decimals=1, hint="", section="main"):
    return {
        "id": setting, "setting": setting, "label": label, "type": "range",
        "min": min_v, "max": max_v, "step": step, "default": default,
        "decimals": decimals, "hint": hint, "section": section,
    }


def _n(setting, label, *, min_v=None, max_v=None, step=1, default=None, hint="",
       section="main", placeholder=""):
    return {
        "id": setting, "setting": setting, "label": label, "type": "number",
        "min": min_v, "max": max_v, "step": step, "default": default,
        "hint": hint, "section": section, "placeholder": placeholder,
    }


def _s(setting, label, options, *, default="", hint="", section="main"):
    return {
        "id": setting, "setting": setting, "label": label, "type": "select",
        "options": options, "default": default, "hint": hint, "section": section,
    }


def _t(setting, label, *, default="", hint="", section="design", placeholder="", rows=2):
    return {
        "id": setting, "setting": setting, "label": label, "type": "text",
        "default": default, "hint": hint, "section": section,
        "placeholder": placeholder, "rows": rows,
    }


def _c(setting, label, *, default=True, hint="", section="advanced"):
    return {
        "id": setting, "setting": setting, "label": label, "type": "checkbox",
        "default": default, "hint": hint, "section": section,
    }


QWEN_SPEAKERS = [
    {"value": "Ryan", "label": "Ryan (EN masc.)"},
    {"value": "Aiden", "label": "Aiden (EN masc.)"},
    {"value": "Vivian", "label": "Vivian (ZH fem.)"},
    {"value": "Serena", "label": "Serena (ZH fem.)"},
    {"value": "Uncle_Fu", "label": "Uncle Fu (ZH masc.)"},
    {"value": "Dylan", "label": "Dylan (ZH Pequim)"},
    {"value": "Eric", "label": "Eric (ZH Sichuan)"},
]

KOKORO_VOICES = [
    {"value": "af_heart", "label": "af_heart (EN fem.)"},
    {"value": "af_bella", "label": "af_bella (EN fem.)"},
    {"value": "af_sarah", "label": "af_sarah (EN fem.)"},
    {"value": "am_adam", "label": "am_adam (EN masc.)"},
    {"value": "am_michael", "label": "am_michael (EN masc.)"},
    {"value": "bf_emma", "label": "bf_emma (EN-GB fem.)"},
    {"value": "bm_george", "label": "bm_george (EN-GB masc.)"},
    {"value": "pf_dora", "label": "pf_dora (PT fem.)"},
    {"value": "pm_alex", "label": "pm_alex (PT masc.)"},
    {"value": "pm_santa", "label": "pm_santa (PT masc.)"},
]

POCKET_VOICES = [
    {"value": "alba", "label": "alba"},
    {"value": "marius", "label": "marius"},
    {"value": "javert", "label": "javert"},
    {"value": "jean", "label": "jean"},
    {"value": "fantine", "label": "fantine"},
    {"value": "cosette", "label": "cosette"},
]

VOXTRAL_VOICES = [
    {"value": "casual_male", "label": "casual_male"},
    {"value": "casual_female", "label": "casual_female"},
    {"value": "formal_male", "label": "formal_male"},
    {"value": "formal_female", "label": "formal_female"},
    {"value": "narrator", "label": "narrator"},
]

# Controles compartilhados (AR sampling)
_AR_TEMP = _r("gen_temperature", "Temperatura", min_v=0.05, max_v=2.0, step=0.05,
              default=0.8, decimals=2, hint="Alto = mais variação; baixo = mais estável")
_AR_TOP_P = _r("gen_top_p", "Top-p", min_v=0.1, max_v=1.0, step=0.05,
               default=0.95, decimals=2, hint="Nucleus sampling")
_AR_TOP_K = _r("gen_top_k", "Top-k", min_v=0, max_v=200, step=1,
               default=50, decimals=0, hint="0 = desligado em alguns backends")
_AR_REP = _r("gen_repetition_penalty", "Penalidade de repetição", min_v=1.0, max_v=2.0,
             step=0.05, default=1.1, decimals=2, hint=">1 reduz loops/eco de palavras")
_AR_MAX = _n("gen_max_tokens", "Máx. tokens", min_v=64, max_v=8192, step=64,
             default=2048, hint="Teto de tokens de áudio/texto", section="advanced")
_INSTRUCT_FREE = _t("omni_instruct", "Instruct / estilo",
                    hint="Texto livre: emoção, estilo, descrição da voz",
                    placeholder="ex.: happy, energetic, low pitch", rows=2)
_INSTRUCT_OMNI = _t("omni_instruct", "Instruct (tags OmniVoice)",
                    hint="Só tags fechadas: gender, age, pitch, accent, whisper",
                    placeholder="male, middle-aged, low pitch", rows=1)
_REF_MAX = _n("omni_ref_max_s", "Ref. da amostra (s)", min_v=3, max_v=30, step=1,
              default=10, hint="Quanto da gravação usar no clone")

FAMILY_CONTROLS: dict[str, list[dict]] = {
    "omnivoice": [
        _r("omni_num_steps", "Passos (unmasking)", min_v=4, max_v=64, step=1,
           default=16, decimals=0, hint="Mais passos = melhor qualidade, mais lento"),
        _r("omni_guidance_scale", "Aderência (CFG)", min_v=0, max_v=10, step=0.1,
           default=2.0, decimals=1, hint="Alto = mais fiel ao texto/voz"),
        _r("omni_class_temperature", "Var. token", min_v=0, max_v=2, step=0.05,
           default=0.0, decimals=2, hint="0 = estável/determinístico"),
        _r("omni_position_temperature", "Var. posição", min_v=0, max_v=20, step=0.5,
           default=5.0, decimals=1, hint="Diversidade na ordem de revelação"),
        _r("omni_layer_penalty_factor", "Penalidade de camada", min_v=0, max_v=20, step=0.5,
           default=5.0, decimals=1, hint="Anti-artefato de codebook"),
        _r("omni_t_shift", "t_shift", min_v=0, max_v=1, step=0.05,
           default=0.1, decimals=2, hint="Deslocamento do cronograma de difusão"),
        _n("omni_duration_s", "Duração fixa (s)", min_v=0.5, max_v=60, step=0.5,
           default=None, placeholder="auto", hint="Vazio = automático"),
        _REF_MAX,
        # seed e instruct ficam no painel Voice design (tags + seed dedicados)
        _INSTRUCT_OMNI,
        _c("omni_denoise", "denoise — limpa ruído do áudio gerado", default=True),
        _c("omni_preprocess_prompt", "preprocess_prompt — pré-processa o texto", default=True),
        _c("omni_postprocess_output", "postprocess_output — pós-processa o áudio", default=True),
        _n("omni_audio_chunk_duration", "Chunk texto longo · duração (s)",
           min_v=1, max_v=60, step=1, default=15, section="advanced"),
        _n("omni_audio_chunk_threshold", "Chunk · limiar (s)",
           min_v=5, max_v=120, step=1, default=30, section="advanced"),
    ],
    "qwen3_tts": [
        _AR_TEMP, _AR_TOP_P, _AR_TOP_K, _AR_REP, _AR_MAX,
        _INSTRUCT_FREE,
        _REF_MAX,
    ],
    "qwen3_custom": [
        _s("gen_speaker", "Speaker preset", QWEN_SPEAKERS, default="Ryan",
           hint="Vozes embutidas do CustomVoice"),
        _AR_TEMP, _AR_TOP_P, _AR_TOP_K, _AR_REP, _AR_MAX,
        _INSTRUCT_FREE,
    ],
    "qwen3_design": [
        _AR_TEMP, _AR_TOP_P, _AR_TOP_K, _AR_REP, _AR_MAX,
        _t("omni_instruct", "Descrição da voz",
           hint="Descreva a voz em texto livre (obrigatório neste modelo)",
           placeholder="A cheerful young female voice with high pitch", rows=3),
    ],
    "fish": [
        _AR_TEMP, _AR_TOP_P, _AR_TOP_K, _AR_REP, _AR_MAX,
        _n("gen_chunk_length", "Chunk length", min_v=50, max_v=600, step=10,
           default=300, hint="Tamanho de chunk interno do Fish", section="advanced"),
        _INSTRUCT_FREE,
        _REF_MAX,
    ],
    "chatterbox": [
        _r("gen_exaggeration", "Exagero (expressividade)", min_v=0, max_v=2, step=0.05,
           default=0.5, decimals=2, hint="0 = neutro; alto = mais dramático"),
        _r("gen_cfg_weight", "CFG weight", min_v=0, max_v=1, step=0.05,
           default=0.5, decimals=2, hint="Aderência ao prompt de voz"),
        _AR_TEMP, _AR_TOP_P, _AR_REP, _AR_MAX,
        _r("gen_min_p", "Min-p", min_v=0, max_v=0.5, step=0.01,
           default=0.05, decimals=2, hint="Filtro de tokens pouco prováveis",
           section="advanced"),
        _REF_MAX,
    ],
    "kokoro": [
        _s("gen_kokoro_voice", "Voz preset", KOKORO_VOICES, default="af_heart",
           hint="Kokoro não clona amostras — só presets"),
    ],
    "pocket_tts": [
        _s("gen_pocket_voice", "Voz preset (se sem sample)", POCKET_VOICES,
           default="alba", hint="Usada quando não há gravação selecionada"),
        _AR_TEMP,
        _REF_MAX,
    ],
    "voxcpm2": [
        _r("omni_num_steps", "Timesteps (DDPM)", min_v=4, max_v=40, step=1,
           default=10, decimals=0, hint="Passos de difusão"),
        _r("omni_guidance_scale", "CFG value", min_v=0, max_v=10, step=0.1,
           default=2.0, decimals=1, hint="Classifier-free guidance"),
        _AR_MAX,
        _INSTRUCT_FREE,
        _REF_MAX,
    ],
    "voxtral_tts": [
        _s("gen_voxtral_voice", "Voz preset", VOXTRAL_VOICES, default="casual_male",
           hint="Preset se não houver sample de clone"),
        _AR_TEMP, _AR_TOP_P, _AR_TOP_K, _AR_MAX,
    ],
    "moss_nano": [
        _AR_MAX,
        _REF_MAX,
    ],
    "indextts": [
        _AR_MAX,
        _REF_MAX,
    ],
    "generic": [
        _AR_TEMP, _AR_TOP_P, _AR_REP, _INSTRUCT_FREE, _REF_MAX,
    ],
}

# UI: modo do painel de voice design
FAMILY_DESIGN_MODE: dict[str, str] = {
    "omnivoice": "omni_tags",       # dropdowns gender/age/pitch + seed
    "qwen3_tts": "free_text",       # instruct livre opcional
    "qwen3_custom": "free_text",    # emoção livre + speaker
    "qwen3_design": "free_text_required",
    "fish": "free_text",
    "chatterbox": "none",
    "kokoro": "none",
    "pocket_tts": "none",
    "voxcpm2": "free_text",
    "voxtral_tts": "none",
    "moss_nano": "none",
    "indextts": "none",
    "generic": "free_text",
}

# ---------------------------------------------------------------------------
# Catálogo de backends
# ---------------------------------------------------------------------------

BACKENDS: dict[str, dict[str, Any]] = {
    "omnivoice": {
        "label": "OmniVoice — 646 idiomas (atual)",
        "repo": "omnivoice",
        "family": "omnivoice",
        "clone": True,
        "voice_design": True,
        "langs": "646",
        "size": "~0.6B / ~2–3 GB",
        "license": "Apache-2.0",
        "notes": "Cobertura máxima. Rápido no M3 com cache de ref.",
    },
    "qwen3-0.6b": {
        "label": "Qwen3-TTS 0.6B Base — clonagem rápida",
        "repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16",
        "family": "qwen3_tts",
        "clone": True,
        "voice_design": False,
        "langs": "~10",
        "size": "0.6B / ~1.5 GB",
        "license": "Apache-2.0",
        "notes": "Melhor equilíbrio qualidade/velocidade. Clone com amostra.",
    },
    "qwen3-1.7b": {
        "label": "Qwen3-TTS 1.7B Base — qualidade alta (8-bit)",
        "repo": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
        "family": "qwen3_tts",
        "clone": True,
        "voice_design": False,
        "langs": "~10",
        "size": "1.7B / ~2 GB (8-bit)",
        "license": "Apache-2.0",
        "notes": "Mais natural que o 0.6B. 8-bit p/ estabilidade no Mac 16 GB.",
    },
    "qwen3-custom": {
        "label": "Qwen3-TTS 1.7B CustomVoice — emoção (8-bit)",
        "repo": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "family": "qwen3_custom",
        "clone": False,
        "voice_design": False,
        "langs": "~10",
        "size": "1.7B / ~2 GB (8-bit)",
        "license": "Apache-2.0",
        "notes": "Vozes preset + instruct de emoção (sem clone). 8-bit mais estável que bf16.",
        "default_speaker": "Ryan",
    },
    "qwen3-design": {
        "label": "Qwen3-TTS 1.7B VoiceDesign — descrição (8-bit)",
        "repo": "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
        "family": "qwen3_design",
        "clone": False,
        "voice_design": True,
        "langs": "~10",
        "size": "1.7B / ~2 GB (8-bit)",
        "license": "Apache-2.0",
        "notes": "Cria voz só com texto (campo instruct). Sem sample.",
    },
    "fish-s2": {
        "label": "Fish Audio S2 Pro (8-bit) — expressivo",
        "repo": "mlx-community/fish-audio-s2-pro-8bit",
        "family": "fish",
        "clone": True,
        "voice_design": True,
        "langs": "80+",
        "size": "grande / ~4–6 GB",
        "license": "Research (non-commercial)",
        "notes": "Qualidade top; tags de prosódia. Licença de pesquisa.",
    },
    "chatterbox": {
        "label": "Chatterbox TTS (8-bit) — expressivo",
        "repo": "mlx-community/Chatterbox-TTS-8bit",
        "family": "chatterbox",
        "clone": True,
        "voice_design": False,
        "langs": "EN (+ multi em v3)",
        "size": "~0.5B / ~1–2 GB",
        "license": "MIT",
        "notes": "Clone + exaggeration. Bom p/ diálogo/agentes.",
    },
    "chatterbox-turbo": {
        "label": "Chatterbox Turbo — baixa latência",
        "repo": "mlx-community/chatterbox-turbo-fp16",
        "family": "chatterbox",
        "clone": True,
        "voice_design": False,
        "langs": "EN",
        "size": "turbo / ~1–2 GB",
        "license": "MIT",
        "notes": "Mais rápido que Chatterbox full.",
    },
    "chatterbox-multi": {
        "label": "Chatterbox Multilingual v3 — ~23 idiomas",
        "repo": "mlx-community/chatterbox-multilingual-v3",
        "family": "chatterbox",
        "clone": True,
        "voice_design": False,
        "langs": "~23",
        "size": "~1–3 GB",
        "license": "MIT",
        "notes": "Clone multilíngue. Inclui PT em muitos setups.",
    },
    "kokoro": {
        "label": "Kokoro 82M — leve (sem clone) ⚠ bug MLX",
        "repo": "mlx-community/Kokoro-82M-bf16",
        "family": "kokoro",
        "clone": False,
        "voice_design": False,
        "langs": "EN (+ multi parcial)",
        "size": "82M / ~300 MB",
        "license": "Apache-2.0",
        "notes": "Vozes preset. Em mlx-audio 0.4.4 o decoder falha (broadcast_shapes).",
        "default_voice": "af_heart",
    },
    "pocket-tts": {
        "label": "PocketTTS — CPU/leve, 6 idiomas + clone",
        "repo": "mlx-community/pocket-tts",
        "family": "pocket_tts",
        "clone": True,
        "voice_design": False,
        "langs": "EN ES FR DE PT IT",
        "size": "100M / ~200 MB",
        "license": "Apache-2.0",
        "notes": "Leve; clone com ref_audio. PT suportado.",
        "default_voice": "alba",
    },
    "voxcpm2": {
        "label": "VoxCPM2 (8-bit) — design + clone",
        "repo": "mlx-community/VoxCPM2-8bit",
        "family": "voxcpm2",
        "clone": True,
        "voice_design": True,
        "langs": "~30",
        "size": "2B / ~3 GB",
        "license": "Apache-2.0",
        "notes": "Voice design + clone. 48 kHz em alguns builds.",
    },
    "voxtral-tts": {
        "label": "Mistral Voxtral TTS 4B (4-bit)",
        "repo": "mlx-community/Voxtral-4B-TTS-2603-mlx-4bit",
        "family": "voxtral_tts",
        "clone": True,
        "voice_design": False,
        "langs": "~9",
        "size": "4B / ~3–4 GB (4-bit)",
        "license": "CC BY-NC 4.0",
        "notes": "Alta qualidade; licença non-commercial. Pesado.",
    },
    "moss-nano": {
        "label": "MOSS-TTS Nano 100M — micro clone",
        "repo": "mlx-community/MOSS-TTS-Nano-100M",
        "family": "moss_nano",
        "clone": True,
        "voice_design": False,
        "langs": "EN (parcial multi)",
        "size": "100M / ~200 MB",
        "license": "Apache-2.0",
        "notes": "Ultra-leve com zero-shot. Qualidade limitada.",
    },
    "indextts2": {
        "label": "IndexTTS-2 — duração + clone",
        "repo": "mlx-community/IndexTTS-2-fp16",
        "family": "indextts",
        "clone": True,
        "voice_design": False,
        "langs": "EN ZH (+)",
        "size": "~1–3 GB",
        "license": "varia",
        "notes": "Bom p/ dublagem com controle de duração.",
    },
}

ALIASES: dict[str, str] = {
    "": "omnivoice",
    "omni": "omnivoice",
    "omnivoice-bf16": "omnivoice",
    "qwen3": "qwen3-0.6b",
    "qwen3-tts": "qwen3-0.6b",
    "fish": "fish-s2",
    "fish-audio": "fish-s2",
    "pocket": "pocket-tts",
    "voxcpm": "voxcpm2",
    "voxtral": "voxtral-tts",
    "moss": "moss-nano",
    "indextts": "indextts2",
}


def controls_for_family(family: str) -> list[dict[str, Any]]:
    return list(FAMILY_CONTROLS.get(family) or FAMILY_CONTROLS["generic"])


def design_mode_for_family(family: str) -> str:
    return FAMILY_DESIGN_MODE.get(family, "free_text")


def list_backends() -> list[dict[str, Any]]:
    out = []
    for bid, meta in BACKENDS.items():
        fam = meta["family"]
        out.append({
            "id": bid,
            "label": meta["label"],
            "repo": meta["repo"],
            "family": fam,
            "clone": bool(meta.get("clone")),
            "voice_design": bool(meta.get("voice_design")),
            "langs": meta.get("langs", "?"),
            "size": meta.get("size", "?"),
            "license": meta.get("license", "?"),
            "notes": meta.get("notes", ""),
            "controls": controls_for_family(fam),
            "design_mode": design_mode_for_family(fam),
            "precision_applies": fam == "omnivoice",
        })
    return out


def resolve_backend(model_setting: str) -> dict[str, Any]:
    raw = str(model_setting or "").strip()
    key = raw.lower()
    if key in ALIASES:
        key = ALIASES[key]
    if key in BACKENDS:
        meta = BACKENDS[key]
        fam = meta["family"]
        return {
            "id": key,
            "family": fam,
            "path": meta["repo"],
            "meta": meta,
            "is_shortcut": True,
            "controls": controls_for_family(fam),
            "design_mode": design_mode_for_family(fam),
        }
    family = _guess_family(raw)
    return {
        "id": raw,
        "family": family,
        "path": raw,
        "meta": {
            "label": raw,
            "repo": raw,
            "family": family,
            "clone": True,
            "voice_design": family in ("omnivoice", "qwen3_design", "fish", "voxcpm2"),
        },
        "is_shortcut": False,
        "controls": controls_for_family(family),
        "design_mode": design_mode_for_family(family),
    }


def _guess_family(path: str) -> str:
    p = path.lower().replace("_", "-")
    if "omnivoice" in p or "omni-voice" in p:
        return "omnivoice"
    if "voicedesign" in p or "voice-design" in p:
        return "qwen3_design"
    if "customvoice" in p or "custom-voice" in p:
        return "qwen3_custom"
    if "qwen3-tts" in p or "qwen3_tts" in p or "qwen3tts" in p:
        return "qwen3_tts"
    if "fish" in p:
        return "fish"
    if "chatterbox" in p or "chatter-box" in p:
        return "chatterbox"
    if "kokoro" in p:
        return "kokoro"
    if "pocket" in p:
        return "pocket_tts"
    if "voxcpm" in p:
        return "voxcpm2"
    if "voxtral" in p:
        return "voxtral_tts"
    if "moss" in p:
        return "moss_nano"
    if "indextts" in p or "index-tts" in p:
        return "indextts"
    return "generic"


def _to_numpy(audio) -> "Any":
    import numpy as np

    if audio is None:
        return np.zeros(0, dtype=np.float32)
    try:
        import mlx.core as mx
        if isinstance(audio, mx.array):
            # copia para host e solta a ref do tensor Metal o quanto antes
            arr = np.array(audio, dtype=np.float32, copy=True)
            del audio
            if arr.ndim > 1:
                arr = arr.reshape(-1)
            return arr
    except Exception:  # noqa: BLE001
        pass
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.reshape(-1)
    return arr


def _collect_audio(results) -> "Any":
    import numpy as np

    if results is None:
        return np.zeros(0, dtype=np.float32)
    if hasattr(results, "__iter__") and not isinstance(results, (str, bytes, dict)):
        pieces = []
        for r in results:
            a = getattr(r, "audio", r)
            pieces.append(_to_numpy(a))
            # solta refs do resultado MLX (evita reter tensores do generate)
            try:
                if hasattr(r, "audio"):
                    r.audio = None
            except Exception:  # noqa: BLE001
                pass
        if not pieces:
            return np.zeros(0, dtype=np.float32)
        out = np.concatenate(pieces)
        del pieces
        return out
    return _to_numpy(getattr(results, "audio", results))


def _lang_name(code: Optional[str]) -> str:
    m = {
        "pt": "Portuguese", "en": "English", "es": "Spanish", "fr": "French",
        "de": "German", "it": "Italian", "ja": "Japanese", "zh": "Chinese",
        "ru": "Russian", "ko": "Korean", "ar": "Arabic", "nl": "Dutch",
        "auto": "Auto", "none": "Auto", "": "Auto",
    }
    c = str(code or "auto").strip().lower()
    if c in ("none", "null", "auto", ""):
        return "Auto"
    return m.get(c, code)


def _filter_kwargs(fn, kwargs: dict) -> dict:
    """Remove kwargs que o generate() não aceita (salvo **kwargs)."""
    import inspect
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kwargs
    allowed = set(sig.parameters)
    return {k: v for k, v in kwargs.items() if k in allowed}


def _temp(o: dict, default: float = 0.8) -> float:
    """Temperatura: gen_temperature tem prioridade; fallback class_temperature (legado)."""
    if o.get("temperature") is not None:
        return float(o["temperature"])
    if o.get("class_temperature") is not None:
        t = float(o["class_temperature"])
        # legado Omni: 0 = greedy; em AR 0 quebra → default
        return t if t > 0 else default
    return default


def generate_with_backend(
    model,
    family: str,
    text: str,
    *,
    language: str = "auto",
    ref_audio: Optional[str] = None,
    ref_text: Optional[str] = None,
    ref_tokens=None,
    omni: Optional[dict] = None,
    meta: Optional[dict] = None,
) -> "Any":
    """Gera um trecho de áudio com o adapter da família. Retorna np.float32 mono."""
    o = omni or {}
    meta = meta or {}
    instruct = (o.get("instruct") or "").strip() or None
    speed = float(o.get("speed") or 1.0)
    temperature = _temp(o, 0.8 if family != "omnivoice" else 0.0)
    top_p = float(o.get("top_p") if o.get("top_p") is not None else 0.95)
    top_k = int(o.get("top_k") if o.get("top_k") is not None else 50)
    rep = float(o.get("repetition_penalty") if o.get("repetition_penalty") is not None else 1.1)
    max_tokens = int(o.get("max_tokens") or 2048)
    lang_code = language if language and str(language).lower() not in (
        "auto", "none", "null", "") else None

    if family == "omnivoice":
        seed = o.get("seed")
        if seed is not None and int(seed) >= 0:
            import mlx.core as mx
            mx.random.seed(int(seed))
        # mapa nome->código (português/english) p/ o token de idioma do OmniVoice
        _omap = {
            "português": "pt", "portugues": "pt", "portuguese": "pt",
            "inglês": "en", "ingles": "en", "english": "en",
            "espanhol": "es", "español": "es", "spanish": "es",
            "francês": "fr", "frances": "fr", "french": "fr",
            "alemão": "de", "alemao": "de", "german": "de",
            "italiano": "it", "italian": "it",
        }
        if lang_code:
            lang = _omap.get(str(lang_code).lower(), lang_code)
        else:
            lang = "None"
        # class_temperature do Omni: 0 é válido
        class_t = o.get("class_temperature", 0.0)
        if class_t is None:
            class_t = 0.0
        results = model.generate(
            text=text,
            ref_tokens=ref_tokens,
            ref_text=ref_text,
            language=lang,
            num_steps=int(o.get("num_steps") or 16),
            guidance_scale=o.get("guidance_scale", 2.0),
            class_temperature=float(class_t),
            position_temperature=o.get("position_temperature", 5.0),
            layer_penalty_factor=o.get("layer_penalty_factor", 5.0),
            t_shift=o.get("t_shift", 0.1),
            instruct=instruct or "None",
            duration_s=o.get("duration_s"),
        )
        audio = _collect_audio(results)

    elif family == "qwen3_tts":
        kwargs: dict[str, Any] = {
            "text": text, "verbose": False,
            "temperature": temperature, "top_p": top_p, "top_k": top_k,
            "repetition_penalty": rep, "max_tokens": max_tokens,
        }
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
        if ref_text:
            kwargs["ref_text"] = ref_text
        if instruct:
            kwargs["instruct"] = instruct
        if lang_code:
            kwargs["lang_code"] = lang_code
        if abs(speed - 1.0) > 1e-3:
            kwargs["speed"] = speed
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    elif family == "qwen3_custom":
        speaker = (o.get("speaker") or meta.get("default_speaker") or "Ryan")
        kwargs = {
            "text": text,
            "speaker": speaker,
            "language": _lang_name(lang_code or "en"),
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "repetition_penalty": rep,
            "max_tokens": max_tokens,
            "verbose": False,
        }
        if instruct:
            kwargs["instruct"] = instruct
        if hasattr(model, "generate_custom_voice"):
            audio = _collect_audio(model.generate_custom_voice(
                **_filter_kwargs(model.generate_custom_voice, kwargs)))
        else:
            kwargs2 = {"text": text, "voice": speaker, "verbose": False,
                       "temperature": temperature, "instruct": instruct}
            audio = _collect_audio(model.generate(
                **_filter_kwargs(model.generate, {k: v for k, v in kwargs2.items() if v is not None})))

    elif family == "qwen3_design":
        if not instruct:
            instruct = "A clear natural adult voice, moderate pitch."
        kwargs = {
            "text": text,
            "language": _lang_name(lang_code or "en"),
            "instruct": instruct,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "repetition_penalty": rep,
            "max_tokens": max_tokens,
            "verbose": False,
        }
        if hasattr(model, "generate_voice_design"):
            audio = _collect_audio(model.generate_voice_design(
                **_filter_kwargs(model.generate_voice_design, kwargs)))
        else:
            audio = _collect_audio(model.generate(
                **_filter_kwargs(model.generate, {"text": text, "instruct": instruct, "verbose": False})))

    elif family == "fish":
        kwargs = {
            "text": text, "verbose": False,
            "temperature": temperature, "top_p": top_p, "top_k": top_k,
            "repetition_penalty": rep, "max_tokens": max_tokens,
            "chunk_length": int(o.get("chunk_length") or 300),
        }
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
        if ref_text:
            kwargs["ref_text"] = ref_text
        if instruct:
            kwargs["instruct"] = instruct
        if abs(speed - 1.0) > 1e-3:
            kwargs["speed"] = speed
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    elif family == "chatterbox":
        exaggeration = o.get("exaggeration")
        if exaggeration is None:
            # legado: guidance_scale mapeava p/ exaggeration
            exaggeration = float(o.get("guidance_scale") or 0.5)
            if exaggeration > 2:
                exaggeration = exaggeration / 5.0  # era escala 0–10
        cfg_w = o.get("cfg_weight")
        if cfg_w is None:
            cfg_w = min(1.0, float(exaggeration))
        kwargs = {
            "text": text,
            "temperature": max(0.05, temperature),
            "exaggeration": float(exaggeration),
            "cfg_weight": float(cfg_w),
            "top_p": top_p,
            "repetition_penalty": rep,
            "min_p": float(o.get("min_p") if o.get("min_p") is not None else 0.05),
            "max_tokens": max_tokens,
        }
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
        if lang_code:
            kwargs["lang_code"] = lang_code
        if abs(speed - 1.0) > 1e-3:
            kwargs["speed"] = speed
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    elif family == "kokoro":
        _kmap = {
            "en": "a", "eng": "a", "english": "a",
            "pt": "p", "por": "p", "portuguese": "p", "portugues": "p",
            "es": "e", "spa": "e", "spanish": "e",
            "fr": "f", "fre": "f", "french": "f",
            "it": "i", "ita": "i", "italian": "i",
            "ja": "j", "jpn": "j", "japanese": "j",
            "zh": "z", "chi": "z", "chinese": "z",
            "hi": "h", "hin": "h", "hindi": "h",
        }
        klang = _kmap.get((lang_code or "en").lower(), "a")
        voice = o.get("kokoro_voice") or meta.get("default_voice") or "af_heart"
        kwargs = {"text": text, "voice": voice, "lang_code": klang}
        if abs(speed - 1.0) > 1e-3:
            kwargs["speed"] = speed
        try:
            audio = _collect_audio(model.generate(**kwargs))
        except Exception:
            kwargs["lang_code"] = "a"
            audio = _collect_audio(model.generate(**kwargs))

    elif family == "pocket_tts":
        kwargs = {"text": text, "temperature": temperature, "verbose": False}
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
        else:
            kwargs["voice"] = o.get("pocket_voice") or meta.get("default_voice") or "alba"
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    elif family == "voxcpm2":
        kwargs = {
            "text": text,
            "inference_timesteps": int(o.get("num_steps") or 10),
            "cfg_value": float(o.get("guidance_scale") if o.get("guidance_scale") is not None else 2.0),
            "max_tokens": max_tokens,
        }
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
            if ref_text:
                kwargs["ref_text"] = ref_text
        if instruct:
            kwargs["instruct"] = instruct
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    elif family == "voxtral_tts":
        voice = ref_audio or o.get("voxtral_voice") or "casual_male"
        kwargs = {
            "text": text, "voice": voice,
            "temperature": temperature, "top_p": top_p, "top_k": top_k,
            "max_tokens": max_tokens, "verbose": False,
        }
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    elif family == "moss_nano":
        kwargs = {"text": text, "max_tokens": max_tokens}
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
        if ref_text:
            kwargs["ref_text"] = ref_text
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    elif family == "indextts":
        kwargs = {"text": text, "verbose": False, "max_tokens": max_tokens}
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    else:
        kwargs = {"text": text, "temperature": temperature}
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
        if ref_text:
            kwargs["ref_text"] = ref_text
        if instruct:
            kwargs["instruct"] = instruct
        if lang_code:
            kwargs["lang_code"] = lang_code
        audio = _collect_audio(model.generate(**_filter_kwargs(model.generate, kwargs)))

    return _to_numpy(audio)
