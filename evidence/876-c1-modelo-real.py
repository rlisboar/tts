"""#876 C1 com modelo REAL (script): turno do Live (`_stt_app`) e passada de comando, C0 × C1."""
import json, sys, time, pathlib, statistics as st, types
import numpy as np, soundfile as sf
repo = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(repo)); sys.path.insert(0, str(repo / "tools"))
import app, live_pipeline  # noqa: E402
from live_bench import wer  # noqa: E402
app._live_voz_ligado = lambda: None
app._separacao_do_turno = lambda p: (p, None)
refs = json.loads(pathlib.Path("/tmp/mls-pt-wav/refs.json").read_text())
arqs = sorted(refs)[1::6][:40]
def pcm(a): return (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
for nome, eng, adia in (("C0", "", False), ("C1", "parakeet", True)):
    app._settings["live_stt_engine"] = eng; app._settings["live_stt_release_adiado"] = adia
    a0, _ = sf.read(f"/tmp/mls-pt-wav/{arqs[0]}", dtype="float32")
    for _ in range(2): live_pipeline._stt_app(pcm(a0[:48000]), "pt", {})
    ts, ws, cmd = [], [], []
    for k in arqs:
        a, _ = sf.read(f"/tmp/mls-pt-wav/{k}", dtype="float32")
        t = time.monotonic(); txt = live_pipeline._stt_app(pcm(a), "pt", {}); ts.append(time.monotonic() - t)
        app._stt_solta_adiado()
        ws.append(wer(refs[k], txt))
        t = time.monotonic()
        app._live_transcreve_trecho({"id": "s", "pipe": types.SimpleNamespace(language="pt", _stt=None)}, pcm(a[:24000]), cru=True, prazo_s=5.0)
        cmd.append(time.monotonic() - t)
    time.sleep(0.3)
    print(nome, f"turno p50 {1000*st.median(ts):.0f} ms p95 {1000*np.percentile(ts,95):.0f} | WER médio {np.mean(ws):.4f} | comando p50 {1000*st.median(cmd):.0f} ms | pendente {app._stt_release_pendente.is_set()}", flush=True)
