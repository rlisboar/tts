#!/usr/bin/env python3
"""Bench offline do motor de turnos do Live (LIVE-2).

Mede o que o enunciado pede registrar: **taxa de falso barge-in** (eco do TTS
sozinho disparando turno) e a latência de detecção, com o Silero ONNX real,
sem MLX e sem rede. Serve para calibrar `barge_in_margin_db` / `barge_in_ms` e
para decidir quando ligar o fallback de UI "segurar pra falar".

Cenários (mic = eco_gain*TTS + voz_gain*humano + ruído de fundo):
  - `voz`: humano fala por cima do playback (barge-in verdadeiro);
  - `eco`: só o eco, com o TTS em chunks e pausas entre frases — o caso que mais
    provoca falso positivo (o eco cai na pausa e o próximo chunk entra como
    transiente). Nenhum barge-in aqui é falso;
  - `sem_eco`: o mic entrega fala ANTES e durante o playback e o playback não
    acrescenta eco (mic falso do Chromium, fone de ouvido). O motor tem de
    perceber que não há eco — senão calibra a fala como eco e nunca dispara.

Uso:
    ./.venv-mlx/bin/python smoke_live_turns.py                 # tabela padrão
    ./.venv-mlx/bin/python smoke_live_turns.py --wav caminho.wav
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

import live_turns as lt  # noqa: E402  (depois do sys.path)

SR = lt.SAMPLE_RATE
CHUNK = 1600                       # 100 ms: o mesmo tamanho que o WS do LIVE-1 usa
VOZ_DBFS = -18.0                   # fala a ~15 cm do mic
NIVEL_RUIDO_DBFS = -55.0
PERDA_ECO_DB = 10.0                # atenuação alto-falante -> mic (payload vs eco)


def _db(x: float) -> float:
    return 20.0 * np.log10(max(x, 1e-9))


def _ganho(dbfs: float) -> float:
    return 10.0 ** (dbfs / 20.0)


def carregar_wav(caminho: Path, dur_s: float) -> np.ndarray:
    """WAV do app (24 kHz) → float32 mono 16 kHz normalizado em RMS.

    RMS (e não pico) porque os níveis deste bench são comparados com o `dbfs`
    que o motor calcula — que também é RMS por frame."""
    import soundfile as sf
    from scipy.signal import resample_poly

    a, sr = sf.read(str(caminho), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    a = resample_poly(a, SR, int(sr)).astype(np.float32)
    a = a / max(1e-9, float(np.sqrt(np.mean(a * a))))
    falta = int(dur_s * SR) - a.size
    return np.tile(a, 1 + max(0, falta) // a.size + 1)[: int(dur_s * SR)].copy()


def fala_em_chunks(eco: np.ndarray, pausa_ms: int = 180) -> np.ndarray:
    """TTS por sentença: frases curtas com pausa — como o pipeline real manda."""
    pedaco = int(1.1 * SR)
    pausa = np.zeros(int(pausa_ms / 1000 * SR), dtype=np.float32)
    return np.concatenate([np.concatenate([eco[i:i + pedaco], pausa])
                           for i in range(0, len(eco), pedaco)])


def wavs_disponiveis() -> list[Path]:
    return sorted((RAIZ / "voices").glob("*.wav"))


def rodada(*, eco_dbfs: float, com_voz: bool, semente: int,
           voz_dbfs: float = VOZ_DBFS, atraso_voz_s: float = 2.0,
           margem_db: float | None = None, quantil: float | None = None,
           teto: int | None = None, sem_eco: bool = False) -> dict:
    """Uma sessão simulada de 6 s com playback ligado o tempo todo.

    Nível do mic = eco + voz (+ ruído), tudo em RMS dBFS. `voz_dbfs` acima de
    `eco_dbfs` é a premissa do barge-in (você fala mais alto que o eco)."""
    rng = np.random.default_rng(semente)
    dur = 6.0
    n = int(dur * SR)
    wavs = wavs_disponiveis()
    voz = carregar_wav(wavs[semente % len(wavs)], dur)
    eco = fala_em_chunks(carregar_wav(wavs[(semente + 1) % len(wavs)], dur))

    if sem_eco:
        # regime do mic falso do Chromium / fone de ouvido: o mic entrega fala
        # ANTES e durante o playback, e o playback (marcado ~2 s depois) não
        # acrescenta eco nenhum ao sinal
        mic = (_ganho(voz_dbfs) * voz)[:n]
        inicio_playback_s = atraso_voz_s
    else:
        mic = (_ganho(eco_dbfs) * eco)[:n]
        inicio_playback_s = 0.0
        if com_voz:
            # o humano entra ~2 s depois do início do playback
            inicio = int(atraso_voz_s * SR)
            falado = _ganho(voz_dbfs) * voz[: int(1.4 * SR)]
            mic[inicio:inicio + falado.size] += falado
    mic = mic + _ganho(NIVEL_RUIDO_DBFS) * rng.normal(0, 1, n).astype(np.float32)

    over = {}
    if margem_db is not None:
        over["barge_in_margin_db"] = margem_db
    if quantil is not None:
        over["eco_calibracao_quantil"] = quantil
    if teto is not None:
        over["eco_calibracao_teto"] = teto
    cfg = lt.Config(**over)
    motor = lt.TurnEngine(cfg)
    # o nível do PAYLOAD é o do TTS digital; o que o mic ouve é o eco atenuado
    # (o motor compara os dois para saber se o playback somou eco de verdade)
    nivel_payload = (voz_dbfs if sem_eco else eco_dbfs) + PERDA_ECO_DB
    marcado = False
    eventos = []
    for i in range(0, n, CHUNK):
        if not marcado and i / SR >= inicio_playback_s:
            motor.set_speaking(True, nivel_dbfs=nivel_payload)
            marcado = True
        eventos += motor.feed(np.clip(mic[i:i + CHUNK], -1, 1).astype(np.float32))

    barge = [e for e in eventos if e.tipo == "barge_in"]
    # rodando offline o `t_ms` é relógio de parede (30x mais rápido que o áudio),
    # então o instante do disparo sai da posição em amostras do evento
    c = lt.Config()
    atraso_frames = (c._frames_barge + c._frames_prefix + c._frames_envelope) * lt.FRAME_SAMPLES
    disparos = [(e.amostra + atraso_frames) / SR * 1000 for e in barge]
    corte = (inicio_playback_s * 1000 - 200) if (com_voz or sem_eco) else 0
    falsos = sum(1 for d in disparos if d < corte)
    verdadeiros = [d for d in disparos if d >= corte]
    curtos = sum(1 for e in eventos if e.tipo == "speech_end" and e.curto)
    return {"barge_in": len(barge), "falsos": falsos,
            "detectou": bool(verdadeiros), "curtos": curtos,
            "latencia_ms": round(min(verdadeiros) - atraso_voz_s * 1000) if verdadeiros else None,
            "stats": motor.estatisticas()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeticoes", type=int, default=6)
    ap.add_argument("--margem", type=float, default=None,
                    help="barge_in_margin_db (default: o do módulo)")
    ap.add_argument("--quantil", type=float, default=None,
                    help="eco_calibracao_quantil (default: o do módulo)")
    ap.add_argument("--teto", type=int, default=None,
                    help="eco_calibracao_teto (default: o do módulo)")
    ap.add_argument("--json", action="store_true", help="saída crua p/ registro")
    args = ap.parse_args()

    if len(wavs_disponiveis()) < 1:
        print("nenhum wav em voices/ (grave uma voz na UI)", flush=True)
        return 2

    if not args.json:
        print(f"backend do VAD: {lt.BACKEND or 'sera carregado agora'} | "
              f"silence_ms={lt.Config().silence_ms} barge_ms={lt.Config().barge_in_ms} "
              f"margem_eco={lt.Config().barge_in_margin_db}dB", flush=True)

    cenarios = [(eco, voz, False) for eco in (-42.0, -36.0, -30.0, -24.0)
                for voz in (True, False)]
    cenarios.append((-30.0, True, True))          # sem eco (mic falso/fone)
    linhas = []
    for eco_dbfs, com_voz, sem_eco in cenarios:
            reps = [rodada(eco_dbfs=eco_dbfs, com_voz=com_voz, semente=s,
                           margem_db=args.margem, quantil=args.quantil,
                           teto=args.teto, sem_eco=sem_eco)
                    for s in range(args.repeticoes)]
            disparos = sum(r["barge_in"] for r in reps)
            falsos = sum(r["falsos"] for r in reps)
            lat = [r["latencia_ms"] for r in reps if r["latencia_ms"] is not None]
            linhas.append({
                "cenario": "sem_eco" if sem_eco else ("voz" if com_voz else "eco"),
                "eco_dbfs": eco_dbfs,
                "disparos": disparos,
                "esperados": (sum(1 for r in reps if r["detectou"]) if com_voz
                              else sum(1 for r in reps if r["barge_in"] == 0)),
                "falsos": falsos,
                "taxa_falso": (falsos / disparos) if disparos else 0.0,
                "lat_p50": int(np.percentile(lat, 50)) if lat else None,
                "lat_p90": int(np.percentile(lat, 90)) if lat else None,
                "curtos": sum(r["curtos"] for r in reps),
                "sugeriu_hold": any(r["stats"]["hold_to_talk_sugerido"] for r in reps),
            })

    if args.json:
        print(json.dumps(linhas, indent=2, ensure_ascii=False))
        return 0

    print(f"\n{'cenario':8} {'eco':>7} {'disparos':>9} {'esperados':>10} "
          f"{'falsos':>7} {'taxa_falso':>11} {'lat_p50':>8} {'lat_p90':>8}")
    for linha in linhas:
        print(f"{linha['cenario']:8} {linha['eco_dbfs']:>7.0f} {linha['disparos']:>9} "
              f"{linha['esperados']:>10} {linha['falsos']:>7} "
              f"{linha['taxa_falso']:>11.2f} {str(linha['lat_p50']):>8} "
              f"{str(linha['lat_p90']):>8}")

    com_voz = [t for t in linhas if t["cenario"] == "voz"]
    eco = [t for t in linhas if t["cenario"] == "eco"]
    detecta = sum(t["esperados"] for t in com_voz)
    esperados = args.repeticoes * len(com_voz)
    falso_eco = sum(t["falsos"] for t in linhas)
    print(f"\nbarge-in verdadeiro detectado: {detecta}/{esperados} "
          f"({detecta / esperados * 100:.0f}%)")
    print(f"disparo ANTES da fala (falso): {falso_eco} em "
          f"{sum(t['disparos'] for t in linhas)} disparos; eco puro sem disparo: "
          f"{sum(t['esperados'] for t in eco)}/{args.repeticoes * len(eco)} rodadas")
    pior = max((t["lat_p90"] or 0) for t in com_voz)
    print(f"latencia p90 maxima (fala->barge_in): {pior} ms "
          f"(a janela de {lt.Config().barge_in_ms} ms ja esta no pre-roll do audio)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())