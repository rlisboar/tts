#!/usr/bin/env bash

# Flag de diagnóstico do AEC no Live (#1067, pedido do #870).
#
# `?aec=0` na URL abre o mic do Live com `echoCancellation: false` para medir
# o eco CRU (a suspeita do #870: o `mic.wav` do dump já chega pós-AEC do
# Chrome e por isso não correlaciona com a referência). Sem a flag, tudo como
# antes. Esta suíte cobra os DOIS lados, no navegador e não no texto:
#   1. sem flag  -> track.getSettings().echoCancellation === true, badge oculto,
#                   setup leva `captura.aec: true`
#   2. ?aec=0    -> echoCancellation === false, NS/AGC iguais ao normal,
#                   badge "AEC desligado — diagnóstico" visível, setup leva
#                   `captura.aec: false`, log do console e evento do Live
#   3. console sem erro de página nos dois
#
# Não abre WS nem carrega modelo: chama `lxCaptura()` direto (é a mesma função
# que o `onopen` chama) e intercepta o `lxEnviaJSON` do `lxSetup()`. O
# servidor é PRÓPRIO, em porta livre, com settings/chaves em /tmp.
# Mic: dispositivo falso do Chromium (`--use-fake-device-for-media-stream`),
# que respeita o `echoCancellation` pedido no `getSettings()`.
#
#   ./tests/live_aec_diag_ui.sh             # verde/vermelho, não escreve na árvore
#   EVIDENCE=1 ./tests/live_aec_diag_ui.sh  # regrava evidence/1067-live-aec-diag.png
#
# Mordida conferida: com `echoCancellation: true` fixo no `lxCaptura` a rodada
# `?aec=0` fica vermelha (echoCancellation, console e evento do Live).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
export LIVE_AEC_LOG="${LIVE_AEC_LOG:-/tmp/live_aec_diag_servidor-$$.log}"

cd "$RAIZ"
"$PY" - <<'PYEOF'
import os, pathlib, signal, socket, subprocess, sys, time, urllib.request

RAIZ = pathlib.Path.cwd()
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

# Fora da faixa do mic-router (7861-7865, #898).
def porta_livre():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]
        if not 7861 <= porta <= 7865:
            return porta

porta = porta_livre()
_SETTINGS = pathlib.Path(f"/tmp/live_aec_diag_settings-{porta}.json")
_SETTINGS.write_text("{}")
_KEYS = pathlib.Path(f"/tmp/live_aec_diag_apikeys_{porta}.json")
_LEGACY = pathlib.Path(f"/tmp/live_aec_diag_apikey_{porta}")
env = {**os.environ, "LIVE_UI_PORT": str(porta), "TTS_SETTINGS_PATH": str(_SETTINGS),
       "TTS_APIKEYS_PATH": str(_KEYS), "TTS_LEGACY_APIKEY_PATH": str(_LEGACY)}
log = pathlib.Path(os.environ.get("LIVE_AEC_LOG") or f"/tmp/live_aec_diag_servidor-{porta}.log")
_launcher = '''
import os, uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["LIVE_UI_PORT"]))
'''
proc = subprocess.Popen([str(RAIZ / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                        start_new_session=True)
base = f"http://127.0.0.1:{porta}"

def rodada(b, sufixo, diag):
    ctx = b.new_context(permissions=["microphone"])
    pg = ctx.new_page()
    erros, consola = [], []
    pg.on("pageerror", lambda e: erros.append(str(e)))
    pg.on("console", lambda m: consola.append(m.text))
    pg.goto(base + sufixo, wait_until="networkidle")
    pg.wait_for_selector("#view-live", state="attached", timeout=15000)
    r = pg.evaluate("""async () => {
        let setup = null;
        window.lxEnviaJSON = m => { setup = m; };   // sem WS: só captura o payload
        lxSetup();
        await lxCaptura();
        const st = LX.stream.getAudioTracks()[0].getSettings();
        const badge = document.getElementById('lxAecDiag');
        const log = document.getElementById('lxLog').textContent;
        lxParaCaptura();
        return { ec: st.echoCancellation, ns: st.noiseSuppression, agc: st.autoGainControl,
                 badge: !!badge && !badge.hidden && badge.offsetParent !== null,
                 badgeTxt: badge ? badge.textContent.trim() : null,
                 captura: setup && setup.captura, log };
    }""")
    pg.wait_for_timeout(200)
    linha = next((c for c in consola if c.startswith("[live] captura:")), None)
    nome = "?aec=0" if diag else "sem flag"
    print(f"  {nome}: echoCancellation={r['ec']} noiseSuppression={r['ns']} "
          f"autoGainControl={r['agc']} badge={r['badge']} captura={r['captura']}")
    print(f"    console: {linha!r}")
    cobrar(r["ec"] is (not diag), f"{nome}: echoCancellation={r['ec']!r} (esperado {not diag})")
    cobrar(r["ns"] is True, f"{nome}: noiseSuppression mudou ({r['ns']!r}) — a flag só mexe no AEC")
    cobrar(r["agc"] is False, f"{nome}: autoGainControl mudou ({r['agc']!r}) — a flag só mexe no AEC")
    cobrar(r["badge"] is diag, f"{nome}: badge do diagnóstico visível={r['badge']} (esperado {diag})")
    cobrar(r["captura"] == {"aec": not diag}, f"{nome}: setup.captura={r['captura']!r}")
    cobrar(linha is not None and (f"echoCancellation={str(not diag).lower()}" in linha),
           f"{nome}: log do console sem o echoCancellation aplicado ({linha!r})")
    if diag:
        cobrar(r["badgeTxt"] == "AEC desligado — diagnóstico", f"texto do badge: {r['badgeTxt']!r}")
        cobrar("AEC do navegador DESLIGADO" in r["log"], "evento do Live não registrou o AEC desligado")
    else:
        cobrar("AEC" not in r["log"], "sem flag o log do Live fala de AEC")
    cobrar(not erros, f"{nome}: erros de JS: " + "; ".join(erros[:3]))
    # PNG versionado só sob pedido (#720: rodada comum não suja a árvore)
    if diag and os.environ.get("EVIDENCE") == "1":
        pg.set_viewport_size({"width": 1280, "height": 800})
        pg.screenshot(path=str(RAIZ / "evidence" / "1067-live-aec-diag.png"))
    ctx.close()

try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu na subida (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor de teste em {base}")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-fake-ui-for-media-stream",
                                    "--use-fake-device-for-media-stream"])
        rodada(b, "/#live", diag=False)
        rodada(b, "/?aec=0#live", diag=True)
        rodada(b, "/?aec=1#live", diag=False)   # só "0" liga o diagnóstico
        b.close()
finally:
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try: proc.terminate()
        except Exception: pass
    try: proc.wait(timeout=10)
    except Exception:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass
    for f in (_SETTINGS, _KEYS, _LEGACY):
        try: f.unlink()
        except Exception: pass

print()
if falhas:
    print("✖ FALHOU — log do servidor da rodada em", log)
    for f in falhas: print("  ·", f)
    sys.exit(1)
try: log.unlink()
except Exception: pass
print("✔ OK — ?aec=0 abre o mic do Live com echoCancellation=false (NS/AGC iguais), "
      "badge visível e setup.captura.aec=false; sem a flag tudo como antes")
PYEOF
