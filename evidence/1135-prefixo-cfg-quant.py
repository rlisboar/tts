"""#1135 — de onde vem o custo do `tts_gen` do OmniVoice (micro-bench, MLX, 1 voz sintética).

Mede a geração de um texto de ~18 chars (o tamanho do 1º trecho do Live) variando UMA
coisa por vez: comprimento do prefixo de referência, CFG, passos e quantização do
backbone. Voz de referência = a fixture `tests/fixtures/voz_fala_5s_16k.wav` dobrada
para 10 s (serve ao CUSTO, que só depende do comprimento; não mede timbre).

uso (raiz do repo, venv do app):   [TTS_OMNI_DIR=…/.omnivoice-bf16] ./.venv-mlx/bin/python evidence/1135-prefixo-cfg-quant.py [bits]
  bits = 0 (bf16, padrão) | 8 | 4  → quantiza o backbone como `app._quantize_backbone`
Estado: não importa o app; nada do dono. Ordem das condições embaralhada, 9 rodadas,
1 aquecimento por condição.
"""
import json
import os
import random
import statistics as st
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
bits = int(sys.argv[1]) if len(sys.argv) > 1 else 0

import mlx.core as mx  # noqa: E402
import mlx.nn as nn  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from mlx_audio.tts.models.omnivoice.utils import create_voice_clone_prompt  # noqa: E402
from mlx_audio.tts.utils import load_model  # noqa: E402

# `.omnivoice-bf16` é gitignored: num worktree aponte `TTS_OMNI_DIR` para o da raiz
m = load_model(os.environ.get("TTS_OMNI_DIR") or str(RAIZ / ".omnivoice-bf16"))
if bits:
    nn.quantize(m.backbone, group_size=64, bits=bits, class_predicate=lambda p, mod: (
        getattr(mod, "weight", None) is not None and mod.weight.ndim >= 2
        and mod.weight.shape[-1] % 64 == 0))
with tempfile.TemporaryDirectory(prefix="1135-") as tmp:
    x, sr = sf.read(str(RAIZ / "tests/fixtures/voz_fala_5s_16k.wav"), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    ref10 = Path(tmp) / "ref10.wav"
    sf.write(str(ref10), np.concatenate([x, x]), sr, subtype="PCM_16")
    full = create_voice_clone_prompt(str(ref10), ref_text=None,
                                     tokenizer=m.audio_tokenizer, max_duration_s=10.0)
print("ref completo", tuple(full.shape), "bits", bits, flush=True)
REFTXT = "Esta é uma frase de referência com cerca de dez segundos de fala para o teste."


def gera(ref, steps, cfg, rt=None):
    t = time.perf_counter()
    r = list(m.generate(text="Claro, posso ajudar.", ref_tokens=ref, ref_text=rt, language="pt",
                        num_steps=steps, guidance_scale=cfg, class_temperature=0.0,
                        position_temperature=5.0, layer_penalty_factor=5.0, t_shift=0.1,
                        instruct="None"))
    mx.eval(r[0].audio)
    return (time.perf_counter() - t) * 1000


conds = [(f"ref{s:g}s", full[: int(s * 25)] if int(s * 25) < full.shape[0] else full, 10, 2.0, None)
         for s in (10.0, 6.0, 4.0, 3.0, 2.0)]
conds += [("ref10s+reftext", full, 10, 2.0, REFTXT), ("ref4s+reftext", full[:100], 10, 2.0, REFTXT),
          ("ref10s cfg0", full, 10, 0.0, None), ("ref4s cfg0", full[:100], 10, 0.0, None),
          ("ref10s 8passos", full, 8, 2.0, None), ("ref10s 5passos", full, 5, 2.0, None),
          ("ref10s 16passos", full, 16, 2.0, None)]
amostras = {c[0]: [] for c in conds}
for c in conds:
    gera(*c[1:])
random.seed(1)
for _ in range(9):
    ordem = conds[:]
    random.shuffle(ordem)
    for c in ordem:
        amostras[c[0]].append(gera(*c[1:]))
print(json.dumps({"bits": bits, **{k: {"mediana_ms": round(st.median(v)), "min": round(min(v)),
                                       "max": round(max(v)), "n": len(v)}
                                   for k, v in amostras.items()}}, indent=1))
