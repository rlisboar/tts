"""#984 — o eco da resposta SOZINHO no portão "só a minha voz" (perfil do E2E)."""
import glob, os, sys, pathlib, json, inspect
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
repo = pathlib.Path(sys.argv[1])
os.environ["TTS_STATE_DIR"] = "/tmp/ab984/state"; os.environ["TTS_SPEAKER_AUDIO_DIR"] = "/tmp/ab984/state/speaker-audio"
sys.path.insert(0, str(repo)); import app  # noqa
cfg = {"on": True, "perfil": "Dono E2E", "rigor_far": "1.8", "limiar": 0.0}
eco = np.concatenate([resample_poly(*(lambda a, sr: (a, 16000, sr))(*sf.read(p, dtype="float32"))) for p in sorted(glob.glob("/tmp/e2e-out/*.wav"))])
for p in sorted(glob.glob("/tmp/e2e-out/*.json")):
    j = json.load(open(p)); print(pathlib.Path(p).stem, j.get("voice_id") or j.get("voice"), (j.get("text") or "")[:40])
tem = "fala_ms" in inspect.signature(app._voz_do_dono).parameters
ok = 0; n = 0; sc = []
for k in range(40):
    i = (k * 7919) % (len(eco) - 22016)
    x = eco[i:i + 22016]; x = x / (np.sqrt((x * x).mean()) + 1e-9) * 10 ** (-26 / 20)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    v = app._voz_do_dono(pcm, cfg, fala_ms=600) if tem else app._voz_do_dono(pcm, cfg)
    ok += bool(v["ok"]); n += 1; sc.append(v.get("score"))
print("eco sozinho a -26 dBFS: aceito", ok, "/", n, "score p50", np.median([s for s in sc if s is not None]))
