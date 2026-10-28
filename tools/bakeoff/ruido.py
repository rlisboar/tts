"""Camas de ruído sintéticas e reprodutíveis (#520).

Sintéticas de propósito: nenhuma dependência de download, mesma semente = mesmo
arquivo, e cada família tem a assinatura espectral do problema real (ventoinha =
passa-baixa + tom; teclado = transientes; TV = babble com fala de fundo). Sempre
que possível a evidência aponta a diferença para gravação real como ressalva.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

SR = 48000


def _rng(semente: int) -> np.random.Generator:
    return np.random.default_rng(semente)


def branco(segundos: float, semente: int = 1) -> np.ndarray:
    return _rng(semente).standard_normal(int(segundos * SR)).astype(np.float32)


def rosa(segundos: float, semente: int = 2) -> np.ndarray:
    """Espectro 1/f por filtro de forma (Voss-McCartney simplificado)."""
    n = int(segundos * SR)
    b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
    a = [1, -2.494956002, 2.017265875, -0.522189400]
    return signal.lfilter(b, a, _rng(semente).standard_normal(n)).astype(np.float32)


def marrom(segundos: float, semente: int = 3) -> np.ndarray:
    x = np.cumsum(_rng(semente).standard_normal(int(segundos * SR)))
    return (x - signal.lfilter([1 - 0.995], [1, -0.995], x)).astype(np.float32)


def ventoinha(segundos: float, semente: int = 4) -> np.ndarray:
    """Ventoinha: passa-baixa forte + tom de rotação (o caso do dono)."""
    x = rosa(segundos, semente)
    b, a = signal.butter(4, 900 / (SR / 2), "low")
    x = signal.lfilter(b, a, x)
    t = np.arange(x.size) / SR
    return (x + 0.15 * np.sin(2 * np.pi * 118 * t)).astype(np.float32)


def ar_condicionado(segundos: float, semente: int = 5) -> np.ndarray:
    x = rosa(segundos, semente)
    b, a = signal.butter(4, 400 / (SR / 2), "low")
    x = signal.lfilter(b, a, x)
    t = np.arange(x.size) / SR
    # ciclo de compressor: modulação lenta de amplitude
    mod = 1.0 + 0.3 * np.sin(2 * np.pi * 0.2 * t)
    return (x * mod + 0.05 * np.sin(2 * np.pi * 50 * t)).astype(np.float32)


def teclado(segundos: float, semente: int = 6) -> np.ndarray:
    """Transientes curtos e aleatórios (digitação) sobre piso baixo."""
    n = int(segundos * SR)
    x = 0.02 * _rng(semente).standard_normal(n)
    rng = _rng(semente + 1)
    for ini in rng.integers(0, n - 2000, size=int(segundos * 7)):
        dur = int(rng.integers(120, 900))
        t = np.arange(dur) / SR
        clique = np.exp(-t * 60) * np.sin(2 * np.pi * rng.uniform(1500, 5000) * t)
        x[ini:ini + dur] += 0.6 * clique[: max(0, min(dur, n - ini))]
    return x.astype(np.float32)


def musica(segundos: float, semente: int = 7) -> np.ndarray:
    """Acorde sustentado com vibrato e percussão leve — o caso do import."""
    t = np.arange(int(segundos * SR)) / SR
    x = np.zeros_like(t)
    for f in (196.0, 246.94, 293.66, 392.0):
        x += np.sin(2 * np.pi * f * t) * (1 + 0.1 * np.sin(2 * np.pi * 5 * t))
    pulso = (np.sin(2 * np.pi * 2 * t) > 0.9).astype(np.float64)
    x = x * (0.8 + 0.2 * pulso)
    return (0.25 * x).astype(np.float32)


def rua(segundos: float, semente: int = 8) -> np.ndarray:
    x = marrom(segundos, semente)
    b, a = signal.butter(2, 2000 / (SR / 2), "low")
    return signal.lfilter(b, a, x).astype(np.float32)


def babble(segundos: float, semente: int = 9) -> np.ndarray:
    """Fala de fundo: várias vozes sintéticas desafinadas somadas."""
    t = np.arange(int(segundos * SR)) / SR
    x = np.zeros_like(t)
    rng = _rng(semente)
    for _ in range(6):
        f0 = rng.uniform(90, 220)
        env = 0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(0.7, 2.5) * t + rng.uniform(0, 6))
        x += env * sum(np.sin(2 * np.pi * f0 * k * t) / k for k in (1, 2, 3))
    return (0.1 * x).astype(np.float32)


def reverb(x: np.ndarray, segundos: float = 0.35, semente: int = 10) -> np.ndarray:
    """Reverb por convolução com cauda exponencial."""
    rng = _rng(semente)
    n = int(segundos * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.25 * n))
    ir[0] = 1.0
    return signal.fftconvolve(x, ir)[: x.size].astype(np.float32)


FAMILIAS = {
    "branco": branco, "rosa": rosa, "marrom": marrom, "ventoinha": ventoinha,
    "ar_condicionado": ar_condicionado, "teclado": teclado, "musica": musica,
    "rua": rua, "babble": babble,
}


def normaliza_rms(x: np.ndarray, alvo_db: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(np.float64(x) ** 2))) or 1e-9
    return (x * (10 ** (alvo_db / 20) / rms)).astype(np.float32)
