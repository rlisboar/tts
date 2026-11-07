#!/usr/bin/env bash
# Prévia A/B da limpeza (#528, contrato do #690): "Limpar" NÃO aplica mais de
# cara — mostra original × limpa com DNSMOS antes/depois e timbre preservado, e só
# grava quando o dono aprova.
#
# Cobrado aqui:
#   · o painel aparece com os dois lados, os números e o botão de descartar;
#   · enquanto é só prévia, o `.wav` NÃO muda (hash idêntico);
#   · "Descartar" fecha o painel e o arquivo continua intacto;
#   · "Aplicar limpeza" grava (hash muda) e a lista recarrega;
#   · recusa da guarda (`X-Recusado: 1`) mostra o motivo e trava o aplicar;
#   · servidor sem a prévia (404) cai no caminho antigo, que ainda aplica direto;
#   · etapas da limpeza (#528) nascem desligadas, são admin e, com painel aberto,
#     refazem a prévia (as etapas entram na medição).
#
# Vozes SINTÉTICAS em tmp (o `voices/` do dono não é tocado), sem microfone.
#
#   ./tests/voz_previa_limpa_ui.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - <<'PYEOF'
import hashlib, json, os, pathlib, shutil, socket, subprocess, sys, threading, time, traceback, urllib.request

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

for _velho in pathlib.Path("/tmp").glob("voz_previa_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 3600:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"voz_previa_{porta}"
VOZES = DIR / "voices"
for _sub in (VOZES, DIR / "outputs", DIR / "state"):
    _sub.mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")

import shutil as _sh
# voz de FALA de verdade (a guarda de timbre compara locutor: um tom puro é
# recusado e aí o "aplicar" nunca escreve)
_sh.copy("tests/fixtures/voz_fala_5s_16k.wav", str(VOZES / "voz-ruidosa.wav"))
(VOZES / "voz-ruidosa.json").write_text(json.dumps(
    {"id": "voz-ruidosa", "name": "voz-ruidosa", "created_at": "2026-01-01 00:00:00",
     "duration": 5.0}))

porta_stub = porta_livre()
env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_stub}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(DIR / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(DIR / "speaker.json"), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_VOICES_DIR": str(VOZES), "TTS_METRICS_DIR": str(DIR / "state"),
       "VOZ_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = DIR / "servidor.log"
proc = subprocess.Popen([sys.executable, "-c",
    "import os, uvicorn, app\nuvicorn.run(app.app, host='127.0.0.1', port=int(os.environ['VOZ_PORT']))"],
    cwd=str(pathlib.Path.cwd()), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
    start_new_session=True)
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

def sha():
    return hashlib.sha256((VOZES / "voz-ruidosa.wav").read_bytes()).hexdigest()

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
        b = pw.chromium.launch()
        ctx = b.new_context()
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(base, wait_until="domcontentloaded")
        pg.click('.nav-item[data-view="vozes"]')
        pg.wait_for_selector("#voiceList .item", timeout=45000)
        pg.wait_for_timeout(500)

        def limpar():
            pg.evaluate("() => document.querySelector('#voiceList .item .clean').click()")

        # ── 1) a prévia aparece sem tocar no arquivo ─────────────────────────
        antes = sha()
        limpar()
        esperar(lambda: pg.wait_for_selector(".ab-previa", timeout=60000), "o painel de prévia")
        painel = pg.evaluate("""() => ({ audios: document.querySelectorAll('.ab-previa audio').length,
            aplicar: !!document.querySelector('.ab-previa .ab-aplicar'),
            descartar: !!document.querySelector('.ab-previa .ab-descartar'),
            nums: (document.querySelector('.ab-previa .ab-num') || {}).textContent || '' })""")
        print(f"  painel: {painel}")
        cobrar(painel["audios"] == 2, f"o painel não tem os dois lados ({painel['audios']})")
        cobrar(painel["aplicar"] and painel["descartar"], "faltou aplicar/descartar no painel")
        cobrar("DNSMOS" in painel["nums"], f"os números não aparecem ({painel['nums']!r})")
        cobrar(sha() == antes, "a PRÉVIA já escreveu no arquivo")

        # ── 2) descartar não escreve nada ────────────────────────────────────
        pg.evaluate("() => document.querySelector('.ab-previa .ab-descartar').click()")
        pg.wait_for_timeout(300)
        cobrar(not pg.query_selector(".ab-previa"), "o painel não fechou ao descartar")
        cobrar(sha() == antes, "descartar mexeu no arquivo")
        # `.orig` é o backup do #443: só nasce no ACEITE, nunca na prévia/descarte
        cobrar(not (VOZES / "voz-ruidosa.orig.wav").exists(),
               "descartar criou o `.orig` (o backup só pode nascer no aplicar)")

        # ── 3) aplicar grava de verdade ──────────────────────────────────────
        limpar()
        esperar(lambda: pg.wait_for_selector(".ab-previa .ab-aplicar", timeout=60000), "o painel de novo")
        pg.evaluate("() => document.querySelector('.ab-previa .ab-aplicar').click()")
        esperar(lambda: pg.wait_for_function(
            "() => !document.querySelector('.ab-previa')", timeout=60000), "a lista recarregar")
        pg.wait_for_timeout(500)
        cobrar(sha() != antes, "aplicar não mudou o áudio")
        cobrar((VOZES / "voz-ruidosa.orig.wav").exists(),
               "aplicar não criou o `.orig` (sem ele não há como desfazer)")
        cobrar("limpa" in (pg.text_content("#voiceList") or ""), "a voz não ficou marcada como limpa")

        # ── 4) recusa da guarda trava o aplicar e diz o motivo ───────────────
        pg.route(lambda u: "denoise?previa=true" in u, lambda route: route.fulfill(
            status=200, content_type="audio/wav",
            headers={"X-Recusado": "1", "X-Motivo": "timbre caiu abaixo de 0,82",
                     "X-Timbre": "0.61", "X-Dnsmos-Antes": "3.4", "X-Dnsmos-Depois": "3.6"},
            body=(VOZES / "voz-ruidosa.wav").read_bytes()))
        limpar()
        esperar(lambda: pg.wait_for_selector(".ab-previa .ab-recusa", timeout=60000), "o aviso de recusa")
        rec = pg.evaluate("""() => ({ texto: document.querySelector('.ab-previa .ab-recusa').textContent,
            desab: document.querySelector('.ab-previa .ab-aplicar').disabled })""")
        print(f"  recusa: {rec['texto'][:70]!r} · aplicar travado={rec['desab']}")
        cobrar("timbre caiu" in rec["texto"], f"o motivo da recusa não aparece ({rec['texto'][:70]!r})")
        cobrar(rec["desab"], "com recusa o aplicar devia ficar travado")
        pg.evaluate("() => document.querySelector('.ab-previa .ab-descartar').click()")
        pg.unroute(lambda u: "denoise?previa=true" in u)

        # ── 5) etapas da limpeza (#528): nascem desligadas, são admin e, com um
        #       painel aberto, a prévia é REFEITA (as etapas entram na medição) ──
        cobrar(pg.is_checked("#vozLimpaMusica") is False, "música deveria nascer desligada")
        cobrar(pg.is_checked("#vozLimpaDereverb") is False, "reverb deveria nascer desligado")
        for cid, chave in (("#vozLimpaMusica", "voice_limpar_musica"),
                           ("#vozLimpaDereverb", "voice_limpar_dereverb")):
            cobrar(pg.get_attribute(cid, "data-admin-setting") == chave,
                   f"{cid} perdeu o marcador de admin")
        vistas, posts = [], []
        pg.on("request", lambda r: vistas.append(r) if "denoise?previa=true" in r.url else None)
        pg.on("request", lambda r: posts.append(r)
              if r.url.endswith("/api/settings") and r.method == "POST" else None)
        limpar()
        esperar(lambda: pg.wait_for_selector(".ab-previa", timeout=60000), "painel antes da etapa")
        n0 = len(vistas)
        # `esperar` avalia UMA vez (não repete): a requisição é aguardada pelo
        # próprio Playwright, sem corrida contra o onchange assíncrono
        with pg.expect_request(lambda r: "denoise?previa=true" in r.url, timeout=60000):
            pg.check("#vozLimpaMusica")
        esperar(lambda: pg.wait_for_selector(".ab-previa", timeout=60000), "painel refeito")
        cobrar(len(vistas) > n0, "mudar etapa não refez a prévia")
        cobrar(bool(posts), "a etapa não foi gravada em /api/settings")
        cobrar(any("voice_limpar_musica" in (r.post_data or "") for r in posts),
               "o POST da etapa não nomeia o campo")
        cobrar(pg.is_checked("#vozLimpaMusica") is True, "o toggle não ficou ligado")
        pg.evaluate("() => document.querySelector('.ab-previa .ab-descartar')?.click()")
        pg.uncheck("#vozLimpaMusica")
        pg.wait_for_timeout(400)

        # ── 6) servidor sem prévia (404): caminho antigo, aplica direto ──────
        pg.route(lambda u: "denoise?previa=true" in u,
                 lambda route: route.fulfill(status=404, content_type="application/json",
                                             body='{"detail":"sem previa"}'))
        antes2 = sha()
        limpar()
        esperar(lambda: pg.wait_for_function(
            "() => !document.querySelector('.ab-previa')", timeout=60000), "o fallback aplicar")
        pg.wait_for_timeout(800)
        cobrar(not pg.query_selector(".ab-previa"), "o 404 abriu painel")
        cobrar(sha() != antes2, "o caminho antigo (404) não aplicou")

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
    print("✖ prévia da limpeza:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — a limpeza mostra antes × depois com DNSMOS/timbre, não escreve antes do "
      "aceite, descarta de graça, trava na recusa, refaz a prévia ao mexer nas etapas "
      "e cai no caminho antigo se o servidor não tiver prévia")
PYEOF
