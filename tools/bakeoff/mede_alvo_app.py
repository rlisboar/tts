"""Reprodução da medição do #663 (alternado × sobreposto) com o corpus do R4.

Uso (na raiz do repo, venv do app):
    TTS_SETTINGS_PATH=/tmp/i663.json TTS_SPEAKER_PATH=/tmp/i663p.json \\
      TTS_OUTPUTS_DIR=/tmp/i663o ./.venv-mlx/bin/python tools/bakeoff/mede_alvo_app.py

Corpus: LibriSpeech dev-clean público já montado por `corpus.py` (alvo 1272 ×
concorrente 1462, 3 clipes de cada, 7 s por `junta`). Alternado = blocos de 2 s
(`alvo.alternado`, determinístico); sobreposto = soma com SIR 0 (`alvo.mistura`).
Perfil = 3 embeddings do alvo. Vazamento e WER por `metricas` (mesma régua do R4).
"""
import pathlib
import sys


sys.path.insert(0, "/Users/lisboa/Documents/tts-rod/tools/bakeoff")
sys.path.insert(0, "/Users/lisboa/Documents/tts-rod")
import comum, alvo as A, metricas, soundfile as sf
import app, separa_voz

RAIZ = "/Users/lisboa/Documents/tts-rod"
fal = A._falantes()

def cenarios():
    for alvo_id, conc_id in (("1272", "1462"), ("1462", "1272")):
        a, txt_a = A.junta(fal[alvo_id][:3], 7.0)
        c, _ = A.junta(fal[conc_id][:3], 7.0)
        n = min(a.size, c.size); a, c = a[:n], c[:n]
        yield alvo_id, a, c, txt_a
        break   # um alvo basta para o par de cenários

alvo_id, a, c, txt_a = next(cenarios())
mix_sempre = A.mistura(a, c, 0.0)                    # sobreposto total
alt = A.alternado(a, c, 0.0, blocos_s=2.0)  # alternado
limpo = pathlib.Path(comum.RESULTADOS / "_i6_limpo.wav")
sf.write(str(limpo), a, 16000, subtype="PCM_16")
vecs = []
for arq in fal[alvo_id][:3]:                     # perfil = 3 amostras, como no app
    vecs.append(app._speaker_embed(arq, app.SPEAKER_MODELO_ATUAL))
ref = txt_a.lower().split()

def transcreve(x, tag):
    p = comum.RESULTADOS / f"_663_{tag}.wav"
    sf.write(str(p), x, 16000, subtype="PCM_16")
    r = app._transcribe(p, language="en")
    return (r.get("text") or "").lower().split()

def mede(nome, sinal):
    palavras = transcreve(sinal, nome)
    print("   texto:", " ".join(palavras)[:150])
    return {"vazamento": round(100*metricas.vazamento(palavras, ref), 1),
            "wer": round(metricas.alinhamento(palavras, ref)["wer"], 3)}

for nome, sinal in (("alternado", alt), ("sobreposto", mix_sempre)):
    print(f"== {nome} ==")
    print("  mistura   ", mede(f"{nome}_mix", sinal))
    p = comum.RESULTADOS / f"_663_{nome}.wav"
    sf.write(str(p), sinal, 16000, subtype="PCM_16")
    saida, info = separa_voz.separa_alvo(p, vecs, app.SPEAKER_MODELO_ATUAL)
    y, _ = sf.read(str(saida), dtype="float32")
    print("  alvo      ", mede(f"{nome}_alvo", y), "janelas sobrepostas:", info.get("sobrepostas"),
          "descartados:", info.get("descartados"))
separa_voz.fecha()
print("janelas alternado:", info)
