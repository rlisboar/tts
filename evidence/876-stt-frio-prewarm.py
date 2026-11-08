"""#876 — 1º turno frio isolado (script do A/B do prewarm): processo NOVO, com × sem `_stt_aquece`, mede o
1º e o 2º `_stt_julga` num turno de fala real. Uso: frio876.py <repo> <on|off|full> <wav>"""
import json, sys, time, pathlib
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
repo = pathlib.Path(sys.argv[1]); modo = sys.argv[2]; wav = sys.argv[3]
sys.path.insert(0, str(repo))
t_imp = time.monotonic()
import app, live_pipeline  # noqa: E402
t_imp = time.monotonic() - t_imp
app._live_voz_ligado = lambda: None
app._separacao_do_turno = lambda p: (p, None)
a, sr = sf.read(wav, dtype="float32")
a = resample_poly(a, 16000, sr)[int(0.3 * 16000):int(2.8 * 16000)]          # 2,5 s
a = a / (np.sqrt((a * a).mean()) + 1e-9) * 10 ** (-26 / 20)
pcm = (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
with app._stt_do_live():
    repo_live = app._whisper_repo()
pre = None
if modo in ("on", "full"):
    t = time.monotonic()
    if modo == "full":
        app._vad_load()                  # o lado STT do `_prewarm_app`
    live_pipeline._stt_aquece(app); pre = time.monotonic() - t
ts, txt = [], []
for _ in range(2):
    j = {}; t = time.monotonic()
    txt.append(live_pipeline._stt_julga(pcm, "pt", j)); ts.append(time.monotonic() - t)
print(json.dumps({"modo": modo, "repo": repo_live, "import_s": round(t_imp, 2),
                  "prewarm_s": pre and round(pre, 3), "turno1_s": round(ts[0], 3),
                  "turno2_s": round(ts[1], 3), "ok": all(txt)}))
