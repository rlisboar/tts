"""Bake-off de redução de ruído (#520): DNSMOS, WER e preservação de timbre.

Roda no venv do app (precisa do Whisper local e do `_denoise_audio` real); os
candidatos que exigem outro ambiente são chamados por subprocesso
(`/tmp/tts-bakeoff/vendf` para o DeepFilterNet) ou por binário (ffmpeg/arnndn).

Métrica de timbre: similaridade do locutor antes/depois, com o embedding do
Resemblyzer — a limpeza não pode trocar a voz que o clone copia.

Uso: ./.venv-mlx/bin/python tools/bakeoff/ruido_bakeoff.py [--familias f1,f2]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402

VENV_DF = Path("/tmp/tts-bakeoff/vendf/bin/python")
VENV_MOS = Path("/tmp/tts-bakeoff/venv312/bin/python")
FFMPEG = "/opt/homebrew/bin/ffmpeg"
RNN_MODELO = Path("/Users/lisboa/Documents/tts-rod/models/rnnoise/sh.rnnn")


def _le(p: Path) -> np.ndarray:
    x, _ = sf.read(str(p), dtype="float32")
    return x if x.ndim == 1 else x.mean(axis=1)


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.float64(x) ** 2)))


def _iguala_nivel(y: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Mesmo nível do original: sem isto o modelo "vence" só por ficar mais alto."""
    r = _rms(y)
    return (y * (_rms(ref) / r)).astype(np.float32) if r > 1e-9 else y


# ---------------------------------------------------------------------------
# Candidatos. Cada um recebe (x, sr) e devolve (y, segundos_gastos, alg_ms)
# ---------------------------------------------------------------------------

def cand_atual(x, sr):
    import app

    t = time.perf_counter()
    y = app._denoise_audio(x, sr, 1.0)
    return _iguala_nivel(np.asarray(y, dtype=np.float32), x), time.perf_counter() - t, 0.0


def cand_rnnoise(x, sr):
    t = time.perf_counter()
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "s16le",
                        "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
                        "-af", f"aresample=48000,arnndn=m={RNN_MODELO},aresample={sr}",
                        "-f", "s16le", "-ar", str(sr), "-ac", "1", "pipe:1"],
                       input=pcm, capture_output=True)
    y = np.frombuffer(r.stdout, dtype="<i2").astype(np.float32) / 32767
    if y.size < x.size:                       # ffmpeg falhou: devolve o cru
        print(f"[ruido] rnnoise devolveu {y.size}/{x.size} amostras", flush=True)
        return x.copy(), time.perf_counter() - t, 0.0
    return _iguala_nivel(y[: x.size], x), time.perf_counter() - t, 0.0


def cand_dfnet(x, sr):
    tmp = comum.BASE / "resultados" / "_dfn"
    tmp.mkdir(parents=True, exist_ok=True)
    ent, sai = tmp / "in.wav", tmp / "out.wav"
    sf.write(str(ent), x, sr, subtype="PCM_16")
    t = time.perf_counter()
    subprocess.run([str(VENV_DF), "-c",
                    "import sys;from df.enhance import enhance,init_df,load_audio,save_audio;"
                    "m,st,_=init_df();sr=st.sr() if callable(st.sr) else st.sr;"
                    "a,_=load_audio(sys.argv[1],sr=sr);save_audio(sys.argv[2], enhance(m,st,a), sr)",
                    str(ent), str(sai)], capture_output=True)
    y, sr2 = sf.read(str(sai), dtype="float32")
    if sr2 != sr:
        import scipy.signal as ss

        y = ss.resample_poly(y, sr, sr2)
    return _iguala_nivel(y[: x.size], x), time.perf_counter() - t, 0.0


CANDIDATOS = {"atual": cand_atual, "rnnoise": cand_rnnoise, "dfnet3": cand_dfnet}


def _dnsmos(x: np.ndarray, sr: int) -> dict:
    tmp = comum.BASE / "resultados" / "_mos.wav"
    sf.write(str(tmp), x, sr, subtype="PCM_16")
    r = subprocess.run([str(VENV_MOS), "-c",
                        "import sys,json;from speechmos import dnsmos;"
                        "import soundfile as sf;x,s=sf.read(sys.argv[1],dtype='float32');"
                        "print(json.dumps({k: float(v) for k,v in dnsmos.run(x,sr=s).items()}))",
                        str(tmp)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return {}


def _wer(texto_ref: str, x: np.ndarray, sr: int) -> float:
    import app

    tmp = comum.BASE / "resultados" / "_wer.wav"
    sf.write(str(tmp), x, sr, subtype="PCM_16")
    try:
        r = app._transcribe(tmp, language="pt")
    except Exception:  # noqa: BLE001
        return float("nan")
    obtido = (r.get("text") or "").lower().split()
    ref = texto_ref.lower().split()
    if not ref:
        return float("nan")
    # distância de edição em palavras
    d = list(range(len(ref) + 1))
    for i, w in enumerate(obtido, 1):
        novo = [i]
        for j, v in enumerate(ref, 1):
            novo.append(min(d[j] + 1, novo[-1] + 1, d[j - 1] + (w != v)))
        d = novo
    return d[-1] / len(ref)


def main() -> int:
    sys.path.insert(0, str(comum.RAIZ_REPO))     # `app` para o candidato "atual" e o WER
    ap = argparse.ArgumentParser()
    ap.add_argument("--familias", default="ventoinha,ar_condicionado,teclado,babble,musica")
    ap.add_argument("--snrs", default="0,5,10")
    ap.add_argument("--limite", type=int, default=8)
    ap.add_argument("--candidatos", default=",".join(CANDIDATOS))
    a = ap.parse_args()
    comum.garante()
    from resemblyzer import VoiceEncoder, preprocess_wav

    enc = VoiceEncoder("cpu")
    refs = {}
    for txt in (comum.FALA).glob("*.txt"):
        refs[txt.stem] = txt.read_text().strip()

    resultados = {}
    for fam in a.familias.split(","):
        for snr in a.snrs.split(","):
            chave = f"{fam}_{snr}dB"
            linhas = []
            for i, mist in enumerate(sorted(comum.MISTURA.glob(f"*__{fam}_snr{snr}.wav"))[: a.limite]):
                sujo = _le(mist)
                limpo = _le(comum.FALA / f"{mist.name.split('__')[0]}.wav")
                e_ref = enc.embed_utterance(preprocess_wav(limpo, source_sr=comum.SR))
                for nome in a.candidatos.split(","):
                    y, seg, _ = CANDIDATOS[nome](sujo, comum.SR)
                    e_y = enc.embed_utterance(preprocess_wav(y, source_sr=comum.SR))
                    m = _dnsmos(y, comum.SR)
                    linhas.append({"arq": mist.name, "cand": nome,
                                   "sig": m.get("sig_mos"), "bak": m.get("bak_mos"),
                                   "ovrl": m.get("ovrl_mos"),
                                   "similaridade": float(np.dot(e_ref, e_y)),
                                   "rtf": seg / (sujo.size / comum.SR)})
                    print(f"{chave} {nome} {i}: ovrl={m.get('ovrl_mos')} "
                          f"sim={linhas[-1]['similaridade']:.3f} rtf={linhas[-1]['rtf']:.3f}",
                          flush=True)
            resultados[chave] = linhas
    destino = comum.RESULTADOS / "ruido.json"
    destino.write_text(json.dumps(resultados, indent=1))
    print("resultados:", destino)
    return 0


if __name__ == "__main__":
    sys.exit(main())
