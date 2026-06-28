# Qwen3-TTS — servidor de CLONAGEM (4090), processo separado do OmniVoice.
# venv isolado /root/qwen3tts/.venv (transformers 4.57). Porta 8801. Compartilha
# a pasta de vozes do OmniVoice -> o MESMO clone funciona nos 2 engines.
import io, os, re, glob, time, asyncio
import numpy as np, torch, soundfile as sf
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from qwen_tts import Qwen3TTSModel

DEV = "cuda:0"
VOICES_DIR = "/root/omnivoice/voices"
REPO = os.getenv("QWEN_TTS_REPO", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qtts")  # AR: 1 geração por vez
async def offload(fn, *a): return await asyncio.get_event_loop().run_in_executor(POOL, fn, *a)

LANGS = {"pt": "Portuguese", "en": "English", "es": "Spanish", "fr": "French", "de": "German",
         "it": "Italian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ru": "Russian"}
def _lang(x):
    x = str(x or "").strip()
    if x.lower() in LANGS: return LANGS[x.lower()]
    for v in LANGS.values():
        if v.lower() == x.lower(): return v
    return "English"
def _safe(n): return re.sub(r"[^a-zA-Z0-9_-]", "_", str(n or "").strip())[:64]
def _voice_paths(v):
    s = _safe(v)
    if not s: return None, None
    w = os.path.join(VOICES_DIR, s + ".wav"); t = os.path.join(VOICES_DIR, s + ".txt")
    return (w if os.path.exists(w) else None), (t if os.path.exists(t) else None)

print("loading Qwen3-TTS...", flush=True)
MODEL = Qwen3TTSModel.from_pretrained(REPO, device_map=DEV, dtype=torch.bfloat16, attn_implementation="sdpa")
SR = 24000
print("ready", flush=True)

def synth(text, language=None, voice=None, ref_text=None, gen=None):
    wav_path, txt = _voice_paths(voice)
    if not wav_path:
        raise HTTPException(400, f"voz '{voice}' nao encontrada (Qwen3-TTS so faz clonagem; escolha uma voz salva)")
    rt = ref_text or (open(txt, encoding="utf-8").read().strip() if txt else "")
    g = gen or {}
    kw = {}  # kwargs do generate (HF)
    for k in ("temperature", "top_p", "top_k", "max_new_tokens", "repetition_penalty"):
        if g.get(k) is not None:
            kw[k] = g[k]
    if g.get("do_sample") is not None:        # False = greedy (determinístico)
        kw["do_sample"] = bool(g["do_sample"])
    xvec = bool(g.get("x_vector_only_mode", False))       # clona só pelo x-vector (rápido, menos fiel)
    nonstream = bool(g.get("non_streaming_mode", False))  # geração não-streaming (qualidade)
    out = MODEL.generate_voice_clone(text=text, language=_lang(language), ref_audio=wav_path, ref_text=rt,
                                     x_vector_only_mode=xvec, non_streaming_mode=nonstream, **kw)
    wav, sr = out if isinstance(out, tuple) else (out, SR)
    w = wav[0] if (hasattr(wav, "__len__") and not isinstance(wav, np.ndarray)) else wav
    a = np.asarray(w, dtype=np.float32).squeeze()
    pk = float(np.abs(a).max()) or 1.0
    return (a / pk * 0.95).astype(np.float32), int(sr)
def wav_bytes(a, sr):
    buf = io.BytesIO(); sf.write(buf, a, sr, format="WAV", subtype="PCM_16"); return buf.getvalue()

try:
    vs = sorted(glob.glob(VOICES_DIR + "/*.wav"))
    if vs:
        t = time.time(); synth("Aquecendo.", "Portuguese", voice=os.path.basename(vs[0])[:-4]); torch.cuda.synchronize()
        print(f"warmup {time.time()-t:.1f}s", flush=True)
except Exception as e:
    print("warmup skip:", repr(e)[:140], flush=True)

app = FastAPI(title="Qwen3-TTS clone", version="1.0")
class Req(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str; language: str | None = "Portuguese"; voice: str | None = None
    ref_text: str | None = None; speed: float | None = 1.0
class SpeechReq(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = "qwen3-tts"; input: str; voice: str | None = None
    language: str | None = None; response_format: str | None = "wav"; ref_text: str | None = None

@app.get("/health")
def health():
    return {"status": "ok", "engine": "qwen3-tts", "repo": REPO, "gpu": torch.cuda.get_device_name(0),
            "vram_mb": round(torch.cuda.memory_allocated() / 1024 / 1024, 1),
            "voices": len(glob.glob(VOICES_DIR + "/*.wav"))}
@app.get("/voices")
def voices():
    return {"voices": [{"name": os.path.basename(p)[:-4], "has_ref_text": os.path.exists(p[:-4] + ".txt")}
                       for p in sorted(glob.glob(VOICES_DIR + "/*.wav"))]}
@app.post("/tts")
async def tts(r: Req):
    t = time.time(); a, sr = await offload(synth, r.text, r.language, r.voice, r.ref_text, (r.model_extra or {})); dt = time.time() - t
    return StreamingResponse(io.BytesIO(wav_bytes(a, sr)), media_type="audio/wav",
        headers={"X-Gen-Seconds": f"{dt:.2f}", "X-RTF": f"{dt/max(len(a)/sr,1e-9):.3f}"})
@app.post("/v1/audio/speech")
async def speech(r: SpeechReq):
    if not (r.input or "").strip(): raise HTTPException(400, "input vazio")
    t = time.time(); a, sr = await offload(synth, r.input, r.language, r.voice, r.ref_text, (r.model_extra or {})); dt = time.time() - t
    return Response(content=wav_bytes(a, sr), media_type="audio/wav", headers={"X-Gen-Seconds": f"{dt:.2f}"})
