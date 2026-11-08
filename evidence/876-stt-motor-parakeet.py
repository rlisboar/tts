"""#876 — motor do STT do Live: Whisper turbo (o de hoje) × Parakeet TDT 0.6B v3.

Caminho do app (`app._transcribe`, Silero incluso), turbo pelo `_stt_do_live()`.
WER: MLS Portuguese dev+test (20 locutores, CC BY 4.0) + FLEURS pt_br local (3 vozes,
40 frases), normalização de `tools/live_bench.wer`. Latência: falas inteiras e
recortes de 1/3/6 s (o que o Live paga), modelos quentes. Uso: stt_motor.py <repo> <out.json>"""
import json, sys, time, pathlib, tempfile, statistics as st
import numpy as np, soundfile as sf
repo = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(repo)); sys.path.insert(0, str(repo / "tools"))
import app  # noqa: E402
from live_bench import wer  # noqa: E402
SR = 16000
mls = json.loads(pathlib.Path("/tmp/mls-pt-wav/refs.json").read_text())
fl = json.loads(pathlib.Path("/tmp/tts-bakeoff/fala_pt/meta.json").read_text())["refs"]
corpus = [(f"/tmp/mls-pt-wav/{k}", v, "mls") for k, v in sorted(mls.items())][::2]   # 120
corpus += [(f"/tmp/tts-bakeoff/fala_pt/{k}", v, "fleurs") for k, v in sorted(fl.items())]
def stt(motor, a):
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        sf.write(f.name, a, SR, subtype="PCM_16")
        app._settings["stt_local_engine"] = motor
        t = time.monotonic()
        if motor == "whisper":
            with app._stt_do_live():
                r = app._transcribe(pathlib.Path(f.name), "pt", allow_remote=False)
        else:
            r = app._transcribe(pathlib.Path(f.name), "pt", allow_remote=False)
        return (r.get("text") or "").strip(), time.monotonic() - t
def le(p):
    a, sr = sf.read(p, dtype="float32"); assert sr == SR; return a if a.ndim == 1 else a.mean(1)
res = {}
for motor in ("whisper", "parakeet"):
    for _ in range(2): stt(motor, le(corpus[0][0]))                 # aquece
    linhas, lat = [], {1: [], 3: [], 6: [], "inteira": []}
    for p, ref, fonte in corpus:
        a = le(p); hip, dt = stt(motor, a)
        linhas.append({"arq": pathlib.Path(p).name, "fonte": fonte, "dur": round(len(a) / SR, 2), "wer": wer(ref, hip), "s": round(dt, 3)})
        lat["inteira"].append(dt)
    for p, _ref, _f in corpus[::4]:
        a = le(p)
        for d in (1, 3, 6):
            if len(a) >= d * SR: lat[d].append(stt(motor, a[: d * SR])[1])
    w = {f: [x["wer"] for x in linhas if x["fonte"] == f] for f in ("mls", "fleurs")}
    res[motor] = {"wer_medio": {f: round(float(np.mean(v)), 4) for f, v in w.items()},
                  "wer_mediana": {f: round(float(np.median(v)), 4) for f, v in w.items()},
                  "lat_ms": {str(k): {"p50": round(1000 * st.median(v)), "p95": round(1000 * float(np.percentile(v, 95))), "n": len(v)} for k, v in lat.items()},
                  "rtf_inteira": round(float(np.sum(lat["inteira"]) / sum(x["dur"] for x in linhas)), 4),
                  "linhas": linhas}
    print(motor, json.dumps({k: v for k, v in res[motor].items() if k != "linhas"}), flush=True)
out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
