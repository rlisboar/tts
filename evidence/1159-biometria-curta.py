"""#1159 — verificação de locutor por duração (0,3–3 s). Ver 1159-prereg-biometria-curta.md.

Uso: python -I 1159-biometria-curta.py REPO MLS_DIR FLEURS_DIR SAIDA.json
Só corpus público (MLS PT dev+test, FLEURS pt_br test). Não toca voices/, perfis, settings.
Chama o encoder ECAPA direto (sem o piso de 0,6 s do app)."""
import glob, json, pathlib, sys
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

repo, mls_dir, fleurs_dir, saida = (pathlib.Path(a) for a in sys.argv[1:5])
sys.path.insert(0, str(repo))
import torch  # noqa: E402
import app  # noqa: E402

SR, SEED, CROPS, FOLDS = 16000, 1159, 60, 5
DURS = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
BASE = {"1": 0.39, "0.1": 0.48}            # θ(3 s) de produção por alvo de FAR (#982)
ALVO = {"1": 0.01, "0.1": 0.001}
rng = np.random.default_rng(SEED)
enc = app._speaker_encoder_ecapa()


def le(p):
    a, sr = sf.read(p, dtype="float32")
    a = a if a.ndim == 1 else a.mean(1)
    return (resample_poly(a, SR, sr) if sr != SR else a).astype(np.float32)


def ativo(a):
    """máscara por janela de 20 ms: energia > pico−40 dB."""
    w = SR // 50
    n = len(a) // w
    e = np.sqrt((a[: n * w].reshape(n, w) ** 2).mean(1) + 1e-12)
    return e > e.max() * 10 ** (-40 / 20)


def trim(a):
    m = ativo(a)
    if not m.any():
        return a[:0]
    i, j = np.flatnonzero(m)[[0, -1]]
    return a[i * (SR // 50): (j + 1) * (SR // 50)]


def recorta(a, d):
    """recorte de d s com ≥85 % de janelas ativas; None se não achar em 20 tentativas."""
    n = int(d * SR)
    if len(a) < n:
        return None
    for _ in range(20):
        i = int(rng.integers(0, len(a) - n + 1))
        c = a[i:i + n]
        if ativo(c).mean() >= 0.85:
            return c
    return None


def emb(lote):
    out = []
    for k in range(0, len(lote), 64):
        x = torch.from_numpy(np.stack(lote[k:k + 64]))
        with torch.no_grad():
            e = enc.encode_batch(x).squeeze(1).numpy()
        out.append(e / np.linalg.norm(e, axis=1, keepdims=True))
    return np.concatenate(out)


def wilson(k, n, z=1.96):
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(float(max(0, c - h)), 5), round(float(min(1, c + h)), 5)]


# ---------- MLS: locutores, cadastro (3×3 s de 3 enunciados), recortes por duração
por_loc = {}
for f in sorted(glob.glob(str(mls_dir / "*.flac"))):
    por_loc.setdefault(pathlib.Path(f).name.split("_")[0], []).append(f)
cad, rec = {}, {}                           # cad[loc] = centróide; rec[loc][d] = embs
for loc, arqs in sorted(por_loc.items()):
    aud = [t for t in (trim(le(f)) for f in arqs)]
    longos = [i for i, a in enumerate(aud) if len(a) >= 3 * SR]
    if len(longos) < 8:
        continue
    escolha = [int(i) for i in rng.choice(longos, 3, replace=False)]
    cs = []
    for i in escolha:
        c = recorta(aud[i], 3.0)
        if c is not None:
            cs.append(c)
    if len(cs) < 3:
        continue
    e = emb(cs).mean(0)
    cad[loc] = e / np.linalg.norm(e)
    resto = [a for i, a in enumerate(aud) if i not in escolha and len(a) >= 0.3 * SR]
    rec[loc] = {}
    for d in DURS:
        cs = []
        tent = 0
        while len(cs) < CROPS and tent < CROPS * 6:
            tent += 1
            c = recorta(resto[int(rng.integers(len(resto)))], d)
            if c is not None:
                cs.append(c)
        rec[loc][d] = emb(cs) if cs else np.zeros((0, 192))
locs = sorted(cad)
print("locutores", len(locs), flush=True)
C = np.stack([cad[l] for l in locs])        # cadastros


def escores(loc_crop_idx, enr_idx, d, gen):
    """cosseno cru dos recortes dos locutores loc_crop_idx contra os cadastros enr_idx."""
    out = []
    for i in loc_crop_idx:
        E = rec[locs[i]][d]
        if not len(E):
            continue
        S = E @ C.T
        if gen:
            out.append(S[:, i])
        else:
            out.append(S[:, [j for j in enr_idx if j != i]].ravel())
    return np.concatenate(out) if out else np.zeros(0)


def theta(imp, base, far):
    return max(base, float(np.quantile(imp, 1 - far))) if len(imp) else None


# ---------- validação cruzada por locutor
idx = np.arange(len(locs))
perm = rng.permutation(idx)
dobras = [perm[k::FOLDS] for k in range(FOLDS)]
res = {}
for d in DURS:
    G, I = [], {a: [] for a in ALVO}
    th_fold = {a: [] for a in ALVO}
    gen_all = {a: [] for a in ALVO}
    for k, teste in enumerate(dobras):
        cal = np.setdiff1d(idx, teste)
        imp_cal = escores(cal, cal, d, False)
        for a in ALVO:
            t = theta(imp_cal, BASE[a], ALVO[a])
            th_fold[a].append(t)
            g = escores(teste, [], d, True)
            im = escores(teste, idx, d, False)     # recorte de locutor NÃO visto × todos os cadastros
            gen_all[a].append((int((g < t).sum()), len(g)))
            I[a].append((int((im >= t).sum()), len(im)))
    linha = {}
    for a in ALVO:
        kf = sum(x[0] for x in I[a]); nf = sum(x[1] for x in I[a])
        kr = sum(x[0] for x in gen_all[a]); nr = sum(x[1] for x in gen_all[a])
        linha[a] = {"theta_por_dobra": [round(t, 3) for t in th_fold[a]],
                    "FAR": round(kf / nf, 5) if nf else None, "FAR_IC": wilson(kf, nf), "n_imp": nf,
                    "FRR": round(kr / nr, 5) if nr else None, "FRR_IC": wilson(kr, nr), "n_gen": nr}
    res[str(d)] = linha

# ---------- θ final (todos os locutores) e EER por duração
final = {}
for d in DURS:
    imp = escores(idx, idx, d, False); gen = escores(idx, [], d, True)
    ts = np.linspace(-0.2, 1.0, 1201)
    far = np.array([(imp >= t).mean() for t in ts]); frr = np.array([(gen < t).mean() for t in ts])
    i = int(np.argmin(abs(far - frr)))
    # θ do #1169: o quantil que realiza o alvo em CADA corpus; vale o MAIOR dos dois
    # (o corpus mais duro manda). O θ de produção fica ao lado, como referência.
    q_cv = {a: float(np.quantile(imp, 1 - ALVO[a])) for a in ALVO}
    final[str(d)] = {"theta_cv": {a: round(q_cv[a], 3) for a in ALVO},
                     "theta": {a: round(theta(imp, BASE[a], ALVO[a]), 3) for a in ALVO},
                     "EER": round(float((far[i] + frr[i]) / 2), 4), "EER_limiar": round(float(ts[i]), 3),
                     "imp_p50_p99_max": [round(float(np.percentile(imp, q)), 3) for q in (50, 99)] + [round(float(imp.max()), 3)],
                     "gen_p5_p50": [round(float(np.percentile(gen, q)), 3) for q in (5, 50)],
                     "n_imp": len(imp), "n_gen": len(gen)}

# ---------- FLEURS: impostor garantido (corpora disjuntos), θ final
# `**` alcança `test/x.wav` E o atalho `x.wav` na raiz: sem dedupe por nome o
# mesmo arquivo entra duas vezes e o IC sai ~1,4x estreito do que devia.
fl = sorted({pathlib.Path(x).name: x for x in
             glob.glob(str(fleurs_dir / "**" / "*.wav"), recursive=True)}.values())
fl_aud = [trim(le(f)) for f in fl]   # #1169: todos (919), não 400
fleurs = {}
for d in DURS:
    cs = []
    for a_ in fl_aud:
        c = recorta(a_, d)
        if c is not None:
            cs.append(c)
        if len(cs) >= len(fl_aud):
            break
    if not cs:
        continue
    S = (emb(cs) @ C.T).ravel()
    gen_d = escores(idx, [], d, True)          # FRR no MESMO θ, para não vender só o FAR
    fleurs[str(d)] = {}
    for a in ALVO:
        q_fl = float(np.quantile(S, 1 - ALVO[a]))
        t = max(q_fl, float(np.quantile(escores(idx, idx, d, False), 1 - ALVO[a])))
        k = int((S >= t).sum())
        kc = int((escores(idx, idx, d, False) >= t).sum())
        nc = len(escores(idx, idx, d, False))
        kg = int((gen_d < t).sum())
        fleurs[str(d)][a] = {"theta_final": round(t, 3), "theta_fleurs": round(q_fl, 3),
                             "theta_09": round(max(float(np.quantile(S, 0.991)),
                                                   float(np.quantile(escores(idx, idx, d, False), 0.991))), 3),
                             "FRR_09_cv": round(float((gen_d < max(float(np.quantile(S, 0.991)),
                                                                   float(np.quantile(escores(idx, idx, d, False), 0.991)))).mean()), 5),
                             "FAR": round(k / len(S), 5), "FAR_IC": wilson(k, len(S)), "n": len(S),
                             "FAR_cv": round(kc / nc, 5), "FRR_cv": round(kg / len(gen_d), 5),
                             "n_gen": len(gen_d)}
saida.write_text(json.dumps({"locutores": len(locs), "seed": SEED, "crops": CROPS, "cv": res,
                             "final": final, "fleurs": fleurs, "fleurs_arquivos": len(fl_aud)},
                            ensure_ascii=False, indent=1))
print("ok", saida)
