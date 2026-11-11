"""Gera CLONES dos locutores do corpus com o próprio app (#521, ameaça real).

O TTS de referência do produto clona a partir de um áudio: aqui cada locutor
público vira uma voz sintética em diretório temporário (nunca `voices/`), e o app
fala três frases novas com essa voz. O clone é a ameaça que o modelo de identidade
precisa aceitar como "é ele" — o número que mais importa no card.

Uso: `./.venv-mlx/bin/python tools/bakeoff/clone.py [n_locutores]`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402

FRASES = ["A reunião de hoje mudou o plano inteiro e precisamos revisar o prazo.",
          "Me manda o resumo do que ficou combinado antes de sair, por favor.",
          "O cliente pediu mais duas semanas e o time já está com a agenda cheia."]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    comum.garante()
    sys.path.insert(0, str(comum.RAIZ_REPO))
    comum.isola_estado()
    import app

    app._settings["chunk_max_chars"] = 400
    modelo = app._get_model()
    feitos, falhas = 0, []
    for wav in sorted(comum.FALA.glob("pub_*.wav")):
        loc = wav.stem.split("_")[1]
        if (comum.CLONE / f"{loc}_0.wav").exists():
            continue
        if len({p.name.split("_")[0] for p in comum.CLONE.glob(f"{loc}_*.wav")}) >= 3:
            continue
        try:
            # a voz de referência é o próprio wav do locutor (clone de verdade):
            # `_cond_for` monta os ref_tokens a partir do arquivo
            conds = app._cond_for(modelo, loc, wav)
            for i, frase in enumerate(FRASES):
                audio = app._generate_chunk(modelo, frase, "pt", conds, None,
                                            {"num_steps": 8}, ref_audio=str(wav),
                                            sr=24000)
                x = np.asarray(audio, dtype=np.float32)
                x = app._pos_processa(x, 24000)
                sf.write(str(comum.CLONE / f"{loc}_{i}.wav"), np.clip(x, -1, 1),
                         24000, subtype="PCM_16")
                feitos += 1
        except Exception as exc:  # noqa: BLE001
            falhas.append(f"{loc}: {type(exc).__name__}: {exc}")
        if len(list(comum.CLONE.glob("*_0.wav"))) >= n:
            break
    print(f"clones: {feitos} arquivos, falhas: {falhas[:3]}")
    (comum.RESULTADOS / "clones.json").write_text(
        json.dumps({"falhas": falhas, "locutores": sorted({p.stem.split('_')[0]
                                                           for p in comum.CLONE.glob('*.wav')})}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
