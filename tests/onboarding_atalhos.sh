#!/usr/bin/env bash
# Primeiro uso e mapa de atalhos (task #1cfbbfa7).
#
# Cobra o que o cartão promete e o que o mapa tem de cumprir:
#   1. o cartão aparece no primeiro uso (sem chave, sem geração anterior, sem
#      dispensa) e ensina os 3 passos com destino clicável;
#   2. dispensar esconde E persiste (não volta na próxima carga);
#   3. uma geração bem-sucedida também aposenta o cartão (não é só a dispensa);
#   4. `?` abre o mapa fora de campo de texto, NÃO abre dentro de um, `Esc` fecha
#      e o foco volta para quem abriu — com o fundo inerte enquanto está aberto;
#   5. conferência cruzada: cada atalho que o app de fato tem está listado, e
#      nenhuma tecla listada é inventada (a lista vem do fonte).
#
#   ./tests/onboarding_atalhos.sh          # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/onboarding_atalhos.sh
#
# Chromium headless. Usa um contexto limpo (o `localStorage` do dono não manda),
# mas a geração de verdade grava um output no histórico dele — o trap apaga o
# que a rodada criar.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:7860}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

command -v curl >/dev/null || { echo "curl não encontrado"; exit 1; }
curl -sf -m 5 -o /dev/null "$BASE/" || { echo "✖ app não respondeu em $BASE — rode ./run.sh primeiro" >&2; exit 1; }

# ESTA é suíte de `instancia` que ESCREVE: a prova gera fala de verdade e o job
# fica no histórico do app alvo. Ids vistos antes servem de linha de base; o trap
# apaga só o que a rodada criou (idempotente, `|| true`, como manda o #446).
OUT_ANTES="$(curl -sf "$BASE/api/outputs?limit=50" | "$PY" -c 'import json,sys
try: print(" ".join(i["id"] for i in json.load(sys.stdin)["items"]))
except Exception: print("")' 2>/dev/null || true)"
limpa_outputs() {
  local id
  curl -sf "$BASE/api/outputs?limit=50" | "$PY" -c 'import json,sys
antes = set(sys.argv[1].split())
try: itens = json.load(sys.stdin)["items"]
except Exception: sys.exit(0)
for i in itens:
    if i["id"] not in antes: print(i["id"])' "$OUT_ANTES" 2>/dev/null \
  | while read -r id; do
      [ -n "$id" ] && curl -sf -X DELETE "$BASE/api/outputs/$id" >/dev/null 2>&1 || true
    done
  return 0
}
trap 'limpa_outputs' EXIT INT TERM

BASE="$BASE" "$PY" - <<'PYEOF'
import os, re, sys
from playwright.sync_api import sync_playwright

BASE = os.environ["BASE"]
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    # ── 1. primeiro uso: contexto virgem ───────────────────────────────────
    pg.evaluate("""() => { sessionStorage.clear();
        ['ttsStudioKey','ttsJaGerou','ttsOnboardingDispensado'].forEach(k => localStorage.removeItem(k));
        localStorage.setItem('ttsStudioView','gerar'); }""")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)
    estado = pg.evaluate("""() => { const c = document.getElementById('onboarding');
        return { oculto: c.hidden, altura: Math.round(c.getBoundingClientRect().height),
                 passos: c.querySelectorAll('.onb-passos li').length,
                 destinos: [...c.querySelectorAll('[data-onb]')].map(b => b.dataset.onb) }; }""")
    cobrar(not estado["oculto"] and estado["altura"] > 60, f"o cartão de primeiro uso não apareceu ({estado})")
    cobrar(estado["passos"] == 3, f"o cartão deveria ter 3 passos, tem {estado['passos']}")
    cobrar(set(estado["destinos"]) == {"acesso", "vozes", "atalhos"}, f"passos sem destino: {estado['destinos']}")

    # destino do passo 1 leva à aba certa
    pg.evaluate("document.querySelector('#onboarding [data-onb=\"acesso\"]').click()")
    pg.wait_for_timeout(300)
    cobrar(pg.evaluate("document.querySelector('.stab.active').dataset.stab") == "acesso",
           "o passo 1 não abriu Configurações → Acesso")
    pg.evaluate("showView('gerar')")

    # ── 2. dispensar persiste ──────────────────────────────────────────────
    pg.evaluate("document.getElementById('onbFechar').click()")
    pg.wait_for_timeout(120)
    cobrar(pg.evaluate("document.getElementById('onboarding').hidden"), "dispensar não escondeu o cartão")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)
    cobrar(pg.evaluate("document.getElementById('onboarding').hidden"),
           "o cartão voltou depois de recarregar, mesmo dispensado")

    # ── 3. geração aposenta o cartão ───────────────────────────────────────
    pg.evaluate("""() => { sessionStorage.clear(); localStorage.removeItem('ttsStudioKey');
        localStorage.removeItem('ttsJaGerou'); localStorage.removeItem('ttsOnboardingDispensado'); }""")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(2200)
    cobrar(not pg.evaluate("document.getElementById('onboarding').hidden"), "o cartão não voltou com o estado limpo")
    # a lista de vozes vem por fetch: sem ela o Gerar recusa com "grave uma voz"
    for _ in range(20):
        if pg.evaluate("document.getElementById('voiceSelect').value"):
            break
        pg.wait_for_timeout(250)
    cobrar(pg.evaluate("!!document.getElementById('voiceSelect').value"),
           "o seletor de voz ficou vazio — não dá para gerar")
    # Uma voz sem `.wav` (sobra de outra suíte, meta sem par) derruba a geração.
    # O alvo é o CARTÃO, não o fixture: tenta até 5 vozes e, se nenhuma servir,
    # imprime SKIP em vez de reprovar.
    ok_gerou = False
    for i in range(5):
        # o toast da tentativa ANTERIOR fica na tela até 6 s: sem limpar, o laço lê
        # o erro velho e desiste antes da nova tentativa terminar
        pg.evaluate("""() => { const t = document.getElementById('toast');
            t.textContent = ''; t.classList.remove('show'); }""")
        pg.evaluate("""i => { const o = document.getElementById('voiceSelect').options[i];
            if (!o) return; document.getElementById('voiceSelect').value = o.value;
            const ta = document.getElementById('ttsText');
            ta.value = 'primeiro uso'; ta.dispatchEvent(new Event('input', {bubbles:true}));
            document.getElementById('ttsBtn').click(); }""", i)
        for _ in range(120):             # até 60 s: modelo frio depois de restart
            pg.wait_for_timeout(500)
            status = pg.text_content("#ttsStatus") or ""
            if re.search(r"pronto em", status):
                ok_gerou = True
                break
            if re.search(r"Erro:", pg.text_content("#toast") or ""):
                break
        if ok_gerou:
            break
    if ok_gerou:
        pg.wait_for_timeout(200)
        cobrar(pg.evaluate("!!localStorage.getItem('ttsJaGerou')"), "gerar não marcou o fim do primeiro uso")
        cobrar(pg.evaluate("document.getElementById('onboarding').hidden"), "o cartão continuou depois de gerar")
    else:
        print("· SKIP do fim do primeiro uso: nenhuma das 5 vozes gerou ("
              + (pg.text_content("#toast") or pg.text_content("#ttsStatus") or "sem motivo").strip()[:90] + ")")

    # ── 4. mapa: ? abre, campo de texto não abre, Esc fecha, foco volta ────
    pg.evaluate("document.body.focus()")
    pg.evaluate("document.body.dispatchEvent(new KeyboardEvent('keydown', {key:'?', bubbles:true}))")
    pg.wait_for_timeout(200)
    cobrar(pg.evaluate("document.getElementById('atalhos').style.display") == "flex", "`?` não abriu o mapa")
    cobrar(pg.evaluate("document.querySelector('.app').inert === true"), "o fundo não ficou inerte com o mapa aberto")
    pg.evaluate("document.getElementById('atFechar').focus()")
    pg.keyboard.press("Shift+Tab")
    pg.wait_for_timeout(150)
    cobrar(pg.evaluate("document.getElementById('atalhos').contains(document.activeElement)"),
           f"o foco escapou do mapa (foi para {pg.evaluate('document.activeElement.id || document.activeElement.tagName')})")
    pg.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
    pg.wait_for_timeout(200)
    cobrar(pg.evaluate("document.getElementById('atalhos').style.display") == "none", "Esc não fechou o mapa")
    cobrar(pg.evaluate("document.querySelector('.app').inert === false"), "o fundo continuou inerte depois de fechar")

    pg.evaluate("""() => { const ta = document.getElementById('ttsText'); ta.focus();
        ta.dispatchEvent(new KeyboardEvent('keydown', {key:'?', bubbles:true})); }""")
    pg.wait_for_timeout(200)
    cobrar(pg.evaluate("document.getElementById('atalhos').style.display") == "none",
           "o `?` abriu o mapa com o foco dentro de um campo de texto")
    b.close()

# ── 5. conferência cruzada com o fonte ────────────────────────────────────
fonte = open("static/index.html", encoding="utf-8").read()
i = fonte.find('id="atalhos"')
fim = fonte.find('id="audioEditor"', i)          # o overlay seguinte fecha o bloco
mapa = fonte[i:fim] if i > 0 and fim > i else ""
cobrar(bool(mapa), "não achei o bloco do mapa no fonte")

# teclas que o app REALMENTE escuta (literais de `e.key`/`onkeydown` no script)
reais = set(re.findall(r'e\.key === "([^"]+)"', fonte))
rotulos = {" ": "Espaço", "Enter": "Enter", "Escape": "Esc", "ArrowRight": "→",
           "ArrowLeft": "←", "Home": "Home", "End": "End", "?": "?", "Tab": "Tab"}
for k in sorted(reais):
    if k in ("Shift", "Meta", "Control", "Alt"):
        continue
    nome = rotulos.get(k, k)
    cobrar(nome in mapa, f"o atalho {nome!r} existe no código mas NÃO está no mapa")
# atalhos 1–7
for d in "1234567":
    cobrar(f"<kbd>{d}</kbd>" in mapa, f"o atalho {d} (troca de tela) não está no mapa")
cobrar("⌘" in mapa and "Ctrl" in mapa, "faltou listar ⌘/Ctrl+Enter")

# nenhuma tecla listada pode ser inventada
for nome in re.findall(r"<kbd>([^<]+)</kbd>", mapa):
    if nome in ("1", "2", "3", "4", "5", "6", "7", "⌘", "Ctrl", "Tab", "Shift", "Esc",
                "Espaço", "Enter", "←", "→", "Home", "End", "?"):
        continue
    cobrar(False, f"o mapa lista {nome!r}, que não é atalho conhecido")

if falhas:
    print("✖ falhas:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print("✔ onboarding: cartão no 1º uso, dispensa persistente, geração aposenta; "
      "mapa com trap, fechando por Esc e sem abrir em campo de texto; atalhos conferidos dos dois lados")
PYEOF
