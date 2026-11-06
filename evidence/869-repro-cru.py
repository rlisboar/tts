"""#869 — reproduz o descarte `motivo=vad` do Live com áudio gravado.

Alimenta o caminho REAL do WS (ring cru `_live_cru_guarda` → RNNoise real
`_live_pcm_limpo` → `TurnEngine.feed`) e, no `speech_end`, compara o que vai ao
STT (`_live_audio_cru`) com o áudio do turno do motor. Mede: fatia usada, RMS,
fala do Silero (`_vad_tem_fala`, o portão do turno). Sem rede, sem LLM.

Uso: ./.venv-mlx/bin/python evidence/869-repro-cru.py [wav16k ...]
"""
import sys, tempfile, wave, pathlib
import numpy as np, soundfile as sf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app, live_turns, live_rnnoise  # noqa: E402

FR = 1600  # 100 ms @16k, como o navegador


def pcm(a):
    return (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()


def vad_s(b):
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        with wave.open(f.name, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b)
        a = np.frombuffer(b, "<i2").astype(np.float32) / 32768
        import torch
        from silero_vad import get_speech_timestamps
        ts = get_speech_timestamps(torch.from_numpy(a), app._vad_load(), sampling_rate=16000,
                                   threshold=0.6, speech_pad_ms=100)
        return sum(t["end"] - t["start"] for t in ts) / 16000, app._vad_tem_fala(pathlib.Path(f.name))


def rms_db(b):
    a = np.frombuffer(b, "<i2").astype(np.float32) / 32768
    return 20 * np.log10(max(float(np.sqrt((a * a).mean())) if a.size else 0, 1e-7))


def roda(wav, filtro=True, falas=3):
    voz, sr = sf.read(wav, dtype="float32")
    import os
    g = os.environ.get("GAIN_DB")
    if g:
        voz = voz / (np.sqrt((voz**2).mean()) + 1e-9) * 10 ** (float(g) / 20)
    assert sr == 16000
    rng = np.random.default_rng(0)
    ruido = lambda n: rng.normal(0, 10 ** (float(__import__("os").environ.get("NOISE_DB", "-55")) / 20), n).astype(np.float32)
    sinal = np.concatenate([ruido(16000)] + sum([[voz + ruido(len(voz)), ruido(24000)] for _ in range(falas)], []))
    sess = {"id": "repro"}
    if filtro:
        ns = live_rnnoise.RnnoiseAoVivo(); ns.abrir(); sess["rnnoise"] = ns
    eng = live_turns.TurnEngine()
    res = []
    for i in range(0, len(sinal) - FR + 1, FR):
        cru = pcm(sinal[i:i + FR])
        app._live_cru_guarda(sess, cru)
        limpo = app._live_pcm_limpo(sess, cru)
        for ev in eng.feed(limpo):
            if ev.tipo != "speech_end":
                continue
            stt = app._live_audio_cru(sess, ev)
            ini = (ev.amostra - sess["cru"][1]) * 2
            fonte = ("motor(filtrado)" if stt == bytes(ev.audio) and sess.get("rnnoise") else "cru" if stt == bytes(ev.audio) else
                     "ring@amostra")
            fala, ok = vad_s(stt)
            fala_m, ok_m = vad_s(bytes(ev.audio))
            res.append((ev.fala_ms, len(stt) // 32, fonte, round(rms_db(stt), 1),
                        round(fala, 2), ok, round(fala_m, 2), ok_m, ini, len(sess["cru"][0])))
    if filtro:
        sess["rnnoise"].fechar()
    return res


if __name__ == "__main__":
    for wav in sys.argv[1:] or ["tests/fixtures/voz_curta_16k.wav"]:
        for filtro in (True, False):
            print(f"\n== {wav} filtro={'rnnoise' if filtro else 'off'}")
            print("fala_ms  ms_stt  fonte             dBFS   vad_s  passa | motor: vad_s passa | desloc  ring")
            for r in roda(wav, filtro):
                print("%6d  %6d  %-16s %6.1f  %5.2f  %-5s |        %5.2f %-5s | %6d  %6d" % r)
