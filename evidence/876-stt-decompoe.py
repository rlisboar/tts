"""#876 — onde vai o tempo do STT do turno (turbo): Silero, decode, release do MLX."""
import sys, time, pathlib, tempfile, json, statistics as st
import soundfile as sf
repo = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(repo)); import app  # noqa
mls = json.loads(pathlib.Path("/tmp/mls-pt-wav/refs.json").read_text())
arqs = [f"/tmp/mls-pt-wav/{k}" for k in sorted(mls)][::6][:40]
T = {"vad": [], "wav": [], "decode": [], "release": [], "total": []}
def um(p, dur=3):
    a, sr = sf.read(p, dtype="float32"); a = a[: dur * sr]
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        sf.write(f.name, a, sr, subtype="PCM_16"); P = pathlib.Path(f.name)
        t0 = time.monotonic(); app._vad_tem_fala(P); t1 = time.monotonic()
        x = app._wav_to_mono16k(P); t2 = time.monotonic()
        with app._stt_do_live(), app._stt_lock:
            app._transcreve_em_janelas(x, "pt")
        t3 = time.monotonic(); app._release_mlx_memory(); t4 = time.monotonic()
        return t1 - t0, t2 - t1, t3 - t2, t4 - t3, t4 - t0
for p in arqs[:3]: um(p)
for p in arqs:
    for k, v in zip(T, um(p)): T[k].append(v)
print({k: round(1000 * st.median(v), 1) for k, v in T.items()})
# decode SEM o release entre chamadas (o Metal mantém o pool)
D = []
for p in arqs:
    a, sr = sf.read(p, dtype="float32"); a = a[: 3 * sr]
    t = time.monotonic()
    with app._stt_do_live(), app._stt_lock:
        app._transcreve_em_janelas(a, "pt")
    D.append(time.monotonic() - t)
print("decode sem release p50 ms", round(1000 * st.median(D), 1))
