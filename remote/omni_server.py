# OmniVoice TTS + Whisper ASR + tradutor LLM — servidor REMOTO (NÃO faz parte do app do Mac).
#
# Roda numa máquina com 2 GPUs NVIDIA (CUDA), via systemd (omnivoice-tts.service):
# `uvicorn server:app --host 0.0.0.0 --port 8800`. Drop-in define CUDA_VISIBLE_DEVICES=1,0
# (CUDA_DEVICE_ORDER=PCI_BUS_ID): cuda:0 = RTX 4090 (OmniVoice+Whisper), cuda:1 = RTX 4070
# (tradutor LLM, isolado). Whisper usa device_index=0.
#
# Motores: OmniVoice (PyTorch bf16) — clone por `ref_audio` + voice design por `instruct`
# (tags válidas); faster-whisper large-v3 (float16) p/ ASR; e Qwen2.5-14B-Instruct em 4-bit
# (bitsandbytes nf4) como tradutor — carregado em thread separada na 4070.
# API OpenAI-compatível: POST /v1/audio/speech, /v1/audio/transcriptions, /v1/audio/translations,
# /v1/chat/completions (tradução); além de POST /tts simples e /voices (CRUD de clones).
# O app TTS-Rod (Mac, MLX) conecta como cliente em Configurações → Modelos remotos.
#
# mt_chat força greedy + injeta um system prompt que trava o idioma de saída (sem vazar
# para outro idioma/escrita, ex.: chinês) — corrige instabilidade do 4-bit.
#
# Requisitos no servidor: torch+cuda, omnivoice, faster-whisper, transformers, bitsandbytes,
# accelerate, fastapi, uvicorn, soundfile, numpy; um ffmpeg (/usr/local/bin/ffmpeg8); o
# modelo TTS em ./model_local; vozes clonadas em ./voices (wav + .txt de ref_text).
#
# Esta é uma cópia versionada do que roda em /root/omnivoice/server.py (v2.4).
import io, os, re, time, asyncio, tempfile, subprocess, numpy as np, torch, soundfile as sf
from concurrent.futures import ThreadPoolExecutor
torch.set_float32_matmul_precision("high")
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
torch.backends.cudnn.benchmark = True
from omnivoice import OmniVoice
from faster_whisper import WhisperModel
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
try:
    from omnivoice import OmniVoiceGenerationConfig
except Exception:
    from omnivoice.models.omnivoice import OmniVoiceGenerationConfig

FFMPEG = "/usr/local/bin/ffmpeg8"
DEV = "cuda:0"  # CUDA_VISIBLE_DEVICES=1 -> RTX 4090
VOICES_DIR = "/root/omnivoice/voices"
os.makedirs(VOICES_DIR, exist_ok=True)

# ---------- separated, env-tunable thread pools ----------
TTS_WORKERS = int(os.getenv("OMNI_TTS_WORKERS", "4"))   # GPU: OmniVoice generate
ASR_WORKERS = int(os.getenv("OMNI_ASR_WORKERS", "4"))   # GPU: faster-whisper transcribe
IO_WORKERS  = int(os.getenv("OMNI_IO_WORKERS",  "16"))  # CPU: ffmpeg transcode + file io
TTS_POOL = ThreadPoolExecutor(max_workers=TTS_WORKERS, thread_name_prefix="tts")
ASR_POOL = ThreadPoolExecutor(max_workers=ASR_WORKERS, thread_name_prefix="asr")
IO_POOL  = ThreadPoolExecutor(max_workers=IO_WORKERS,  thread_name_prefix="io")
MT_POOL  = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt")  # 1 generate por vez no LLM
async def offload(pool, fn, *a):
    return await asyncio.get_event_loop().run_in_executor(pool, fn, *a)

# ---------- TTS: OmniVoice ----------
print("loading OmniVoice...", flush=True)
m = OmniVoice.from_pretrained("/root/omnivoice/model_local", torch_dtype=torch.bfloat16,
                              device_map=DEV, attn_implementation="sdpa")
try: m.eval()
except Exception: pass
SR = int(getattr(m, "sample_rate", None) or getattr(getattr(m,'config',None),'sample_rate',None) or 24000)
try:
    if getattr(m, "llm", None) is not None:
        m.llm = torch.compile(m.llm, mode="default", fullgraph=False, dynamic=True)
        print("torch.compile enabled", flush=True)
except Exception as e:
    print("compile skip:", repr(e)[:120], flush=True)

# ---------- ASR: faster-whisper large-v3 (float16) ----------
print(f"loading Whisper large-v3 (float16, num_workers={ASR_WORKERS})...", flush=True)
asr = WhisperModel("large-v3", device="cuda", device_index=0, compute_type="float16", num_workers=ASR_WORKERS)
print("whisper ready", flush=True)

def _safe_name(name):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or "").strip())[:64]

def _voice_paths(voice):
    safe = _safe_name(voice)
    if not safe: return None, None
    wav = os.path.join(VOICES_DIR, safe + ".wav")
    txt = os.path.join(VOICES_DIR, safe + ".txt")
    return (wav if os.path.exists(wav) else None), (txt if os.path.exists(txt) else None)

_GC_FIELDS = {"num_step","guidance_scale","t_shift","layer_penalty_factor",
             "position_temperature","class_temperature","denoise",
             "preprocess_prompt","postprocess_output","audio_chunk_duration","audio_chunk_threshold"}
_GC_ALIAS = {"num_steps":"num_step","steps":"num_step"}
def _gen_config(params):
    if not params: return None
    kw={}
    for k,v in params.items():
        if v is None: continue
        kk=_GC_ALIAS.get(k,k)
        if kk in _GC_FIELDS: kw[kk]=v
    if not kw: return None
    try: return OmniVoiceGenerationConfig(**kw)
    except Exception: return None

def synth(text, language=None, speed=None, voice=None, instruct=None, ref_text=None, gen_params=None):
    kw = {"text": text, "language": language}
    if speed and abs(float(speed)-1.0) > 1e-3: kw["speed"] = float(speed)
    wav, txt = _voice_paths(voice)
    if wav:                                  # voz salva -> clonagem por referencia
        kw["ref_audio"] = wav
        rt = ref_text or (open(txt, encoding="utf-8").read().strip() if txt else "")
        if rt: kw["ref_text"] = rt
    if instruct: kw["instruct"] = instruct   # voice design textual
    # seed: voz reprodutível (mesmo instruct/seed -> mesmo timbre/sotaque). <0 = aleatório.
    seed = (gen_params or {}).get("seed")
    if seed is not None and int(seed) >= 0:
        torch.manual_seed(int(seed)); torch.cuda.manual_seed_all(int(seed))
    gc = _gen_config(gen_params)
    if gc is not None: kw["generation_config"] = gc
    try: audio = m.generate(**kw)
    except Exception: audio = m.generate(text=text, language=language)
    a = audio[0] if isinstance(audio,(list,tuple)) else audio
    a = np.asarray(a, dtype=np.float32).squeeze()
    pk = float(np.abs(a).max()) or 1.0
    return (a/pk*0.95).astype(np.float32)

def wav_bytes(a):
    buf=io.BytesIO(); sf.write(buf, a, SR, format="WAV", subtype="PCM_16"); return buf.getvalue()

def _to_wav24(b):
    p = subprocess.run([FFMPEG,"-hide_banner","-loglevel","error","-i","pipe:0","-ar","24000","-ac","1","-f","wav","pipe:1"],
                       input=b, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0: raise HTTPException(400, f"audio decode: {p.stderr.decode()[:200]}")
    return p.stdout

FMT = {"wav":("audio/wav",None),"pcm":("audio/pcm",None),
       "mp3":("audio/mpeg",["-c:a","libmp3lame","-q:a","2","-f","mp3"]),
       "opus":("audio/ogg",["-c:a","libopus","-b:a","64k","-f","ogg"]),
       "aac":("audio/aac",["-c:a","aac","-b:a","128k","-f","adts"]),
       "flac":("audio/flac",["-c:a","flac","-f","flac"])}
def encode(a, fmt):
    fmt=(fmt or "mp3").lower()
    if fmt not in FMT: raise HTTPException(400, f"unsupported response_format '{fmt}'")
    media, ff = FMT[fmt]
    if fmt=="pcm": return (np.clip(a,-1,1)*32767).astype("<i2").tobytes(), media
    if ff is None: return wav_bytes(a), media
    p=subprocess.run([FFMPEG,"-hide_banner","-loglevel","error","-i","pipe:0",*ff,"pipe:1"],
                     input=wav_bytes(a), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode!=0: raise HTTPException(500, f"ffmpeg: {p.stderr.decode()[:200]}")
    return p.stdout, media

def _ts(x, sep="."):
    h=int(x//3600); mm=int((x%3600)//60); s=x%60
    return f"{h:02d}:{mm:02d}:{s:06.3f}".replace(".",sep)
def to_srt(segs):
    return "\n".join(f"{i}\n{_ts(s['start'],',')} --> {_ts(s['end'],',')}\n{s['text'].strip()}\n" for i,s in enumerate(segs,1))
def to_vtt(segs):
    return "WEBVTT\n\n"+"\n".join(f"{_ts(s['start'])} --> {_ts(s['end'])}\n{s['text'].strip()}\n" for s in segs)
def run_asr(path, task, language, beam=5):
    segments, info = asr.transcribe(
        path, task=task, language=language, beam_size=beam,
        temperature=0.0, condition_on_previous_text=False,
        no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
        vad_filter=True,
        # menos agressivo: o cliente já corta por VAD; aqui só removemos silêncio longo.
        # threshold alto derrubava fala baixa (fim de frase, consoantes) -> palavras perdidas.
        vad_parameters=dict(threshold=0.3, min_speech_duration_ms=0, min_silence_duration_ms=700))
    segs=[{"id":i,"start":float(s.start),"end":float(s.end),"text":s.text,
           "no_speech_prob":float(getattr(s,"no_speech_prob",0.0) or 0.0),
           "avg_logprob":float(getattr(s,"avg_logprob",0.0) or 0.0),
           "compression_ratio":float(getattr(s,"compression_ratio",0.0) or 0.0)}
          for i,s in enumerate(segments)]
    return "".join(s["text"] for s in segs).strip(), segs, info

print("warmup TTS...", flush=True)
try:
    t=time.time(); synth("System warming up.", "English"); torch.cuda.synchronize()
    print(f"warmup done {time.time()-t:.1f}s", flush=True)
except Exception as e: print("warmup err:", repr(e)[:160], flush=True)

# ---------- MT: tradutor LLM (4-bit) — na 4070 ociosa (cuda:1), isolado do OmniVoice ----------
# Carrega em thread separada: o TTS sobe na hora; a tradução fica pronta ~1-2 min depois.
# Dois tradutores: 14B (qualidade, cuda:0/4090) e 7B (velocidade, cuda:1/4070).
# /v1/chat/completions roteia pelo NOME do modelo ("7b" -> rápido).
MT_REPO = os.getenv("OMNI_MT_REPO", "Qwen/Qwen2.5-14B-Instruct")
MT_DEV  = os.getenv("OMNI_MT_DEV",  "cuda:1" if torch.cuda.device_count() > 1 else "cuda:0")
MT_FAST_REPO = os.getenv("OMNI_MT_FAST_REPO", "Qwen/Qwen2.5-7B-Instruct")
MT_FAST_DEV  = os.getenv("OMNI_MT_FAST_DEV",  "cuda:1" if torch.cuda.device_count() > 1 else "cuda:0")
mt      = {"tok": None, "model": None, "ready": False, "err": None, "repo": MT_REPO, "dev": MT_DEV}
mt_fast = {"tok": None, "model": None, "ready": False, "err": None, "repo": MT_FAST_REPO, "dev": MT_FAST_DEV}
def _load_into(slot):
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                 bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
        print(f"loading MT {slot['repo']} (4-bit) on {slot['dev']}...", flush=True)
        slot["tok"] = AutoTokenizer.from_pretrained(slot["repo"])
        slot["model"] = AutoModelForCausalLM.from_pretrained(
            slot["repo"], quantization_config=bnb, device_map={"": slot["dev"]}, torch_dtype=torch.bfloat16)
        slot["model"].eval(); slot["ready"] = True
        print(f"MT ready: {slot['repo']} on {slot['dev']}", flush=True)
    except Exception as e:  # noqa: BLE001
        slot["err"] = repr(e)[:200]; print("MT load failed:", slot["err"], flush=True)
MT_SYS = ("You are a professional translation engine. Follow the user's instruction and "
          "translate into the requested target language ONLY. Render the meaning idiomatically, "
          "never word-for-word. Output ONLY the translation — no explanations, no quotes, and do "
          "NOT emit any other language or writing system (e.g. Chinese characters) than the requested target.")
def mt_chat(slot, messages, temperature=0.3, max_new_tokens=512):
    tok, model = slot["tok"], slot["model"]
    if not messages or messages[0].get("role") != "system":   # injeta trava de idioma se faltar
        messages = [{"role": "system", "content": MT_SYS}] + list(messages)
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tok(prompt, return_tensors="pt").to(model.device)
    # tradução: greedy por padrão (estável, não vaza idioma); só amostra se temp claramente alta
    sample = bool(temperature and temperature > 0.5)
    kw = dict(max_new_tokens=int(max_new_tokens), do_sample=sample,
              repetition_penalty=1.05, pad_token_id=tok.eos_token_id)
    if sample: kw.update(temperature=float(temperature), top_p=0.9)
    with torch.inference_mode():
        out = model.generate(**inputs, **kw)
    return tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
import threading as _th
_th.Thread(target=_load_into, args=(mt,), daemon=True).start()
_th.Thread(target=_load_into, args=(mt_fast,), daemon=True).start()

app = FastAPI(title="OmniVoice TTS + Whisper ASR + MT", version="2.4",
              description="OpenAI-compatible speech + transcription + chat(translation) on dual RTX, voice cloning + voice design")

class Req(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str
    language: str | None = "English"
    speed: float | None = 1.0
    voice: str | None = None
    instruct: str | None = None
    ref_text: str | None = None
class SpeechReq(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = "omnivoice"
    input: str
    voice: str | None = "alloy"
    response_format: str | None = "mp3"
    speed: float | None = 1.0
    language: str | None = None
    instruct: str | None = None
    ref_text: str | None = None
class ChatReq(BaseModel):                       # OpenAI /v1/chat/completions (tradução LLM)
    model_config = ConfigDict(extra="allow")
    model: str | None = "qwen2.5-14b-instruct"
    messages: list
    temperature: float | None = 0.3
    max_tokens: int | None = 512

@app.get("/health")
def health():
    return {"status":"ok","gpu":torch.cuda.get_device_name(0),"tts_sr":SR,"asr_model":"large-v3",
            "mt":{"repo":MT_REPO,"dev":MT_DEV,"ready":mt["ready"],"err":mt["err"]},
            "mt_fast":{"repo":MT_FAST_REPO,"dev":MT_FAST_DEV,"ready":mt_fast["ready"],"err":mt_fast["err"]},
            "pools":{"tts":TTS_WORKERS,"asr":ASR_WORKERS,"io":IO_WORKERS},
            "voices":len([f for f in os.listdir(VOICES_DIR) if f.endswith('.wav')]),
            "vram_mb":round(torch.cuda.memory_allocated()/1024/1024,1)}

@app.get("/v1/models")
def models():
    ids=["omnivoice","tts-1","gpt-4o-mini-tts","whisper-1","large-v3","qwen2.5-14b-instruct","qwen2.5-7b-instruct"]
    return {"object":"list","data":[{"id":i,"object":"model","owned_by":"local"} for i in ids]}

@app.post("/v1/chat/completions")
async def chat_completions(r: ChatReq):
    fast = "7b" in str(r.model or "").lower()        # roteia: "...7b..." -> tradutor rápido
    slot = mt_fast if fast else mt
    if not slot["ready"]:
        raise HTTPException(503, f"MT {'7B' if fast else '14B'} not ready ({slot['err'] or 'loading'})")
    msgs=[{"role":str(m.get("role","user")),"content":str(m.get("content",""))} for m in (r.messages or [])]
    if not msgs: raise HTTPException(400, "messages is required")
    t=time.time()
    text=await offload(MT_POOL, mt_chat, slot, msgs, float(r.temperature if r.temperature is not None else 0.3), int(r.max_tokens or 512))
    return {"id":"chatcmpl-local","object":"chat.completion","created":int(time.time()),
            "model": r.model or slot["repo"],
            "choices":[{"index":0,"message":{"role":"assistant","content":text},"finish_reason":"stop"}],
            "usage":{},"x_gen_seconds":round(time.time()-t,3)}

# ---------- voices: clonagem por amostra ----------
@app.post("/voices")
async def add_voice(name: str = Form(...), audio: UploadFile = File(...), ref_text: str = Form("")):
    safe = _safe_name(name) or "voz"
    b = await audio.read()
    wav = await offload(IO_POOL, _to_wav24, b)
    with open(os.path.join(VOICES_DIR, safe + ".wav"), "wb") as f: f.write(wav)
    txt = os.path.join(VOICES_DIR, safe + ".txt")
    if ref_text.strip():
        with open(txt, "w", encoding="utf-8") as f: f.write(ref_text.strip())
    elif os.path.exists(txt):
        os.unlink(txt)
    return {"ok": True, "voice": safe}

@app.get("/voices")
def list_voices():
    out=[]
    for fn in sorted(os.listdir(VOICES_DIR)):
        if fn.endswith(".wav"):
            n=fn[:-4]
            out.append({"name": n, "has_ref_text": os.path.exists(os.path.join(VOICES_DIR, n+".txt"))})
    return {"voices": out}

@app.delete("/voices/{name}")
def del_voice(name: str):
    safe = _safe_name(name)
    rm=False
    for ext in (".wav",".txt"):
        p=os.path.join(VOICES_DIR, safe+ext)
        if os.path.exists(p): os.unlink(p); rm=True
    if not rm: raise HTTPException(404, "voice not found")
    return {"ok": True}

@app.post("/tts")
async def tts(r: Req):
    t=time.time(); a=await offload(TTS_POOL, synth, r.text, r.language, r.speed, r.voice, r.instruct, r.ref_text, (r.model_extra or {})); dt=time.time()-t
    data=await offload(IO_POOL, wav_bytes, a); dur=len(a)/SR
    return StreamingResponse(io.BytesIO(data), media_type="audio/wav",
        headers={"X-Gen-Seconds":f"{dt:.3f}","X-RTF":f"{dt/max(dur,1e-9):.4f}"})

@app.post("/v1/audio/speech")
async def openai_speech(r: SpeechReq):
    if not r.input or not r.input.strip(): raise HTTPException(400, "input is required")
    t=time.time(); a=await offload(TTS_POOL, synth, r.input, r.language, r.speed, r.voice, r.instruct, r.ref_text, (r.model_extra or {})); dt=time.time()-t
    data, media = await offload(IO_POOL, encode, a, r.response_format); dur=len(a)/SR
    return Response(content=data, media_type=media,
        headers={"X-Gen-Seconds":f"{dt:.3f}","X-Audio-Seconds":f"{dur:.2f}","X-RTF":f"{dt/max(dur,1e-9):.4f}"})

async def _asr(file, task, language, response_format, beam=5):
    data = await file.read()
    suffix = os.path.splitext(file.filename or "a.wav")[1] or ".wav"
    def _write_tmp(b):
        tf=tempfile.NamedTemporaryFile(suffix=suffix, delete=False); tf.write(b); tf.close(); return tf.name
    path = await offload(IO_POOL, _write_tmp, data)
    try:
        beam = max(1, min(10, int(beam or 5)))   # 1=rápido, 5=padrão, 8+=qualidade
        text, segs, info = await offload(ASR_POOL, run_asr, path, task, language, beam)
    finally:
        try: os.unlink(path)
        except OSError: pass
    rf=(response_format or "json").lower()
    if rf=="text": return Response(text+"\n", media_type="text/plain")
    if rf=="srt":  return Response(to_srt(segs), media_type="application/x-subrip")
    if rf=="vtt":  return Response(to_vtt(segs), media_type="text/vtt")
    if rf=="verbose_json":
        return {"task":task,"language":info.language,"duration":float(info.duration),"text":text,"segments":segs}
    return {"text": text}

@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), model: str = Form("whisper-1"),
                         language: str = Form(None), prompt: str = Form(None),
                         response_format: str = Form("json"), temperature: float = Form(0.0),
                         beam_size: int = Form(5)):
    return await _asr(file, "transcribe", language, response_format, beam_size)

@app.post("/v1/audio/translations")
async def translations(file: UploadFile = File(...), model: str = Form("whisper-1"),
                       prompt: str = Form(None), response_format: str = Form("json"),
                       temperature: float = Form(0.0), beam_size: int = Form(5)):
    return await _asr(file, "translate", None, response_format, beam_size)
