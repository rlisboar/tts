# F5-TTS pt-br — 3o engine (clonagem nativa em PORTUGUÊS BRASILEIRO). Processo
# separado (venv /root/f5tts/.venv). Porta 8802. Compartilha a pasta de vozes.
import io, os, re, glob, time, asyncio
import numpy as np, torch, soundfile as sf
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from f5_tts.api import F5TTS

DEV = "cuda:0"
VOICES_DIR = "/root/omnivoice/voices"
SNAP = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--firstpixel--F5-TTS-pt-br/snapshots/*/"))[0]
CKPT = os.getenv("F5_CKPT", SNAP + "pt-br/model_last.safetensors")
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="f5")
async def offload(fn, *a): return await asyncio.get_event_loop().run_in_executor(POOL, fn, *a)

def _safe(n): return re.sub(r"[^a-zA-Z0-9_-]", "_", str(n or "").strip())[:64]
def _voice_paths(v):
    s = _safe(v)
    if not s: return None, None
    w = os.path.join(VOICES_DIR, s + ".wav"); t = os.path.join(VOICES_DIR, s + ".txt")
    return (w if os.path.exists(w) else None), (t if os.path.exists(t) else None)

print("loading F5-TTS pt-br...", flush=True)
# ode_method=midpoint -> integração de 2ª ordem (mais qualidade que euler)
MODEL = F5TTS(model="F5TTS_Base", ckpt_file=CKPT, ode_method="midpoint", device=DEV)
SR = 24000
print("ready", flush=True)

def synth(text, voice=None, ref_text=None, gen=None):
    wav_path, txt = _voice_paths(voice)
    if not wav_path:
        raise HTTPException(400, f"voz '{voice}' nao encontrada (F5 so faz clonagem; escolha uma voz salva)")
    rt = ref_text or (open(txt, encoding="utf-8").read().strip() if txt else "")
    g = gen or {}
    kw = dict(nfe_step=int(g.get("nfe_step", 32)),
              cfg_strength=float(g.get("cfg_strength", 2.0)),
              sway_sampling_coef=float(g.get("sway_sampling_coef", -1.0)),
              speed=float(g.get("speed", 1.0)),
              cross_fade_duration=float(g.get("cross_fade_duration", 0.15)),
              target_rms=float(g.get("target_rms", 0.1)),
              remove_silence=bool(g.get("remove_silence", False)))
    seed = g.get("seed")
    if seed is not None and int(seed) >= 0:
        kw["seed"] = int(seed)
    wav, sr, _ = MODEL.infer(ref_file=wav_path, ref_text=rt, gen_text=text, show_info=lambda *a, **k: None, **kw)
    a = np.asarray(wav, dtype=np.float32).squeeze()
    pk = float(np.abs(a).max()) or 1.0
    return (a / pk * 0.95).astype(np.float32), int(sr)
def wav_bytes(a, sr):
    buf = io.BytesIO(); sf.write(buf, a, sr, format="WAV", subtype="PCM_16"); return buf.getvalue()

try:
    vs = sorted(glob.glob(VOICES_DIR + "/*.wav"))
    if vs:
        t = time.time(); synth("Aquecendo o motor.", voice=os.path.basename(vs[0])[:-4]); torch.cuda.synchronize()
        print(f"warmup {time.time()-t:.1f}s", flush=True)
except Exception as e:
    print("warmup skip:", repr(e)[:140], flush=True)

app = FastAPI(title="F5-TTS pt-br", version="1.0")
class Req(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str; language: str | None = "pt"; voice: str | None = None
    ref_text: str | None = None; speed: float | None = 1.0
class SpeechReq(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = "f5-tts-ptbr"; input: str; voice: str | None = None
    language: str | None = None; response_format: str | None = "wav"; ref_text: str | None = None

@app.get("/health")
def health():
    return {"status": "ok", "engine": "f5-tts-ptbr", "ckpt": os.path.basename(CKPT),
            "gpu": torch.cuda.get_device_name(0), "vram_mb": round(torch.cuda.memory_allocated()/1024/1024, 1),
            "voices": len(glob.glob(VOICES_DIR + "/*.wav"))}
@app.get("/voices")
def voices():
    return {"voices": [{"name": os.path.basename(p)[:-4], "has_ref_text": os.path.exists(p[:-4] + ".txt")}
                       for p in sorted(glob.glob(VOICES_DIR + "/*.wav"))]}
@app.post("/tts")
async def tts(r: Req):
    t = time.time(); a, sr = await offload(synth, r.text, r.voice, r.ref_text, (r.model_extra or {})); dt = time.time() - t
    return StreamingResponse(io.BytesIO(wav_bytes(a, sr)), media_type="audio/wav",
        headers={"X-Gen-Seconds": f"{dt:.2f}", "X-RTF": f"{dt/max(len(a)/sr,1e-9):.3f}"})
@app.post("/v1/audio/speech")
async def speech(r: SpeechReq):
    if not (r.input or "").strip(): raise HTTPException(400, "input vazio")
    t = time.time(); a, sr = await offload(synth, r.input, r.voice, r.ref_text, (r.model_extra or {})); dt = time.time() - t
    return Response(content=wav_bytes(a, sr), media_type="audio/wav", headers={"X-Gen-Seconds": f"{dt:.2f}"})
