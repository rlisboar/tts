#!/usr/bin/env bash
# Assistente do cadastro (#528): frase sugerida + medidor AO VIVO de fala e ruído
# durante a gravação, pelo endpoint `/api/speaker/avalia` (#690) — a pessoa
# corrige ANTES de gastar a amostra.
#
# Cobrado aqui:
#   · a frase sugerida aparece e "outra frase" troca;
#   · durante a gravação o medidor aparece e mostra a fala acumulada ("de 3s") e o SNR;
#   · ao terminar a gravação o medidor some;
#   · servidor sem o endpoint (404) não quebra nada: o medidor não aparece e o
#     cadastro segue (feature-detect).
#
# Estado próprio em tmp (nada do dono), microfone falso do Chromium sobre a fixture
# de fala.
#
#   ./tests/voz_assistente_ui.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
FIX="$RAIZ/tests/fixtures/voz_fala_5s_16k.wav"
[ -f "$FIX" ] || { echo "✖ fixture de fala ausente ($FIX)" >&2; exit 1; }

source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - <<'PYEOF'
import json, os, pathlib, shutil, socket, subprocess, sys, threading, time, traceback, urllib.request

falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

for _velho in pathlib.Path("/tmp").glob("voz_assist_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 3600:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"voz_assist_{porta}"
for _sub in ("outputs", "voices", "state"):
    (DIR / _sub).mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")
porta_stub = porta_livre()
env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_stub}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(DIR / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(DIR / "speaker.json"), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_VOICES_DIR": str(DIR / "voices"), "TTS_METRICS_DIR": str(DIR / "state"),
       "VOZ_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = DIR / "servidor.log"
proc = subprocess.Popen([sys.executable, "-c",
    "import os, uvicorn, app\nuvicorn.run(app.app, host='127.0.0.1', port=int(os.environ['VOZ_PORT']))"],
    cwd=str(pathlib.Path.cwd()), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
    start_new_session=True)
base = f"http://127.0.0.1:{porta}"
FIX = pathlib.Path("tests/fixtures/voz_fala_5s_16k.wav")

class Stub(threading.Thread):
    def run(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')
        HTTPServer(("127.0.0.1", porta_stub), H).serve_forever()
threading.Thread(target=Stub().run, daemon=True).start()

def espera_saude(prazo=90):
    for _ in range(prazo * 5):
        if proc.poll() is not None:
            raise SystemExit(f"servidor morreu na subida (log {log})")
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read(); return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"servidor não subiu em {prazo}s (log {log})")

def esperar(fn, msg, timeout=60000):
    try:
        fn(); return True
    except Exception as e:
        if type(e).__name__ != "TimeoutError":
            raise
        cobrar(False, f"tempo esgotado esperando: {msg}")
        return False

try:
    espera_saude()
    print(f"servidor de teste em {base}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--use-fake-device-for-media-stream",
                                     "--use-fake-ui-for-media-stream",
                                     f"--use-file-for-fake-audio-capture={FIX}"])
        ctx = b.new_context(permissions=["microphone"])
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(base, wait_until="domcontentloaded")
        pg.click('.nav-item[data-view="config"]')
        pg.click('[data-stab="rede"]')
        pg.wait_for_selector("#spEnroll", state="visible", timeout=20000)

        # ── 1) frase sugerida e troca ────────────────────────────────────────
        f1 = (pg.text_content("#spFrase") or "").strip()
        print(f"  frase 1: {f1[:50]!r}")
        cobrar(len(f1) > 10, f"a frase sugerida não apareceu ({f1!r})")
        pg.click("#spOutraFrase")
        pg.wait_for_timeout(150)
        f2 = (pg.text_content("#spFrase") or "").strip()
        cobrar(f2 and f2 != f1, f"o 'outra frase' não trocou ({f2[:50]!r})")

        # ── 2) medidor durante a gravação ────────────────────────────────────
        pg.fill("#spName", "perfil-medidor")
        pg.click("#spEnroll")
        esperar(lambda: pg.wait_for_selector("#spMedidor:not([hidden])", timeout=30000),
                "o medidor aparecer durante a gravação")
        esperar(lambda: pg.wait_for_function(
            "() => /fala \\d/.test(document.getElementById('spMedidor').textContent)",
            timeout=30000), "a fala acumulada no medidor")
        med = (pg.text_content("#spMedidor") or "").strip()
        print(f"  medidor: {med[:70]!r}")
        cobrar("de 3s" in med, f"o medidor não mostra o alvo de 3 s ({med[:70]!r})")
        cobrar("SNR" in med, f"o medidor não mostra o SNR ({med[:70]!r})")
        # a gravação termina sozinha (8 s) e o medidor sai
        esperar(lambda: pg.wait_for_function(
            "() => document.getElementById('spMedidor').hidden", timeout=60000),
            "o medidor sumir depois da gravação")
        esperar(lambda: pg.wait_for_function(
            "() => /amostra 1/.test(document.getElementById('spProgresso').textContent)",
            timeout=60000), "a amostra entrar")

        # ── 3) servidor sem o endpoint (404): não quebra o cadastro ──────────
        pg.route(lambda u: "/api/speaker/avalia" in u,
                 lambda route: route.fulfill(status=404, content_type="application/json",
                                             body='{"detail":"sem avalia"}'))
        pg.fill("#spName", "perfil-sem-medidor")
        pg.evaluate("() => { const o = window.spGravar; window.spGravar = (s, st) => o(5, st); }")
        pg.click("#spEnroll")
        # o 404 só é descoberto no 1º trecho (~2,5 s). A prova tem de ser DURANTE a
        # gravação: no fim o `parar()` esconde o medidor de qualquer jeito e a
        # célula não distinguiria o ramo do 404 de nada.
        # tem de esconder DURANTE a gravação: no fim o `parar()` esconde de
        # qualquer jeito e a célula não distinguiria o ramo do 404 de nada
        cedo = False
        for _ in range(28):                  # ~7 s, dentro dos 8 s de gravação
            pg.wait_for_timeout(250)
            st = pg.text_content("#spStatus") or ""
            if pg.evaluate("() => document.getElementById('spMedidor').hidden") and "gravando" in st:
                cedo = True
                break
        cobrar(cedo, "o medidor não se escondeu DURANTE a gravação — o ramo do 404 não agiu")
        cobrar(not (pg.text_content("#spMedidor") or "").strip(),
               f"o medidor ficou com texto depois do 404 ({(pg.text_content('#spMedidor') or '')[:60]!r})")
        esperar(lambda: pg.wait_for_function(
            "() => /amostra 1/.test(document.getElementById('spProgresso').textContent)",
            timeout=60000), "o cadastro seguir sem o medidor")

        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        b.close()

except BaseException as exc:
    if not isinstance(exc, SystemExit):
        falhas.append(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
finally:
    try: proc.terminate()
    except Exception: pass
    try: proc.wait(timeout=10)
    except Exception:
        try: proc.kill()
        except Exception: pass

print()
if falhas:
    print("✖ assistente do cadastro:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — frase sugerida trocável, medidor ao vivo de fala/SNR durante a gravação, "
      "sai no fim e não quebra o cadastro se o servidor não tiver o endpoint")
PYEOF
