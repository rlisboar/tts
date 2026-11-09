#!/usr/bin/env python3
"""Relatório da janela 2×2 (#876 motor × #870 duck rápido): SLOs e veredito.

Lê o que a rodada gravou e imprime as células lado a lado com PASS/FAIL. Não
coleta nada — é leitura de arquivo, para o veredito não depender de planilha.

    .venv-mlx/bin/python tools/relatorio_rodada.py --dir /tmp/rodada-876 \
        --sha <sha-no-ar> --log /tmp/tts-studio.log --marcas <dir>/marcas.txt \
        --ordem <dir>/ordem.txt --aquecimento 1

Células: `<f0|f1>-<c0|c1>` — f0/f1 é `TTS_LIVE_BARGE_DUCK_RAPIDO` desligada/
ligada, c0/c1 é whisper/parakeet.

AQUECIMENTO: as `--aquecimento` primeiras falas-pares de cada célula são
descartadas (o bloco reinicia o app e o `_cond_for` em cache miss custa
~1500 ms). Por isso as métricas são RECOMPUTADAS das falas e não lidas do
`resumo` da bancada, que inclui o aquecimento.

n=10 não sustenta p95 sozinha: as tabelas trazem p50 e máximo ao lado da p95 e o
veredito diz que a decisão não sai só da p95.

OS LIMITES SÃO DECLARADOS AQUI E NÃO SE MOVEM DEPOIS DE VER NÚMERO — estão
copiados do cabeçalho do `rodada_c0c1.sh`. Arquivo ausente vira INDETERMINADO,
nunca PASSA por omissão. A base do `eco_falso_corte` é C1/F0 (não o C1 da
célula): com o C1 da própria célula o duck viraria confundidor.
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
#: abaixo disto a p95 é frouxa e o veredito tem de dizer isso.
N_MINIMO_P95 = 20

LINHA_ESTAGIO = re.compile(r"stage_fim .*stage=stt ms=(\d+)")
CELULAS = ("f0-c0", "f0-c1", "f1-c0", "f1-c1")


def _ler(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:                                   # noqa: BLE001
        return None


def _amostra(rel: dict | None, aq: int) -> list[dict]:
    """Falas da célula SEM o aquecimento (2 falas por ciclo)."""
    if not rel:
        return []
    corte = 2 * max(0, aq)
    return [l for l in rel.get("falas", []) if (l.get("fala") or 0) >= corte]


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


def _janelas(p: Path | None) -> dict[str, tuple[str, str]]:
    import datetime
    marcas = []
    try:
        for linha in (p.read_text().splitlines() if p else []):
            partes = linha.split()
            if len(partes) == 2:
                marcas.append((partes[0], partes[1]))
    except Exception:                                   # noqa: BLE001
        return {}
    out = {}
    for i, (cel, ini) in enumerate(marcas):
        fim = (marcas[i + 1][1] if i + 1 < len(marcas)
               else datetime.datetime.now().strftime("%H:%M:%S"))
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


def _s3(amostra: list[dict]) -> dict:
    inter = [l for l in amostra if l.get("tipo") == "interrupcao"]
    eco = [l for l in amostra if "eco_falso_corte" in l]
    duck = [l["s3_duck_ms"] for l in inter if l.get("s3_duck_ms") is not None]
    pausa = [l["s3_pausa_ms"] for l in inter if l.get("s3_pausa_ms") is not None]
    stop = [l["s3_stop_ms"] for l in inter if l.get("s3_stop_ms") is not None]
    return {"duck": _pct(duck, 50), "duck_n": len(duck),
            "pausa_p50": _pct(pausa, 50), "pausa_p95": _pct(pausa, 95),
            "pausa_max": max(pausa) if pausa else None, "pausa_n": len(pausa),
            "stop": _pct(stop, 50), "stop_n": len(stop),
            "eco_duck": (sum(bool(l["eco_falso_duck"]) for l in eco) / len(eco)
                         if eco else None),
            "eco_corte": (sum(bool(l["eco_falso_corte"]) for l in eco) / len(eco)
                          if eco else None),
            "eco_n": len(eco)}


def _s1(amostra: list[dict]) -> dict:
    obs = [l for l in amostra if not l.get("recusada_identidade")]
    wer = [l["wer_ciclo"] for l in obs if l.get("wer_ciclo") is not None]
    aceitas = sum(bool(l.get("aceito")) for l in obs)
    return {"n": len(obs),
            "aceitas": round(100 * aceitas / len(obs), 1) if obs else None,
            "wer_a": (round(sum(wer) / len(wer), 3) if wer else None)}


def _s2(amostra: list[dict]) -> dict:
    s2 = [l["s2_ms"] for l in amostra if l.get("s2_ms") is not None]
    return {"n": len(s2), "p50_ms": _pct(s2, 50)}


def _cel(v, unidade: str = "", dec: int = 0) -> str:
    return "—" if v is None else f"{v:.{dec}f}{unidade}"


def _ok(cond: bool | None) -> str:
    return "—" if cond is None else ("PASSA" if cond else "FALHA")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--sha", default="?")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--marcas", type=Path)
    ap.add_argument("--ordem", type=Path)
    ap.add_argument("--aquecimento", type=int, default=1)
    args = ap.parse_args(argv)
    d, aq = args.dir, max(0, args.aquecimento)

    amostras = {c: _amostra(_ler(d / f"{c}.json"), aq) for c in CELULAS}
    eco = {c: _ler(d / f"eco-{c}.json") for c in CELULAS}
    vent = {c: _ler(d / f"ventoinha-{c}.json") for c in CELULAS}
    janelas = _janelas(args.marcas)
    est = {c: _estagios(args.log, *janelas[c]) if c in janelas else [] for c in CELULAS}
    ordem = (args.ordem.read_text().strip() if args.ordem
             and args.ordem.exists() else " ".join(CELULAS))

    print(f"# Janela 2×2 #876 × #870 — SHA {args.sha} — {d}")
    print(f"# ORDEM EXECUTADA: {ordem}")
    print(f"# f0/f1 = TTS_LIVE_BARGE_DUCK_RAPIDO off/on · c0/c1 = whisper/parakeet "
          f"· aquecimento descartado: {aq} ciclo(s) por célula")
    print("# LIMITES DECLARADOS ANTES: pausa p95 ≤800 ms · duck p50 ≤300 ms · "
          "eco_falso_duck ≤5 % (IC95 sup) · eco_falso_corte ≤ C1/F0 · S1 e S2 sem piora")
    print(f"# n<{N_MINIMO_P95} por célula: a p95 é frouxa — leia com a mediana e o "
          "máximo ao lado; a decisão NÃO sai só da p95.\n")
    print("  célula   n_falas  stt_p50  werA    duck_p50  pausa_p50  pausa_p95  "
          "pausa_max  stop_p50  eco_duck%  eco_corte%")
    for c in CELULAS:
        s1, s3 = _s1(amostras[c]), _s3(amostras[c])
        print(f"  {c:<8} {s1['n']:<8} {_cel(_pct(est[c], 50)):<8} "
              f"{_cel(s1.get('wer_a'), '', 3):<7} {_cel(s3.get('duck')):<9} "
              f"{_cel(s3.get('pausa_p50')):<10} {_cel(s3.get('pausa_p95')):<10} "
              f"{_cel(s3.get('pausa_max')):<10} {_cel(s3.get('stop')):<9} "
              f"{_cel(None if s3.get('eco_duck') is None else s3['eco_duck'] * 100, '', 1):<10} "
              f"{_cel(None if s3.get('eco_corte') is None else s3['eco_corte'] * 100, '', 1)}")

    # ── veredito da flag (#870), no motor novo (C1) ─────────────────────────
    b, t = _s3(amostras["f0-c1"]), _s3(amostras["f1-c1"])
    s1b, s1t = _s1(amostras["f0-c1"]), _s1(amostras["f1-c1"])
    s2b, s2t = _s2(amostras["f0-c1"]), _s2(amostras["f1-c1"])
    ic_duck = None
    try:
        import live_bench as _lb
        eco_t = [l for l in amostras["f1-c1"] if "eco_falso_duck" in l]
        ic_duck = _lb._ic95(sum(bool(l["eco_falso_duck"]) for l in eco_t), len(eco_t))
    except Exception:                                   # noqa: BLE001
        pass
    cond = {
        "pausa p95 ≤ 800 ms": (None if t.get("pausa_p95") is None
                               else t["pausa_p95"] <= LIM_PAUSA_P95_MS),
        "duck p50 ≤ 300 ms": None if t.get("duck") is None else t["duck"] <= LIM_DUCK_P50_MS,
        "eco_falso_duck ≤ 5 %": (None if not ic_duck
                                 else ic_duck[1] <= LIM_ECO_FALSO_DUCK_PCT),
        "eco_falso_corte ≤ C1/F0": (None if None in (t.get("eco_corte"), b.get("eco_corte"))
                                    else t["eco_corte"] <= b["eco_corte"]),
        "S1 sem piora": (None if None in (s1t.get("aceitas"), s1b.get("aceitas"))
                         else s1t["aceitas"] >= s1b["aceitas"]),
        "S2 sem piora": (None if None in (s2t.get("p50_ms"), s2b.get("p50_ms"))
                         else s2t["p50_ms"] <= s2b["p50_ms"]),
    }
    print("\n## Veredito da flag `TTS_LIVE_BARGE_DUCK_RAPIDO` (célula f1-c1)\n")
    for k, v in cond.items():
        print(f"  {k:<28} {_ok(v)}")
    n_t = len([l for l in amostras["f1-c1"] if l.get("tipo") == "interrupcao"])
    if any(v is False for v in cond.values()):
        veredito = "NÃO LIGA"
    elif all(v is True for v in cond.values()):
        veredito = "LIGA"
    else:
        veredito = "INDETERMINADO (falta amostra)"
    print(f"\n  >>> {veredito}")
    if n_t < N_MINIMO_P95:
        print(f"  (n={n_t} interrupções: p95 sobre {n_t} amostras é frouxa — "
              f"pausa p50={_cel(t.get('pausa_p50'))} ms, máx={_cel(t.get('pausa_max'))} ms; "
              "a decisão não sai só da p95)")

    # ── eixo do #876 (motor), nas duas flag states ──────────────────────────
    print("\n## Eixo do motor (#876): c0 × c1 dentro de cada flag state\n")
    for f in ("f0", "f1"):
        a, z = _s1(amostras[f"{f}-c0"]), _s1(amostras[f"{f}-c1"])
        pa, pz = _pct(est[f"{f}-c0"], 50), _pct(est[f"{f}-c1"], 50)
        da = None if None in (a.get("wer_a"), z.get("wer_a")) else (z["wer_a"] - a["wer_a"]) * 100
        print(f"  {f}: stt p50 {_cel(pa)} → {_cel(pz)} ms "
              f"({_ok(None if pz is None else pz <= 750)}) · WER A "
              f"{_cel(a.get('wer_a'), '', 3)} → {_cel(z.get('wer_a'), '', 3)} "
              f"({_cel(da, ' pp', 1)}; {_ok(None if da is None else da <= 2.0)})")

    print("\n## Falso turno por ruído (fases)\n")
    for c in CELULAS:
        e, v = eco.get(c) or {}, vent.get(c) or {}
        print(f"  {c}: eco {_cel(None if e.get('taxa') is None else e['taxa'] * 100, '%', 1)} "
              f"IC95 {e.get('ic95')} (n={e.get('janelas')}) · "
              f"ventoinha {_cel(None if v.get('taxa') is None else v['taxa'] * 100, '%', 1)} "
              f"(n={v.get('janelas')})")

    print("\n## S1–S5 por célula (amostra sem aquecimento)\n")
    for c in CELULAS:
        r = _ler(d / f"{c}.json")
        if not r:
            print(f"  {c}: (sem JSON)")
            continue
        s5 = (r.get("resumo") or {}).get("S5") or {}
        print(f"  {c}: S1 {json.dumps(_s1(amostras[c]), ensure_ascii=False)} "
              f"S2 {json.dumps(_s2(amostras[c]), ensure_ascii=False)} · "
              f"S5 ws_vivo={s5.get('ws_vivo_no_fim')} erro={s5.get('erro_ws')}")

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
