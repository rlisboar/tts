"""DSP do DeepFilterNet3 em numpy/scipy — sem `df`, sem `libdf`, sem torch.

O grafo ONNX traz só a rede. Em volta dele o DFN3 precisa de STFT, banco ERB,
normalização exponencial, máscara, filtro profundo e síntese. O pacote `df`
oferece isso, mas arrasta torch e depende do `libdf` (extensão Rust) — as duas
coisas que quebram na `.venv-mlx` (decisão do PM, #523: o app não depende dele).

Aqui tudo isso é reimplementado. O que foi conferido contra o `libdf`/`df`:

- **STFT idêntica**, erro relativo 0,0: janela Vorbis de 960, passo 480, rFFT/960,
  preenchimento de 480 nas DUAS pontas, quadro j começando em (j−1)·480.
- **ERB idêntico**: `10·log10(|X|² · fb)` com o mesmo banco de 32 bandas.
- **`unit_norm` idêntico**: estado = α·estado + (1−α)·|x|, saída x/√estado.
- **Filtro profundo idêntico**: preenchimento de `ordem−1−lookahead` na frente e
  `lookahead` atrás, janelas de `ordem` no tempo e soma ponderada dos
  coeficientes.

Os parâmetros estáticos (janela e banco ERB) vêm de `dfn_params.npz`, versionado —
são o resultado dos mesmos cálculos do `libdf`, guardados para não precisar dele.

Convenções que custaram caro e ficam fixas: `[canais, amostras]` na entrada e na
saída (o `synthesis` do libdf devolvia esse formato e cortar sem achatar dava
vazio), e o atraso de `fft−hop` compensado nas duas pontas.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

PARAMS = Path(__file__).with_name("dfn_params.npz")
FFT = 960
HOP = 480
NB_DF = 96
ORDEM = 5
LOOKAHEAD = 2
ALPHA = 0.99
MIN_POT = 1e-8


def carrega_params() -> dict[str, np.ndarray]:
    with np.load(PARAMS) as d:
        p = {k: d[k].astype(np.float64) for k in ("janela", "erb_fb", "erb_inv")}
    assert p["janela"].shape == (FFT,) and p["erb_fb"].shape == (FFT // 2 + 1, 32)
    return p


def stft(x: np.ndarray, janela: np.ndarray) -> np.ndarray:
    """Espectro complexo [T, F]. Preenche fft−hop nas duas pontas."""
    pad = np.pad(np.asarray(x, np.float64), (HOP, HOP))
    quadros = np.lib.stride_tricks.sliding_window_view(pad, FFT)[::HOP]
    return np.fft.rfft(quadros * janela, n=FFT, axis=1) / FFT


def istft(espectro: np.ndarray, janela: np.ndarray) -> np.ndarray:
    """Inversa exata de `stft` (WOLA). Devolve só a parte que não é borda."""
    quadros = np.fft.irfft(espectro, n=FFT, axis=1) * FFT * janela
    saida = np.zeros(quadros.shape[0] * HOP + FFT)
    peso = np.zeros_like(saida)
    for i, q in enumerate(quadros):
        ini = i * HOP
        saida[ini: ini + FFT] += q
        peso[ini: ini + FFT] += janela ** 2
    saida /= np.maximum(peso, 1e-8)
    # o primeiro quadro é a borda preenchida: o sinal começa em HOP e tem
    # (quadros − 1) · hop amostras, que é o tamanho original
    return saida[HOP: HOP + (quadros.shape[0] - 1) * HOP]


def erb_db(mag2: np.ndarray, fb: np.ndarray) -> np.ndarray:
    """Energia por banda ERB em dB. `mag2` tem F bins na última dimensão."""
    return 10 * np.log10(np.maximum(mag2 @ fb, MIN_POT))


def erb_norm(x: np.ndarray, estado: np.ndarray, alpha: float = ALPHA) -> np.ndarray:
    """Centraliza a banda em dB numa média exponencial (SUBTRAI, não divide).

    As features ERB chegam em dB; o `libdf` devolve valores perto de zero (média
    medida 0,0008), o que só fecha com subtração. `x` é [T, bandas] e o estado tem
    uma entrada por banda, então a recorrência anda no tempo."""
    saida = np.empty_like(x)
    for t in range(x.shape[0]):
        estado *= alpha
        estado += (1 - alpha) * x[t]
        saida[t] = x[t] - estado
    return saida


def unit_norm_init(bins: int) -> np.ndarray:
    """Estado inicial do `unit_norm`: 1e-3 · 0,7^i (como o libdf)."""
    return 1e-3 * 0.7 ** np.arange(bins)


def unit_norm(x: np.ndarray, estado: np.ndarray, alpha: float = ALPHA) -> np.ndarray:
    """Normalização exponencial de espectro complexo, quadro a quadro: x / √estado."""
    saida = np.empty_like(x)
    for t in range(x.shape[0]):
        estado *= alpha
        estado += (1 - alpha) * np.abs(x[t])
        saida[t] = x[t] / np.sqrt(np.maximum(estado, MIN_POT))
    return saida


def aplica_mascara(spec: np.ndarray, mascara_erb: np.ndarray, erb_inv: np.ndarray):
    """Máscara ERB espalhada para os bins e aplicada no espectro."""
    return spec * (mascara_erb @ erb_inv)


def filtro_profundo(spec: np.ndarray, coefs: np.ndarray) -> np.ndarray:
    """Filtro profundo multi-quadro nos `NB_DF` primeiros bins.

    `spec`: [T, F] complexo. `coefs`: [ordem, T, NB_DF] complexo. Preenche
    `ordem−1−lookahead` na frente e `lookahead` atrás, como o original."""
    frente = ORDEM - 1 - LOOKAHEAD
    pad = np.pad(spec[:, :NB_DF], ((frente, LOOKAHEAD), (0, 0)))
    janelas = np.lib.stride_tricks.sliding_window_view(pad, ORDEM, axis=0)  # [T, F, ordem]
    saida = np.zeros_like(spec)
    saida[:, :NB_DF] = np.einsum("tfn,ntf->tf", janelas, coefs)
    return saida
