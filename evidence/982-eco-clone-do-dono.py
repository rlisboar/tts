"""#982 — por que o eco "passa como dono": a voz da resposta é um clone do dono do E2E."""
import glob, os, sys, json
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
os.environ["TTS_STATE_DIR"] = "/tmp/ab984/state"; os.environ["TTS_SPEAKER_AUDIO_DIR"] = "/tmp/ab984/state/speaker-audio"
sys.path.insert(0, sys.argv[1]); import app  # noqa
cfg = {"on": True, "perfil": "Dono E2E", "rigor_far": "1.8", "limiar": 0.0}
for p in sorted(glob.glob("/tmp/e2e-out/*.wav")):
    a, sr = sf.read(p, dtype="float32"); a = resample_poly(a, 16000, sr)
    j = json.load(open(p[:-4] + ".json")); oks = []
    for k in range(0, len(a) - 22016, 8000):
        x = a[k:k + 22016]; x = x / (np.sqrt((x * x).mean()) + 1e-9) * 10 ** (-26 / 20)
        v = app._voz_do_dono((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes(), cfg, fala_ms=600)
        oks.append((v["estado"], v.get("score")))
    print(j.get("voice_id"), "ok", sum(e == "ok" for e, _ in oks), "/", len(oks), "scores ok:", [s for e, s in oks if e == "ok"], "máx recusado", max([s for e, s in oks if e != "ok"] or [None]))
d = app._speaker_load()["Dono E2E"]["vecs"]
for p in sorted(glob.glob("/tmp/e2e-out/*.wav")):
    a, sr = sf.read(p, dtype="float32"); a = resample_poly(a, 16000, sr)
    e = app._speaker_embed_ecapa_x(a[:48000]); print(os.path.basename(p), "cos arquivo×perfil", round(app._cos_sim(e, app._speaker_centroide(d)), 3))
