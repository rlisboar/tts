#!/usr/bin/env bash
# Job CANCELADO não é falha (#799). O servidor mantém `status: "error"` no
# cancelamento (contrato) e a tela pintava "erro no modelo" — o dono caçava
# defeito que não existia.
#
# Cobrado aqui:
#   · `error` com texto de cancelamento/morte por sinal → painel diz "geração
#     cancelada" em tom de aviso, não "erro no modelo";
#   · `error` de verdade (OOM, por exemplo) continua vermelho e com o texto;
#   · o erro do job carrega a marca `cancelado` para quem chama o polling.
#
# Instância própria em tmp, tudo por `page.route` (nenhum modelo é carregado).
#
#   ./tests/cancelado_ui.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - <<'PYEOF'
import json, os, pathlib, shutil, socket, subprocess, sys, time, traceback, urllib.request

falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

# Fora da faixa de controle do mic-router (7861-7865, #898): se o servidor do
# teste cair ali, a sonda do painel OBS fala com ele no lugar do roteador e
# leva CORS — vermelho que não existe.
def porta_livre():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]
        if not 7861 <= porta <= 7865:
            return porta

for _velho in pathlib.Path("/tmp").glob("cancelado_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 3600:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"cancelado_{porta}"
for _sub in ("voices", "outputs", "state"):
    (DIR / _sub).mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")
env = {**os.environ, "TTS_VOICES_DIR": str(DIR / "voices"),
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(DIR / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(DIR / "speaker.json"), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_METRICS_DIR": str(DIR / "state"), "VOZ_PORT": str(porta)}
log = DIR / "servidor.log"
proc = subprocess.Popen([sys.executable, "-c",
    "import os, uvicorn, app\nuvicorn.run(app.app, host='127.0.0.1', port=int(os.environ['VOZ_PORT']))"],
    cwd=str(pathlib.Path.cwd()), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
    start_new_session=True)
base = f"http://127.0.0.1:{porta}"

def espera_saude(prazo=90):
    for _ in range(prazo * 5):
        if proc.poll() is not None:
            raise SystemExit(f"servidor morreu na subida (log {log})")
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read(); return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"servidor não subiu em {prazo}s (log {log})")

try:
    espera_saude()
    print(f"servidor de teste em {base}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(base, wait_until="domcontentloaded")

        def status_com(msg):
            pg.route("**/api/status", lambda r: r.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"status": "error", "error": msg, "model": "voz-teste"})))
            pg.evaluate("() => refreshStatus()")
            pg.wait_for_timeout(400)
            r = pg.evaluate("""() => ({ txt: document.getElementById('status').textContent,
                                        cls: document.getElementById('status').className })""")
            pg.unroute("**/api/status")
            return r

        # 1) morte por sinal (o que o cancelamento produz) não é defeito
        r = status_com("Worker morto por sinal 15 (voz-teste). Servidor OK.")
        print(f"  sinal 15 -> {r['txt']!r} ({r['cls']!r})")
        cobrar("cancelada" in r["txt"], f"cancelamento não foi dito: {r['txt']!r}")
        cobrar("erro no modelo" not in r["txt"], f"cancelamento ainda pintado de erro: {r['txt']!r}")
        cobrar("err" not in r["cls"].split(), f"cancelamento com tom de erro: {r['cls']!r}")

        # 2) falha de verdade segue vermelha, com o motivo
        r = status_com("CUDA out of memory ao carregar o modelo")
        print(f"  OOM -> {r['txt']!r} ({r['cls']!r})")
        cobrar("erro no modelo" in r["txt"], f"falha real não foi dita: {r['txt']!r}")
        cobrar("err" in r["cls"].split(), f"falha real sem tom de erro: {r['cls']!r}")

        # 3) o erro do job carrega a marca para quem chama
        pg.route("**/api/tts/jobs/*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"status": "error", "error": "Worker morto por sinal 15"})))
        marca = pg.evaluate("""async () => {
            try { await pollPieces('x', {stopped: false, sources: [], ctx: null}); return {}; }
            catch (e) { return { msg: e.message, cancelado: e.cancelado === true }; } }""")
        print(f"  erro do job -> {marca}")
        cobrar(marca.get("cancelado") is True, f"o erro do job não veio marcado: {marca}")
        pg.unroute("**/api/tts/jobs/*")

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
    print("✖ cancelamento:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — cancelamento sai como 'geração cancelada' (aviso, sem marca de erro) e a "
      "falha real continua vermelha; o erro do job carrega a marca para quem chama")
PYEOF
