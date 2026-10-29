"""Worker do Demucs: separa a música e devolve só a VOZ (#520, I1b).

Processo separado de propósito. O Demucs é torch; o app é MLX. Rodar torch no
processo do servidor mistura duas pilhas de Metal no mesmo processo — é o que já
derrubou o app com segfault antes — então o modelo mora aqui e o pai só lê o
resultado.

Uso: `python musica_worker.py <entrada.wav> <saida.wav>`
O áudio sai 44,1 kHz (taxa de trabalho do Demucs); quem chama reamostra.
"""

from __future__ import annotations

import sys
from pathlib import Path

SR = 44100


def main() -> int:
    if len(sys.argv) < 3:
        print("uso: musica_worker.py <entrada.wav> <saida.wav>", file=sys.stderr)
        return 2
    entrada, saida = Path(sys.argv[1]), Path(sys.argv[2])

    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    # pesos locais quando existirem (`TTS_DEMUCS_DIR`): sem isso o `get_model`
    # baixa do hub e a etapa deixa de ser offline
    import os

    repo = os.environ.get("TTS_DEMUCS_DIR") or None
    modelo = get_model("htdemucs", repo=repo)
    modelo.eval()

    from scipy.signal import resample_poly

    x, sr = sf.read(str(entrada), dtype="float32", always_2d=True)
    if sr != SR:                       # taxa de trabalho do Demucs; o pai volta
        x = resample_poly(x, SR, sr, axis=0).astype("float32")
    if x.shape[1] == 1:                # o htdemucs é ESTÉREO: mono vira 2 canais
        x = np.repeat(x, 2, axis=1)
    mistura = torch.from_numpy(x.T)                       # [canais, amostras]
    with torch.no_grad():
        saida_t = apply_model(modelo, mistura[None], split=True, overlap=0.25)[0]
    voz = saida_t[modelo.sources.index("vocals")].mean(axis=0).numpy()
    sf.write(str(saida), voz, SR, subtype="PCM_16")
    return 0


if __name__ == "__main__":
    sys.exit(main())
