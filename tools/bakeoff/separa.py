"""Separação de duas falas (MossFormer2_SS_16K) + seleção do fluxo do alvo.

Por que existe: fala de terceiro **sobreposta** ao alvo não é ruído para um
denoiser e não some com seleção por janela — a janela inteira tem as duas vozes
(`evidence/532-bakeoff-locutor-alvo-PARCIAL.md`). Só um modelo de separação tira
o concorrente de dentro da janela.

Os dois fluxos saem sem ordem: quem diz qual é o do dono é o extrator do R2
(ECAPA), e é ele que escolhe aqui. Sem essa escolha a saída seria aleatória.

Licença: código Apache-2.0 (ClearerVoice-Studio) e pesos Apache-2.0
(`alibabasglab/MossFormer2_SS_16K`, tag `license:apache-2.0`) — linha no
`LICENCAS.md`. Nada disso entra em `requirements.txt` nesta fase: roda na venv de
bancada (/tmp/tts-bakeoff/venv312), não na .venv-mlx do app.

Uso:
    import separa
    sep = separa.carrega()            # baixa/vendoriza na 1ª vez
    fluxos = separa.separa(sep, mistura)
    alvo = separa.escolhe(fluxos, ref_do_dono, eca)
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402

MODELOS = comum.BASE / "vendf" / "mf2_ss"
HF = "https://huggingface.co/alibabasglab/MossFormer2_SS_16K/resolve/main"
GH = ("https://raw.githubusercontent.com/modelscope/ClearerVoice-Studio/main"
      "/clearvoice/clearvoice/models/mossformer2_ss")
ARQUIVOS = ["__init__.py", "conv_module.py", "fsmn.py", "layer_norm.py",
            "mossformer2.py", "mossformer2_block.py"]
CHECKPOINT = "last_best_checkpoint.pt"

#: argumentos que o `ClearerVoice` passa para a rede (defaults do parser deles);
#: conferidos contra as formas do checkpoint (enc 512×1×16, output 512×512×1)
ARGS = {"num_spks": 2, "encoder_kernel_size": 16, "encoder_embedding_dim": 512,
        "mossformer_sequence_dim": 512, "num_mossformer_layer": 24}


def garante(verbose: bool = False) -> Path:
    """Vendoriza código e pesos se faltarem (670 MB, só na primeira vez)."""
    MODELOS.mkdir(parents=True, exist_ok=True)
    for f in ARQUIVOS:
        destino = MODELOS / f
        if destino.exists() and destino.stat().st_size:
            continue
        if verbose:
            print(f"baixando {f}", flush=True)
        urllib.request.urlretrieve(f"{GH}/{f}", destino)
    ck = MODELOS / CHECKPOINT
    if not ck.exists() or ck.stat().st_size < 100_000_000:
        if verbose:
            print(f"baixando {CHECKPOINT} (670 MB)", flush=True)
        urllib.request.urlretrieve(f"{HF}/{CHECKPOINT}", ck)
    return MODELOS


def carrega(modelos: Path | None = None, dispositivo: str | None = None):
    """Modelo em eval. `strict=True`: se o checkpoint não casar, grita.

    Dispositivo: `TTS_SEPARA_DISP` (padrão MPS quando existe). A diferença não é
    cosmética — em CPU este modelo dá RTF 6–9 s por segundo de áudio, em MPS 0,31.
    CPU continua como queda, com o custo declarado."""

    import torch
    from types import SimpleNamespace

    modelos = modelos or garante()
    # o código vendorizado usa import relativo (`from .mossformer2_block import ...`),
    # então o pacote tem de ser importado pelo diretório PAI
    sys.path.insert(0, str(modelos.parent))
    from mf2_ss.mossformer2 import MossFormer2_SS_16K  # type: ignore[import-not-found]

    rede = MossFormer2_SS_16K(SimpleNamespace(**ARGS)).model
    ck = torch.load(modelos / CHECKPOINT, map_location="cpu", weights_only=False)
    rede.load_state_dict(ck["model"], strict=True)
    disp = dispositivo or os.environ.get("TTS_SEPARA_DISP") or (
        "mps" if torch.backends.mps.is_available() else "cpu")
    rede.eval().to(disp)
    return rede


def _dispositivo(modelo) -> str:
    """Onde o modelo está. MPS não aceita entrada em CPU (erro de dispositivo)."""
    try:
        return str(next(modelo.parameters()).device)
    except StopIteration:
        return "cpu"


def separa(modelo, mistura: np.ndarray, janela_s: float = 1.0) -> list[np.ndarray]:
    """Devolve `num_spks` fluxos na mesma taxa de amostragem do app (16 kHz).

    Segue o decodificador de referência do ClearerVoice (`utils/decode.py`,
    `decode_one_audio_mossformer2_ss_16k`): janela de 1 s, passo de 0,75 s e
    descarte de (janela−passo)/2 nas duas bordas de cada bloco. A primeira versão
    aqui usava blocos de 6 s com janela de Hann e saía com SI-SDRi NEGATIVO — o
    modelo foi treinado nesse enquadramento e não em blocos longos.

    A saída ainda é reescalada para o RMS da entrada, como no original: sem isso o
    nível do fluxo fica solto e a comparação com a mistura vira loteria."""
    import torch

    x = np.asarray(mistura, dtype=np.float32)
    janela = int(janela_s * comum.SR)
    passo = int(janela * 0.75)
    mexer = (janela - passo) // 2
    rms_in = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    if x.size < janela:
        x = np.pad(x, (0, janela - x.size))
    elif x.size < janela + passo:
        x = np.pad(x, (0, janela + passo - x.size))
    elif (x.size - janela) % passo:
        x = np.pad(x, (0, x.size - (x.size - janela) // passo * passo))

    saida = [np.zeros(x.size, dtype=np.float32) for _ in range(ARGS["num_spks"])]
    with torch.no_grad():
        pos = 0
        while pos + janela <= x.size:
            bloco = torch.from_numpy(x[pos: pos + janela])[None]
            if _dispositivo(modelo) != "cpu":
                bloco = bloco.to(_dispositivo(modelo))
            fluxos = modelo(bloco)
            for i, f in enumerate(fluxos):
                f = f[0].detach().cpu().numpy()
                if pos == 0:
                    saida[i][pos: pos + janela - mexer] = f[:-mexer]
                else:
                    saida[i][pos + mexer: pos + janela - mexer] = f[mexer:-mexer]
            pos += passo
    for i in range(len(saida)):
        rms = float(np.sqrt(np.mean(saida[i].astype(np.float64) ** 2)))
        if rms > 0:
            saida[i] = (saida[i] / rms * (rms_in or 1.0)).astype(np.float32)
    return [s[: x.size] for s in saida]


def escolhe(fluxos: list[np.ndarray], ref: np.ndarray, eca) -> tuple[int, list[float]]:
    """Índice do fluxo mais parecido com a referência do dono, e os escores.

    O ECAPA já é o extrator vencedor do R2; aqui ele só ordena dois candidatos, o
    que é bem mais fácil do que decidir "é ou não é ele" (§ limite honesto na
    evidência)."""
    tmp = comum.BASE / "resultados" / "_esc"
    tmp.mkdir(parents=True, exist_ok=True)
    import soundfile as sf

    caminhos = [tmp / "ref.wav", *[tmp / f"f{i}.wav" for i in range(len(fluxos))]]
    sf.write(str(caminhos[0]), ref, comum.SR, subtype="PCM_16")
    for i, f in enumerate(fluxos):
        sf.write(str(caminhos[i + 1]), f, comum.SR, subtype="PCM_16")
    embs = eca(caminhos)
    e_ref = embs[0]
    escores = [float(e @ e_ref) for e in embs[1:]]
    return int(np.argmax(escores)), escores


def _cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("entrada")
    ap.add_argument("--saida", default=str(comum.RESULTADOS / "_separa"))
    a = ap.parse_args()
    import soundfile as sf

    x, sr = sf.read(a.entrada, dtype="float32")
    x = x if x.ndim == 1 else x.mean(axis=1)
    fluxos = separa(carrega(), x)
    Path(a.saida).mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(fluxos):
        sf.write(f"{a.saida}/fluxo{i}.wav", f, sr, subtype="PCM_16")
    print(f"{len(fluxos)} fluxos em {a.saida}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
