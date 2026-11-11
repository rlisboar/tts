#!/usr/bin/env python3
"""Corpus PÚBLICO da bancada do Live, a partir do FLEURS pt_br (#728).

A bancada (`live_bench.py`, `fase_ruido.py`, `rodada_c0c1.sh`, `mede_pos_llm.sh`)
lê pares `<id>.wav` + `<id>.json` com `ref_text`. Esta ferramenta os gera do FLEURS
pt_br test (`google/fleurs`, CC BY 4.0), montado em `fala_pt/` por
`tools/bakeoff/wer_pt_real.py --monta` (40 locutores, 16 kHz, `meta.json` com
`refs`). NUNCA de `voices/`: aquilo é o cadastro de vozes do dono (#728).

Saída em `<destino>/`:
    corpus/flNN.wav + flNN.json   ->  o "mic" da bancada (todas as falas menos a última)
    voices/flvoz.wav + flvoz.json ->  a voz da SESSÃO (a última fala, fora do corpus)

O `voices/` da saída é o diretório de vozes da INSTÂNCIA DE BANCADA
(`TTS_VOICES_DIR`): sem ele o app clona a partir do `voices/` do repo.

Fora do `/tmp`: o `com.apple.tmp_cleaner` do macOS apaga do `/tmp` à meia-noite o
que está parado há dias. O padrão é `~/tts-bench/` (nem repo, nem dado do dono).

    python tools/corpus_bancada.py                       # padrões de ~/tts-bench
    python tools/corpus_bancada.py --fleurs F --destino D
Imprime o voice_id da sessão (`flvoz`).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

BANCADA = Path.home() / "tts-bench"
FLEURS_PADRAO = BANCADA / "fleurs" / "fala_pt"
DESTINO_PADRAO = BANCADA / "corpus-live"
VOZ_SESSAO = "flvoz"


def _dentro_de_voices(p: Path) -> bool:
    return "voices" in p.expanduser().resolve().parts


def monta(fleurs: Path, destino: Path) -> str:
    """Gera `destino/corpus` e `destino/voices` a partir de `fleurs/meta.json`.

    Idempotente: refaz do zero as duas pastas (só elas) a cada chamada."""
    fleurs, destino = fleurs.expanduser(), destino.expanduser()
    if _dentro_de_voices(fleurs):
        raise SystemExit(f"fonte dentro de voices/ ({fleurs}): o corpus é público (FLEURS), "
                         "nunca o cadastro de vozes do dono (#728)")
    meta_p = fleurs / "meta.json"
    if not meta_p.is_file():
        raise SystemExit(f"FLEURS não montado em {fleurs} (falta meta.json): "
                         "rode tools/bakeoff/wer_pt_real.py --monta")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    nomes = sorted(meta.get("refs") or {})
    if len(nomes) < 3:
        raise SystemExit(f"FLEURS com {len(nomes)} falas em {fleurs}: preciso de >= 3")
    corpus, vozes = destino / "corpus", destino / "voices"
    for d in (corpus, vozes):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    *mic, ult = nomes                      # a voz da sessão fica FORA do mic
    for i, nome in enumerate(mic):
        vid = f"fl{i:02d}"
        shutil.copy(fleurs / nome, corpus / f"{vid}.wav")
        (corpus / f"{vid}.json").write_text(json.dumps(
            {"id": vid, "name": vid, "ref_text": meta["refs"][nome]},
            ensure_ascii=False), encoding="utf-8")
    shutil.copy(fleurs / ult, vozes / f"{VOZ_SESSAO}.wav")
    (vozes / f"{VOZ_SESSAO}.json").write_text(json.dumps(
        {"id": VOZ_SESSAO, "name": VOZ_SESSAO, "ref_text": meta["refs"][ult]},
        ensure_ascii=False), encoding="utf-8")
    return VOZ_SESSAO


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fleurs", type=Path, default=FLEURS_PADRAO)
    ap.add_argument("--destino", type=Path, default=DESTINO_PADRAO)
    a = ap.parse_args(argv)
    print(monta(a.fleurs, a.destino))
    return 0


if __name__ == "__main__":
    sys.exit(main())
