#!/usr/bin/env python3
"""Relatório da janela 2×2 (#876 motor × #870 duck rápido): SLOs e veredito.

Lê o que a rodada gravou e imprime as células lado a lado com PASS/FAIL. Não
coleta nada — é leitura de arquivo, para o veredito não depender de planilha.

    .venv-mlx/bin/python tools/relatorio_rodada.py --dir /tmp/rodada-876 \
        --sha <sha> --logs <dir>/app-0.log <dir>/app-1.log \
        --marcas <dir>/marcas.txt --aquecimento 1

Células: `<f0|f1>-<c0|c1>` — f0/f1 é `TTS_LIVE_BARGE_DUCK_RAPIDO` desligada/
ligada, c0/c1 é whisper/parakeet. `marcas.txt` tem uma linha por FASE
(`<celula> <aquec|ciclos|eco|ventoinha> <HH:MM:SS>`) e uma `fim <HH:MM:SS>`: a
janela do `stt` é só a fase `ciclos` da célula e começa no FIM do aquecimento —
a fase de eco injeta gatilhos de 0,7 s e, se entrasse, puxaria o p50 para baixo.
A ORDEM EXECUTADA sai das próprias marcas (ordem de 1ª aparição) e é impressa no
topo: é ela que se confere, não um arquivo à parte.

AQUECIMENTO: as `--aquecimento` primeiras falas-pares de cada célula são
descartadas (o bloco reinicia o app e o `_cond_for` em cache miss custa
~1500 ms). Por isso as métricas são RECOMPUTADAS das falas e não lidas do
`resumo` da bancada, que inclui o aquecimento.

n=10 não sustenta p95 sozinha: as tabelas trazem p50 e máximo ao lado, e o
veredito diz que a decisão não sai só da p95.

O critério do `eco_falso_duck` é o PONTO (falsos/respostas), não o IC95: com
n=10 e zero falso o Wilson dá [0; 27,8] e o ramo LIGA nunca passaria. O IC vai
impresso junto, como resolução. Faixas: ≤5 % PASSA · 5–10 % INDETERMINADO ·
>10 % FALHA.

Arquivo ausente vira INDETERMINADO, nunca PASSA por omissão.
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
LIM_ECO_FALSO_INDET_PCT = 10.0
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


def _janelas(p: Path | None) -> dict[tuple[str, str], tuple[str, str]]:
    """(célula, fase) → (início, fim). A janela fecha na PRÓXIMA marca."""
    marcas = []
    try:
        for linha in (p.read_text().splitlines() if p else []):
            partes = linha.split()
            if len(partes) == 3:
                marcas.append((partes[0], partes[1], partes[2]))
            elif len(partes) == 2 and partes[0] == "fim":
                marcas.append((None, "fim", partes[1]))
    except Exception:                                   # noqa: BLE001
        return {}
    out = {}
    for i, (cel, fase, ini) in enumerate(marcas):
        if cel is None:
            continue
        fim = marcas[i + 1][2] if i + 1 < len(marcas) else ini
        out[(cel, fase)] = (ini, fim)
    return out


def _ordem_executada(p: Path | None) -> str:
    """Células na ordem de 1ª aparição no `marcas.txt` (o que de fato rodou)."""
    vistas: list[str] = []
    try:
        for linha in (p.read_text().splitlines() if p else []):
            partes = linha.split()
            if len(partes) == 3 and partes[0] not in vistas:
                vistas.append(partes[0])
    except Exception:                                   # noqa: BLE001
        pass
    return " ".join(vistas) or "(sem marcas)"


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
            "eco_duck": sum(bool(l["eco_falso_duck"]) for l in eco),
            "eco_corte": sum(bool(l["eco_falso_corte"]) for l in eco),
            "eco_n": len(eco)}


def _s1(amostra: list[dict]) -> dict:
    obs = [l for l in amostra if not l.get("recusada_identidade")]
    aceitas = sum(bool(l.get("aceito")) for l in obs)
    return {"n": len(obs),
            "aceitas": round(100 * aceitas / len(obs), 1) if obs else None}


def _wer_por_fala(amostra: list[dict]) -> dict[int, float]:
    return {l["fala"]: l["wer_ciclo"] for l in amostra
            if l.get("wer_ciclo") is not None}


def _wer_par(b: dict[int, float], t: dict[int, float]) -> tuple[float | None, float | None, int]:
    """WER médio das MESMAS falas nos dois lados (o aceite é pareado)."""
    comuns = sorted(set(b) & set(t))
    if not comuns:
        return None, None, 0
    return (round(sum(b[k] for k in comuns) / len(comuns), 3),
            round(sum(t[k] for k in comuns) / len(comuns), 3), len(comuns))


def _s2(amostra: list[dict]) -> dict:
    s2 = [l["s2_ms"] for l in amostra if l.get("s2_ms") is not None]
    return {"n": len(s2), "p50_ms": _pct(s2, 50)}


def _s4(rel: dict | None) -> float | None:
    if not rel:
        return None
    resp = [r for r in rel.get("respostas", []) if r.get("chars")]
    if not resp:
        return None
    return round(100 * sum(bool(r.get("s4_simbolos")) for r in resp) / len(resp), 1)


def _rss(rel: dict | None):
    return ((rel or {}).get("resumo") or {}).get("S5", {}).get("proc_fim", {}).get("rss_mb")


def _cel(v, unidade: str = "", dec: int = 0) -> str:
    return "—" if v is None else f"{v:.{dec}f}{unidade}"


def _ok(cond: bool | None) -> str:
    return "—" if cond is None else ("PASSA" if cond else "FALHA")


def _faixa_eco(falsos: int | None, n: int | None) -> bool | str | None:
    """Ponto: ≤5 % PASSA · 5–10 % INDETERMINADO · >10 % FALHA (zero falso PASSA)."""
    if not n or falsos is None:
        return None
    p = 100 * falsos / n
    if p <= LIM_ECO_FALSO_DUCK_PCT:
        return True
    if p <= LIM_ECO_FALSO_INDET_PCT:
        return "INDETERMINADO"
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--sha", default="?")
    ap.add_argument("--logs", type=Path, nargs="+", default=[])
    ap.add_argument("--marcas", type=Path)
    ap.add_argument("--aquecimento", type=int, default=1)
    args = ap.parse_args(argv)
    d, aq = args.dir, max(0, args.aquecimento)

    # um log por estado de flag: misturar os dois é misturar dois processos
    logs = {("f0" if "app-0" in p.name else "f1"): p for p in args.logs}
    rels = {c: _ler(d / f"{c}.json") for c in CELULAS}
    amostras = {c: _amostra(rels[c], aq) for c in CELULAS}
    eco = {c: _ler(d / f"eco-{c}.json") for c in CELULAS}
    vent = {c: _ler(d / f"ventoinha-{c}.json") for c in CELULAS}
    janelas = _janelas(args.marcas)
    est = {c: _estagios(logs.get(c[:2]), *janelas[(c, "ciclos")])
           if (c, "ciclos") in janelas else [] for c in CELULAS}
    ordem = _ordem_executada(args.marcas)

    print(f"# Janela 2×2 #876 × #870 — SHA {args.sha} — {d}")
    print(f"# ORDEM EXECUTADA (do marcas.txt): {ordem}")
    print("# f0/f1 = TTS_LIVE_BARGE_DUCK_RAPIDO off/on · c0/c1 = whisper/parakeet "
          f"· aquecimento descartado: {aq} ciclo(s) por célula")
    print("# LIMITES DECLARADOS ANTES: pausa p95 ≤800 ms · duck p50 ≤300 ms · "
          "eco_falso_duck ≤5 % (PONTO) · eco_falso_corte ≤ C1/F0 · S1-S4 e RSS sem piora")
    print(f"# n<{N_MINIMO_P95} por célula: a p95 é frouxa — leia com a mediana e o "
          "máximo ao lado; a decisão NÃO sai só da p95.\n")
    print("  célula   n_falas  stt_p50  duck_p50  pausa_p50  pausa_p95  pausa_max  "
          "stop_p50  eco_duck   eco_corte   S4_simb%")
    for c in CELULAS:
        s3 = _s3(amostras[c])
        print(f"  {c:<8} {_s1(amostras[c])['n']:<8} {_cel(_pct(est[c], 50)):<8} "
              f"{_cel(s3.get('duck')):<9} {_cel(s3.get('pausa_p50')):<10} "
              f"{_cel(s3.get('pausa_p95')):<10} {_cel(s3.get('pausa_max')):<10} "
              f"{_cel(s3.get('stop')):<9} "
              f"{s3['eco_duck']}/{s3['eco_n']:<9} {s3['eco_corte']}/{s3['eco_n']:<10} "
              f"{_cel(_s4(rels[c]), '', 1)}")

    # ── veredito da flag (#870), no motor novo (C1) ─────────────────────────
    b, t = _s3(amostras["f0-c1"]), _s3(amostras["f1-c1"])
    s1b, s1t = _s1(amostras["f0-c1"]), _s1(amostras["f1-c1"])
    s2b, s2t = _s2(amostras["f0-c1"]), _s2(amostras["f1-c1"])
    wb, wt, n_par = _wer_par(_wer_por_fala(amostras["f0-c1"]),
                             _wer_por_fala(amostras["f1-c1"]))
    s4b, s4t = _s4(rels["f0-c1"]), _s4(rels["f1-c1"])
    rssb, rsst = _rss(rels["f0-c1"]), _rss(rels["f1-c1"])
    ic_duck = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import live_bench as _lb
        ic_duck = _lb._ic95(t["eco_duck"], t["eco_n"])
    except Exception:                                   # noqa: BLE001
        pass
    cond = {
        "pausa p95 ≤ 800 ms": (None if t.get("pausa_p95") is None
                               else t["pausa_p95"] <= LIM_PAUSA_P95_MS),
        "duck p50 ≤ 300 ms": None if t.get("duck") is None else t["duck"] <= LIM_DUCK_P50_MS,
        "eco_falso_duck (ponto)": _faixa_eco(t.get("eco_duck"), t.get("eco_n")),
        "eco_falso_corte ≤ C1/F0": (None if not b.get("eco_n") or not t.get("eco_n")
                                    else t["eco_corte"] <= b["eco_corte"]),
        "S1 sem piora": (None if None in (s1t.get("aceitas"), s1b.get("aceitas"))
                         else s1t["aceitas"] >= s1b["aceitas"]),
        "WER A sem piora (pareado)": (None if None in (wb, wt) else wt <= wb + 0.02),
        "S2 sem piora": (None if None in (s2t.get("p50_ms"), s2b.get("p50_ms"))
                         else s2t["p50_ms"] <= s2b["p50_ms"]),
        "S3 duck sem piora": (None if None in (t.get("duck"), b.get("duck"))
                              else t["duck"] <= b["duck"]),
        "S4 sem piora": (None if None in (s4t, s4b) else s4t <= s4b),
        "RSS ≤ C1/F0": None if None in (rsst, rssb) else rsst <= rssb,
    }
    print("\n## Veredito da flag `TTS_LIVE_BARGE_DUCK_RAPIDO` (célula f1-c1)\n")
    for k, v in cond.items():
        print(f"  {k:<28} {'INDETERMINADO' if v == 'INDETERMINADO' else _ok(v)}")
    if any(v is False for v in cond.values()):
        veredito = "NÃO LIGA"
    elif all(v is True for v in cond.values()):
        veredito = "LIGA"
    else:
        veredito = "INDETERMINADO (falta amostra ou faixa 5–10 % no eco)"
    print(f"\n  >>> {veredito}")
    print(f"  resolução: eco_falso_duck {t['eco_duck']}/{t['eco_n']} "
          f"(IC95 {ic_duck}) · WER pareado em {n_par} fala(s)")
    # A p95 com n pequeno é frouxa POR CONSTRUÇÃO: mediana e máximo vão escritos
    # ao lado e a ressalva é fixa — a decisão não sai só da p95.
    _n = t.get("pausa_n") or 0
    print(f"  (pausa em {_n} interrupção(ões): p50={_cel(t.get('pausa_p50'))} ms · "
          f"máx={_cel(t.get('pausa_max'))} ms"
          + (f" · n<{N_MINIMO_P95}, p95 frouxa" if _n < N_MINIMO_P95 else "")
          + " — a decisão não sai só da p95)")

    # ── eixo do #876 (motor), nas duas flag states ──────────────────────────
    print("\n## Eixo do motor (#876): c0 × c1 dentro de cada flag state\n")
    for f in ("f0", "f1"):
        a, z = _s1(amostras[f"{f}-c0"]), _s1(amostras[f"{f}-c1"])
        pa, pz = _pct(est[f"{f}-c0"], 50), _pct(est[f"{f}-c1"], 50)
        wa, wz, n = _wer_par(_wer_por_fala(amostras[f"{f}-c0"]),
                             _wer_por_fala(amostras[f"{f}-c1"]))
        da = None if None in (wa, wz) else round((wz - wa) * 100, 1)
        print(f"  {f}: stt p50 {_cel(pa)} → {_cel(pz)} ms "
              f"({_ok(None if pz is None else pz <= 750)}) · WER pareado "
              f"{_cel(wa, '', 3)} → {_cel(wz, '', 3)} ({_cel(da, ' pp', 1)}; "
              f"{_ok(None if da is None else da <= 2.0)}; n={n}) · S1 "
              f"{_cel(a.get('aceitas'), '%', 1)} → {_cel(z.get('aceitas'), '%', 1)} "
              f"({_ok(None if None in (a.get('aceitas'), z.get('aceitas')) else z['aceitas'] >= a['aceitas'])})")

    print("\n## Falso turno por ruído (fases)\n")
    for c in CELULAS:
        e, v = eco.get(c) or {}, vent.get(c) or {}
        print(f"  {c}: eco {e.get('falsos')}/{e.get('respostas')} IC95 {e.get('ic95')} "
              f"· ventoinha {v.get('falsos')} falso(s) em "
              f"{_cel(v.get('duracao_s'), ' s', 0)} (n={v.get('respostas')})")

    print("\n## S1–S5 por célula (amostra sem aquecimento)\n")
    for c in CELULAS:
        r = rels[c]
        if not r:
            print(f"  {c}: (sem JSON)")
            continue
        s5 = (r.get("resumo") or {}).get("S5") or {}
        print(f"  {c}: S1 {json.dumps(_s1(amostras[c]), ensure_ascii=False)} "
              f"S2 {json.dumps(_s2(amostras[c]), ensure_ascii=False)} · "
              f"S4 {_cel(_s4(r), '%', 1)} · RSS {_cel(_rss(r), ' MB', 0)} · "
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
