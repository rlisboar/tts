"""#979 — coorte PT-BR (FLEURS pt_br test, 40 locutores, 2 trechos de 3 s) para o ECAPA — MEDIÇÃO, não artefato."""
import glob, json, sys, pathlib, hashlib
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
repo = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(repo))
import app  # noqa: E402
SR = 16000; T = 3 * SR; vecs = []; usados = []
for w in sorted(glob.glob("/tmp/tts-bakeoff/fala_pt/*.wav")):
    a, sr = sf.read(w, dtype="float32")
    a = resample_poly(a if a.ndim == 1 else a.mean(1), SR, sr).astype(np.float32)
    for i in (0, T):
        t = a[i:i + T]
        if len(t) < T:
            t = np.resize(a, T)           # repete a fala, nunca zerofill (protocolo do #526)
        vecs.append(app._speaker_embed_ecapa_x(t))
    usados.append({"arquivo": pathlib.Path(w).name, "sha256": hashlib.sha256(open(w, "rb").read()).hexdigest()})
m = np.array(vecs, dtype=np.float32)
out = pathlib.Path(sys.argv[2])
np.save(out.with_suffix(".npy"), m)
out.with_suffix(".json").write_text(json.dumps({"modelo": "models/ecapa-voxceleb", "dimensao": int(m.shape[1]),
    "vetores": int(m.shape[0]), "locutores": len(usados), "dur_s": 3.0, "por_locutor": 2,
    "corpus": "FLEURS pt_br test (CC BY 4.0) /tmp/tts-bakeoff/fala_pt", "fala_usada": usados}, indent=1))
print(m.shape)
