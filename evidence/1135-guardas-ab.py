"""#1135 — guardas OBJETIVAS de B (CFG desligado no 1º trecho) e A (prefixo de ref de ~4 s).

Pré-registro: `evidence/1135-tts-gen-alavancas.md` §4 (`197dfa0`). Mede, no 1º trecho do Live,
o custo (`generate`, modelo quente) e as guardas de timbre (cosseno ECAPA) e inteligibilidade
(WER pelo STT) de cada braço contra o CONTROLE (CFG 2.0, ref inteira + transcrição, 10 passos):

  ctrl   = hoje                           ctrlb = hoje, OUTRA semente (piso de ruído, #1079)
  B      = guidance_scale 0               A4 / A6 = prefixo de ref cortado a 4 s / 6 s
                                          (sem `ref_text`: a transcrição é da amostra INTEIRA)
  AB     = A4 + B

Voz e corpus: FLEURS pt_br público via `tools/corpus_bancada.py` — NUNCA `voices/` do dono.
Geração in-process, sem tocar produção (7860) nem bancada (7890).

    SAIDA="$(mktemp -d /tmp/1135-XXXXXX)"
    ./.venv-mlx/bin/python tools/corpus_bancada.py --fleurs ~/tts-bench/fleurs/fala_pt --destino "$SAIDA/bancada"
    TTS_STATE_DIR="$SAIDA/estado" TTS_VOICES_DIR="$SAIDA/bancada/voices" TTS_OUTPUTS_DIR="$SAIDA/out" \\
      TTS_SETTINGS_PATH="$SAIDA/estado/settings.json" TTS_APIKEYS_PATH="$SAIDA/estado/k.json" \\
      TTS_SPEAKER_PATH="$SAIDA/estado/sp.json" TTS_SPEAKER_AUDIO_DIR="$SAIDA/estado/spa" \\
      TTS_METRICS_DIR="$SAIDA/estado/m" TTS_JOBS_HIST_PATH="$SAIDA/estado/jh.json" \\
      ./.venv-mlx/bin/python evidence/1135-guardas-ab.py "$SAIDA"
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import app  # noqa: E402
import live_pipeline as lp  # noqa: E402

VOZ = "flvoz"
SEMENTES = (7, 8)
REPS = 2
TEXTOS = (
    ("curto", "Claro,"), ("curto", "Sim, claro."), ("curto", "Deixa eu ver isso."),
    ("curto", "Claro, posso te ajudar."), ("curto", "Certo, entendi."), ("curto", "Tá bom, vamos lá."),
    ("longo", "Bom, vamos por partes, porque tem mais de um ponto aqui."),
    ("longo", "Entendi, faz sentido, e dá para começar pelo mais simples."),
    ("longo", "Olha, o caminho mais curto é esse, mas tem um detalhe importante."),
    ("longo", "Primeiro a gente confere o que você já tem, depois eu te digo o resto."),
    ("longo", "Faz assim: abre a parte de cima e me diz o que aparece na tela."),
    ("longo", "Se não funcionar de primeira, tenta de novo e me chama."),
)
#: nome -> (semente, guidance_scale, segundos de prefixo (None = inteiro))
CONDICOES = {"ctrl": (7, 2.0, None), "ctrlb": (8, 2.0, None), "B": (7, 0.0, None),
             "A4": (7, 2.0, 4.0), "A6": (7, 2.0, 6.0), "AB": (7, 0.0, 4.0),
             # isolam o confundidor das braços A: ref INTEIRA mas SEM transcrição
             # (A corta o prefixo E perde o `ref_text`, que é da amostra inteira)
             "ctrlnr": (7, 2.0, "nr")}
#: `B1135_CONDS=ctrl,ctrlnr,A4` roda só esse subconjunto (a ctrl é sempre a base)
_SO = [c for c in (os.environ.get("B1135_CONDS") or "").split(",") if c]
if _SO:
    CONDICOES = {c: v for c, v in CONDICOES.items() if c in _SO or c == "ctrl"}


def wav_escreve(p: Path, a: np.ndarray, sr: int) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())


def wer(ref: str, hip: str) -> float:
    def norm(s):
        return re.sub(r"[^\w\s]", " ", s.lower()).split()
    r, h = norm(ref), norm(hip)
    if not r:
        return 0.0 if not h else 1.0
    d = np.arange(len(h) + 1)
    for i, pr in enumerate(r, 1):
        ant, d[0] = d[0], i
        for j, ph in enumerate(h, 1):
            atual = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, ant + (pr != ph))
            ant = atual
    return float(d[len(h)]) / len(r)


def cos(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def main() -> int:
    saida = Path(sys.argv[1])
    (saida / "wav").mkdir(parents=True, exist_ok=True)
    assert app.VOICES_DIR.resolve() != (REPO / "voices").resolve(), "VOICES_DIR é o do repo/dono"
    app._settings["omni_precision"] = "bf16"
    voz = app.VOICES_DIR / f"{VOZ}.wav"
    ref_text = json.loads((app.VOICES_DIR / f"{VOZ}.json").read_text())["ref_text"]
    modelo = app._get_model()
    SR = int(getattr(modelo, "sample_rate", 24000) or 24000)
    cheio = app._cond_for(modelo, VOZ, voz)
    print(f"[1135] ref inteiro = {tuple(cheio.shape)} tokens", flush=True)
    emb_ref = app._speaker_embed_ecapa_x(app._wav_to_mono16k(voz))

    def prefixo(seg):
        if seg is None or seg == "nr":
            return cheio
        return cheio[: min(int(seg * 25), cheio.shape[0])]

    def gera(texto, cond, rep=0):
        semente, cfg, seg = CONDICOES[cond]
        app._settings["omni_seed"] = semente
        perfil = lp._perfil_live(primeiro_chunk=True, max_steps=lp._LIVE_STEPS_1O_TRECHO,
                                 fixo=lp._LIVE_STEPS_1O_TRECHO)
        perfil["guidance_scale"] = cfg
        t = time.perf_counter()
        a = app._generate_chunk(modelo, texto, "pt", prefixo(seg),
                                None if seg is not None else ref_text, perfil)
        return a, (time.perf_counter() - t) * 1000

    for c in CONDICOES:                                   # aquecimento, descartado
        gera("Aquecendo.", c)
    print("[1135] aquecido", flush=True)
    regs = []
    for i, (bucket, texto) in enumerate(TEXTOS):
        for cond in CONDICOES:
            for r in range(REPS):
                a, ms = gera(texto, cond, r)
                a = np.asarray(lp._pos_live(a), dtype=np.float32).ravel()
                nome = f"t{i}_{cond}_r{r}.wav"
                wav_escreve(saida / "wav" / nome, a, SR)
                emb = app._speaker_embed_ecapa_x(app._wav_to_mono16k(saida / "wav" / nome))
                regs.append({"texto": i, "bucket": bucket, "cond": cond, "rep": r, "arquivo": nome,
                             "gen_ms": round(ms, 1), "dur_s": round(len(a) / SR, 3), "emb": emb})
        print(f"  texto {i} ok", flush=True)
    app._release_mlx_memory(aggressive=True)
    base = {(x["texto"], x["rep"]): x["emb"] for x in regs if x["cond"] == "ctrl"}
    for x in regs:
        x["cos_ctrl"] = (cos(x["emb"], base[(x["texto"], x["rep"])])
                         if x["emb"] is not None and base.get((x["texto"], x["rep"])) is not None
                         and x["cond"] != "ctrl" else None)
        x["cos_ref"] = cos(x["emb"], emb_ref) if x["emb"] is not None and emb_ref is not None else None
        try:
            hip = (app._transcribe(saida / "wav" / x["arquivo"], language="pt").get("text") or "").strip()
            x["hip"], x["wer"] = hip, round(wer(TEXTOS[x["texto"]][1], hip), 3)
        except Exception as exc:                          # noqa: BLE001
            x["erro_stt"], x["wer"] = f"{type(exc).__name__}: {exc}"[:120], 1.0

    def med(c, k, b=None):
        v = [x[k] for x in regs if x["cond"] == c and (b is None or x["bucket"] == b) and x.get(k) is not None]
        return round(float(np.median(v) if k == "gen_ms" else np.mean(v)), 3) if v else None

    resumo = {c: {"gen_ms_mediana": med(c, "gen_ms"), "gen_ms_curto": med(c, "gen_ms", "curto"),
                  "cos_ctrl_curto": med(c, "cos_ctrl", "curto"), "cos_ctrl_longo": med(c, "cos_ctrl", "longo"),
                  "cos_ref_curto": med(c, "cos_ref", "curto"), "cos_ref_longo": med(c, "cos_ref", "longo"),
                  "wer_medio": med(c, "wer"),
                  "wer_max": max(x["wer"] for x in regs if x["cond"] == c),
                  "vazios": sum(1 for x in regs if x["cond"] == c and not x.get("hip"))}
              for c in CONDICOES}
    (saida / "resumo.json").write_text(json.dumps(
        {"voz": VOZ, "ref_tokens": int(cheio.shape[0]), "resumo": resumo,
         "registros": [{k: v for k, v in x.items() if k != "emb"} for x in regs]},
        ensure_ascii=False, indent=1))
    print(json.dumps(resumo, ensure_ascii=False, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
