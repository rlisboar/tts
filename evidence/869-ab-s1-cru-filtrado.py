"""#869 item 3 (A/B isolado do S1, script) — S1 ISOLADO do PCM cru: mesmo SHA, mesmo áudio, só muda o que o
STT do turno recebe (TTS_LIVE_RNNOISE_STT cru × filtrado). Caminho real:
ring `_live_cru_guarda` → RNNoise real `_live_pcm_limpo` → `TurnEngine.feed` →
`speech_end` → `_live_audio_cru` → `live_pipeline._stt_julga` (VAD + juiz).
Sem servidor, sem LLM, sem rede. Portão "só a minha voz" e separação fora (são
outros cards). Uso: python ab_s1.py <repo> <out.json>"""
import json, os, sys, time, pathlib, glob
import numpy as np, soundfile as sf
from scipy.signal import resample_poly

repo = pathlib.Path(sys.argv[1]).resolve(); out = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(repo))
import app, live_turns, live_rnnoise, live_pipeline  # noqa: E402
sys.path.insert(0, str(repo / "tools"))
from live_bench import wer  # noqa: E402

app._live_voz_ligado = lambda: None
app._separacao_do_turno = lambda p: (p, None)
FR = 1600

def pcm(a):
    return (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()

def carrega():
    falas = []
    for w in sorted(glob.glob(os.environ.get("AB_VOICES", str(repo / "voices")) + "/*.wav")):
        nome = pathlib.Path(w).stem
        if nome.startswith("vd-"):
            continue
        ref = (json.load(open(w[:-4] + ".json")).get("ref_text") or "").strip()
        if not ref:
            continue
        a, sr = sf.read(w, dtype="float32")
        if a.ndim > 1: a = a.mean(1)
        a = resample_poly(a, 16000, sr).astype(np.float32)
        falas.append((nome, ref, a))
    return falas

def nivel(a, dbfs):
    return a / (np.sqrt((a * a).mean()) + 1e-9) * 10 ** (dbfs / 20)

def roda(voz, modo, ruido_db, fala_db, seed):
    app._TTS_LIVE_RNNOISE_STT = modo
    rng = np.random.default_rng(seed)
    rd = lambda n: rng.normal(0, 10 ** (ruido_db / 20), n).astype(np.float32)
    v = nivel(voz, fala_db)
    sinal = np.concatenate([rd(16000), v + rd(len(v)), rd(32000)])
    sess = {"id": "ab869"}
    ns = live_rnnoise.RnnoiseAoVivo(); ns.abrir(); sess["rnnoise"] = ns
    eng = live_turns.TurnEngine()
    turnos = []
    try:
        for i in range(0, len(sinal) - FR + 1, FR):
            cru = pcm(sinal[i:i + FR])
            app._live_cru_guarda(sess, cru)
            for ev in eng.feed(app._live_pcm_limpo(sess, cru)):
                if ev.tipo != "speech_end":
                    continue
                stt = app._live_audio_cru(sess, ev)
                fonte = "filtrado" if stt == bytes(ev.audio) else "cru"
                j = {}
                t0 = time.monotonic()
                txt = live_pipeline._stt_julga(stt, "pt", j)
                turnos.append({"fala_ms": int(ev.fala_ms or 0), "fonte": fonte,
                               "aceito": bool(txt) and "motivo" not in j,
                               "motivo": j.get("motivo"), "detalhe": j.get("detalhe"),
                               "stt_ms": int((time.monotonic() - t0) * 1000), "txt": txt})
    finally:
        ns.fechar()
    return turnos

def main():
    falas = carrega()
    cenarios = [("limpo", -60, -26), ("ventoinha", -45, -26), ("baixo", -50, -38)]
    res = []
    for cen, rdb, fdb in cenarios:
        for k, (nome, ref, a) in enumerate(falas):
            curto = a[int(0.3 * 16000):int(0.3 * 16000) + int(1.5 * 16000)]  # 1,5 s
            for modo in ("cru", "filtrado"):
                for tipo, voz in (("longa", a), ("curta", curto)):
                    t = roda(voz, modo, rdb, fdb, seed=k)
                    hip = " ".join(x["txt"] for x in t if x["aceito"])
                    r = {"cenario": cen, "fala": nome, "modo": modo, "tipo": tipo,
                         "turnos": t, "aceita": any(x["aceito"] for x in t)}
                    if tipo == "longa":
                        r["wer"] = round(wer(ref, hip), 3)
                    res.append(r)
                    print(cen, nome, modo, tipo, r["aceita"], r.get("wer"),
                          [(x["fonte"], x["motivo"]) for x in t], flush=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1))

main()
