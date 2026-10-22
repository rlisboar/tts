#!/usr/bin/env bash
# Falha de microfone: a UI tem de dizer a CAUSA e o painel OBS não pode mentir
# (achado B1 da auditoria #285, task #332).
#
# Cobre as DUAS causas confundidas — são ações diferentes para a pessoa:
#   1. origem NÃO segura (o caso de abrir a UI pelo IP da LAN em http): o
#      `navigator.mediaDevices` nem existe. Antes a tela dizia "permissão de
#      microfone negada" e o chip do OBS ficava preso em "● gravando".
#   2. permissão negada DE VERDADE (`NotAllowedError`) — essa continua dizendo
#      permissão negada.
#
# As quatro telas com captura são cobradas (Vozes/gravar, STT/ditado,
# Conversa/Ligar, Biometria): cada uma tem de (a) mostrar a causa na superfície
# DELA (toast ou status) e (b) sair do estado de gravação no painel OBS. O Live
# já tinha a mensagem certa e é conferido pelo texto-fonte da guarda única.
#
#   ./tests/mic_erro_ui.sh          # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/mic_erro_ui.sh
#
# Chromium headless; nada é escrito no estado do dono (nada de voz/chave nova).
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:7860}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

command -v curl >/dev/null || { echo "curl não encontrado"; exit 1; }
if ! curl -sf -m 5 -o /dev/null "$BASE/"; then
  echo "✖ app não respondeu em $BASE — rode ./run.sh primeiro" >&2
  exit 1
fi

# a guarda tem de ficar num lugar só (o Live caiu na mesma frase que as outras
# telas); cinco cópias divergindo era o estado anterior
GRUPO="$(grep -c 'function micErroTexto' "$RAIZ/static/index.html" || true)"
[ "$GRUPO" = "1" ] || { echo "✖ micErroTexto deveria ser única, achei $GRUPO" >&2; exit 1; }

BASE="$BASE" "$PY" - <<'PYEOF'
import os, sys
from playwright.sync_api import sync_playwright

BASE = os.environ["BASE"]
falhas = []
def cobrar(cond, msg):
    if not cond: falhas.append(msg)

# () => { ... } injetado ANTES da página rodar: é o único jeito de o app já
# nascer sem a API de mic, como numa origem não segura de verdade.
# add_init_script executa a FONTE como script: o arrow function precisa de ser
# chamado (uma arrow solta só avalia para uma função e nunca roda).
SEM_API = "(() => { Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true }); })()"
NEGADO = """(() => {
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: () => Promise.reject(Object.assign(new Error('denied'), { name: 'NotAllowedError' })),
    enumerateDevices: () => Promise.resolve([]),
  } });
})()"""

# (view, botão que dispara a captura, seletor onde a mensagem tem de aparecer)
TELAS = [
    ("vozes",     "#recBtn",    "#toast"),
    ("tradutor",  "#sttRecBtn", "#toast"),
    ("conversa",  "#cvMic",     "#cvStatus"),
    ("config",    "#spEnroll",  "#toast"),
]

def abre(browser, init):
    ctx = browser.new_context()
    if init: ctx.add_init_script(init)
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(1200)
    return ctx, pg

def dispara(pg, view, botao):
    # clique por JS: o botão do ditado mora dentro de um <details> fechado, então
    # `click` do Playwright esperaria visibilidade para sempre. `.click()` chama o
    # mesmo handler `onclick` — o que se mede aqui é a REAÇÃO à falha, não o layout.
    pg.evaluate("v => showView(v)", view)
    pg.wait_for_timeout(120)
    # biometria exige nome de perfil antes do clique
    if botao == "#spEnroll":
        pg.evaluate('document.getElementById("spName").value = "probe"')
    pg.evaluate("s => document.querySelector(s).click()", botao)
    pg.wait_for_timeout(500)

with sync_playwright() as p:
    b = p.chromium.launch()

    # ── 1. origem não segura ────────────────────────────────────────────────
    ctx, pg = abre(b, SEM_API)
    for view, botao, alvo in TELAS:
        dispara(pg, view, botao)
        txt = (pg.text_content(alvo) or "").strip()
        cobrar("origem segura" in txt, f"[{view}] não disse a causa real (texto: {txt[:90]!r})")
        cobrar("permiss" not in txt.lower(), f"[{view}] ainda culpa a permissão (texto: {txt[:90]!r})")
        # o painel OBS da tela tem de sair de "gravando"
        painel = {"vozes": "#obsVozes", "tradutor": "#obsStt", "conversa": "#obsCv", "config": "#obsVozes"}[view]
        estado = pg.text_content(painel) or ""
        cobrar("gravando" not in estado, f"[{view}] OBS ficou preso em gravando ({estado[:90]!r})")
    cobrar(pg.text_content("#cvMic") == "🎙 Ligar", "botão da Conversa ficou em 'Desligar' depois da falha")
    ctx.close()

    # ── 2. permissão negada de verdade ──────────────────────────────────────
    ctx, pg = abre(b, NEGADO)
    for view, botao, alvo in TELAS:
        dispara(pg, view, botao)
        txt = (pg.text_content(alvo) or "").strip()
        cobrar("permiss" in txt.lower(), f"[{view}] permissão negada não foi dita (texto: {txt[:90]!r})")
        cobrar("origem segura" not in txt, f"[{view}] culpou a origem com permissão negada (texto: {txt[:90]!r})")
    ctx.close()
    b.close()

# ── 3. guarda do Live (mesma frase, sem orchestrar o WS aqui) ──────────────
fonte = open("static/index.html", encoding="utf-8").read()
i = fonte.find("async function lxCaptura()")
cobrar(i > 0 and "micIndisponivel()" in fonte[i:i + 700], "lxCaptura perdeu a guarda de origem segura")

if falhas:
    print("✖ falhas:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print(f"✔ mic: {len(TELAS)} telas dizem a causa real (origem não segura ×permissão negada) e o OBS sai de 'gravando'")
PYEOF
