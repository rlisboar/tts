#!/usr/bin/env python3
"""Série do estágio `stt` do Live no log do app — quanto dura e COMO fecha (#943).

Read-only: só lê o log (arquivo ou stdin). Serve para comparar a cauda do STT
antes e depois do conserto do #943 (o estágio só abre no `speech_start` do
pipeline; cancel não espera o whisper) sem ninguém reinterpretar grep na mão.

O que conta como FECHAMENTO do intervalo (primeiro que aparecer depois do
`stage_fim stage=stt` da MESMA sessão):

    transcript  — o STT devolveu (o caso bom)
    interrupted — barge/cancel derrubou o turno
    tts         — saiu áudio do turno anterior no meio (o estágio "stt" não
                  devia estar aberto; é o defeito que o #943 removeu)
    turn_complete / fecha — o turno acabou por outro caminho
    vazio       — nada depois (log cortado)

`pendente` marca intervalo com `turno_pendente`/`barge_decisao` no meio: são os
casos em que o áudio caiu na fila e o relógio corria sem STT em voo.

Uso:
    tools/stage_stt.py /tmp/tts-studio.log [--desde HH:MM:SS] [--ate HH:MM:SS]
    ssh mini cat /tmp/tts-studio.log | tools/stage_stt.py -
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

LINHA = re.compile(r"^(\d\d:\d\d:\d\d) \[live\] (\S+)(.*)$")
SESS = re.compile(r"sess=(\w+)")
MS = re.compile(r"\bms=(\d+)")

FECHA = {"transcript_user": "transcript", "interrupted": "interrupted",
         "turn_complete": "turn_complete", "fecha": "fecha"}
PENDENTE = {"turno_pendente", "barge_decisao"}


def eventos(caminho: str):
    fluxo = sys.stdin if caminho == "-" else Path(caminho).open(encoding="utf-8",
                                                               errors="replace")
    for linha in fluxo:
        m = LINHA.match(linha.rstrip("\n"))
        if not m:
            continue
        hora, tipo, resto = m.groups()
        s = SESS.search(resto)
        if not s:
            continue
        yield hora, s.group(1), tipo, resto


def pct(valores: list[int], p: float) -> int:
    if not valores:
        return 0
    ordenado = sorted(valores)
    i = min(len(ordenado) - 1, int(round((p / 100) * (len(ordenado) - 1))))
    return ordenado[i]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="arquivo de log do app, ou - para stdin")
    ap.add_argument("--desde", default="00:00:00", help="corte de início HH:MM:SS")
    ap.add_argument("--ate", default="23:59:59", help="corte de fim HH:MM:SS")
    ap.add_argument("--minimo-ms", type=int, default=2000,
                    help="só detalha intervalos a partir daqui (padrão 2000)")
    args = ap.parse_args()

    por_sess: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for hora, sess, tipo, resto in eventos(args.log):
        if not (args.desde <= hora <= args.ate):
            continue
        por_sess[sess].append((hora, tipo, resto))

    duracoes: list[int] = []
    fechamentos: Counter[str] = Counter()
    casos: list[tuple[int, str, str, bool]] = []
    limpos: list[int] = []
    com_pendente: list[int] = []

    for sess, evs in por_sess.items():
        for i, (hora, tipo, resto) in enumerate(evs):
            if tipo != "stage_fim" or "stage=stt" not in resto:
                continue
            m = MS.search(resto)
            if not m:
                continue
            ms = int(m.group(1))
            duracoes.append(ms)
            como, pendente = "vazio", False
            for _, t2, r2 in evs[i + 1:]:
                if t2 in PENDENTE or "turno_pendente" in r2:
                    pendente = True
                if t2 in FECHA:
                    como = FECHA[t2]
                    break
                if t2 == "stage_inicio" and "stage=tts" in r2:
                    como = "tts"
                    break
            fechamentos[como] += 1
            (com_pendente if pendente else limpos).append(ms)
            if ms >= args.minimo_ms:
                casos.append((ms, sess, como, pendente))

    n = len(duracoes)
    print(f"intervalos de estágio stt: {n}")
    if n:
        print(f"  p50 {pct(duracoes, 50)} ms · p95 {pct(duracoes, 95)} ms · "
              f"max {max(duracoes)} ms · >= {args.minimo_ms} ms: {len(casos)}")
        print("fechamento: " + " · ".join(f"{k}={v}" for k, v in
                                          sorted(fechamentos.items(),
                                                 key=lambda kv: -kv[1])))
        for rotulo, serie in (("sem pendente", limpos),
                              ("com pendente", com_pendente)):
            if serie:
                print(f"  {rotulo}: n={len(serie)} p50={pct(serie, 50)} "
                      f"p95={pct(serie, 95)} max={max(serie)}")
    if casos:
        print("\ncasos longos:")
        for ms, sess, como, pendente in sorted(casos, reverse=True):
            print(f"  {ms:6d} ms  sess={sess}  fecha={como}"
                  f"{'  COM PENDENTE' if pendente else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
