#!/usr/bin/env bash
# Gate de locutor por ALVO DE ERRO (#528, parte destravada pelo #525/I3): o rigor
# deixa de ser um número solto e passa a ser escolhido por FAR — "1 erro a cada
# 100 falas" / "1 a cada 1000", com o número cru em "Avançado".
#
# O contrato do servidor (`ee24f17`) é lista FECHADA: `""` | `"1"` | `"0.1"`; o
# POST canoniza qualquer outra coisa em `""`. A tela tem de:
#
#   · oferecer exatamente essa lista, com o alvo DITO em fala (100 / 1000);
#   · guardar o número cru debaixo de "Avançado" (com o rótulo de que só vale no
#     modo cru) e avisar disso quando um alvo está ativo;
#   · não inventar opção quando o settings traz valor fora da lista (cai em "");
#   · mandar `speaker_far_alvo` no Salvar e refletir no reload o que o servidor
#     guardou;
#   · mostrar o anti-clone como "em breve", desligado e inerte (não vai no payload);
#   · avisar quando o servidor diz que caiu para o extrator LEGADO
#     (`extrator_efetivo`) — e não afirmar nada quando o campo não vem.
#
# Estado próprio em tmp (nada do dono), sem gravação de áudio: todas as células são
# de settings/DOM. O `extrator_efetivo` é injetado por `page.route` no GET (hoje o
# servidor ainda não o manda), com o corpo real buscado antes — o resto é servidor
# de verdade. Diretório morre no verde; sobras caem por idade.
#
#   ./tests/voz_far_alvo_ui.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

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

for _velho in pathlib.Path("/tmp").glob("voz_far_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 3600:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"voz_far_{porta}"
(DIR / "outputs").mkdir(parents=True, exist_ok=True)
(DIR / "voices").mkdir(exist_ok=True)
SETTINGS = DIR / "settings.json"
SETTINGS.write_text("{}")
porta_stub = porta_livre()

env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_stub}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(SETTINGS),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(DIR / "speaker.json"), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_VOICES_DIR": str(DIR / "voices"), "VOZ_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = DIR / "servidor.log"
_launcher = '''
import os, pathlib, metrics_hist
metrics_hist.DIR = pathlib.Path(os.environ["TTS_OUTPUTS_DIR"]).parent / "state"
import uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["VOZ_PORT"]))
'''
proc = subprocess.Popen([sys.executable, "-c", _launcher], cwd=str(pathlib.Path.cwd()), env=env,
                        stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
base = f"http://127.0.0.1:{porta}"

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

def settings_get():
    with urllib.request.urlopen(base + "/api/settings", timeout=10) as r:
        return json.loads(r.read())

def settings_post(corpo):
    r = urllib.request.Request(base + "/api/settings", data=json.dumps(corpo).encode(),
                               method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read())

def esperar(fn, msg, timeout=20000):
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
        def abre(extrator=None):
            b = pw.chromium.launch()
            ctx = b.new_context()
            pg = ctx.new_page()
            erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
            if extrator is not None:
                # injeta só o campo novo no GET real (o servidor ainda não o manda)
                def injeta(route):
                    if route.request.method != "GET":
                        return route.continue_()
                    corpo = settings_get()
                    corpo["extrator_efetivo"] = extrator
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(corpo))
                pg.route("**/api/settings", injeta)
            pg.goto(base, wait_until="domcontentloaded")
            pg.click('.nav-item[data-view="config"]')
            pg.click('[data-stab="rede"]')
            pg.wait_for_selector("#spFarAlvo", state="visible", timeout=20000)
            return b, ctx, pg, erros

        # ── 1) a lista é a FECHADA do servidor, dita em fala ─────────────────
        b, ctx, pg, erros = abre()
        opcoes = pg.evaluate("""() => [...document.querySelectorAll('#spFarAlvo option')]
            .map(o => ({v: o.value, t: o.textContent}))""")
        print("  opções:", [(o["v"], o["t"][:40]) for o in opcoes])
        cobrar([o["v"] for o in opcoes] == ["", "1", "0.1"],
               f"a lista não é a fechada do servidor (''|1|0.1): {[o['v'] for o in opcoes]}")
        cobrar(any("100" in o["t"] for o in opcoes) and any("1000" in o["t"] for o in opcoes),
               "as opções não dizem o alvo em fala (100 / 1000 falas)")
        cobrar(pg.input_value("#spFarAlvo") == "", "o select nasce com alvo escolhido")

        # ── 2) o número cru mora em "Avançado" e se rotula com alvo ativo ────
        cobrar(pg.evaluate("""() => {
            const d = document.getElementById('spAvancado');
            return !!d && d.tagName === 'DETAILS' && !!d.querySelector('#spThr');
        }"""), "o número cru não está dentro de um bloco 'Avançado'")
        cobrar(not pg.is_visible("#spThr"), "o número cru aparece sem abrir o Avançado")
        cobrar(not pg.is_visible("#spThrNota"), "o rótulo do rigor aparece sem alvo ativo")
        pg.select_option("#spFarAlvo", "0.1")
        pg.wait_for_timeout(200)
        nota = (pg.text_content("#spThrNota") or "").strip()
        print(f"  nota com alvo:", nota[:90])
        cobrar("modo cru" in nota.lower(), f"o rótulo não diz que só vale no modo cru ({nota[:80]!r})")
        cobrar("1 erro a cada 1000" in nota, f"o rótulo não diz o alvo ativo ({nota[:80]!r})")
        pg.select_option("#spFarAlvo", "")
        pg.wait_for_timeout(200)
        cobrar(not pg.is_visible("#spThrNota"), "o rótulo ficou com o alvo vazio")

        # ── 3) anti-clone: "em breve", desligado e inerte ────────────────────
        anti = pg.evaluate("""() => { const c = document.getElementById('spAntiClone');
            return { existe: !!c, desab: c && c.disabled, marcado: c && c.checked,
                     rotulo: (c && c.parentElement.textContent) || '' }; }""")
        print("  anti-clone:", {k: anti[k] for k in ("existe", "desab", "marcado")},
              "·", anti["rotulo"][:40].strip())
        cobrar(anti["existe"], "toggle do anti-clone não existe")
        cobrar(anti["desab"], "o anti-clone não está desabilitado (ainda não há servidor)")
        cobrar(not anti["marcado"], "o anti-clone nasce marcado")
        cobrar("em breve" in anti["rotulo"].lower(), "o anti-clone não diz que está por vir")

        # ── 4) salva o alvo e o reload devolve o que o SERVIDOR guardou ──────
        capturado = {}
        pg.on("request", lambda r: capturado.update(json.loads(r.post_data or "{}"))
              if r.url.endswith("/api/settings") and r.method == "POST" else None)
        pg.select_option("#spFarAlvo", "1")
        pg.click("#cfgSave")
        esperar(lambda: pg.wait_for_function(
            "() => !!document.getElementById('cfgStatus').textContent", timeout=20000),
            "o Salvar responder")
        print("  payload speaker_far_alvo:", capturado.get("speaker_far_alvo"))
        cobrar(capturado.get("speaker_far_alvo") == "1",
               f"o alvo não foi no payload do Salvar ({capturado.get('speaker_far_alvo')!r})")
        cobrar("anti_clone" not in capturado or "spAntiClone" not in capturado,
               "o anti-clone (ainda inerte) foi mandado no payload")
        cobrar(settings_get().get("speaker_far_alvo") == "1", "o servidor não guardou o alvo")
        pg.reload(wait_until="domcontentloaded")
        pg.click('.nav-item[data-view="config"]')
        pg.click('[data-stab="rede"]')
        esperar(lambda: pg.wait_for_selector("#spFarAlvo", state="visible", timeout=20000),
                "o cadastro reabrir depois do reload")
        pg.wait_for_timeout(400)
        cobrar(pg.input_value("#spFarAlvo") == "1",
               f"o reload não refletiu o alvo salvo ({pg.input_value('#spFarAlvo')!r})")
        cobrar(pg.is_visible("#spThrNota"), "o rótulo não voltou com o alvo salvo")

        # ── 5) valor FORA da lista (settings à mão) não vira opção fantasma ──
        settings_post({"speaker_far_alvo": "2"})
        pg.reload(wait_until="domcontentloaded")
        pg.click('.nav-item[data-view="config"]')
        pg.click('[data-stab="rede"]')
        esperar(lambda: pg.wait_for_selector("#spFarAlvo", state="visible", timeout=20000),
                "o cadastro reabrir")
        pg.wait_for_timeout(400)
        n_op = pg.evaluate("() => document.querySelectorAll('#spFarAlvo option').length")
        cobrar(n_op == 3, f"apareceu opção fantasma para o valor inválido (n={n_op})")
        cobrar(pg.input_value("#spFarAlvo") == "", "valor fora da lista não caiu em ''")
        # o servidor canoniza em "" no próximo Salvar: o alvo inválido não é regravado
        pg.click("#cfgSave")
        pg.wait_for_timeout(800)
        cobrar(settings_get().get("speaker_far_alvo") == "",
               f"o alvo inválido foi regravado ({settings_get().get('speaker_far_alvo')!r})")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

        # ── 6) aviso do extrator legado (feature-detect) ─────────────────────
        b, ctx, pg, erros = abre("ecapa:v1")
        cobrar(not pg.is_visible("#spExtratorAviso"),
               "avisou queda para o legado com o extrator atual")
        ctx.close(); b.close()

        b, ctx, pg, erros = abre("resemblyzer:v1")
        esperar(lambda: pg.wait_for_selector("#spExtratorAviso:not([hidden])", timeout=20000),
                "o aviso do extrator legado")
        aviso = (pg.text_content("#spExtratorAviso") or "").strip()
        print(f"  aviso do extrator: {aviso[:90]}")
        cobrar("resemblyzer:v1" in aviso, f"o aviso não nomeia o extrator ({aviso[:80]!r})")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

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
    print("✖ rigor por FAR / anti-clone / aviso de extrator:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — alvo de FAR na lista fechada do servidor, número cru em Avançado e rotulado, "
      "anti-clone como 'em breve' inerte e aviso quando o app cai para o extrator legado")
PYEOF
