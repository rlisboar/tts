"""#984 — o portão "só a minha voz" no BARGE CURTO: aceite do dono × falso aceite de
terceiro, com o eco da resposta por baixo do trecho inteiro. `_voz_do_dono` real,
perfil ECAPA do E2E (#980, cópia em /tmp/ab984/state), sem servidor.
Uso: barge984.py <repo> <out.json>"""
import glob, inspect, json, os, sys, pathlib
import numpy as np, soundfile as sf
from scipy.signal import resample_poly
repo = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2])
os.environ["TTS_STATE_DIR"] = "/tmp/ab984/state"
os.environ["TTS_SPEAKER_AUDIO_DIR"] = "/tmp/ab984/state/speaker-audio"
sys.path.insert(0, str(repo))
import app  # noqa: E402
SR = 16000
TEM_FALA = "fala_ms" in inspect.signature(app._voz_do_dono).parameters
cfg = {"on": True, "perfil": "Dono E2E", "rigor_far": "1.8", "limiar": 0.0}

def le(p):
    a, sr = sf.read(p, dtype="float32")
    a = a if a.ndim == 1 else a.mean(1)
    return resample_poly(a, SR, sr).astype(np.float32) if sr != SR else a

def rms(x): return float(np.sqrt((x * x).mean()) + 1e-9)
def nivel(x, db): return x / rms(x) * 10 ** (db / 20)

dono = le("/tmp/e2e-dono-3.wav")                    # fora do cadastro
eco = np.concatenate([le(p) for p in sorted(glob.glob("/tmp/e2e-out/*.wav"))])
emb_dono = np.array(app._speaker_embed_ecapa_x(dono))
terceiros = {"Valentina(e2e)": le("/tmp/e2e-terceiro.wav")}
for w in sorted(glob.glob(os.environ.get("AB_VOICES", "/Users/lisboa/Documents/tts-rod/voices") + "/*.wav")):
    k = pathlib.Path(w).stem
    if k.startswith("vd-"): continue
    x = le(w)
    e = np.array(app._speaker_embed_ecapa_x(x[:3 * SR]))
    if float(e @ emb_dono / np.linalg.norm(e) / np.linalg.norm(emb_dono)) > 0.5:
        continue                                     # mesma pessoa que o dono: fora
    terceiros[k] = x

def recortes(x, dur, maximo):
    n = int(dur * SR); r = rms(x); res = []
    for i in range(0, len(x) - n, SR // 4):
        c = x[i:i + n]
        if rms(c) > 0.6 * r: res.append(c)
        if len(res) >= maximo: break
    return res

def trecho(fala, eco_db, pos, k):
    total = max(int(1.376 * SR), int(0.4 * SR) + len(fala) + int(0.3 * SR))
    e0 = (k * 7919) % (len(eco) - total)
    x = nivel(eco[e0:e0 + total], -26 + eco_db).copy()
    ini = total - len(fala) if pos == "fim" else int(0.4 * SR)
    x[ini:ini + len(fala)] += nivel(fala, -26)
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()

def roda(x, fala_ms):
    return app._voz_do_dono(x, cfg, fala_ms=fala_ms) if TEM_FALA else app._voz_do_dono(x, cfg)

res = []
for fala_s in (0.4, 0.6, 0.8, 1.0, 1.4):
    for eco_db in (-12, -15, -18):
        for pos in ("fim", "pos_preroll"):
            for k, c in enumerate(recortes(dono, fala_s, 8)):
                v = roda(trecho(c, eco_db, pos, k), int(fala_s * 1000))
                res.append({"quem": "dono", "fala_s": fala_s, "eco_db": eco_db, "pos": pos, "ok": bool(v.get("ok")), "estado": v.get("estado"), "score": v.get("score"), "lat": v.get("latencia_ms")})
            for nome, t in terceiros.items():
                for k, c in enumerate(recortes(t, fala_s, 3)):
                    v = roda(trecho(c, eco_db, pos, k + 100), int(fala_s * 1000))
                    res.append({"quem": nome, "fala_s": fala_s, "eco_db": eco_db, "pos": pos, "ok": bool(v.get("ok")), "estado": v.get("estado"), "score": v.get("score"), "lat": v.get("latencia_ms")})
out.write_text(json.dumps({"tem_fala_ms": TEM_FALA, "terceiros": list(terceiros), "res": res}, ensure_ascii=False))
d = [r for r in res if r["quem"] == "dono"]; t = [r for r in res if r["quem"] != "dono"]
print("fala_ms no portão:", TEM_FALA, "| terceiros:", len(terceiros))
for fs in (0.4, 0.6, 0.8, 1.0, 1.4):
    dd = [r for r in d if r["fala_s"] == fs]; tt = [r for r in t if r["fala_s"] == fs]
    print(f"fala {fs:.1f}s  dono aceito {sum(r['ok'] for r in dd):3}/{len(dd):<3}  terceiro aceito {sum(r['ok'] for r in tt):3}/{len(tt):<4}  indisp {sum(r['estado']=='indisponivel' for r in dd+tt)}")
lat = [r["lat"] for r in res if r["lat"] is not None]
print("latência p50/p95 ms", np.percentile(lat, 50), np.percentile(lat, 95))
