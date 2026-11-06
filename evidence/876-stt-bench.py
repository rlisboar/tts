"""#876 — latência e WER do STT local do Live por modelo, com e sem TTS em paralelo.

Fala pt REAL: FLEURS pt_br test (CC BY 4.0), 40 locutores, montado por
`tools/bakeoff/wer_pt_real.py --monta` em /tmp/tts-bakeoff/fala_pt. Nada do dono.

- WER: fala inteira (~8–15 s) contra a transcrição do corpus.
- Latência: recortes de 1/3/6 s do começo de cada fala, pelo MESMO caminho do
  app (`_transcreve_em_janelas`, greedy, temperatura 0) — é o que o Live paga.
- Carga: `--carga` sobe um `tts_worker.py` real (OmniVoice) gerando em laço
  enquanto o STT roda (a disputa de GPU do Mini).

`stt_beam` NÃO entra: só vale no STT remoto; o `mlx_whisper` local nem tem beam
(`NotImplementedError`), o decoder local é sempre greedy.

Uso: ./.venv-mlx/bin/python evidence/876-stt-bench.py [--carga] [--n 20]
"""
import argparse, json, os, re, subprocess, sys, tempfile, time, unicodedata, pathlib
import numpy as np, soundfile as sf

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
CORPUS = pathlib.Path("/tmp/tts-bakeoff/fala_pt")
MODELOS = ["mlx-community/whisper-large-v3-mlx", "mlx-community/whisper-large-v3-turbo"]


def norm(t):
    t = unicodedata.normalize("NFKC", t.lower())
    return re.findall(r"[^\W_]+", t)


def wer(h, r):
    h, r = norm(h), norm(r)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        p, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (rw != hw))
    return d[len(h)] / max(1, len(r))


def carga():
    """Sobe o worker OmniVoice em laço (cada rodada = um pedido completo)."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="876-carga-"))
    (tmp / "out").mkdir(); (tmp / "pc").mkdir()
    import app
    cfg = {"text": "Este é um texto longo para manter o modelo de voz ocupado durante a "
                   "medição do reconhecimento de fala. " * 4,
           "voice_id": "", "voice_path": "", "language": "pt",
           "omni": {"speed": 1.0, "num_steps": 16}, "model": "omnivoice",
           "settings": {"chunk_max_chars": 200, "omni_ref_max_s": 10.0,
                        "omni_precision": "bf16", "audio_gain_db": 0.0},
           "piece_dir": str(tmp / "pc"), "outputs_dir": str(tmp / "out"),
           "voices_dir": str(app.VOICES_DIR), "base_dir": str(RAIZ),
           "status_path": str(tmp / "st.json")}
    (tmp / "cfg.json").write_text(json.dumps(cfg))
    laco = f"while true; do {sys.executable} {RAIZ/'tts_worker.py'} {tmp/'cfg.json'} >/dev/null 2>&1; done"
    p = subprocess.Popen(["bash", "-c", laco], start_new_session=True)
    time.sleep(40)                        # carga do modelo + 1ª geração em curso
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carga", action="store_true")
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args()
    import app
    meta = json.loads((CORPUS / "meta.json").read_text())["refs"]
    falas = sorted(meta)[: a.n]
    proc = carga() if a.carga else None
    res = {}
    try:
        for repo in MODELOS:
            app._settings["stt_whisper_repo"] = repo
            app._transcreve_em_janelas(np.zeros(16000, np.float32), "pt")   # aquece
            ws, lat = [], {1: [], 3: [], 6: []}
            for f in falas:
                x, sr = sf.read(str(CORPUS / f), dtype="float32")
                if x.ndim > 1:
                    x = x.mean(1)
                if sr != 16000:
                    import librosa
                    x = librosa.resample(x, orig_sr=sr, target_sr=16000)
                r = app._transcreve_em_janelas(x, "pt")
                ws.append(wer(r.get("text") or "", meta[f]))
                for s in lat:
                    t0 = time.perf_counter()
                    app._transcreve_em_janelas(x[: s * 16000], "pt")
                    lat[s].append((time.perf_counter() - t0) * 1000)
            res[repo] = {"wer_medio": round(float(np.mean(ws)), 4),
                         "wer_mediano": round(float(np.median(ws)), 4),
                         **{f"ms_{s}s_p50": round(float(np.median(v))) for s, v in lat.items()},
                         **{f"ms_{s}s_p90": round(float(np.percentile(v, 90))) for s, v in lat.items()}}
            print(repo.split("/")[-1], res[repo], flush=True)
    finally:
        if proc:
            os.killpg(proc.pid, 15)
    saida = RAIZ / "evidence" / f"876-stt-bench-{'carga' if a.carga else 'livre'}.json"
    saida.write_text(json.dumps({"n": len(falas), "carga": a.carga,
                                 "maquina": os.uname().nodename, "res": res}, indent=1))


if __name__ == "__main__":
    main()
