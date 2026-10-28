"""DeepFilterNet3 pelo grafo ONNX, sem torch no caminho da inferência.

O R1 elegeu o DFN3 para limpar a amostra, mas mediu-o pelo pacote `df` (torch).
O PM pediu a mesma coisa **via ONNX**, e a medição refeita: um grafo ONNX dispensa
o binário externo e o torch no processo que atende o pedido.

O repositório `soniqo/DeepFilterNet3-ONNX` (MIT, export verificado do DFN3 v0.5.6 —
a MESMA versão do pacote instalado aqui) traz só a REDE: o consumidor tem de fazer
STFT, normalização, aplicação da máscara e do filtro profundo, e síntese. É isso
que este módulo faz, reusando o DSP do pacote `df` (que é Apache-2.0/MIT) e
trocando apenas a chamada da rede pelo onnxruntime.

Contrato do grafo (do cartão do repositório):
    entrada  feat_erb  [1, 1, T, 32]   float32
    entrada  feat_spec [1, 2, T, 96]   float32
    saída    erb_mask  [1, 1, T, 32]   float32
    saída    df_coefs  [1, 5, T, 96, 2] float32

Uso:
    import dfn_onnx
    limpo = dfn_onnx.limpa(audio_48k)          # mono float32
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

HF = "https://huggingface.co/soniqo/DeepFilterNet3-ONNX/resolve/main"
PASTA = comum.BASE / "modelos" / "dfn3-onnx"
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
    """Sessão ONNX + estado de DSP. Caro; cacheado por processo."""
    import onnxruntime as ort
    import torch
    from df.enhance import init_df
    from df.modules import erb_fb
    from df.multiframe import DF

    pasta = baixa()
    cfg = json.loads((pasta / "config.json").read_text())
    sess = ort.InferenceSession(str(pasta / "deepfilter.onnx"),
                                providers=["CPUExecutionProvider"])
    # o `init_df` também carrega os pesos torch, mas só queremos o DFState (STFT,
    # ERB e síntese); o modelo dele não é usado aqui
    _, estado, _ = init_df(log_level="ERROR", log_file=None)
    df_op = DF(num_freqs=cfg["df_bins"], frame_size=cfg["df_order"],
               lookahead=cfg["df_lookahead"])
    erb_inv = erb_fb(estado.erb_widths(), cfg["sample_rate"], inverse=True)
    return {"sess": sess, "estado": estado, "df_op": df_op, "erb_inv": erb_inv,
            "nb_df": cfg["df_bins"], "torch": torch}


def _enhance_grafo(c, spec, erb_feat, spec_feat):
    import torch

    sess = c["sess"]
    entradas = {
        "feat_erb": np.ascontiguousarray(erb_feat.numpy()),
        "feat_spec": np.ascontiguousarray(
            spec_feat.squeeze(1).permute(0, 3, 1, 2).numpy()),
    }
    erb_mask, df_coefs = sess.run(None, entradas)
    mascara = torch.from_numpy(erb_mask).matmul(c["erb_inv"]).unsqueeze(4)
    spec_m = spec * mascara
    coefs = torch.from_numpy(df_coefs)
    spec_e = c["df_op"](spec.clone(), coefs)
    # acima de nb_df o modelo não filtra: fica a máscara ERB
    spec_e[..., c["nb_df"]:, :] = spec_m[..., c["nb_df"]:, :]
    return spec_e


def limpa(audio: np.ndarray, atenuacao_db: float | None = None) -> np.ndarray:
    """Limpa ruído de um áudio 48 kHz mono. Devolve float32 do mesmo tamanho."""
    import torch
    from df.enhance import df_features

    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size < 1024:
        return x
    c = carrega()
    # mesmo compensa de atraso do `enhance` de referência: o STFT/ISTFT atrasa
    # n_fft − hop, então preenche n_fft no fim e corta a janela deslocada. Sem
    # isso a saída sai certa em nível e DESALINHADA (correlação 0,04 com o torch).
    n_fft, hop = c["estado"].fft_size(), c["estado"].hop_size()
    orig = x.size
    x = np.pad(x, (0, n_fft))
    with torch.no_grad():
        tens = torch.from_numpy(x)[None]
        spec, erb_feat, spec_feat = df_features(tens, c["estado"], c["nb_df"])
        spec_e = _enhance_grafo(c, spec, erb_feat, spec_feat)
        if atenuacao_db:
            lim = 10 ** (-abs(atenuacao_db) / 20)
            spec_e = spec * lim + spec_e * (1 - lim)
        # `synthesis` devolve [canais, amostras]; sem o squeeze o corte por
        # amostra fatiaria o eixo do canal e sairia vazio
        saida = np.asarray(c["estado"].synthesis(
            torch.view_as_complex(spec_e.squeeze(1)).numpy()), dtype=np.float32)
    saida = saida.reshape(-1)
    d = n_fft - hop
    return saida[d: orig + d]


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
