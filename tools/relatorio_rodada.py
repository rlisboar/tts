#!/usr/bin/env python3
"""Relatório da janela C0 × C1 (#876): SLOs, eco falso e curva de RSS/swap.

Lê os JSONs que a bancada e as fases de ruído gravaram e imprime a comparação
lado a lado com PASS/FAIL pelos aceites do card. Não coleta nada: é só leitura
de arquivo, para a leitura da janela não depender de planilha à mão.

    .venv-mlx/bin/python tools/relatorio_rodada.py --dir /tmp/rodada-876 \
        --sha <sha-no-ar>

Espera em `--dir`: c0.json, c1.json, eco-c0.json, eco-c1.json,
ventoinha-c0.json, ventoinha-c1.json, rss.csv (`<iso>,<rss_mb>,<swap_mb>`).
Arquivo ausente vira `—` no relatório e o SLO correspondente sai INDETERMINADO
(nunca PASSA por omissão).

O estágio `stt` NÃO vem do JSON da bancada: ele sai do log do app (`--log`),
na mesma linha `stage_fim … stage=stt ms=N` que o `tools/stage_stt.py` lê — é o
número do servidor, não do cliente.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LINHA_ESTAGIO = re.compile(r"stage_fim .*stage=stt ms=(\d+)")

#: aceites do card #876 / #874 — mudar aqui é mudar o veredito.
ACEITES = {
    "stt_p50_ms": ("<=", 750, "stt p50 ≤ 0,75 s"),
    "wer_a_delta_pp": ("<=", 2.0, "WER das falas A ≤ C0 + 2 pp"),
    "eco_falso_delta_pp": ("<=", 0.0, "sem falso turno a mais (eco)"),
    "vent_falso_delta_pp": ("<=", 0.0, "sem falso turno a mais (ventoinha)"),
    "rss_delta_mb": ("<=", 0.0, "RSS ≤ C0"),
}


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


def _pct(serie: list[float], p: float) -> float | None:
    if not serie:
        return None
    s = sorted(serie)
    i = min(len(s) - 1, max(0, int(round((p / 100) * (len(s) - 1)))))
    return round(s[i], 1)


def _estagios(log: Path | None, desde: str, ate: str) -> list[int]:
    """Durações do estágio stt no log, dentro da janela [desde, ate] (HH:MM:SS)."""
    if not log:
        return []
    out = []
    try:
        for linha in log.read_text(errors="replace").splitlines():
            if not (desde <= linha[:8] <= ate):
                continue
            m = LINHA_ESTAGIO.search(linha)
            if m:
                out.append(int(m.group(1)))
    except Exception:                                   # noqa: BLE001
        return []
    return out


def _wer_a(rel: dict | None) -> float | None:
    if not rel:
        return None
    wer = [l["wer_ciclo"] for l in rel.get("falas", [])
           if l.get("papel") == "A" and l.get("wer_ciclo") is not None]
    return round(sum(wer) / len(wer), 3) if wer else None


def _linha(nome: str, c0, c1, chave: str, unidade: str = "") -> str:
    """Uma linha do relatório. `stt_p50_ms` compara o ABSOLUTO; o resto, o delta."""
    op, limite, texto = ACEITES[chave]
    if c0 is None or c1 is None:
        veredito = "INDETERMINADO"
        c1_txt = "—"
    else:
        delta = c1 - c0
        if chave == "stt_p50_ms":
            ok, c1_txt = c1 <= limite, f"{c1:.0f}{unidade}"
        elif chave == "rss_delta_mb":
            ok, c1_txt = delta <= limite, f"{c1:.0f}{unidade} ({delta:+.0f})"
        else:
            ok, c1_txt = delta <= limite, f"{c1:.1f}{unidade} ({delta:+.1f})"
        veredito = "PASSA" if ok else "FALHA"
    c0_txt = ("—" if c0 is None
              else f"{c0:.0f}{unidade}" if unidade == "MB" else f"{c0:.1f}{unidade}")
    return f"  {nome:<26} C0={c0_txt:<9} C1={c1_txt:<18} {veredito:<8} ({texto})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--sha", default="?", help="SHA no ar durante a rodada")
    ap.add_argument("--log", type=Path, help="log do app (para o estágio stt)")
    ap.add_argument("--janela-c0", default="00:00:00-23:59:59",
                    help="HH:MM:SS-HH:MM:SS da fase C0")
    ap.add_argument("--janela-c1", default="00:00:00-23:59:59",
                    help="HH:MM:SS-HH:MM:SS da fase C1")
    args = ap.parse_args(argv)
    d = args.dir

    c0, c1 = _ler(d / "c0.json"), _ler(d / "c1.json")
    eco0, eco1 = _ler(d / "eco-c0.json"), _ler(d / "eco-c1.json")
    ve0, ve1 = _ler(d / "ventoinha-c0.json"), _ler(d / "ventoinha-c1.json")
    curva = _curva(d / "rss.csv")

    j0 = args.janela_c0.split("-")
    j1 = args.janela_c1.split("-")
    est0 = _estagios(args.log, j0[0], j0[-1])
    est1 = _estagios(args.log, j1[0], j1[-1])
    print(f"# Janela C0 × C1 (#876) — SHA {args.sha} — {d}")
    print(f"# estágio stt: n C0={len(est0)} C1={len(est1)} (do log)")
    print("\n## SLOs (C0 = whisper, C1 = parakeet)\n")
    print(_linha("stt p50 do estágio", _pct(est0, 50), _pct(est1, 50),
                 "stt_p50_ms", "ms"))
    w0, w1 = _wer_a(c0), _wer_a(c1)
    print(_linha("WER falas A", None if w0 is None else round(w0 * 100, 1),
                 None if w1 is None else round(w1 * 100, 1), "wer_a_delta_pp", "%"))

    def _taxa(r):
        return None if not r or r.get("taxa") is None else round(r["taxa"] * 100, 1)
    print(_linha("falso turno por ECO", _taxa(eco0), _taxa(eco1),
                 "eco_falso_delta_pp", "%"))
    print(_linha("falso turno por VENTOINHA", _taxa(ve0), _taxa(ve1),
                 "vent_falso_delta_pp", "%"))
    rss0 = (c0 or {}).get("resumo", {}).get("S5", {}).get("proc_fim", {}).get("rss_mb")
    rss1 = (c1 or {}).get("resumo", {}).get("S5", {}).get("proc_fim", {}).get("rss_mb")
    print(_linha("RSS ao fim", rss0, rss1, "rss_delta_mb", "MB"))

    print("\n## S1–S4 (do resumo da bancada; ver o JSON para o detalhe)\n")
    for k in ("S1", "S2", "S3", "S4"):
        for rot, rel in (("C0", c0), ("C1", c1)):
            v = ((rel or {}).get("resumo", {}) or {}).get(k)
            if v is None:
                continue
            if k == "S2":
                v = {x: v.get(x) for x in ("n", "p50_ms", "p95_ms", "llm_ttft_p50_ms",
                                           "pos_llm_p50_ms")}
            elif k == "S1":
                v = {x: v.get(x) for x in ("falas", "pct_aceitas", "wer_ciclo_medio",
                                           "falas_ge_0_8s_perdidas")}
            print(f"  {k} {rot}: {json.dumps(v, ensure_ascii=False)}")

    print(f"\n## Curva RSS/swap ({len(curva)} amostras, 1/min)\n")
    if curva:
        print("  iso                       rss_mb   swap_mb")
        for iso, rss, swap in curva:
            print(f"  {iso:<24} {rss:>7} {swap:>9}")
    else:
        print("  (sem amostras — rss.csv ausente)")

    piores = [l for l in (eco1 or ve1 or {}).get("detalhe", []) if l.get("falso")]
    if piores:
        print(f"\n## Falsos no C1 ({len(piores)})\n")
        for l in piores[:20]:
            print(f"  k={l.get('k')} inicio_s={l.get('inicio_s')} "
                  f"eventos={l.get('eventos')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
