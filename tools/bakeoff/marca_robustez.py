"""Robustez da marca d'água AudioSeal à degradação (#522).

A marca só serve se sobreviver ao caminho por onde o áudio realmente passa. Este
harness aplica as degradações do card, uma de cada vez, e mede a probabilidade de
detecção em cada uma. **Degradação que derruba a detecção é resultado, não erro:
fica no relatório.**

Degradações (cada uma isolada, a partir do áudio MARCADO):
- mp3 32k e opus 32k (idem volta para wav 16k)
- reamostragem 8k → 16k, 44,1k → 16k
- ganho -12 dB e +6 dB
- corte de um trecho do meio (perde 60 % do material)
- regravação alto-falante → microfone (o caso do eco do Live)

Além disso mede **falso positivo** em fala humana limpa (a marca não pode aparecer
onde não foi posta) — sem isso "detectou" não quer dizer nada.

Uso (na venv de bancada, onde o audioseal vive):
    ./venv312/bin/python tools/bakeoff/marca_robustez.py --limite 6
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alvo  # noqa: E402
import comum  # noqa: E402

FFMPEG = "/opt/homebrew/bin/ffmpeg"
LIMIAR = 0.5      # probabilidade ≥ 0,5 = marca detectada


def _marcador():
    """Carrega o AudioSeal (MIT) e devolve `(marca, detecta)`."""
    import torch
    from audioseal import AudioSeal

    gerador = AudioSeal.load_generator("audioseal_wm_16bits")
    detector = AudioSeal.load_detector("audioseal_detector_16bits")
    gerador.eval()
    detector.eval()

    def marca(x: np.ndarray, sr: int) -> np.ndarray:
        with torch.no_grad():
            wav = torch.from_numpy(np.asarray(x, np.float32))[None, None]
            return gerador(wav, sample_rate=sr).squeeze().numpy()

    def detecta(x: np.ndarray, sr: int) -> float:
        with torch.no_grad():
            wav = torch.from_numpy(np.asarray(x, np.float32))[None, None]
            prob, _ = detector.detect_watermark(wav, sample_rate=sr)
        return float(prob.squeeze())

    return marca, detecta


def _via_codec(x: np.ndarray, sr: int, codec: str, kbps: int, tmp: Path) -> np.ndarray:
    """Passa pelo codec e volta. É como o áudio sai do app para fora."""
    entrada, saida = tmp / "in.wav", tmp / f"out.{codec}"
    sf.write(str(entrada), x, sr, subtype="PCM_16")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(entrada),
                    "-b:a", f"{kbps}k", str(saida)], check=True)
    volta = tmp / "volta.wav"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(saida),
                    "-ar", str(sr), "-ac", "1", str(volta)], check=True)
    y, _ = sf.read(str(volta), dtype="float32")
    return y


def _reamostra(x: np.ndarray, sr: int, meio: int) -> np.ndarray:
    from scipy.signal import resample_poly

    ida = resample_poly(x, meio, sr).astype(np.float32)
    return resample_poly(ida, sr, meio).astype(np.float32)


def _ganho(x: np.ndarray, db: float) -> np.ndarray:
    return (x * 10 ** (db / 20)).astype(np.float32)


def _corta(x: np.ndarray) -> np.ndarray:
    """Mantém 40 % no meio: a marca é espalhada, deve sobreviver a um corte."""
    meio = x.size // 2
    meia = int(x.size * 0.2)
    return x[meio - meia: meio + meia]


def _regrava(pasta: Path, limite: int) -> dict[str, np.ndarray] | None:
    """Regravação alto-falante → microfone, 1 m. Devolve None se o Mac não deixar.

    Usa o próprio `afplay` (alto-falante) e o `ffmpeg avfoundation` (microfone).
    O dono precisa deixar o volume audível; é a única degradação que depende da
    sala e por isso o resultado dela é o mais honesto dos limites."""
    if subprocess.run(["which", "afplay"], capture_output=True).returncode != 0:
        return None
    try:
        mic = subprocess.run([FFMPEG, "-f", "avfoundation", "-list_devices", "true",
                              "-i", ""], capture_output=True, text=True)
        if "audio" not in mic.stderr.lower():
            return None
    except Exception:  # noqa: BLE001
        return None
    saida = {}
    for i, arquivo in enumerate(sorted(pasta.glob("marcado_*.wav"))[:limite]):
        destino = pasta / f"regravado_{i}.wav"
        gravador = subprocess.Popen(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "avfoundation",
             "-i", ":0", "-ar", "16000", "-ac", "1", "-t", "6", str(destino)],
            stdin=subprocess.DEVNULL)
        subprocess.run(["afplay", "-v", "1", str(arquivo)], check=False)
        gravador.wait()
        if destino.exists():
            y, _ = sf.read(str(destino), dtype="float32")
            saida[f"regravado_{i}"] = y
    return saida or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=6)
    a = ap.parse_args()
    comum.garante()
    tmp = comum.RESULTADOS / "_marca"
    tmp.mkdir(parents=True, exist_ok=True)
    marca, detecta = _marcador()

    por = alvo._falantes()
    falantes = [f for f, ps in sorted(por.items()) if len(ps) >= 2]
    amostras = []
    for f in falantes[: a.limite]:
        x, _ = alvo.junta(por[f], 4)
        amostras.append((f, x))

    linhas = []
    for nome, x in amostras:
        marcado = marca(x, comum.SR)
        sf.write(str(tmp / f"marcado_{nome}.wav"), marcado, comum.SR, subtype="PCM_16")
        base = detecta(marcado, comum.SR)
        casos = {"marcado": marcado, "cru": x}
        for codec, kbps in (("mp3", 32), ("opus", 32)):
            casos[codec] = _via_codec(marcado, comum.SR, codec, kbps, tmp)
        casos["resample_8k"] = _reamostra(marcado, comum.SR, 8000)
        casos["resample_44k"] = _reamostra(marcado, comum.SR, 44100)
        casos["ganho_-12"] = _ganho(marcado, -12)
        casos["ganho_+6"] = _ganho(marcado, 6)
        casos["corte_60%"] = _corta(marcado)
        linha = {"falante": nome, "marcado": round(base, 3)}
        for caso, audio in casos.items():
            linha[caso] = round(detecta(audio, comum.SR), 3)
        linhas.append(linha)
        print(f"{nome}: " + " ".join(f"{k}={v:.2f}" for k, v in linha.items()
                                    if k != "falante"), flush=True)

    regravados = _regrava(tmp, a.limite)
    if regravados:
        probs = [detecta(y[: 4 * comum.SR], comum.SR) for y in regravados.values()]
        print(f"regravação alto-falante→mic: mediana {np.median(probs):.2f} "
              f"({len(probs)} amostras)", flush=True)
    else:
        probs = None
        print("regravação: não foi possível (sem microfone/afplay acessível)", flush=True)

    colunas = [c for c in linhas[0] if c != "falante"]
    resumo = {c: round(float(np.mean([l[c] for l in linhas])), 3) for c in colunas}
    deteccao = {c: round(float(np.mean([l[c] >= LIMIAR for l in linhas])), 3)
                for c in colunas}
    print("\nresumo (média de probabilidade):", resumo, flush=True)
    print("taxa de detecção (≥ 0,5):", deteccao, flush=True)

    (comum.RESULTADOS / "marca_robustez.json").write_text(json.dumps(
        {"limiar": LIMIAR, "linhas": linhas, "resumo": resumo,
         "deteccao": deteccao,
         "regravacao_mediana": None if probs is None else round(float(np.median(probs)), 3)},
        indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
