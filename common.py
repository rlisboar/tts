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
import tempfile
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

# pausa entre trechos, POR PONTUAÇÃO (segundos). Antes era um vão fixo de 0,25 s
# mais 0,3 s de pad no trim (550 ms) mesmo onde havia vírgula: pausa de fim de
# frase no meio da frase (#319).
PAUSA_SEG = {",": 0.15, ";": 0.17, ":": 0.17, ".": 0.40, "!": 0.42, "?": 0.42, "…": 0.42}
PAUSA_PADRAO_S = 0.15          # corte seco (sem pontuação) = pausa de vírgula
# cauda que o trim deixa no FIM de cada trecho, só para o fade não comer a fala
PAD_CAUDA_S = 0.08
# legado: quem ainda monta um silêncio fixo usa este
CHUNK_SILENCE_S = 0.25
# modelo sai com volume baixo (RMS ~0,10); normaliza para nível de fala
TARGET_RMS = 0.15
PEAK_LIMIT = 0.95
# loudness integrada do ARQUIVO FINAL (EBU R128). O RMS por trecho iguala os
# trechos entre si; este alvo ancora o job inteiro numa referência de verdade e é
# aplicado como UM ganho sobre o WAV montado (#302).
LUFS_ALVO = -18.0
LUFS_TOLERANCIA = 1.0
LUFS_GANHO_MAX_DB = 12.0

# backends que aplicam `speed` nativamente no generate() — NÃO reaplicar
# time-stretch (senão fish/chatterbox/qwen ficavam com velocidade²)
NATIVE_SPEED_FAMILIES = frozenset({
    "qwen3_tts", "qwen3_custom", "fish", "chatterbox", "kokoro",
})


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def write_json_atomic(path, payload) -> None:
    """Escrita atômica de JSON (tmp único + os.replace): crash no meio da escrita
    não deixa arquivo truncado/corrompido. Use para settings, chaves e metas — um
    arquivo corrompido reseta defaults/gera chave nova silenciosamente.

    O tmp é ÚNICO por escritor (`tempfile.mkstemp` no MESMO diretório, para o
    `os.replace` continuar atômico). Com nome fixo (`x.json.tmp`), dois
    escritores concorrentes no mesmo arquivo se atropelavam: A criava o tmp, B
    sobrescrevia o mesmo tmp, A substituía o destino (o tmp sumia) e B estourava
    FileNotFoundError -> 500. Acontece de verdade aqui: 6 agentes rodando a suíte
    em paralelo disputando o settings.json REAL do repo.
    """
    path = Path(path)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        # mkstemp cria 0600; preserva o modo do destino (senão settings.json
        # virava 0600 no primeiro write e .apikeys.json idem)
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    finally:
        # erro no meio (disco cheio, chmod/replace falho): não deixa .tmp órfão
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# FX de saída (EQ 3 bandas + ganho) — compartilhado entre app e worker
# ---------------------------------------------------------------------------

def biquad(kind: str, f0: float, gain_db: float, sr: int, q: float = 0.707):
    """Coeficientes RBJ (1 seção SOS) p/ shelf/peaking EQ."""
    import numpy as np

    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / sr
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2.0 * q)
    if kind == "peak":
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    elif kind == "lowshelf":
        s = 2.0 * np.sqrt(A) * alpha
        b0 = A * ((A + 1) - (A - 1) * cw + s); b1 = 2 * A * ((A - 1) - (A + 1) * cw); b2 = A * ((A + 1) - (A - 1) * cw - s)
        a0 = (A + 1) + (A - 1) * cw + s; a1 = -2 * ((A - 1) + (A + 1) * cw); a2 = (A + 1) + (A - 1) * cw - s
    else:  # highshelf
        s = 2.0 * np.sqrt(A) * alpha
        b0 = A * ((A + 1) + (A - 1) * cw + s); b1 = -2 * A * ((A - 1) + (A + 1) * cw); b2 = A * ((A + 1) + (A - 1) * cw - s)
        a0 = (A + 1) - (A - 1) * cw + s; a1 = 2 * ((A - 1) - (A + 1) * cw); a2 = (A + 1) - (A - 1) * cw - s
    return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]


def apply_audio_fx(audio, sr: int, g_low: float = 0.0, g_mid: float = 0.0,
                   g_high: float = 0.0, gain_db: float = 0.0):
    """EQ 3 bandas (grave 150 Hz / médio 1.5 kHz / agudo 5 kHz) + ganho de
    saída. Limiter de segurança só se o resultado empurrar além de 0 dBFS.
    App (settings) e worker (cfg do job) chamam com os valores explícitos."""
    import numpy as np
    from scipy.signal import sosfilt

    if max(abs(g_low), abs(g_mid), abs(g_high), abs(gain_db)) < 0.05:
        return audio
    y = np.asarray(audio, dtype=np.float32)
    bands = []
    if abs(g_low) >= 0.05:
        bands.append(biquad("lowshelf", 150.0, g_low, sr))
    if abs(g_mid) >= 0.05:
        bands.append(biquad("peak", 1500.0, g_mid, sr, 1.0))
    if abs(g_high) >= 0.05:
        bands.append(biquad("highshelf", 5000.0, g_high, sr))
    for sos in bands:
        y = sosfilt(np.array([sos], dtype=np.float64), y).astype(np.float32)
    if abs(gain_db) >= 0.05:
        y = y * (10.0 ** (gain_db / 20.0))
    peak = float(np.abs(y).max() or 0.0)
    if peak > 0.97:                       # só clipa se o usuário pediu ganho demais
        y = (0.97 * np.tanh(y / 0.97)).astype(np.float32)
    return y.astype(np.float32)


def pontua_corte(chunk: str, ultimo: bool = False) -> str:
    """Fecha o trecho SEM apagar a pontuação que o texto já trazia.

    Corte por vírgula/dois-pontos/ponto-e-vírgula mantém a marca (o modelo
    entrega contorno de CONTINUAÇÃO); corte seco no meio da frase ganha vírgula e
    só o último trecho fecha com ponto. Antes todo corte recebia um `.` e saía
    `…na ordem:.` / `…os custos,.` — contorno de fim de frase no meio da frase,
    medido em 2 dos 4 trechos (#319).
    """
    c = (chunk or "").rstrip()
    if not c:
        return chunk
    if c[-1] in ".!?…,;:":
        return c
    return c.rstrip(",") + ("." if ultimo else ",")


def pausa_apos(fim: str) -> float:
    """Silêncio (s) a inserir depois de um trecho, pela pontuação dele."""
    return PAUSA_SEG.get((fim or " ")[-1], PAUSA_PADRAO_S)


#: teto de caracteres por segundo para o teste de truncamento. A fala real medida
#: na auditoria (#284) dá 15,4 chars/s, e o portão antigo aceitava até 45 (quase
#: 3× o natural — cortar 2/3 de um trecho passava). 25 = ~1,6× o medido: aceita a
#: variação normal e reprova o que só caberia se o modelo tivesse falado rápido
#: demais. Calibrado pelos dois pontos do aceite: 40% da duração natural (38,5
#: chars/s) dispara, 90% (17,1 chars/s) não.
CPS_MINIMO = 25.0
RMS_MINIMO = 0.01
#: abaixo disto o OUTPUT é considerado baixo demais e vira aviso (rms do arquivo
#: final, depois do ganho de loudness): é o sinal de regressão tipo phase vocoder
#: 15× mais baixo, que antes só aparecia ouvindo (#323)
RMS_AVISO = 0.02


def motivo_anomalia(audio, sr: int, chunk: str, speed: float = 1.0) -> str:
    """Por que a geração descarrilou ("" = aprovado).

    OmniVoice é masked-diffusion não-AR (sem teto de tokens nem EOS frágil): a
    duração é estimada internamente e varia mais legitimamente, então só
    truncamento grosseiro e áudio inaudível pedem nova tentativa. Com speed>1
    o time-stretch encurta o áudio — o limiar acompanha, senão velocidade alta
    falsifica 'truncamento' e dispara retry (com jitter de seed) à toa.

    Não-finito tem motivo PRÓPRIO de propósito: `nan < 0.01` é **False**, então
    NaN/Inf passava como áudio bom, o `normalize` propagava, o PCM16 gravava
    ZEROS e o job terminava `done` com arquivo mudo e sem retry (#303). O motivo
    sai no log e no meta — degradação não pode ser silenciosa.

    Vive no `common` porque o worker de lote também julga os trechos dele (#320).
    """
    import numpy as np

    a = np.asarray(audio)
    if a.size == 0:
        return "vazio"
    if not np.isfinite(a).all():
        return "não-finito (NaN/Inf)"
    rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
    if not np.isfinite(rms) or rms < RMS_MINIMO:
        return f"inaudível (rms {rms:.4f})"
    if len(a) / sr < len(chunk) / CPS_MINIMO / max(1.0, float(speed or 1.0)):
        return f"truncado ({len(a) / sr:.2f}s p/ {len(chunk)} chars)"
    return ""


def sanitize_text(text: str) -> str:
    """Limpeza leve para o tokenizer multilíngue (Qwen3) do OmniVoice.

    O Qwen3 lida com acentos, pontuação rica e colchetes (tags não-verbais como
    [laughter]); só normalizamos forma Unicode, expandimos símbolos com leitura
    natural e garantimos pontuação terminal estável.
    """
    # acentos digitados em forma decomposta (NFD, comum no macOS) viram o composto
    text = unicodedata.normalize("NFC", text)
    # "S/N" tem leitura própria e não é "ou" genérico
    text = re.sub(r"\bS\s*/\s*N\b", "sim ou não", text)
    # barra entre DÍGITOS é "por" ("24/7", "3/4"), não "ou" (#329)
    text = re.sub(r"(\d)\s*/\s*(\d)", r"\1 por \2", text)
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


def _limiter(y, teto: float):
    """Comprime só a ponta acima do joelho, saturando em `teto`.

    Abaixo do joelho (70% do teto) o sinal passa intacto; acima, a curva tem
    derivada 1 no joelho e vai assintoticamente até o teto — o transiente é
    dobrado e o nível do trecho não muda. Satura em `teto` (não em 1.0), para o
    teto ser de verdade.
    """
    import numpy as np

    joelho = 0.7 * teto
    over = np.abs(y) > joelho
    if not over.any():
        return y
    largura = max(1e-6, teto - joelho)
    mag = np.abs(y)
    mag = np.where(over, joelho + largura * np.tanh((mag - joelho) / largura), mag)
    return (np.sign(y) * np.minimum(mag, teto)).astype(np.float32)


def normalize(audio, target_rms: float = TARGET_RMS, peak_limit: float = PEAK_LIMIT):
    """Ganho LINEAR para o RMS alvo. O teto de pico é LIMITER, não ganho.

    Dois defeitos medidos na auditoria #284:

    1. `nan < 1e-6` é **False**: um trecho com NaN mantinha o RMS NaN, o ganho
       virava NaN e o PCM16 gravava ZEROS — arquivo mudo com job `done` (#303).
       Sanea o não-finito antes de medir.
    2. O ganho era REDUZIDO quando o pico estourava o teto
       (`gain *= peak_limit / peak`), ou seja, trecho de crista alta era
       normalizado pelo PICO e não pelo nível: saía mais baixo que os vizinhos e
       o envelope invertia (medido: 7,5 dB de espalhamento entre trechos virando
       1,9 dB, com a saudação curta ficando a MAIS BAIXA do parágrafo). Agora o
       ganho depende só do RMS e quem segura o transiente é o `_limiter` (#302).
    """
    import numpy as np

    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return audio
    fin = np.isfinite(audio)
    if not fin.all():
        pico = float(np.abs(audio[fin]).max()) if fin.any() else 0.0
        pico = pico or target_rms
        audio = np.nan_to_num(audio, nan=0.0, posinf=pico,
                              neginf=-pico).astype(np.float32)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if not np.isfinite(rms) or rms <= 1e-6:
        return audio
    y = (audio * (target_rms / rms)).astype(np.float32)
    teto = min(peak_limit, 0.99)
    y = _limiter(y, teto)
    # O limiter tira nível justamente dos trechos de crista alta — sem compensar,
    # eles continuariam mais baixos que os outros (só que agora por 1-2 dB, em vez
    # da inversão inteira). Recomposição de ganho em 2 passadas: devolve o que o
    # limiter comeu, com o teto fixo.
    for _ in range(2):
        r_atual = float(np.sqrt(np.mean(y ** 2)))
        if r_atual <= 1e-6:
            break
        razao = target_rms / r_atual
        if abs(20.0 * float(np.log10(razao))) < 0.1:
            break
        y = _limiter((y * razao).astype(np.float32), teto)
    return y.astype(np.float32)


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


def atempo_chain(speed: float) -> str:
    """Cadeia de filtros atempo do ffmpeg (cada instância aceita 0.5–2.0)."""
    fatores = []
    speed = float(speed)
    while speed > 2.0:
        fatores.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        fatores.append(0.5)
        speed /= 0.5
    fatores.append(speed)
    return ",".join(f"atempo={f:g}" for f in fatores)


_AVISO_PV = {"dito": False}   # avisa uma vez que o stretch caiu no phase vocoder


def _ffmpeg_stretch(x, speed: float, sr: int):
    """Time-stretch via ffmpeg atempo (WSOLA) — muito melhor que phase vocoder
    em fala:     preserva transientes, sem smearing de fase (som metálico/robótico)."""
    import subprocess

    import imageio_ffmpeg
    import numpy as np

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-loglevel", "error",
           "-f", "f32le", "-ar", str(int(sr)), "-ac", "1", "-i", "pipe:0",
           "-filter:a", atempo_chain(speed),
           "-f", "f32le", "-ar", str(int(sr)), "-ac", "1", "pipe:1"]
    p = subprocess.run(cmd, input=np.asarray(x, dtype=np.float32).tobytes(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"ffmpeg atempo falhou: {p.stderr.decode(errors='replace')[:120]}")
    y = np.frombuffer(p.stdout, dtype=np.float32).copy()
    return y if y.size else x


def time_stretch(audio, speed: float, sr: int | None = None, n_fft: int = 1024, hop: int = 256):
    """Muda a velocidade SEM mexer no tom. speed>1 = mais rápido (mais curto);
    <1 = mais lento. Preserva TODAS as palavras — ao contrário de forçar
    duration_s, que corta slots de token e pula palavras.

    Com `sr`, usa o atempo do ffmpeg (WSOLA — qualidade bem melhor em fala;
    o OmniVoice não tem speed nativa e dependia disto). Sem `sr`/sem ffmpeg,
    cai no phase vocoder vetorial (fallback).

    Vetorizado (equivalente à versão em laço anterior, validada por teste).
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    x = np.asarray(audio, dtype=np.float32)
    if abs(speed - 1.0) < 1e-3 or x.size == 0:
        return x
    if sr:
        try:
            return _ffmpeg_stretch(x, speed, sr)
        except Exception as e:  # noqa: BLE001 — degrada p/ PV em vez de falhar o job
            # avisa UMA vez: sem isto o fallback silencioso escondia um venv sem
            # imageio-ffmpeg e a fala saía baixa (o PV perde nível — ver abaixo)
            if not _AVISO_PV["dito"]:
                print(f"[stretch] ffmpeg indisponível ({str(e)[:80]}) — usando phase vocoder",
                      flush=True)
                _AVISO_PV["dito"] = True
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
    # O PV perde nível: as magnitudes saem interpoladas entre quadros enquanto a
    # fase vem do acumulador, e a OLA cancela parte do sinal (~15x mais baixo em
    # fala). Renormaliza pelo RMS de entrada — sem isto a fala sai "muda" quando
    # o caminho do ffmpeg não está disponível (venv sem imageio-ffmpeg).
    rms_in = float(np.sqrt(np.mean(x ** 2)))
    rms_out = float(np.sqrt(np.mean(y ** 2)))
    if rms_in > 1e-6 and rms_out > 1e-6:
        y *= rms_in / rms_out
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


# ---------------------------------------------------------------------------
# Loudness do arquivo final (EBU R128) — UM ganho por job (#302)
# ---------------------------------------------------------------------------

def _ffmpeg_cmd() -> str:
    """Binário do ffmpeg: PATH primeiro, senão o do imageio-ffmpeg."""
    import shutil

    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def loudness_lufs(caminho) -> float | None:
    """Loudness integrada (LUFS) do arquivo. None = não deu para medir.

    Silêncio puro sai como `-inf`: o parser devolve None e o chamador não mexe.
    """
    import subprocess

    try:
        p = subprocess.run(
            [_ffmpeg_cmd(), "-hide_banner", "-nostats", "-i", str(caminho),
             "-af", "ebur128=peak=true", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300)
    except Exception:  # noqa: BLE001 — sem ffmpeg, sem medida
        return None
    import re as _re
    achados = _re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", p.stderr or "")
    return float(achados[-1]) if achados else None


def normaliza_loudness(caminho, alvo: float = LUFS_ALVO,
                       tolerancia: float = LUFS_TOLERANCIA,
                       limite_db: float = LUFS_GANHO_MAX_DB) -> float | None:
    """Aplica UM ganho constante no arquivo inteiro até a loudness bater no alvo.

    Ganho constante (não compressor) preserva o envelope que o modelo produziu no
    job: é o par do `normalize` por trecho, que só iguala os trechos entre si.
    Devolve o ganho aplicado em dB; `None` = não mediu, arquivo intocado.

    `alvo` já dentro da tolerância → 0.0 (registrado, sem reescrever o arquivo).
    """
    import subprocess
    from pathlib import Path

    caminho = Path(caminho)
    atual = loudness_lufs(caminho)
    if atual is None:
        return None
    delta = alvo - atual
    if abs(delta) <= tolerancia:
        return 0.0
    delta = max(-limite_db, min(limite_db, delta))

    fd, tmp_nome = tempfile.mkstemp(dir=str(caminho.parent),
                                    prefix=caminho.name + ".", suffix=".lufs")
    os.close(fd)
    tmp = Path(tmp_nome)
    try:
        p = subprocess.run(
            [_ffmpeg_cmd(), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(caminho), "-af", f"volume={delta:.2f}dB",
             "-c:a", "pcm_s16le", "-f", "wav", str(tmp)],
            capture_output=True, timeout=300)
        if p.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return None
        os.replace(tmp, caminho)
    finally:
        tmp.unlink(missing_ok=True)
    return round(delta, 2)
