#!/usr/bin/env bash
# Estado do formulário e falha de ação destrutiva (achados B6/B7/B11, task #334).
#
# Três coisas que o app deixava passar em silêncio:
#   B6 — configuração não salva sumia ao trocar de aba ou recarregar (o "•" no
#        botão era só enfeite; 4 campos nem marcavam);
#   B7 — acima de 5000 caracteres só o contador ficava vermelho e o servidor
#        recusava com 400 no clique;
#   B11 — apagar voz/áudio ("Apagar todos" inclusive) sem `catch`: rejeição virava
#        falha muda com a tela ainda prometendo o item.
#
#   ./tests/cfg_sujo_e_erros.sh          # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/cfg_sujo_e_erros.sh
#
# Chromium headless. Só o `#speakerGate` é tocado — e o valor é DEVOLVIDO no fim.
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

def clica(pg, sel):
    pg.evaluate("s => document.querySelector(s).click()", sel)

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    # `confirm` sempre SIM: o roteiro não quer parar em diálogo nativo
    pg.on("dialog", lambda d: d.accept())
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    # ── B6: marcar sujo, barrar a saída, liberar depois de salvar ───────────
    pg.evaluate("showView('config')")
    pg.wait_for_timeout(400)
    original = pg.evaluate("document.getElementById('speakerGate').value")
    pg.evaluate("""() => {
        const el = document.getElementById('speakerGate');
        el.value = el.value === 'off' ? 'label' : 'off';
        // campo que NÃO marcava sujo antes desta correção
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }""")
    pg.wait_for_timeout(150)
    cobrar("•" in pg.text_content("#cfgSave"), "o campo alterado não marcou 'alterações não salvas'")
    cobrar("alterações não salvas" in (pg.text_content("#cfgStatus") or ""),
           "o status não avisou sobre alteração não salva")

    # confirm=False: tem de ficar na tela
    pg.evaluate("window.__c = window.confirm; window.confirm = () => false; showView('vozes')")
    pg.wait_for_timeout(200)
    cobrar(pg.evaluate("document.querySelector('.view.active').id") == "view-config",
           "saiu de Configurações com alteração não salva e sem confirmação")

    # beforeunload armado só com sujo
    armado = pg.evaluate("""() => { const e = new Event('beforeunload', {cancelable:true});
        window.dispatchEvent(e); return e.defaultPrevented; }""")
    cobrar(armado, "beforeunload não está armado com o formulário sujo")

    pg.evaluate("window.confirm = window.__c")
    clica(pg, "#cfgSave")
    pg.wait_for_timeout(1500)
    limpo = pg.evaluate("""() => { const e = new Event('beforeunload', {cancelable:true});
        window.dispatchEvent(e); return e.defaultPrevented; }""")
    cobrar(not limpo, "beforeunload continua armado depois de salvar")
    cobrar("•" not in pg.text_content("#cfgSave"), "o '•' de sujo não sumiu depois de salvar")

    # devolve o valor original
    pg.evaluate("v => { const el = document.getElementById('speakerGate'); el.value = v; el.dispatchEvent(new Event('change', {bubbles:true})); }", original)
    clica(pg, "#cfgSave")
    pg.wait_for_timeout(1200)

    # ── B7: 5001 caracteres não deixam apertar Gerar e o motivo é dito ─────
    pg.evaluate("""() => { showView('gerar');
        const ta = document.getElementById('ttsText');
        ta.value = 'a'.repeat(5001);
        ta.dispatchEvent(new Event('input', { bubbles: true })); }""")
    pg.wait_for_timeout(150)
    cobrar("acima" in pg.text_content("#ttsCharCount"), "o contador não diz quanto passou do teto")
    cobrar(pg.evaluate("document.getElementById('ttsText').getAttribute('aria-invalid')") == "true",
           "o textarea não é marcado inválido acima do teto")
    clica(pg, "#ttsBtn")
    pg.wait_for_timeout(500)
    cobrar("longo demais" in (pg.text_content("#toast") or ""), "o Gerar não explicou o excesso")
    pg.evaluate("""() => { const ta = document.getElementById('ttsText');
        ta.value = 'teste'; ta.dispatchEvent(new Event('input', {bubbles:true})); }""")

    # ── B11: falha no apagar deixa a lista e avisa ─────────────────────────
    pg.evaluate("showView('historico')")
    pg.wait_for_timeout(600)
    falhou = pg.evaluate("""async () => {
        const orig = window.api;
        window.api = async () => { throw new Error('recusado (teste)'); };
        // a lista pode estar vazia: o alvo é o handler do "Apagar todos"
        document.getElementById('clearAll').click();
        await new Promise(r => setTimeout(r, 600));
        const t = document.getElementById('toast');
        const d = { toast: t.textContent, mostrou: t.classList.contains('show'),
                    reabilitado: !document.getElementById('clearAll').disabled };
        window.api = orig;
        return d;
    }""")
    cobrar("recusado (teste)" in falhou["toast"], f"o erro do apagar não foi mostrado ({falhou['toast']!r})")
    cobrar(falhou["reabilitado"], "o botão 'Apagar todos' ficou travado depois da falha")
    b.close()

if falhas:
    print("✖ falhas:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print("✔ estado: sujo avisa/barra/desarma, 5001 chars bloqueia com motivo, falha do apagar é visível")
PYEOF
