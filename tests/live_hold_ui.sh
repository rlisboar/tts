#!/usr/bin/env bash
# Fallback "segurar para falar" no Live (task #22099a25).
#
# O servidor (task_5fdeb23c) publica `stats.motor.hold_to_talk_sugerido` /
# `hold_to_talk` e aceita `{"type":"hold","on":bool}`. Aqui se cobra a PONTA DA
# UI, com o `stats` injetado (o backend pode não estar no ar ainda) e um WS falso
# só para capturar o que sobe:
#   · sem os campos (servidor antigo) → nada aparece, nem faixa nem estado;
#   · sugestão ligada → faixa com o botão "usar segurar para falar";
#   · modo ligado → texto de ligado e botão invertido ("voltar ao automático"),
#     e o botão de segurar aparece mesmo com o checkbox desligado;
#   · clicar manda o JSON certo, o estado pinta na hora e o pedido temporário
#     cai quando o `stats` confirma;
#   · ligado sem sugestão a faixa CONTINUA (é o único jeito de sair do modo).
#
# E um bloco de SESSÃO REAL (fim do roteiro): abre o WS de verdade no app, espera
# o `stats` e, se o servidor já publicar `motor.hold_to_*`, cobra a faixa e o
# efeito de `{"type":"hold","on":true}` no próprio `stats`. Enquanto o backend
# não tiver o campo (task_5fdeb23c) esse bloco imprime SKIP em vez de reprovar —
# quando ele entrar, o bloco acende sozinho. Não precisa de microfone: a captura
# pode falhar e o `stats` chega do mesmo jeito.
#
#   ./tests/live_hold_ui.sh          # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/live_hold_ui.sh
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

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(2200)
    pg.evaluate("showView('live')")

    def estado():
        return pg.evaluate("""() => ({
            aviso: !document.getElementById('lxHoldAviso').hidden,
            texto: document.getElementById('lxHoldTexto').textContent.trim(),
            botao: document.getElementById('lxHoldBtn').textContent.trim(),
            chip: document.getElementById('lxHold').textContent,
            ptt: document.getElementById('lxPttBtn').style.display !== 'none' })""")

    def injeta(stats):
        pg.evaluate("s => { LX.obs.stats = s; lxHoldRender(); }", stats)

    # ── sem os campos: nada aparece ────────────────────────────────────────
    pg.evaluate("() => { LX.obs.stats = null; lxHoldRender(); }")
    e = estado()
    cobrar(not e["aviso"], "apareceu faixa sem o servidor mandar os campos")
    cobrar(e["chip"] == "—", f"o OBS inventou estado sem campo ({e['chip']!r})")

    # ── sugestão ligada ────────────────────────────────────────────────────
    injeta({"motor": {"hold_to_talk": False, "hold_to_talk_sugerido": True}})
    e = estado()
    cobrar(e["aviso"], "a sugestão não fez a faixa aparecer")
    cobrar("usar segurar para falar" in e["botao"], f"botão errado na sugestão ({e['botao']!r})")
    cobrar(e["chip"] == "automático", f"o OBS não mostra o modo automático ({e['chip']!r})")

    # ── clicar manda o JSON e pinta na hora ────────────────────────────────
    pg.evaluate("""() => { window.__enviados = [];
        LX.ws = { readyState: WebSocket.OPEN, send: m => window.__enviados.push(m) }; }""")
    pg.evaluate("document.getElementById('lxHoldBtn').click()")
    pg.wait_for_timeout(150)
    enviados = pg.evaluate("window.__enviados")
    cobrar(enviados == ['{"type":"hold","on":true}'], f"o clique não mandou hold:on ({enviados})")
    e = estado()
    cobrar(e["chip"] == "segurando", f"o estado não refletiu o pedido na hora ({e['chip']!r})")
    cobrar("voltar ao automático" in e["botao"], f"o botão não inverteu ({e['botao']!r})")
    cobrar(e["ptt"], "o botão de segurar não apareceu com o modo ligado")

    # servidor confirma: o pedido temporário cai
    injeta({"motor": {"hold_to_talk": True, "hold_to_talk_sugerido": True}})
    cobrar(pg.evaluate("LX.obs.holdPedido === undefined"),
           "o pedido temporário não foi descartado depois da confirmação do servidor")

    # ── sair do modo ───────────────────────────────────────────────────────
    pg.evaluate("document.getElementById('lxHoldBtn').click()")
    pg.wait_for_timeout(150)
    enviados = pg.evaluate("window.__enviados")
    cobrar((enviados or [None])[-1] == '{"type":"hold","on":false}',
           f"o desligar não mandou hold:off ({enviados})")
    cobrar("usar segurar para falar" in estado()["botao"], "o botão não voltou ao de ligar")

    # ── ligado SEM sugestão: a faixa continua (senão não há saída) ─────────
    pg.evaluate("() => { LX.obs.holdPedido = undefined; }")
    injeta({"motor": {"hold_to_talk": True, "hold_to_talk_sugerido": False}})
    e = estado()
    cobrar(e["aviso"], "faixa sumiu com o modo ligado e sem sugestão — sem como sair")
    cobrar("voltar ao automático" in e["botao"], "sem botão de saída no modo ligado")

    # ── desligado e sem sugestão: some ─────────────────────────────────────
    injeta({"motor": {"hold_to_talk": False, "hold_to_talk_sugerido": False}})
    cobrar(not estado()["aviso"], "a faixa ficou com o modo desligado e sem sugestão")

    # ── pedido otimista que o servidor RECUSA volta atrás ─────────────────
    # Servidor velho responde `comando desconhecido: 'hold'`. O botão não pode
    # ficar dizendo "segurando" para sempre: um `error` derruba o otimismo.
    pg.evaluate("""() => { LX.obs.holdPedido = undefined;
        LX.obs.stats = { motor: { hold_to_talk: false, hold_to_talk_sugerido: true } };
        LX.ws = { readyState: WebSocket.OPEN, send: () => {} }; lxHoldRender(); }""")
    pg.evaluate("document.getElementById('lxHoldBtn').click()")
    cobrar(pg.evaluate("LX.obs.holdPedido") == True, "o clique não marcou pedido otimista")
    pg.evaluate("""() => lxRecebe({ data: JSON.stringify(
        { type: 'error', code: 'comando', message: "comando desconhecido: 'hold'" }) })""")
    pg.wait_for_timeout(120)
    cobrar(pg.evaluate("LX.obs.holdPedido === undefined"), "o `error` do servidor não desfez o pedido")
    cobrar("usar segurar para falar" in estado()["botao"],
           f"o botão ficou dizendo o modo com o pedido recusado ({estado()['botao']!r})")

    # e o prazo: sem nenhuma confirmação, o otimismo cai sozinho (3 s)
    pg.evaluate("document.getElementById('lxHoldBtn').click()")
    cobrar(pg.evaluate("LX.obs.holdPedido") == True, "segundo clique não marcou pedido")
    pg.wait_for_timeout(3400)
    cobrar(pg.evaluate("LX.obs.holdPedido === undefined"), "sem confirmação o pedido ficou de pé após o prazo")
    pg.evaluate("LX.ws = null")

    # ── SESSÃO REAL (pula enquanto o servidor não publicar os campos) ──────
    pg.evaluate("() => { LX.obs.stats = null; LX.obs.holdPedido = undefined; lxHoldRender(); }")
    pg.evaluate("document.getElementById('lxLigar').click()")
    motor = None
    for _ in range(40):                       # até ~10 s pelo primeiro stats
        pg.wait_for_timeout(250)
        motor = pg.evaluate("(LX.obs.stats || {}).motor || null")
        if motor and ("hold_to_talk" in motor or "hold_to_talk_sugerido" in motor):
            break
    if not motor or not ("hold_to_talk" in motor or "hold_to_talk_sugerido" in motor):
        print("· SKIP do bloco de sessão real: o servidor ainda não publica "
              "motor.hold_to_talk/hold_to_talk_sugerido (task_5fdeb23c)")
    else:
        cobrar(pg.evaluate("document.getElementById('lxHold').textContent") in ("automático", "segurando"),
               "com os campos reais o OBS não mostrou o modo")
        # a sugestão, quando vier do servidor, tem de acender a faixa sozinha
        if motor.get("hold_to_talk_sugerido"):
            cobrar(estado()["aviso"], "a sugestão REAL não fez a faixa aparecer")
        elif "hold_to_talk_sugerido" in motor:
            cobrar(not estado()["aviso"], "a faixa apareceu sem sugestão real")
        # ligar pelo botão tem de virar `hold_to_talk` no stats do servidor
        if not motor.get("hold_to_talk"):
            pg.evaluate("document.getElementById('lxHoldBtn').click()")
            virou = False
            for _ in range(40):
                pg.wait_for_timeout(250)
                virou = bool(pg.evaluate("((LX.obs.stats||{}).motor||{}).hold_to_talk"))
                if virou:
                    break
            cobrar(virou, "o stats do servidor não passou a hold_to_talk depois do clique")
            pg.evaluate("document.getElementById('lxHoldBtn').click()")   # devolve o modo
        pg.evaluate("() => { if (LX.ws) lxDesligar(); }")
    b.close()

if falhas:
    print("✖ falhas:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print("✔ hold: sem campo nada aparece, sugestão mostra o botão, ligar/desligar manda o JSON e o estado acompanha")
PYEOF
