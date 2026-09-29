#!/usr/bin/env python3
"""#216 — leitor da matriz de medição (não roda nada: só lê `evidence/216-*.txt`).

Por que existe: a decisão do default depende de números de ~20 rodadas em 3 regimes.
Ler isso a olho em 20 arquivos de 200 linhas é onde o erro entra. Aqui cada célula
vira uma linha com os DOIS lados do tradeoff e o script DIZ quais células faltam.

Métricas por rodada (todas derivadas do texto do próprio harness):
  ok     — iterações em que a injeção virou `interrupted` (o defeito do #167).
           Sentido `vao`: é O ALVO. Quanto MAIOR, melhor.
  sem    — iterações com `✖ SEM barge` (a assinatura do defeito).
  cortes — `interrupted` cumulativos do cliente.
  motor  — `barge_ativos`/`barge_falsos` do MOTOR (cumulativos). No sentido `eco`
           a maior parte dos cortes não tem humano falando: é o motor se cortando
           sozinho, então MENOR `barge_ativos` é melhor ali.
  falsos — `barge_falsos` do motor (barge fechado sem fala além da confirmação).

CUIDADO DE COMPARABILIDADE: `barge_ativos`/`barge_falsos` só têm a semântica atual
desde o conserto da telemetria do `_live_stats` (16:27). Rodadas anteriores trazem
`barge_ativos` contando só a 1ª porta e `barge_falsos` sempre 0 — o script marca
essas linhas com `(telemetria velha)`.

Uso: ./.venv-mlx/bin/python evidence/216-analise.py
"""
import pathlib
import re
import sys
import time

RAIZ = pathlib.Path(__file__).resolve().parent.parent
# telemetria corrigida em 16:27 (carimbo FIXO: o `app.py` muda de mtime a cada
# edição e amarrar o corte nele reclassificaria o arquivo inteiro a cada commit):
# rodada mais velha que isso rodou com a telemetria velha (`barge_ativos` contava
# só a 1ª porta e `barge_falsos` lia a chave errada, saindo sempre 0) — os
# contadores do motor não são comparáveis entre as duas gerações.
TELEMETRIA_OK_TS = time.mktime((2026, 9, 29, 16, 27, 0, 0, 0, -1))

REGIMES = ("vao", "true", "eco")
ROTULOS = {"baseline": "—", "a": "A", "b": "B", "c": "C", "ab": "AB", "ac": "AC",
           "bc": "BC", "abc": "ABC"}


def flags(rot: str) -> str:
    """'true-abc2' -> 'ABC' (sufixo de repetição sai: dígito ou letra maiúscula)."""
    corpo = rot.split("-", 1)[1]
    while corpo not in ROTULOS and len(corpo) > 1 and (
            corpo[-1].isdigit() or corpo[-1].isupper()):
        corpo = corpo[:-1]
    return ROTULOS.get(corpo, corpo.upper())


def le(arquivo: pathlib.Path) -> dict:
    txt = arquivo.read_text(errors="replace")
    motor = {}
    for m in re.finditer(r"=== MOTOR \(cumulativo da rodada\): (\{.*?\})", txt):
        motor = m.group(1)
    cont = {}
    for m in re.finditer(r"=== CONTAGEM \(cumulativa\): (\{.*?\})", txt):
        cont = m.group(1)
    # a rodada está completa se imprimiu o resumo final (o harness sempre imprime)
    completa = "MOTOR (cumulativo da rodada)" in txt and "RESUMO:" in txt
    return {
        "arquivo": arquivo.name,
        "completa": completa,
        # desistiu de esperar a trava: o arquivo tem a linha mas NENHUMA iteração —
        # um `0/20` aqui NÃO é medição (o `grep -c` do rodar conta 0).
        "sem_trava": "trava de modelo não liberou" in txt,
        "ok": txt.count("✔ barge"),
        "sem": txt.count("✖ SEM barge"),
        "cortes": int(re.search(r'"interrupted": (\d+)', cont).group(1)) if cont else None,
        "barges": int(re.search(r'"barge_in": (\d+)', cont).group(1)) if cont else None,
        "motor": int(re.search(r'"barge_ativos": (\d+)', motor).group(1)) if motor else None,
        "falsos": int(re.search(r'"barge_falsos": (\d+)', motor).group(1)) if motor else None,
        "mtime": arquivo.stat().st_mtime,
    }


def main() -> int:
    alvos = sorted(p for p in (RAIZ / "evidence").glob("216-*.txt")
                   if "resumo" not in p.name)
    linhas = [le(p) for p in alvos]
    if not linhas:
        print("nenhuma rodada em evidence/216-*.txt")
        return 1

    import datetime
    print(f"{'rodada':<16} {'flags':<6} {'hora':<6} {'ok':>3} {'sem':>4} {'cortes':>7} "
          f"{'barges':>7} {'motor':>6} {'falsos':>7}  nota")
    faltando = []
    for l in sorted(linhas, key=lambda x: (x["arquivo"])):
        rot = l["arquivo"][4:-4]                     # tira '216-' e '.txt'
        regime = rot.split("-", 1)[0]
        hora = datetime.datetime.fromtimestamp(l["mtime"]).strftime("%H:%M")
        nota = []
        if l["sem_trava"]:
            nota.append("TRAVA NÃO LIBEROU — não é medição")
            faltando.append(rot)
        elif not l["completa"]:
            nota.append("INCOMPLETA (morta na trava/shell)")
            faltando.append(rot)
        if l["mtime"] < TELEMETRIA_OK_TS:
            nota.append("(telemetria velha)")
        if regime not in REGIMES:
            nota.append("(regime?)")
        print(f"{rot:<16} {flags(rot) if '-' in rot else '?':<6} {hora:<6} "
              f"{l['ok']:>3} {l['sem']:>4} "
              f"{(l['cortes'] if l['cortes'] is not None else '-'):>7} "
              f"{(l['barges'] if l['barges'] is not None else '-'):>7} "
              f"{(l['motor'] if l['motor'] is not None else '-'):>6} "
              f"{(l['falsos'] if l['falsos'] is not None else '-'):>7}  "
              f"{' · '.join(nota)}")

    # A decisão NÃO é baseline x ABC: ABC só pode virar default se não perder para
    # AB (a dupla que já resolve o vão). Baseline entra como piso do defeito.
    print("\n── células que DECIDEM (por regime; pior rodada manda, não a média) ──")
    for regime in REGIMES:
        for flags_ in ("—", "AB", "ABC"):
            celulas = [l for l in linhas if l["arquivo"].startswith(f"216-{regime}-")
                       and flags(l["arquivo"][4:-4]) == flags_
                       and l["completa"] and not l["sem_trava"]]
            if not celulas:
                print(f"  {regime:<4} {flags_:<4} FALTA")
                continue
            oks = [l["ok"] for l in celulas]
            cortes = [l["cortes"] for l in celulas if l["cortes"] is not None]
            print(f"  {regime:<4} {flags_:<4} rodadas={len(celulas)} "
                  f"ok={oks} (pior={min(oks)}, média={sum(oks)/len(oks):.1f}) "
                  f"cortes={cortes}")

    print("\n── leitura ──")
    print("  vao : `ok` é O ALVO (o onset no vão tem de virar interrupção).")
    print("  eco : não há humano falando — TODO corte é falso. `cortes` MENOR é melhor;")
    print("        `ok` ali mede só 'a injeção pegou a janela aberta' e é ambíguo.")
    print("  true: satura perto do teto em todas as células — não decide.")
    if faltando:
        print(f"\nfaltam: {', '.join(faltando)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())