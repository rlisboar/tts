#!/usr/bin/env bash
# Layout estreito: nada pode sair da viewport, e o painel OBS tem de caber
# (achado B2 da auditoria #285, task #331).
#
# O bug que isto protege: em coluna, `flex-wrap: wrap` transforma o
# `flex-basis: 100%` dos painéis OBS em ALTURA e eles viram colunas laterais —
# `#obsTts` ia a 140 px fora da tela em 390 px, e `body{overflow:hidden}` cortava.
# Junto: `.tv-right` tinha largura fixa redeclarada DEPOIS dos media queries, então
# o `width:auto` responsivo era letra morta.
#
# Cobra, nas 7 telas e em duas larguras de celular: zero elemento com a borda
# direita fora da viewport, e cada painel OBS inteiro dentro dela.
#
#   ./tests/mobile_layout.sh          # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/mobile_layout.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:7860}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

command -v curl >/dev/null || { echo "curl não encontrado"; exit 1; }
curl -sf -m 5 -o /dev/null "$BASE/" || { echo "✖ app não respondeu em $BASE — rode ./run.sh primeiro" >&2; exit 1; }

BASE="$BASE" "$PY" - <<'PYEOF'
import os, sys
from playwright.sync_api import sync_playwright

BASE = os.environ["BASE"]
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

TELAS = ["gerar", "vozes", "tradutor", "conversa", "historico", "live", "config"]
PAINEIS = [("gerar", "#obsTts"), ("vozes", "#obsVozes"), ("tradutor", "#obsStt"), ("conversa", "#obsCv")]

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    for largura, altura in ((390, 844), (360, 800)):
        pg.set_viewport_size({"width": largura, "height": altura})
        pg.wait_for_timeout(300)
        # a página inteira, tela por tela
        fora = pg.evaluate("""(telas) => {
            const achados = [];
            for (const id of telas) {
                const v = document.getElementById('view-' + id);
                v.classList.add('active');
                for (const el of v.querySelectorAll('*')) {
                    const r = el.getBoundingClientRect();
                    if (r.right > innerWidth + 1)
                        achados.push({view: id, el: (el.id || el.className), px: Math.round(r.right - innerWidth)});
                }
                v.classList.remove('active');
            }
            document.getElementById('view-gerar').classList.add('active');
            return achados;
        }""", TELAS)
        for a in fora[:6]:
            cobrar(False, f"{largura}px · [{a['view']}] {a['el']} sai {a['px']}px da viewport")

        # os painéis OBS, um a um
        for view, sel in PAINEIS:
            dentro = pg.evaluate("""([view, sel]) => {
                const v = document.getElementById('view-' + view);
                v.classList.add('active');
                const r = document.querySelector(sel).getBoundingClientRect();
                v.classList.remove('active');
                document.getElementById('view-gerar').classList.add('active');
                return r.left >= -1 && r.right <= innerWidth + 1 && r.width > 0;
            }""", [view, sel])
            cobrar(dentro, f"{largura}px · painel {sel} ({view}) não está inteiro na viewport")
    b.close()

if falhas:
    print("✖ falhas:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print("✔ mobile: nenhum elemento fora da viewport e os 4 painéis OBS inteiros em 390 e 360 px")
PYEOF
