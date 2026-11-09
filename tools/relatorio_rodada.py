#!/usr/bin/env python3
"""Relatório da janela 2×2 (#876 motor × #870 duck rápido): SLOs e veredito.

Lê o que a rodada gravou e imprime as células lado a lado com PASS/FAIL. Não
coleta nada — é leitura de arquivo, para o veredito não depender de planilha.

    .venv-mlx/bin/python tools/relatorio_rodada.py --dir /tmp/rodada-876 \
        --sha <sha-no-ar> --log /tmp/tts-studio.log --marcas <dir>/marcas.txt

Células: `<f0|f1>-<c0|c1>` — f0/f1 é `TTS_LIVE_BARGE_DUCK_RAPIDO` desligada/
ligada, c0/c1 é whisper/parakeet. Espera `c0.json`… na verdade `<celula>.json`,
`eco-<celula>.json`, `ventoinha-<celula>.json`, `rss.csv` e `marcas.txt`
(`<celula> <HH:MM:SS>`, uma por célula, na ordem de execução).

OS LIMITES SÃO DECLARADOS AQUI E NÃO SE MOVEM DEPOIS DE VER NÚMERO — estão
copiados do cabeçalho do `rodada_c0c1.sh`. Arquivo ausente vira INDETERMINADO,
nunca PASSA por omissão.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: limites declarados ANTES da rodada (ver cabeçalho do rodada_c0c1.sh).
LIM_DUCK_P50_MS = 300
LIM_PAUSA_P95_MS = 800
LIM_ECO_FALSO_DUCK_PCT = 5.0

LINHA_ESTAGIO = re.compile(r"stage_fim .*stage=stt ms=(\d+)")
CELULAS = ("f0-c0", "f0-c1", "f1-c0", "f1-c1")


def _ler(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:                                   # noqa: BLE001
        return None


def _curva(p: Path) -> list[tuple[str, int, int]]:
    out = []
    try:
        for linha in p.read_text().splitlines():
            partes = linha.split(",")
            if len(partes) == 3:
                out.append((partes[0], int(partes[1]), int(partes[2])))
    except Exception:                                   # noqa: BLE001
        pass
    return out


def _janelas(p: Path) -> dict[str, tuple[str, str]]:
    """célula → (início, fim) pelas marcas; a última fecha na hora atual."""
    import datetime
    marcas = []
    try:
        for linha in p.read_text().splitlines():
            partes = linha.split()
            if len(partes) == 2:
                marcas.append((partes[0], partes[1]))
    except Exception:                                   # noqa: BLE001
        return {}
    out = {}
    for i, (cel, ini) in enumerate(marcas):
        fim = marcas[i + 1][1] if i + 1 < len(marcas) else datetime.datetime.now().strftime("%H:%M:%S")
        out[cel] = (ini, fim)
    return out


def _pct(serie: list[float], p: float):
    if not serie:
        return None
    s = sorted(serie)
    i = min(len(s) - 1, max(0, int(round((p / 100) * (len(s) - 1)))))
    return round(s[i], 1)


def _estagios(log: Path | None, ini: str, fim: str) -> list[int]:
    if not log:
        return []
    out = []
    try:
        for linha in log.read_text(errors="replace").splitlines():
            if not (ini <= linha[:8] <= fim):
                continue
            m = LINHA_ESTAGIO.search(linha)
            if m:
                out.append(int(m.group(1)))
    except Exception:                                   # noqa: BLE001
        return []
    return out


def _s3(rel: dict | None) -> dict:
    if not rel:
        return {}
    s3 = (rel.get("resumo") or {}).get("S3") or {}
    inter = [l for l in rel.get("falas", []) if l.get("tipo") == "interrupcao"]
    return {
        "duck": _pct([l["s3_duck_ms"] for l in inter if l.get("s3_duck_ms") is not None], 50),
        "duck_n": s3.get("duck_n"),
        "pausa_p95": _pct([l["s3_pausa_ms"] for l in inter if l.get("s3_pausa_ms") is not None], 95),
        "pausa_n": s3.get("pausa_n"),
        "stop": _pct([l["s3_stop_ms"] for l in inter if l.get("s3_stop_ms") is not None], 50),
        "stop_n": s3.get("stop_n"),
        "eco_duck": s3.get("eco_falso_duck"),
        "eco_duck_ic": s3.get("eco_falso_duck_ic95"),
        "eco_corte": s3.get("eco_falso_corte"),
        "eco_n": s3.get("eco_respostas"),
    }


def _s1(rel: dict | None) -> dict:
    if not rel:
        return {}
    s1 = (rel.get("resumo") or {}).get("S1") or {}
    wer = [l["wer_ciclo"] for l in rel.get("falas", [])
           if l.get("papel") == "A" and l.get("wer_ciclo") is not None]
    return {"aceitas": s1.get("pct_aceitas"),
            "wer_a": round(sum(wer) / len(wer), 3) if wer else None}


def _s2(rel: dict | None) -> dict:
    if not rel:
        return {}
    s2 = (rel.get("resumo") or {}).get("S2") or {}
    return {k: s2.get(k) for k in ("n", "p50_ms", "p95_ms", "llm_ttft_p50_ms",
                                   "pos_llm_p50_ms")}


def _cel(rot: str, v, unidade: str = "", dec: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:.{dec}f}{unidade}"


def _ok(cond: bool | None) -> str:
    return "—" if cond is None else ("PASSA" if cond else "FALHA")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--sha", default="?")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--marcas", type=Path)
    args = ap.parse_args(argv)
    d = args.dir

    rels = {c: _ler(d / f"{c}.json") for c in CELULAS}
    eco = {c: _ler(d / f"eco-{c}.json") for c in CELULAS}
    vent = {c: _ler(d / f"ventoinha-{c}.json") for c in CELULAS}
    janelas = _janelas(args.marcas) if args.marcas else {}
    est = {c: _estagios(args.log, *janelas[c]) if c in janelas else [] for c in CELULAS}

    print(f"# Janela 2×2 #876 × #870 — SHA {args.sha} — {d}")
    print("# f0/f1 = TTS_LIVE_BARGE_DUCK_RAPIDO off/on · c0/c1 = whisper/parakeet")
    print("# LIMITES DECLARADOS ANTES: pausa p95 ≤800 ms · duck p50 ≤300 ms · "
          "eco_falso_duck ≤5 % (IC95 sup) · eco_falso_corte ≤ C1/f0 · S1 e S2 sem piora\n")
    print("  célula   n_ciclos  stt_p50  werA    duck_p50  pausa_p95  stop_p50  "
          "eco_duck%  eco_corte%")
    for c in CELULAS:
        s1, s3 = _s1(rels[c]), _s3(rels[c])
        print(f"  {c:<8} {str(len(rels[c].get('falas', [])) if rels[c] else '—'):<9} "
              f"{_cel('', _pct(est[c], 50)):<8} {_cel('', s1.get('wer_a'), '', 3):<7} "
              f"{_cel('', s3.get('duck')):<9} {_cel('', s3.get('pausa_p95')):<10} "
              f"{_cel('', s3.get('stop')):<9} "
              f"{_cel('', (s3.get('eco_duck') or 0) * 100 if s3.get('eco_duck') is not None else None, '', 1):<10} "
              f"{_cel('', (s3.get('eco_corte') or 0) * 100 if s3.get('eco_corte') is not None else None, '', 1)}")

    # ── veredito da flag (#870), no motor novo (C1) ─────────────────────────
    b, t = _s3(rels["f0-c1"]), _s3(rels["f1-c1"])
    s1b, s1t = _s1(rels["f0-c1"]), _s1(rels["f1-c1"])
    s2b, s2t = _s2(rels["f0-c1"]), _s2(rels["f1-c1"])
    cond = {
        "pausa p95 ≤ 800 ms": (None if t.get("pausa_p95") is None
                               else t["pausa_p95"] <= LIM_PAUSA_P95_MS),
        "duck p50 ≤ 300 ms": None if t.get("duck") is None else t["duck"] <= LIM_DUCK_P50_MS,
        "eco_falso_duck ≤ 5 %": (None if not t.get("eco_duck_ic")
                                 else t["eco_duck_ic"][1] <= LIM_ECO_FALSO_DUCK_PCT),
        "eco_falso_corte ≤ C1/f0": (None if None in (t.get("eco_corte"), b.get("eco_corte"))
                                    else t["eco_corte"] <= b["eco_corte"]),
        "S1 sem piora": (None if None in (s1t.get("aceitas"), s1b.get("aceitas"))
                         else s1t["aceitas"] >= s1b["aceitas"]),
        "S2 sem piora": (None if None in (s2t.get("p50_ms"), s2b.get("p50_ms"))
                         else s2t["p50_ms"] <= s2b["p50_ms"]),
    }
    print("\n## Veredito da flag `TTS_LIVE_BARGE_DUCK_RAPIDO` (célula f1-c1)\n")
    for k, v in cond.items():
        print(f"  {k:<28} {_ok(v)}")
    if any(v is False for v in cond.values()):
        veredito = "NÃO LIGA"
    elif all(v is True for v in cond.values()):
        veredito = "LIGA"
    else:
        veredito = "INDETERMINADO (falta amostra)"
    print(f"\n  >>> {veredito}\n")

    # ── eixo do #876 (motor), nas duas flag states ──────────────────────────
    print("## Eixo do motor (#876): c0 × c1 dentro de cada flag state\n")
    for f in ("f0", "f1"):
        a, z = _s1(rels[f"{f}-c0"]), _s1(rels[f"{f}-c1"])
        pa, pz = _pct(est[f"{f}-c0"], 50), _pct(est[f"{f}-c1"], 50)
        da = None if None in (a.get("wer_a"), z.get("wer_a")) else (z["wer_a"] - a["wer_a"]) * 100
        print(f"  {f}: stt p50 {_cel('', pa)} → {_cel('', pz)} ms "
              f"({_ok(None if pz is None else pz <= 750)}) · WER A "
              f"{_cel('', a.get('wer_a'), '', 3)} → {_cel('', z.get('wer_a'), '', 3)} "
              f"({_cel('', da, ' pp', 1)}; {_ok(None if da is None else da <= 2.0)})")

    print("\n## Falso turno por ruído (fases)\n")
    for c in CELULAS:
        e, v = eco.get(c) or {}, vent.get(c) or {}
        print(f"  {c}: eco {_cel('', (e.get('taxa') or 0) * 100 if e.get('taxa') is not None else None, '%', 1)} "
              f"IC95 {e.get('ic95')} (n={e.get('janelas')}) · "
              f"ventoinha {_cel('', (v.get('taxa') or 0) * 100 if v.get('taxa') is not None else None, '%', 1)} "
              f"(n={v.get('janelas')})")

    print("\n## S1–S5 por célula\n")
    for c in CELULAS:
        r = rels[c]
        if not r:
            print(f"  {c}: (sem JSON)")
            continue
        print(f"  {c}: S1 {json.dumps(_s1(r), ensure_ascii=False)} "
              f"S2 {json.dumps(_s2(r), ensure_ascii=False)}")
        s5 = (r.get("resumo") or {}).get("S5") or {}
        print(f"        S5 ws_vivo={s5.get('ws_vivo_no_fim')} erro={s5.get('erro_ws')} "
              f"erros={(r.get('resumo') or {}).get('S5', {}).get('erros')}")

    curva = _curva(d / "rss.csv")
    print(f"\n## Curva RSS/swap ({len(curva)} amostras, 1/min)\n")
    if curva:
        print("  iso                       rss_mb   swap_mb")
        for iso, rss, swap in curva:
            print(f"  {iso:<24} {rss:>7} {swap:>9}")
    else:
        print("  (sem amostras — rss.csv ausente)")

    falsos = [l for c in CELULAS for l in ((eco.get(c) or {}).get("detalhe") or [])
              if l.get("falso")]
    if falsos:
        print(f"\n## Janelas de eco com falso turno ({len(falsos)})\n")
        for l in falsos[:25]:
            print(f"  k={l.get('k')} inicio_s={l.get('inicio_s')} eventos={l.get('eventos')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
