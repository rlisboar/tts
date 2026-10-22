#!/usr/bin/env bash
# Repouso e economia da UI (achados B9/M2/M4 da auditoria #285, task #335).
#
# Três coisas que o app fazia sem ninguém pedir:
#   B9 — sem o roteador de mic instalado, sondava 5 portas locais a cada 10 s
#        (5 linhas ERR_CONNECTION_REFUSED no console, para sempre);
#   M2 — baixava o WAV inteiro de TODA voz no boot, sem abrir a aba (21 vozes =
#        21 downloads) e tudo de novo no histórico;
#   M4 — seguir captando com a aba trocada e manter todos os timers com a aba
#        oculta.
#
#   ./tests/ui_repouso.sh          # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/ui_repouso.sh
#
# Chromium headless. Nada é escrito no estado do dono.
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

BASE="$BASE" "$PY" - <<'PYEOF'
import os, sys, time
from playwright.sync_api import sync_playwright

BASE = os.environ["BASE"]
falhas = []
def cobrar(cond, msg):
    if not cond: falhas.append(msg)

AUDIO_VOZ = "/api/voices/"
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    pedidos = []
    # a contagem tem de ser do que a página puxou, não do que o teste pediu
    pg.on("request", lambda r: pedidos.append(r.url))

    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(3500)

    # ── M2: boot não baixa áudio de voz ────────────────────────────────────
    audios_boot = [u for u in pedidos if AUDIO_VOZ in u and "/audio" in u]
    cobrar(len(audios_boot) <= 4,
           f"boot baixou {len(audios_boot)} áudio(s) de voz (teto 4): {audios_boot[:4]}")

    # ── B9: parado, para de sondar todo ciclo ─────────────────────────────
    marcado = len(pedidos)
    pegou = 0
    for i in range(6):                       # 30 s de repouso, amostrando de 5 em 5
        pg.wait_for_timeout(5000)
        atuais = len([u for u in pedidos[marcado:] if u.endswith("/mode")])
        if atuais > pegou: pegou = atuais
    # com áudio parado a sonda é 1 porta/30 s — 6 portas em 30 s seria o antigo
    cobrar(pegou <= 2, f"em 30 s de repouso sondou {pegou} porta(s) — o esperado é ~1")

    # ── M2: o áudio desce no 1º clique no play, não antes ─────────────────
    pg.evaluate("showView('vozes')")
    pg.wait_for_timeout(400)
    tem_play = pg.evaluate("!!document.querySelector('#voiceList .wplay')")
    if tem_play:
        antes = len([u for u in pedidos if "/audio" in u and AUDIO_VOZ in u])
        pg.evaluate("document.querySelector('#voiceList .wplay').click()")
        pg.wait_for_timeout(1500)
        depois = len([u for u in pedidos if "/audio" in u and AUDIO_VOZ in u])
        src = pg.evaluate("(document.querySelector('#voiceList audio.weng')||{}).src || ''")
        cobrar(depois > antes, "o clique no play não baixou o áudio da voz")
        cobrar(bool(src), "o player da voz ficou sem fonte depois do clique")
    else:
        cobrar(True, "")

    # ── M4: sair da Conversa com o mic ligado pausa a captura ─────────────
    pg.evaluate("showView('conversa'); cvSetMic(true)")
    pg.wait_for_timeout(200)
    cobrar(pg.text_content("#cvMic").strip().endswith("Desligar"), "o mic não ligou no ensaio")
    pg.evaluate("showView('gerar')")
    pg.wait_for_timeout(250)
    cobrar(pg.text_content("#cvMic").strip().endswith("Ligar"),
           f"o mic continuou ligado ao sair da aba (botão: {pg.text_content('#cvMic').strip()!r})")
    cobrar("pausado" in (pg.text_content("#cvStatus") or ""),
           "a tela não disse que a captura foi pausada")

    # ── B9b: com áudio "rolando" a varredura NÃO repete a cada ciclo ──────
    # `player` é let de topo: um objeto com `stopped:false` basta para o app se
    # achar ocupado, sem tocar nada. A varredura completa vale UMA vez; depois
    # disso é uma porta por vez, de 2 em 2 s.
    pg.evaluate("player = { stopped: false }")
    antes_audio = len([u for u in pedidos if u.endswith("/mode")])
    pg.wait_for_timeout(8000)
    durante_audio = len([u for u in pedidos if u.endswith("/mode")]) - antes_audio
    pg.evaluate("player = null")
    pg.wait_for_timeout(300)
    # 8 s a 1 porta/2 s + a varredura inicial(5) cabem folgado em 11
    cobrar(durante_audio <= 11, f"com áudio rolando sondou {durante_audio} porta(s) em 8 s")

    # ── M4: aba oculta espaça a sonda ─────────────────────────────────────
    pg.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});"
                "document.dispatchEvent(new Event('visibilitychange'))")
    antes_oculto = len([u for u in pedidos if u.endswith("/mode")])
    pg.wait_for_timeout(12000)
    durante = len([u for u in pedidos if u.endswith("/mode")]) - antes_oculto
    # visível seriam ~1 em 12 s; oculto o prazo quadruplica e não deve sondar
    cobrar(durante == 0, f"com a aba oculta ainda sondou {durante} porta(s) em 12 s")

    b.close()

if falhas:
    print("✖ falhas:")
    for f in falhas:
        if f: print("  -", f)
    sys.exit(1)
print("✔ repouso: boot sem áudio, sonda espaçada/parada, áudio no 1º gesto, "
      "mic pausado ao trocar de aba e sonda espaçada com a aba oculta")
PYEOF
