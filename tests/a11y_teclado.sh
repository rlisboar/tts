#!/usr/bin/env bash
# Acessibilidade de teclado e anúncio (achados B3/B4/B5/B10/M1/M3, task #333).
#
# O que cada bloco protege:
#   B3 — PTT "segurar para falar" era só ponteiro (`onpointerdown` sem `onclick`):
#        Tab+Espaço não fazia nada;
#   B4 — atalhos 1–7 comutavam a view POR BAIXO do editor aberto;
#   B5 — diálogo com `aria-modal` sem trap: Shift+Tab do 1º controle escapava;
#   B10 — abas com `role="tab"` e sem `aria-selected`/`aria-controls`/tabpanel/setas;
#   M1 — progresso de Tradutor/STT/Conversa sem região viva;
#   M3 — ondas em canvas, mouse-only.
#
#   ./tests/a11y_teclado.sh          # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/a11y_teclado.sh
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
def tecla(pg, sel, tipo, key, shift=False):
    pg.evaluate("""([sel, tipo, key, shift]) => {
        const el = document.querySelector(sel);
        el.dispatchEvent(new KeyboardEvent(tipo, { key, shiftKey: shift, bubbles: true, cancelable: true }));
    }""", [sel, tipo, key, shift])

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    # ── B3: PTT responde a Espaço ──────────────────────────────────────────
    feito = pg.evaluate("""() => {
        showView('live');
        document.getElementById('lxPtt').checked = true;
        const b = document.getElementById('lxPttBtn');
        b.style.display = ''; b.focus();
        return document.activeElement === b;
    }""")
    cobrar(feito, "o botão de segurar-para-falar não recebeu foco")
    tecla(pg, "#lxPttBtn", "keydown", " ")
    pg.wait_for_timeout(80)
    apertado = pg.text_content("#lxPttBtn") or ""
    tecla(pg, "#lxPttBtn", "keyup", " ")
    pg.wait_for_timeout(80)
    cobrar("falando" in apertado, f"Espaço não apertou o PTT (rótulo: {apertado.strip()!r})")
    cobrar("Segurar" in (pg.text_content("#lxPttBtn") or ""), "soltar a tecla não devolveu o rótulo")

    # ── B4 + B5: editor aberto ─────────────────────────────────────────────
    pg.evaluate("""() => { showView('vozes');
        openEditor(new Blob([new Uint8Array(2000)], {type:'audio/wav'}), () => {}, 'teste').catch(() => {}); }""")
    pg.wait_for_timeout(600)
    cobrar(pg.evaluate("document.getElementById('audioEditor').style.display !== 'none'"), "o editor não abriu")
    cobrar(pg.evaluate("document.querySelector('.app').inert === true"), "o fundo não ficou inerte com o diálogo aberto")

    view_antes = pg.evaluate("document.querySelector('.view.active').id")
    pg.evaluate("document.getElementById('edClose').focus()")
    tecla(pg, "body", "keydown", "3")
    pg.wait_for_timeout(150)
    cobrar(pg.evaluate("document.querySelector('.view.active').id") == view_antes,
           "atalho trocou a view com o editor aberto")

    # Shift+Tab do PRIMEIRO controle, com tecla DE VERDADE (evento sintético não
    # move foco): com o fundo inerte o foco tem de dar a volta dentro do diálogo
    pg.evaluate("document.getElementById('edClose').focus()")
    pg.keyboard.press("Shift+Tab")
    pg.wait_for_timeout(150)
    dentro = pg.evaluate("document.getElementById('audioEditor').contains(document.activeElement)")
    cobrar(dentro, f"o foco escapou do diálogo (foi para {pg.evaluate('document.activeElement.id || document.activeElement.tagName')})")

    pg.evaluate("closeEditor()")
    pg.wait_for_timeout(150)
    cobrar(pg.evaluate("document.querySelector('.app').inert === false"), "o fundo continuou inerte depois de fechar")

    # o MESMO trap tem de valer para o outro overlay (mapa de atalhos): sem isto,
    # um vermelho no trap não diz qual dos dois quebrou
    pg.evaluate("document.body.focus()")
    pg.evaluate("abreAtalhos()")
    pg.wait_for_timeout(200)
    cobrar(pg.evaluate("document.getElementById('atalhos').style.display") == "flex", "o mapa não abriu para o ensaio")
    cobrar(pg.evaluate("document.querySelector('.app').inert === true"), "o fundo não ficou inerte com o MAPA aberto")
    pg.evaluate("document.getElementById('atFechar').focus()")
    pg.keyboard.press("Shift+Tab")
    pg.wait_for_timeout(150)
    cobrar(pg.evaluate("document.getElementById('atalhos').contains(document.activeElement)"),
           "o foco escapou do MAPA — trap genérico não valeu para os dois overlays")
    pg.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
    pg.wait_for_timeout(150)
    cobrar(pg.evaluate("document.getElementById('atalhos').style.display") == "none", "Esc não fechou o mapa")
    cobrar(pg.evaluate("document.querySelector('.app').inert === false"), "o fundo continuou inerte com o mapa fechado")

    # ── B10: abas ──────────────────────────────────────────────────────────
    pg.evaluate("showView('config')")
    pg.wait_for_timeout(250)
    tabs = pg.evaluate("""() => [...document.querySelectorAll('.settings-tabs .stab')].map(t => ({
        sel: t.getAttribute('aria-selected'), ctrl: t.getAttribute('aria-controls'), ti: t.tabIndex }))""")
    cobrar(pg.evaluate("!!document.querySelector('.settings-tabs').getAttribute('aria-label')"), "tablist sem rótulo")
    cobrar(sum(1 for t in tabs if t["sel"] == "true") == 1, f"aria-selected deveria ter exatamente 1 aba (tem {tabs})")
    cobrar(all(t["ctrl"] for t in tabs), "aba sem aria-controls")
    cobrar(all(t["ti"] == 0 for t in tabs if t["sel"] == "true"), "aba ativa fora da ordem de Tab")
    cobrar(pg.evaluate("""() => [...document.querySelectorAll('.settings-pane')]
        .every(p => p.getAttribute('role') === 'tabpanel' && p.getAttribute('aria-labelledby'))"""),
        "painéis sem role=tabpanel/aria-labelledby")
    pg.evaluate("document.querySelector('.stab').focus()")
    tecla(pg, ".stab", "keydown", "ArrowRight")
    pg.wait_for_timeout(150)
    cobrar(pg.evaluate("document.querySelector('.stab.active').dataset.stab") == "geracao", "seta → não moveu a aba")
    # `aria-selected` tem de ACOMPANHAR a seta, não só existir no HTML de partida
    cobrar(pg.evaluate("document.querySelector('.stab.active').getAttribute('aria-selected')") == "true",
           "a aba aberta pela seta não ficou aria-selected")
    cobrar(pg.evaluate("""() => [...document.querySelectorAll('.stab')]
        .filter(t => !t.classList.contains('active')).every(t => t.getAttribute('aria-selected') === 'false')"""),
        "aba inativa ficou com aria-selected='true'")

    # ── M1: regiões vivas ──────────────────────────────────────────────────
    for ident in ("tvStatus", "sttStatus", "cvStatus"):
        cobrar(pg.evaluate(f"document.getElementById('{ident}').getAttribute('aria-live')") == "polite",
               f"#{ident} sem aria-live")

    # ── M3: onda das vozes por teclado ─────────────────────────────────────
    pg.evaluate("showView('vozes')")
    pg.wait_for_timeout(400)
    # Tenta até 5 vozes: uma voz sem wav (sobra de outra suíte, meta sem par) não
    # pode reprovar o teste de teclado — o alvo é o teclado, não o fixture.
    onda = None
    for i in range(5):
        pg.evaluate("i => { const p = document.querySelectorAll('#voiceList .wplay')[i]; if (p) p.click(); }", i)
        pg.wait_for_timeout(1500)
        onda = pg.evaluate("""(i) => {
            const item = document.querySelectorAll('#voiceList .item')[i];
            if (!item) return null;
            const c = item.querySelector('canvas.wave'), au = item.querySelector('audio.weng');
            if (!c || !au || !au.duration) return null;
            c.focus();
            const antes = au.currentTime;
            c.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowRight', bubbles:true, cancelable:true}));
            return { antes, depois: au.currentTime, tabIndex: c.tabIndex };
        }""", i)
        if onda: break
    if onda is None:
        cobrar(False, "não consegui preparar o áudio de 5 vozes (todas sem wav?)")
    else:
        pg.wait_for_timeout(150)
        cobrar(onda["tabIndex"] == 0, "a onda não é focável por teclado")
        cobrar(onda["depois"] > onda["antes"], "seta → não avançou a onda")
    b.close()

if falhas:
    print("✖ falhas:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print("✔ a11y: PTT por tecla, modal com trap e sem atalho por baixo, abas ARIA com setas, "
      "3 status vivos e onda navegável por teclado")
PYEOF
