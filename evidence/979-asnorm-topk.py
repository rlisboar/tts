"""#979 — mesma fala, mesma coorte: só muda o K do AS-norm (app top-10 × R2 top-50)."""
import glob, sys, pathlib, json
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
repo = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(repo))
import app  # noqa: E402
SR = 16000; T = 3 * SR; mesmo = {"b835c54c36": "1e8e1479ae"}
loc = {}
for w in sorted(glob.glob(str(repo / "voices/*.wav"))):
    k = pathlib.Path(w).stem
    if k.startswith("vd-"): continue
    a, sr = sf.read(w, dtype="float32")
    a = resample_poly(a if a.ndim == 1 else a.mean(1), SR, sr).astype(np.float32)
    loc.setdefault(mesmo.get(k, k), []).extend(np.array(app._speaker_embed_ecapa_x(a[i:i+T])) for i in range(0, len(a)-T+1, T))
coortes = {"en(models/)": np.load(repo / "models/asnorm-coorte-ecapa.npy"), "pt(FLEURS)": np.load("/tmp/ab869/coorte-pt/asnorm-coorte-ecapa-pt.npy")}
def nota(e, cen, coh, K):
    s = float(e @ cen); top = np.sort(coh @ e)[-K:]
    return (s - top.mean()) / (top.std() + 1e-6)
res = {}
for nome, coh in coortes.items():
    coh = coh / np.linalg.norm(coh, axis=1, keepdims=True)
    for K in (10, 50):
        gen, imp = [], []
        for k, embs in loc.items():
            if len(embs) < 4: continue
            cen = np.mean([x / np.linalg.norm(x) for x in embs[:3]], 0); cen /= np.linalg.norm(cen)
            gen += [nota(e / np.linalg.norm(e), cen, coh, K) for e in embs[3:]]
            imp += [nota(e / np.linalg.norm(e), cen, coh, K) for k2, v in loc.items() if k2 != k for e in v]
        gen, imp = np.array(gen), np.array(imp)
        res[f"{nome} K={K}"] = {"FAR@2.434_%": round(100*float((imp>=2.434).mean()),2), "FAR@1.8_%": round(100*float((imp>=1.8).mean()),2),
            "FRR@2.434_%": round(100*float((gen<2.434).mean()),2), "imp_p99": round(float(np.quantile(imp,.99)),3), "imp_max": round(float(imp.max()),3),
            "gen_p5": round(float(np.percentile(gen,5)),3)}
print(json.dumps(res, indent=1))
