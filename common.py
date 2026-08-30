"""Utilidades compartilhadas entre app.py (servidor) e tts_worker.py (processo
isolado): texto, DSP leve e montagem do modelo OmniVoice.

Antes viviam duplicadas nos dois arquivos (e já tinham divergido) — aqui é a
fonte única. Imports pesados (numpy/scipy/mlx/soundfile) são lazy: o módulo
sobe rápido no servidor e no worker.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# OmniVoice: as conversões MLX publicadas vêm quebradas — o repo "-bf16" perde
# o encoder semântico do tokenizer e o "-4bit" não quantiza no load_model.
# Montamos um dir local = backbone bf16 + audio_tokenizer COMPLETO (HuBERT),
# com symlinks para o cache do Hugging Face. Sobreponíveis por env.
# ---------------------------------------------------------------------------
OMNI_BACKBONE_REPO = os.environ.get("TTS_ROD_OMNI_BACKBONE", "mlx-community/OmniVoice-bf16")
OMNI_TOKENIZER_REPO = os.environ.get("TTS_ROD_OMNI_TOKENIZER", "mlx-community/OmniVoice")
# repo fp32 completo (backbone F32 + tokenizer) — carrega direto, sem montagem
OMNI_FP32_REPO = os.environ.get("TTS_ROD_OMNI_FP32", "mlx-community/OmniVoice-fp32")
OMNI_ASSEMBLED_NAME = ".omnivoice-bf16"
# atalhos que significam "o OmniVoice montado/default" (settings['model'])
OMNI_ALIASES = {"", "omnivoice", "omni", "omnivoice-bf16"}

# trechos parciais de jobs interrompidos; silêncio entre trechos
CHUNK_SILENCE_S = 0.25
# modelo sai com volume baixo (RMS ~0,10); normaliza para nível de fala
TARGET_RMS = 0.15
PEAK_LIMIT = 0.95

# backends que aplicam `speed` nativamente no generate() — NÃO reaplicar
# time-stretch (senão fish/chatterbox/qwen ficavam com velocidade²)
NATIVE_SPEED_FAMILIES = frozenset({
    "qwen3_tts", "qwen3_custom", "fish", "chatterbox", "kokoro",
})


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def write_json_atomic(path, payload) -> None:
    """Escrita atômica de JSON (tmp + os.replace): crash no meio da escrita não
    deixa arquivo truncado/corrompido. Use para settings, chaves e metas — um
    arquivo corrompido reseta defaults/gera chave nova silenciosamente."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)


def sanitize_text(text: str) -> str:
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


def split_text(text: str, max_chars: int = 140) -> list[str]:
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


# ---------------------------------------------------------------------------
# DSP leve (numpy/scipy lazy)
# ---------------------------------------------------------------------------

def trim_tail_silence(audio, sr: int, limiar: float = 0.006, pad_s: float = 0.3):
    """Corta cauda silenciosa (sobra típica de geração que estourou o teto)."""
    import numpy as np

    win = int(0.05 * sr)
    fim = len(audio)
    while fim > win:
        if float(np.sqrt(np.mean(audio[fim - win:fim] ** 2))) >= limiar:
            break
        fim -= win
    return audio[:min(len(audio), fim + int(pad_s * sr))]


def normalize(audio, target_rms: float = TARGET_RMS, peak_limit: float = PEAK_LIMIT):
    """Ganho LINEAR p/ o RMS alvo, mas limitado para o pico não passar de
    peak_limit. 100% transparente (sem soft-clip) — não esmaga transientes nem
    distorce. Áudio com muito transiente fica só um pouco mais baixo."""
    import numpy as np

    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return audio
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms <= 1e-6:
        return audio
    gain = target_rms / rms
    peak = float(np.abs(audio).max()) * gain
    if peak > peak_limit:
        gain *= peak_limit / peak     # teto de pico (linear, sem distorção)
    return (audio * gain).astype(np.float32)


def fade_edges(audio, sr: int, ms: float = 8.0):
    """Aplica fade-in/out curto (rampa linear) nas bordas — leva início e fim a
    zero, eliminando o 'click'/estalo de descontinuidade entre trechos. 8ms é
    inaudível na fala."""
    import numpy as np

    n = int(sr * ms / 1000.0)
    if n < 1 or audio.size < 2 * n:
        return audio
    a = np.asarray(audio, dtype=np.float32).copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    a[:n] *= ramp
    a[-n:] *= ramp[::-1]
    return a


def time_stretch(audio, speed: float, n_fft: int = 1024, hop: int = 256):
    """Muda a velocidade SEM mexer no tom (phase vocoder). speed>1 = mais rápido
    (mais curto); <1 = mais lento. Preserva TODAS as palavras — ao contrário de
    forçar duration_s, que corta slots de token e pula palavras.

    Vetorizado (equivalente à versão em laço que havia aqui, validada por
    teste): STFT por janelas deslizantes, fase acumulada com cumsum e
    overlap-add por classes de offset — áudio longo não fica mais lento.
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    x = np.asarray(audio, dtype=np.float32)
    if abs(speed - 1.0) < 1e-3 or x.size < n_fft * 2:
        return x
    win = np.hanning(n_fft).astype(np.float32)

    # STFT (bins, frames) — mesma contagem de frames da versão em laço
    D = np.fft.rfft(sliding_window_view(x, n_fft)[::hop] * win, axis=1).T
    n_bins, n_frames = D.shape
    if n_frames < 2:
        return x

    omega = 2.0 * np.pi * hop * np.arange(n_bins) / n_fft       # avanço de fase esperado/hop
    steps = np.arange(0, n_frames - 1, speed)
    ks = np.floor(steps).astype(np.int64)
    # arange com passo flutuante pode gerar último passo == n_frames-1 (+eps):
    # sem este clamp, ks+1 estoura (IndexError no meio do job — bug latente da
    # versão em laço, ex.: speed=1.4 com trecho de 0,5 s)
    ks = np.minimum(ks, n_frames - 2)
    fracs = np.clip(steps - ks, 0.0, 1.0).astype(np.float32)

    mag = (1.0 - fracs)[None, :] * np.abs(D[:, ks]) + fracs[None, :] * np.abs(D[:, ks + 1])
    dp = np.angle(D[:, ks + 1]) - np.angle(D[:, ks]) - omega[:, None]
    dp -= 2.0 * np.pi * np.round(dp / (2.0 * np.pi))            # phase unwrap
    # fase acumulada: out[:,0] usa o ângulo do frame 0; depois soma (omega+dp)
    inc = omega[:, None] + dp
    phase = np.empty_like(inc)
    phase[:, 0] = np.angle(D[:, 0])
    if inc.shape[1] > 1:
        phase[:, 1:] = np.angle(D[:, 0])[:, None] + np.cumsum(inc[:, :-1], axis=1)
    del D, inc, dp
    out = mag * np.exp(1j * phase)
    del mag, phase

    # overlap-add: n_fft % hop == 0 → frames distantes n_fft//hop NÃO se
    # sobrepõem; cada classe c cobre blocos contíguos começando em c*hop
    segs = np.fft.irfft(out, n=n_fft, axis=0).astype(np.float32) * win[:, None]
    del out
    total = (len(steps) - 1) * hop + n_fft
    y = np.zeros(total, dtype=np.float32)
    wsum = np.zeros(total, dtype=np.float32)
    w2 = (win * win).astype(np.float32)
    for c in range(n_fft // hop):
        sub = segs[:, c::n_fft // hop]
        m = sub.shape[1]
        if not m:
            continue
        start = c * hop
        y[start:start + m * n_fft] += sub.T.reshape(-1)
        wsum[start:start + m * n_fft] += np.tile(w2, m)
    del segs
    y /= np.maximum(wsum, 1e-8)
    peak = float(np.abs(y).max() or 0.0)
    if peak > 0.99:
        y *= 0.99 / peak
    return y.astype(np.float32)


def write_wav_concat(piece_dir: Path, n: int, out_path: Path, sr: int) -> float:
    """Monta o WAV final lendo trecho a trecho (sem carregar tudo na RAM).

    Retorna duração em segundos. Evita np.concatenate de N arrays float32
    (textos longos estouravam dezenas/centenas de MB de pico).
    """
    import numpy as np
    import soundfile as sf

    total_frames = 0
    with sf.SoundFile(str(out_path), mode="w", samplerate=sr, channels=1,
                      subtype="PCM_16") as out:
        for i in range(n):
            piece = piece_dir / f"{i}.wav"
            data, _ = sf.read(str(piece), dtype="float32")
            if getattr(data, "ndim", 1) > 1:
                data = data.mean(axis=1)
            out.write(np.asarray(data, dtype=np.float32))
            total_frames += len(data)
            del data
    return round(total_frames / sr, 1) if sr else 0.0


def release_mlx_memory(aggressive: bool = False):
    """Devolve pool de alocação MLX/Metal ao SO e força GC do Python.

    MLX mantém um cache de blocos GPU/unified memory; sem clear_cache a RAM
    sobe a cada trecho gerado e não desce. aggressive=True faz 2 passagens
    (pós-job / unload).
    """
    import gc

    gc.collect()
    try:
        import mlx.core as mx
        # mx.clear_cache (API atual); metal.clear_cache está deprecado
        clr = getattr(mx, "clear_cache", None)
        if clr is not None:
            clr()
    except Exception:  # noqa: BLE001
        pass
    if aggressive:
        gc.collect()
        try:
            import mlx.core as mx
            clr = getattr(mx, "clear_cache", None)
            if clr is not None:
                clr()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# OmniVoice: montagem/resolução da fonte do modelo
# ---------------------------------------------------------------------------

def assemble_omnivoice_path(base: Path, progress=None) -> str:
    """Monta (uma vez) o dir local que conserta a conversão bf16 do OmniVoice.

    backbone bf16 (sem o audio_tokenizer quebrado) + audio_tokenizer completo
    (com HuBERT) do repo sem sufixo, ligados por symlink em <base>/.omnivoice-bf16/.
    `progress(msg|None)` recebe o andamento do download (opcional).
    """
    from huggingface_hub import snapshot_download

    assembled = Path(base) / OMNI_ASSEMBLED_NAME
    pronto = ((assembled / "model.safetensors").exists()
              and (assembled / "audio_tokenizer" / "model.safetensors").exists())
    if pronto:
        return str(assembled)
    if progress:
        progress("baixando OmniVoice (backbone + tokenizer)…")
    backbone = Path(snapshot_download(OMNI_BACKBONE_REPO, ignore_patterns=["audio_tokenizer/*"]))
    tokrepo = Path(snapshot_download(OMNI_TOKENIZER_REPO, allow_patterns=["audio_tokenizer/*"]))
    assembled.mkdir(exist_ok=True)
    for f in backbone.iterdir():
        if f.name == "audio_tokenizer":
            continue
        dst = assembled / f.name
        if not dst.exists():
            os.symlink(f.resolve(), dst)
    atok = assembled / "audio_tokenizer"
    if not atok.exists():
        os.symlink((tokrepo / "audio_tokenizer").resolve(), atok)
    if progress:
        progress(None)
    return str(assembled)


def resolve_omni_source(settings: dict, base: Path, progress=None) -> str:
    """settings['omni_precision'] → repo fp32 (carrega direto) OU dir montado
    bf16. Usado por app.py e tts_worker.py."""
    if str((settings or {}).get("omni_precision", "bf16")).lower() == "fp32":
        return OMNI_FP32_REPO
    return assemble_omnivoice_path(base, progress)
