"""#979 — validação do limiar ECAPA AS-norm (2,434 @FAR 1 % do R2, 1,8 joelho do Live) num corpus
PT-BR que a coorte NÃO viu: as vozes reais de voices/ (15 locutores). Cadastro =
centróide de 3 trechos de 3 s; teste = trechos de 3 s seguintes (genuíno) × todos os
trechos dos outros locutores (impostor). Coorte e régua: as do app (`_speaker_asnorm`)."""
import glob, json, sys, pathlib
import numpy as np
from scipy.signal import resample_poly
import soundfile as sf
repo = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(repo))
import app  # noqa: E402
M = "ecapa:v1"; SR = 16000; T = 3 * SR
mesmo = {"b835c54c36": "1e8e1479ae"}          # Andrea2 = Andrea
loc = {}
for w in sorted(glob.glob(str(repo / "voices/*.wav"))):
    k = pathlib.Path(w).stem
    if k.startswith("vd-"):
        continue
    a, sr = sf.read(w, dtype="float32")
    a = resample_poly(a if a.ndim == 1 else a.mean(1), SR, sr).astype(np.float32)
    trechos = [a[i:i + T] for i in range(0, len(a) - T + 1, T)]
    loc.setdefault(mesmo.get(k, k), []).extend(app._speaker_embed_ecapa_x(t) for t in trechos)
nomes = {pathlib.Path(w).stem: json.load(open(w))["name"] for w in glob.glob(str(repo / "voices/*.json"))}
gen, imp, dono = [], [], None
for k, embs in loc.items():
    if len(embs) < 4:
        continue
    cad, teste = embs[:3], embs[3:]
    for e in teste:
        gen.append(app._speaker_asnorm(e, cad, M, alvo="1")[0])
    for k2, embs2 in loc.items():
        if k2 != k:
            for e in embs2:
                imp.append(app._speaker_asnorm(e, cad, M, alvo="1")[0])
# dono ("Rodrigo", 8,9 s = 2 trechos): cadastro de 2, nota de cada trecho contra o outro
r = loc.get("9f5265a9f1", [])
if len(r) >= 2:
    dono = [app._speaker_asnorm(r[0], [r[1]], M, alvo="1")[0], app._speaker_asnorm(r[1], [r[0]], M, alvo="1")[0]]
gen, imp = np.array(gen), np.array(imp)
out = {"locutores": len(loc), "cadastrados": int(sum(len(v) >= 4 for v in loc.values())),
       "genuinos": len(gen), "impostores": len(imp),
       "gen_p5_p50": [round(float(np.percentile(gen, q)), 3) for q in (5, 50)],
       "imp_p99_p999_max": [round(float(np.percentile(imp, q)), 3) for q in (99, 99.9)] + [round(float(imp.max()), 3)],
       "dono_2x1": dono and [round(float(x), 3) for x in dono]}
for lim in (1.8, 2.434, 3.044):
    out[f"@{lim}"] = {"FAR_%": round(100 * float((imp >= lim).mean()), 2),
                      "FRR_%": round(100 * float((gen < lim).mean()), 2)}
# EER
ts = np.linspace(min(gen.min(), imp.min()), max(gen.max(), imp.max()), 4000)
far = np.array([(imp >= t).mean() for t in ts]); frr = np.array([(gen < t).mean() for t in ts])
i = int(np.argmin(abs(far - frr))); out["EER_%"] = round(100 * float((far[i] + frr[i]) / 2), 2); out["EER_limiar"] = round(float(ts[i]), 3)
out["limiar_FAR1_PT"] = round(float(np.quantile(imp, 0.99)), 3)
print(json.dumps(out, ensure_ascii=False, indent=1))
# --- diagnóstico: pares impostores mais altos, cru e AS-norm, e mu/sd da coorte
pares = []
for k, embs in loc.items():
    if len(embs) < 4:
        continue
    cad = embs[:3]; cen = app._speaker_centroide(cad)
    for k2, embs2 in loc.items():
        if k2 == k:
            continue
        for e in embs2:
            pares.append((app._speaker_asnorm(e, cad, M, alvo="1")[0], app._cos_sim(e, cen), nomes[k], nomes[k2]))
pares.sort(reverse=True)
print("top impostores (asnorm, cru, cadastro <- teste):")
for p in pares[:12]:
    print(f"  {p[0]:6.2f}  {p[1]:.3f}  {p[2]} <- {p[3]}")
cr = np.array([p[1] for p in pares])
print("impostor cru p50/p99/max", np.round(np.percentile(cr, [50, 99, 100]), 3), "FAR@0.6 cru %", round(100 * float((cr >= 0.6).mean()), 2))
coorte = app._speaker_cohorte(M)
ex = loc["5d214dbbff"][0]
notas = sorted((app._cos_sim(ex, c) for c in coorte), reverse=True)[:10]
print("coorte top10 de um trecho PT: mu", round(float(np.mean(notas)), 3), "sd", round(float(np.std(notas)), 4))
gcr = []
for k, embs in loc.items():
    if len(embs) < 4:
        continue
    cen = app._speaker_centroide(embs[:3])
    gcr += [app._cos_sim(e, cen) for e in embs[3:]]
gcr = np.array(gcr)
print("genuino cru min/p5/p50", np.round([gcr.min(), np.percentile(gcr, 5), np.median(gcr)], 3))
for t in (0.44, 0.5, 0.55, 0.6):
    print(f"cru @{t}: FAR {100*float((cr>=t).mean()):.2f} %  FRR {100*float((gcr<t).mean()):.2f} %")
if len(r) >= 2:
    print("dono cru 1x1", round(app._cos_sim(r[0], r[1]), 3))
