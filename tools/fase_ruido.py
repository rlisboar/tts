#!/usr/bin/env python3
"""Fases de RUÍDO da janela do #876: eco puro e ventoinha, sem fala humana.

As duas fases existem para provar que o motor do STT (C0 = whisper, C1 =
Parakeet) não ganha turno falso. Elas rodam no MESMO run da bancada de ciclos e
reusam o `Cliente` e o handshake (`abre_sessao`) de `tools/live_bench.py` — não
há segunda implementação de cliente para divergir.

  eco       — o assistente fala (um gatilho curto de fala humana) e, durante o
              playback, NINGUÉM fala: o que voltar pelo mic é só o eco atenuado.
              Toda janela de playback que abre turno é FALSO TURNO por eco.
  ventoinha — nenhuma fala, só o piso de ruído, por N minutos. Todo `speech_start`
              é FALSO TURNO por ruído.

Saída (JSON): `falsos` e `respostas` são CONTAGEM — a taxa é `falsos/respostas`.
Booleano não serve: `1/10` viraria "100 %" e não daria para ler a resolução.

Uso (dentro do mini, contra a instância de bancada da 7890):

    .venv-mlx/bin/python tools/fase_ruido.py eco --audio ~/tts-bench/corpus-live/corpus --voice flvoz \
        --url ws://127.0.0.1:7890/api/live/ws --minutos 5 --out /tmp/fase-eco.json
    .venv-mlx/bin/python tools/fase_ruido.py ventoinha --minutos 5 \
        --url ws://127.0.0.1:7890/api/live/ws --out /tmp/fase-ventoinha.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_bench as lb  # noqa: E402 — o Cliente e o handshake moram na bancada

#: gatilho de fala humana: curto de propósito (só para haver resposta tocando).
GATILHO_S = 0.7
#: teto de uma janela de playback; passou disso, segue para o próximo gatilho.
JANELA_MAX_S = 40.0


async def fase_eco(c: lb.Cliente, corpus: list[dict], minutos: float) -> dict:
    """Só eco no mic: cada janela de playback que abre turno é falso turno."""
    prazo = time.monotonic() + minutos * 60
    respostas = falsos = 0
    detalhe: list[dict] = []
    k = 0
    while time.monotonic() < prazo and c.vivo:
        gatilho = corpus[k % len(corpus)]["audio"][: int(GATILHO_S * lb.SR_MIC)]
        k += 1
        if not await lb._fala_ou_morte(c, c.falar(gatilho), 30):
            break
        fim_fala = c.voz_t_fim or c.agora()
        # a resposta tem de começar a tocar; sem áudio não há eco a medir
        if not await c.espera(lambda: c.bin_apos(fim_fala) is not None, 30):
            detalhe.append({"k": k, "sem_audio": True})
            continue
        ini = c.bin_apos(fim_fala)
        respostas += 1
        # janela = playback correndo SEM fala humana injetada
        await c.espera(lambda: (len(c.play) == 0
                                or c.agora() - ini > JANELA_MAX_S
                                or not c.vivo), JANELA_MAX_S)
        fim = c.agora()
        eventos = [ty for _, ty, _ in c.evs(ini, ("speech_start", "interrupted",
                                                  "transcript_user"), ate=fim)]
        falso = bool(eventos)
        falsos += falso
        detalhe.append({"k": k, "inicio_s": round(ini, 2),
                        "dur_s": round(fim - ini, 2), "falso": falso,
                        "eventos": eventos})
        c.play = np.zeros(0, dtype=np.float32)     # descarta a cauda do turno
        await asyncio.sleep(1.0)
    return {"respostas": respostas, "falsos": falsos, "detalhe": detalhe}


async def fase_ventoinha(c: lb.Cliente, minutos: float) -> dict:
    """Nenhuma fala: só o piso de ruído. Todo turno aberto é falso."""
    t_ini = c.agora()
    prazo = time.monotonic() + minutos * 60
    while time.monotonic() < prazo and c.vivo:
        await asyncio.sleep(0.5)
    fim = c.agora()
    evs = [(t, ty) for t, ty, _ in c.evs(t_ini, ("speech_start", "transcript_user",
                                                 "turn_complete"), ate=fim)]
    # `respostas` aqui é o nº de janelas de observação — 1 por fase, e a taxa
    # sai em falsos por fase (não há turno esperado para servir de denominador).
    return {"respostas": 1, "falsos": len(evs),
            "duracao_s": round(fim - t_ini, 1),
            "eventos": [ty for _, ty in evs],
            "detalhe": [{"k": i, "inicio_s": round(t, 2), "eventos": [ty]}
                        for i, (t, ty) in enumerate(evs)]}


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("fase", choices=("eco", "ventoinha"))
    ap.add_argument("--url", default="ws://127.0.0.1:7890/api/live/ws")
    ap.add_argument("--audio", type=Path, help="corpus (obrigatório na fase eco)")
    ap.add_argument("--minutos", type=float, default=5.0)
    ap.add_argument("--eco-db", type=float, default=-15.0)
    ap.add_argument("--eco-atraso-ms", type=int, default=100)
    ap.add_argument("--ruido-dbfs", type=float, default=-60.0)
    ap.add_argument("--prewarm-s", type=float, default=20.0)
    ap.add_argument("--voice", default="",
                    help="voice_id da sessão (#728: na bancada, `flvoz` do corpus público)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    corpus = lb.carrega_corpus(args.audio) if args.audio else []
    if args.fase == "eco" and not corpus:
        raise SystemExit("fase eco precisa de --audio com gravações e ref_text")

    ws, c, rx = await lb.abre_sessao(args.url, args.eco_db, args.eco_atraso_ms,
                                     args.ruido_dbfs, voice=args.voice,
                                     prewarm_s=args.prewarm_s)
    mic = asyncio.create_task(c.mic())
    await asyncio.sleep(1.5)
    try:
        r = (await fase_eco(c, corpus, args.minutos) if args.fase == "eco"
             else await fase_ventoinha(c, args.minutos))
    finally:
        c.vivo = False
        mic.cancel()
        rx.cancel()
        try:
            await ws.close()
        except Exception:                                  # noqa: BLE001
            pass

    n, k = r["respostas"], r["falsos"]
    r.update({"fase": args.fase, "quando": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "taxa": round(k / n, 3) if n else None, "ic95": lb._ic95(k, n),
              "erro_ws": c.erro,
              "meta": "falso turno ≤5 % — ponto; IC95 de Wilson vai junto só como "
                      "resolução"})
    if args.out:
        args.out.write_text(json.dumps(r, ensure_ascii=False, indent=1))
    print(f"{args.fase}: {k}/{n} falso(s) — taxa {r['taxa']} "
          f"IC95 {r['ic95']}" + (f" | erro_ws={c.erro}" if c.erro else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
