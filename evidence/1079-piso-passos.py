"""#1079 — piso de `num_steps` do 1º trecho do Live: custo, timbre (ECAPA), WER e ABX.

O 1º trecho é `gen` puro (1470 de 1472 ms de p50 na janela do mini). O número de
passos de unmasking é a única alavanca de app que sobra. Aqui se mede 16 (hoje) ×
12 × 10 × 8 com as guardas do card — e com um CONTROLE que a primeira tentativa
mostrou necessário.

POR QUE HÁ CONTROLE: com a mesma semente a geração é DETERMINÍSTICA (16×16 = 1,000
exato), então repetir o mesmo nível não dá piso de ruído nenhum. O piso vem de
gerar 16 passos com OUTRA semente (`16b`): o que a mudança de passos fizer tem de
ser lido contra essa variação, não contra 1,000.

POR QUE DOIS TAMANHOS: o ECAPA em trecho curto é comprimido — a própria voz de
referência contra um pedaço dela mesma dá 0,56 (1,5 s) a 0,83 (2,2 s). O piso de
0,80 do card só é interpretável no bucket LONGO; no curto o que vale é a
comparação com o controle.

Guardas: timbre (cosseno ECAPA contra o áudio de 16 passos, mesmo texto e mesma
repetição), WER (o áudio volta ao STT e é comparado com o texto de origem) e ABX
(pares 16×8 e 16×10 com A/B sorteado para escuta).

Custo: cronometrado em volta do `generate`, modelo quente. O mini é ~2,1× mais
lento que o MBP no mesmo trabalho — número do MBP não vale aqui.

    SAIDA="$(mktemp -d /tmp/1079-XXXXXX)"      # único por execução (classe #1079)
    mkdir -p "$SAIDA/estado" "$SAIDA/out"
    TTS_STATE_DIR="$SAIDA/estado" TTS_OUTPUTS_DIR="$SAIDA/out" \
      ./.venv-mlx/bin/python evidence/1079-piso-passos.py "$SAIDA"

Não toca produção (7860) nem a bancada (7890): geração in-process.
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import app  # noqa: E402
import live_pipeline as lp  # noqa: E402 — o perfil e o pós do Live moram aqui

VOZ = "7b620feaba"                     # Willian Bonner, 8,0 s — a do card
#: condição → passos. `16b` é o CONTROLE: mesmo nível, outra semente.
CONDICOES = (("16", 16), ("16b", 16), ("12", 12), ("10", 10), ("8", 8))
REPS = 2
#: sementes por texto: sem isso a comparação é de UMA amostra por célula e a
#: diferença de WER vira sorte de semente (medido: o controle derrubou um texto
#: inteiro só por mudar a semente)
SEMENTES = (7, 8)
#: `curto` = perfil de 1º trecho real; `longo` = mesma voz com fala suficiente
#: para o ECAPA sair da faixa comprimida (o piso de 0,80 só se lê aqui)
TEXTOS = (
    ("curto", "Claro,"),
    ("curto", "Sim, claro."),
    ("curto", "Deixa eu ver isso."),
    ("curto", "Claro, posso te ajudar."),
    ("curto", "Certo, entendi."),
    ("curto", "Tá bom, vamos lá."),
    ("longo", "Bom, vamos por partes, porque tem mais de um ponto aqui."),
    ("longo", "Entendi, faz sentido, e dá para começar pelo mais simples."),
    ("longo", "Olha, o caminho mais curto é esse, mas tem um detalhe importante."),
    ("longo", "Primeiro a gente confere o que você já tem, depois eu te digo o resto."),
    ("longo", "Faz assim: abre a parte de cima e me diz o que aparece na tela."),
    ("longo", "Se não funcionar de primeira, tenta de novo e me chama."),
)


def wav_escreve(caminho: Path, audio: np.ndarray, sr: int) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def wer(ref: str, hip: str) -> float:
    """WER por palavras, normalizado (minúsculas, sem pontuação)."""
    import re

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


def main() -> int:
    # sem argumento, o diretório é NOVO a cada execução: o script não relê saída
    # anterior, então caminho fixo só criaria disputa entre duas rodadas
    saida = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        tempfile.mkdtemp(prefix="1079-"))
    (saida / "wav").mkdir(parents=True, exist_ok=True)

    app._settings["omni_precision"] = "bf16"
    app._settings["omni_num_steps"] = 16

    voz_path = REPO / "voices" / f"{VOZ}.wav"
    ref_text = json.loads((REPO / "voices" / f"{VOZ}.json").read_text())["ref_text"]

    print("[1079] carregando modelo…", flush=True)
    t0 = time.perf_counter()
    modelo = app._get_model()
    SR = int(getattr(modelo, "sample_rate", 24000) or 24000)
    print(f"[1079] modelo pronto em {time.perf_counter() - t0:.1f} s (sr {SR})", flush=True)
    conds = app._cond_for(modelo, VOZ, voz_path)

    for _, p in CONDICOES:                   # aquecimento, descartado
        app._generate_chunk(modelo, "Aquecendo.", "pt", conds, ref_text,
                            lp._perfil_live(primeiro_chunk=True, max_steps=p))
    print("[1079] aquecido", flush=True)

    registros = []
    for i, (bucket, texto) in enumerate(TEXTOS):
        for cond, p in CONDICOES:
            # a semente do controle é a outra; as demais usam a mesma do `16`
            for r in range(REPS):
                app._settings["omni_seed"] = SEMENTES[1] if cond == "16b" else SEMENTES[0]
                perfil = lp._perfil_live(primeiro_chunk=True, max_steps=p)
                assert perfil["num_steps"] == p, perfil
                t = time.perf_counter()
                audio = app._generate_chunk(modelo, texto, "pt", conds, ref_text, perfil)
                ms = (time.perf_counter() - t) * 1000
                # o MESMO pós do Live (trim→normalize→EQ/fade): sem ele o áudio sai
                # ~40 dB baixo no cru e o VAD do STT devolve vazio em tudo
                audio = np.asarray(lp._pos_live(audio), dtype=np.float32).ravel()
                nome = f"t{i}_{cond}_r{r}.wav"
                wav_escreve(saida / "wav" / nome, audio, SR)
                emb = app._speaker_embed_ecapa_x(app._wav_to_mono16k(saida / "wav" / nome))
                registros.append({"texto": i, "bucket": bucket, "cond": cond,
                                  "passos": p, "rep": r, "arquivo": nome,
                                  "gen_ms": round(ms, 1),
                                  "dur_s": round(len(audio) / SR, 3),
                                  "rms": round(float(np.sqrt(np.mean(audio ** 2))), 4),
                                  "emb": emb})
                print(f"  t{i} {cond} r{r}: {ms:.0f} ms · {len(audio) / SR:.2f} s"
                      f"{'' if emb else ' (sem timbre)'}", flush=True)

    app._release_mlx_memory(aggressive=True)

    # ── timbre: cosseno contra o MESMO texto/repetição na condição 16 ────────
    ref = {(x["texto"], x["rep"]): x["emb"] for x in registros if x["cond"] == "16"}
    simil: dict[str, list[float]] = {c: [] for c, _ in CONDICOES if c != "16"}
    for x in registros:
        if x["cond"] == "16" or x["emb"] is None:
            continue
        e = ref.get((x["texto"], x["rep"]))
        if e is not None:
            a = np.asarray(x["emb"])
            b = np.asarray(e)
            simil[x["cond"]].append(float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b))))

    # ── WER: o áudio volta ao STT ────────────────────────────────────────────
    for x in registros:
        try:
            r = app._transcribe(saida / "wav" / x["arquivo"], language="pt")
            hip = (r.get("text") or "").strip()
            x["hip"] = hip
            x["wer"] = round(wer(TEXTOS[x["texto"]][1], hip), 3)
        except Exception as exc:                                   # noqa: BLE001
            x["erro_stt"] = f"{type(exc).__name__}: {exc}"[:120]
            x["wer"] = 1.0

    # ── ABX: pares 16×8 e 16×10, A/B sorteado ────────────────────────────────
    rng = random.Random(875)
    abx = saida / "abx"
    abx.mkdir(exist_ok=True)
    manifesto = []
    for x in registros:
        if x["cond"] not in ("8", "10") or x["rep"] != 0:
            continue
        origem = saida / "wav" / x["arquivo"]
        base = saida / "wav" / x["arquivo"].replace(f"_{x['cond']}_", "_16_")
        if not base.exists():
            continue
        a16 = rng.random() < 0.5
        idx = len(manifesto)
        (abx / f"{idx:02d}_A.wav").write_bytes((base if a16 else origem).read_bytes())
        (abx / f"{idx:02d}_B.wav").write_bytes((origem if a16 else base).read_bytes())
        manifesto.append({"par": f"{idx:02d}", "texto": TEXTOS[x["texto"]][1],
                          "comparado": x["cond"], "A": "16" if a16 else x["cond"],
                          "B": x["cond"] if a16 else "16"})
    (abx / "gabarito.json").write_text(json.dumps(manifesto, ensure_ascii=False, indent=1))

    def _stat(cond, chave, bucket=None):
        v = [x[chave] for x in registros
             if x["cond"] == cond and (bucket is None or x["bucket"] == bucket)
             and x.get(chave) is not None]
        return (round(float(np.median(v)), 1) if chave == "gen_ms" else round(float(np.mean(v)), 3)) if v else None

    resumo = {
        "voz": VOZ, "precisao": "bf16", "contrato": "16b = controle (mesma condicao, outra semente)",
        "por_condicao": {
            c: {
                "gen_ms_mediana": _stat(c, "gen_ms"),
                "timbre_media_curto": (round(float(np.mean([v for x, v in
                                                            zip([y for y in registros
                                                                 if y["cond"] == c and y["bucket"] == "curto"],
                                                                simil.get(c, []))])), 3)
                                       if simil.get(c) else None),
                "timbre_media": round(float(np.mean(simil[c])), 3) if simil.get(c) else None,
                "timbre_min": round(float(min(simil[c])), 3) if simil.get(c) else None,
                "wer_medio": _stat(c, "wer"),
                "wer_max": (max(x["wer"] for x in registros if x["cond"] == c)
                            if any(x["cond"] == c for x in registros) else None),
                "vazios": sum(1 for x in registros if x["cond"] == c and not x.get("hip")),
            } for c, _ in CONDICOES},
        "por_bucket": {b: {c: round(float(np.mean([v for x, v in
                                                   zip([y for y in registros
                                                        if y["cond"] == c and y["bucket"] == b],
                                                       simil.get(c, []))])), 3)
                             for c, _ in CONDICOES if simil.get(c)}
                       for b in ("curto", "longo")},
        "registros": [{k: v for k, v in x.items() if k != "emb"} for x in registros],
    }
    (saida / "resumo.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=1))
    print("\n" + json.dumps(resumo["por_condicao"], ensure_ascii=False, indent=1), flush=True)
    print("\n" + json.dumps(resumo["por_bucket"], ensure_ascii=False, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
