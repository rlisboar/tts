#!/usr/bin/env python3
"""Voxtral STT — servidor compatível com a API OpenAI (/v1/audio/transcriptions).

Mistral Voxtral-Small-24B (4-bit bnb) como STT de QUALIDADE em pt-BR, substituindo
o Whisper no pipeline do TTS-Rod. Roda na RTX 4090 (CUDA_VISIBLE_DEVICES=1 +
CUDA_DEVICE_ORDER=PCI_BUS_ID). O app aponta `remote_stt_base_url` p/ este servidor.

ANTI-ALUCINAÇÃO: Voxtral (LLM-ASR) DELIRA em ruído. Como ele não dá os sinais do
Whisper, o server os RECONSTRÓI e devolve num `segment` p/ o filtro do app (_stt_ok):
  • no_speech_prob ← Silero VAD (proporção sem fala). Sem fala suficiente → nem
    transcreve (economiza + bloqueia o delírio).
  • avg_logprob    ← confiança média dos tokens gerados (output_scores).
  • compression_ratio ← detecta texto repetitivo (alucinação típica).
Os limiares ficam no app: stt_max_no_speech / stt_min_logprob / stt_max_compression.

Endpoint espelha o que o app manda (_transcribe_remote): multipart `file`, `model`,
`language`, `response_format`, `beam_size` (ignorado). Versionado em
remote/voxtral_server.py — editar, ast.parse, scp p/ /root/voxtral/server.py, restart.
"""
import os
import tempfile
import time
import zlib

import librosa
import numpy as np
import soundfile as sf
import torch
import torch.nn.functional as F
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from silero_vad import get_speech_timestamps, load_silero_vad
from transformers import BitsAndBytesConfig, VoxtralForConditionalGeneration, VoxtralProcessor

REPO = os.environ.get("VOXTRAL_REPO", "mistralai/Voxtral-Small-24B-2507")
DEV = "cuda:0"   # CUDA_VISIBLE_DEVICES=1 + PCI_BUS_ID => 4090
MAX_NEW = int(os.environ.get("VOXTRAL_MAX_NEW", "512"))
MIN_SPEECH = float(os.environ.get("VOXTRAL_MIN_SPEECH", "0.2"))   # seg de fala p/ transcrever

print(f"loading Voxtral processor {REPO}...", flush=True)
processor = VoxtralProcessor.from_pretrained(REPO)
print("loading Voxtral model (4-bit nf4)...", flush=True)
_bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                          bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
model = VoxtralForConditionalGeneration.from_pretrained(
    REPO, quantization_config=_bnb, device_map={"": DEV}).eval()
print("loading Silero VAD...", flush=True)
_vad = load_silero_vad(onnx=False)
print("Voxtral ready", flush=True)

app = FastAPI(title="Voxtral STT")


def _save_tmp(raw: bytes):
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    f.write(raw); f.close()
    return f.name


def _load16(path: str):
    """Decodifica QUALQUER formato -> float32 mono @ 16 kHz. librosa usa soundfile
    (WAV/FLAC/OGG) e cai no audioread/ffmpeg p/ mp3/m4a/webm (o processor do
    Voxtral só lê WAV via soundfile -> por isso decodifico aqui)."""
    wav, _ = librosa.load(path, sr=16000, mono=True)
    return np.ascontiguousarray(wav, dtype=np.float32)


def _speech_stats(wav16):
    """(segundos de fala, proporção de fala, duração) via Silero VAD @ 16 kHz."""
    total = max(len(wav16), 1)
    ts = get_speech_timestamps(torch.from_numpy(wav16), _vad, sampling_rate=16000)
    speech = sum(s["end"] - s["start"] for s in ts)
    return speech / 16000.0, speech / total, total / 16000.0


def _compression_ratio(text: str):
    if not text:
        return 0.0
    b = text.encode("utf-8")
    return len(b) / max(len(zlib.compress(b)), 1)


def _run(wav16, language):
    """Transcreve um array float32 mono @ 16 kHz. Passar o ARRAY + format=["wav"]
    faz o processor re-encodar num buffer WAV (evita o soundfile-only do
    load_audio_as, que falha em mp3/m4a). Devolve (texto, avg_logprob greedy)."""
    lang = (language or "").strip().lower() or None
    if lang in ("auto", ""):
        lang = None
    inputs = processor.apply_transcription_request(audio=wav16, model_id=REPO, language=lang,
                                                   sampling_rate=16000, format=["wav"])
    inputs = inputs.to(DEV, dtype=torch.bfloat16)   # casta só os floats (features); ids ficam int
    n = inputs["input_ids"].shape[1]
    with torch.inference_mode():
        gen = model.generate(**inputs, max_new_tokens=MAX_NEW, do_sample=False,
                             return_dict_in_generate=True, output_scores=True)
    new = gen.sequences[0][n:]
    text = processor.batch_decode(new.unsqueeze(0), skip_special_tokens=True)[0].strip()
    lps = []
    for i, score in enumerate(gen.scores):
        if i >= len(new):
            break
        lp = F.log_softmax(score[0].float(), dim=-1)
        lps.append(lp[new[i]].item())
    avg_logprob = sum(lps) / len(lps) if lps else 0.0
    return text, avg_logprob


@app.get("/health")
def health():
    free, total = (torch.cuda.mem_get_info() if torch.cuda.is_available() else (0, 0))
    return {"status": "ok", "engine": "voxtral", "repo": REPO, "min_speech_s": MIN_SPEECH,
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
            "vram_free_mb": round(free / 1e6, 1), "vram_total_mb": round(total / 1e6, 1)}


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), model: str = Form(None),
                         language: str = Form(None), response_format: str = Form("json"),
                         beam_size: int = Form(5), temperature: float = Form(0.0),
                         prompt: str = Form(None)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "áudio vazio")
    path = _save_tmp(raw)
    try:
        wav16 = _load16(path)                       # decode robusto (qualquer formato)
        speech_s, ratio, dur = _speech_stats(wav16)
        nsp = round(1.0 - ratio, 3)
        if speech_s < MIN_SPEECH:
            # sem fala suficiente -> NÃO transcreve (Voxtral deliraria). no_speech alto
            # + logprob baixo -> _stt_ok do app rejeita ("sem fala").
            text, seg = "", {"no_speech_prob": 1.0, "avg_logprob": -5.0, "compression_ratio": 0.0}
            dt = 0.0
        else:
            t = time.time()
            text, alp = _run(wav16, language)
            dt = time.time() - t
            seg = {"no_speech_prob": nsp, "avg_logprob": round(alp, 3),
                   "compression_ratio": round(_compression_ratio(text), 3)}
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"falha na transcrição: {repr(e)[:200]}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if (response_format or "json").lower() == "text":
        return PlainTextResponse(text)
    body = {"text": text, "language": (language or "pt"), "duration": round(dur, 3), "segments": [seg]}
    return JSONResponse(body, headers={"X-Gen-Seconds": f"{dt:.2f}", "X-No-Speech": f"{seg['no_speech_prob']}"})


@app.post("/v1/audio/translations")
async def translations(file: UploadFile = File(...), model: str = Form(None),
                       response_format: str = Form("json"), temperature: float = Form(0.0),
                       prompt: str = Form(None)):
    # tradução STT->EN não é usada pelo pipeline ao vivo; transcreve e devolve.
    raw = await file.read()
    path = _save_tmp(raw)
    try:
        text, _ = _run(_load16(path), "en")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if (response_format or "json").lower() == "text":
        return PlainTextResponse(text)
    return JSONResponse({"text": text, "language": "en", "segments": []})
