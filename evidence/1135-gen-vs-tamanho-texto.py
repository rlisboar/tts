"""#1135 — custo do `generate` contra o tamanho do texto do 1º trecho (micro-bench local, MLX).
uso: ./.venv-mlx/bin/python evidence/1135-gen-vs-tamanho-texto.py <raiz_repo>
A voz é a fixture `voz_fala_5s_16k.wav` dobrada para 10 s, num tmp único. Não importa o app; nada do dono.
9 rodadas embaralhadas, 10 passos, CFG 2.0, ref de 250 tokens."""
import json, os, sys, time, statistics as st, random
raiz = sys.argv[1]; sys.path.insert(0, raiz)
import mlx.core as mx
from mlx_audio.tts.utils import load_model
from mlx_audio.tts.models.omnivoice.utils import create_voice_clone_prompt
m = load_model(os.environ.get("TTS_OMNI_DIR") or os.path.join(raiz, ".omnivoice-bf16"))
import tempfile
import numpy as np
import soundfile as sf
with tempfile.TemporaryDirectory(prefix="1135-") as tmp:
    x, sr = sf.read(os.path.join(raiz, "tests/fixtures/voz_fala_5s_16k.wav"), dtype="float32")
    sf.write(os.path.join(tmp, "ref10.wav"), np.concatenate([x, x]), sr, subtype="PCM_16")
    full = create_voice_clone_prompt(os.path.join(tmp, "ref10.wav"), ref_text=None, tokenizer=m.audio_tokenizer, max_duration_s=10.0)
TX = {"6": "Claro,", "12": "Sim, claro.", "18": "Deixa eu ver isso.", "24": "Claro, posso te ajudar.",
      "40": "Bom, vamos por partes, porque tem mais", "70": "Bom, vamos por partes, porque tem mais de um ponto aqui, e dá para começar."}
def gera(t):
    s = time.perf_counter()
    r = list(m.generate(text=t, ref_tokens=full, ref_text=None, language="pt", num_steps=10, guidance_scale=2.0,
        class_temperature=0.0, position_temperature=5.0, layer_penalty_factor=5.0, t_shift=0.1, instruct="None"))
    mx.eval(r[0].audio); return (time.perf_counter() - s) * 1000, r[0].audio.shape[-1] / 24000
for t in TX.values(): gera(t)
am = {k: [] for k in TX}; dur = {}
random.seed(2)
for _ in range(9):
    ks = list(TX); random.shuffle(ks)
    for k in ks:
        ms, d = gera(TX[k]); am[k].append(ms); dur[k] = d
print(json.dumps({k: {"chars": len(TX[k]), "mediana_ms": round(st.median(v)), "dur_s": round(dur[k], 2)} for k, v in am.items()}, indent=1))
