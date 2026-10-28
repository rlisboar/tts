"""DeepFilterNet3 pelo grafo ONNX, com DSP próprio em numpy (`dfn_dsp`).

O R1 elegeu o DFN3 para limpar a amostra, mas mediu-o pelo pacote `df` (torch +
`libdf`, extensão Rust). O app não pode depender disso (#523): a `.venv-mlx` não
tem `df` e o `libdf` nem existe em Python puro. Então o grafo ONNX vem do
`soniqo/DeepFilterNet3-ONNX` (MIT, export verificado do DFN3 v0.5.6) e o DSP em
volta é nosso, em numpy.

Contrato do grafo (do cartão do repositório):
    entrada  feat_erb  [1, 1, T, 32]    float32
    entrada  feat_spec [1, 2, T, 96]    float32
    saída    erb_mask  [1, 1, T, 32]    float32
    saída    df_coefs  [1, 5, T, 96, 2] float32

O áudio é 48 kHz dentro do modelo; quem chama passa 48 kHz e recebe 48 kHz.
"""

from __future__ import annotations

import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import dfn_dsp  # noqa: E402

HF = "https://huggingface.co/soniqo/DeepFilterNet3-ONNX/resolve/main"
import os

#: `TTS_DFN3_DIR` é o contrato do `models/README.md`; o default é o diretório da
#: bancada, e o app aponta a env para o `models/` dele antes de importar daqui
PASTA = Path(os.environ.get("TTS_DFN3_DIR") or comum.BASE / "modelos" / "dfn3-onnx")
ARQUIVOS = ("deepfilter.onnx", "config.json")
SR = 48000


def baixa() -> Path:
    PASTA.mkdir(parents=True, exist_ok=True)
    for nome in ARQUIVOS:
        destino = PASTA / nome
        if not destino.exists() or destino.stat().st_size < 1000:
            # o urllib da `.venv-mlx` não tem os CAs do sistema; o curl tem
            subprocess.run(["curl", "-sSL", "-o", str(destino), f"{HF}/{nome}"],
                           check=True)
    return PASTA


@lru_cache(maxsize=1)
def carrega():
    """Sessão ONNX + parâmetros de DSP. Caro; cacheado por processo."""
    import onnxruntime as ort

    pasta = baixa()
    cfg = json.loads((pasta / "config.json").read_text())
    sess = ort.InferenceSession(str(pasta / "deepfilter.onnx"),
                                providers=["CPUExecutionProvider"])
    return {"sess": sess, "params": dfn_dsp.carrega_params(), "nb_df": cfg["df_bins"]}


def limpa(audio: np.ndarray, atenuacao_db: float | None = None) -> np.ndarray:
    """Limpa ruído de um áudio 48 kHz mono. Devolve float64 do mesmo tamanho."""
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if x.size < dfn_dsp.FFT:
        return x
    c = carrega()
    janela = c["params"]["janela"]

    # `istft` já compensa o atraso do STFT e devolve exatamente o tamanho da
    # entrada — preencher/cortar de novo aqui DESALINHAVA (correlação 0,04).
    orig = x.size
    spec = dfn_dsp.stft(x, janela)
    pot = np.abs(spec) ** 2
    erb = dfn_dsp.erb_db(pot, c["params"]["erb_fb"])
    feat_erb = dfn_dsp.erb_norm(erb, dfn_dsp.erb_norm_init(erb.shape[1]))
    feat_spec = dfn_dsp.unit_norm(spec[:, : c["nb_df"]],
                                  dfn_dsp.unit_norm_init(min(c["nb_df"], spec.shape[1])))

    entradas = {
        "feat_erb": np.ascontiguousarray(feat_erb[None, None].astype(np.float32)),
        "feat_spec": np.ascontiguousarray(
            np.stack([feat_spec.real, feat_spec.imag], axis=0)[None].astype(np.float32)),
    }
    erb_mask, df_coefs = c["sess"].run(None, entradas)

    mascara = erb_mask[0, 0].astype(np.float64)
    spec_m = dfn_dsp.aplica_mascara(spec, mascara, c["params"]["erb_inv"])
    coefs = df_coefs[0][..., 0] + 1j * df_coefs[0][..., 1]   # [ordem, T, nb_df]
    spec_e = dfn_dsp.filtro_profundo(spec.copy(), coefs)
    # acima de nb_df o modelo não filtra: fica a máscara ERB
    spec_e[:, c["nb_df"]:] = spec_m[:, c["nb_df"]:]

    if atenuacao_db:
        lim = 10 ** (-abs(atenuacao_db) / 20)
        spec_e = spec * lim + spec_e * (1 - lim)
    return dfn_dsp.istft(spec_e, janela)[:orig]


def _cli() -> int:
    import argparse

    import soundfile as sf

    ap = argparse.ArgumentParser()
    ap.add_argument("entrada")
    ap.add_argument("saida")
    a = ap.parse_args()
    x, sr = sf.read(a.entrada, dtype="float32")
    if sr != SR:
        print(f"espera {SR} Hz, veio {sr}", file=sys.stderr)
        return 1
    sf.write(a.saida, limpa(x), sr, subtype="PCM_16")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
