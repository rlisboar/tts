#!/usr/bin/env bash
# Recadastro GUIADO do perfil legado (#646): o perfil de outro extrator continua
# autenticando, e as amostras novas entram num espaço separado (`pendente`) até
# fechar o mínimo — só então a troca acontece. Quem decide é o SERVIDOR; a tela:
#
#   · mostra "perfil antigo em uso", quantas amostras do extrator novo já entraram
#     e quantas faltam (progresso N/3), com o aviso de que a troca é automática;
#   · anuncia a conclusão quando o servidor para de recadastrar;
#   · oferece "descartar o parcial" (`DELETE /api/speaker/{name}/pendente`, admin)
#     — travado com motivo para chave `use`, como as outras ações elevadas.
#
# Perfil legado SINTÉTICO: `modelo: "extrator:v0"`, que nunca é o efetivo (assim o
# caso vale com ou sem os pesos do extrator novo na máquina). Estado próprio em tmp
# (`TTS_SPEAKER_PATH` etc.), microfone falso do Chromium sobre a fixture de fala.
#
#   ./tests/voz_recadastro_ui.sh
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

# Fora da faixa de controle do mic-router (7861-7865, #898): se o servidor do
# teste cair ali, a sonda do painel OBS fala com ele no lugar do roteador e
# leva CORS — vermelho que não existe.
def porta_livre():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]
        if not 7861 <= porta <= 7865:
            return porta

for _velho in pathlib.Path("/tmp").glob("voz_recad_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 3600:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"voz_recad_{porta}"
for _sub in ("outputs", "voices", "state"):
    (DIR / _sub).mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")
SPEAKER = DIR / "speaker.json"
# perfil LEGADO sintético: 2 amostras de um extrator que nunca é o efetivo
SPEAKER.write_text(json.dumps({
    "Antigo": {"vecs": [[0.0] * 256, [0.001] * 256], "updated": 1.0, "modelo": "extrator:v0"},
}))
porta_stub = porta_livre()
env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_stub}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(DIR / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(SPEAKER), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
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

def perfis():
    with urllib.request.urlopen(base + "/api/speaker/profiles", timeout=10) as r:
        return json.loads(r.read())

def perfil(nome):
    return next((p for p in perfis() if p["name"] == nome), None)

def esperar(fn, msg, timeout=30000):
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
        pg.evaluate("() => { const o = window.spGravar; window.spGravar = (s, st) => o(5, st); }")

        def chip(nome):
            el = pg.query_selector(f'[data-nome="{nome}"]')
            return (el.text_content() or "").strip() if el else ""

        # ── 1) perfil legado: "antigo em uso", sem parcial ───────────────────
        esperar(lambda: pg.wait_for_selector('[data-nome="Antigo"]', timeout=20000),
                "o perfil legado na lista")
        c0 = chip("Antigo")
        print(f"  legado na lista: {c0!r}")
        cobrar("antigo em uso" in c0, f"a lista não diz que o perfil antigo está em uso ({c0!r})")
        # sem parcial o chip mostra o que o perfil ANTIGO tem e oferece regravar;
        # a contagem do espaço novo só existe depois da 1ª amostra
        cobrar("2/3" in c0, f"a contagem do perfil antigo não aparece ({c0!r})")
        cobrar("regravar" in c0, f"o atalho de regravar não está lá ({c0!r})")
        cobrar(not pg.query_selector('[data-descarta="Antigo"]'),
               "ofereceu descartar sem parcial nenhum")

        # ── 2) uma amostra: progresso guiado e parcial no servidor ───────────
        pg.fill("#spName", "Antigo")
        pg.click("#spEnroll")
        esperar(lambda: pg.wait_for_function(
            "() => /perfil antigo em uso/.test(document.getElementById('spProgresso').textContent)",
            timeout=90000), "a linha de progresso do recadastro")
        prog = (pg.text_content("#spProgresso") or "").strip()
        print(f"  após 1 amostra: {prog[:110]!r}")
        cobrar("faltam" in prog, f"a linha não diz quantas faltam ({prog[:110]!r})")
        cobrar("sozinha" in prog, "a linha não avisa que a troca é automática")
        p1 = perfil("Antigo")
        cobrar(bool(p1 and p1.get("pendente") and p1["pendente"]["samples"] == 1),
               f"o servidor não guardou a parcial: {p1}")
        cobrar(bool(p1 and p1.get("precisa_recadastrar") is True),
               "o perfil deixou de estar marcado para recadastro")
        esperar(lambda: pg.wait_for_selector('[data-descarta="Antigo"]', timeout=20000),
                "o botão de descartar o parcial")
        c1 = chip("Antigo")
        print(f"  chip com parcial: {c1!r}")
        cobrar("1/3" in c1, f"a lista não mostra o progresso ({c1!r})")
        cobrar("andamento" in (pg.text_content("#spProfiles") or ""),
               "a nota do recadastro em andamento não aparece")

        # ── 3) chave `use`: descartar travado com motivo ─────────────────────
        pg.evaluate("() => { ADMIN.is = false; refreshSpProfiles(); }")
        pg.wait_for_timeout(300)
        trav = pg.evaluate("""() => { const b = document.querySelector('[data-descarta="Antigo"]');
            return b ? { desab: b.disabled, titulo: b.title || '' } : null; }""")
        print(f"  descartar com ADMIN.is=false: {trav}")
        cobrar(bool(trav and trav["desab"]), "o descartar não travou para chave de uso")
        cobrar(bool(trav and "administrativa" in trav["titulo"].lower()),
               f"o descartar travado não diz o motivo ({(trav or {}).get('titulo')!r})")
        pg.evaluate("() => { ADMIN.is = true; refreshSpProfiles(); }")
        pg.wait_for_timeout(300)

        # ── 4) destrava e descarta de verdade ────────────────────────────────
        pg.evaluate("() => document.querySelector('[data-descarta=\"Antigo\"]').click()")
        esperar(lambda: pg.wait_for_function(
            "() => !document.querySelector('[data-descarta=\"Antigo\"]')", timeout=20000),
            "o parcial sumir da lista")
        cobrar(perfil("Antigo").get("pendente") is None, "o servidor manteve o parcial")
        c2 = chip("Antigo")
        print(f"  após descartar: {c2!r}")
        cobrar("regravar" in c2 and "parcial" not in c2,
               f"o perfil não voltou ao estado sem parcial ({c2!r})")
        cobrar("antigo em uso" in c2, "o perfil deixou de estar em uso pelo extrator antigo")
        cobrar("andamento" not in (pg.text_content("#spProfiles") or ""),
               "a nota do recadastro em andamento ficou depois do descarte")
        cobrar("descartado" in (pg.text_content("#toast") or "").lower(),
               f"a tela não confirmou o descarte: {(pg.text_content('#toast') or '')[:60]!r}")

        # ── 5) três amostras concluem o recadastro ───────────────────────────
        pg.fill("#spName", "Antigo")
        for i in (1, 2):
            pg.click("#spEnroll")
            esperar(lambda: pg.wait_for_function(
                f"() => /faltam {3 - i}/.test(document.getElementById('spProgresso').textContent)",
                timeout=90000), f"a amostra {i} do recadastro")
        cobrar((perfil("Antigo").get("pendente") or {}).get("samples") == 2,
               f"o servidor não acumulou 2 parciais: {perfil('Antigo')}")
        pg.click("#spEnroll")
        esperar(lambda: pg.wait_for_function(
            "() => /recadastro de \"Antigo\" concluído/.test(document.getElementById('spProgresso').textContent)",
            timeout=90000), "a conclusão do recadastro")
        fim = perfil("Antigo")
        print(f"  após a 3ª: {fim}")
        cobrar(fim.get("pendente") is None, "o parcial continuou depois da conclusão")
        cobrar(fim.get("precisa_recadastrar") is False,
               "o perfil continua marcado para recadastro depois de concluir")
        cobrar("concluído" in (pg.text_content("#spProgresso") or ""),
               "a tela não anunciou a conclusão")

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
    print("✖ recadastro guiado:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — perfil legado diz 'antigo em uso' com o que falta, o parcial é descartável "
      "(travado para `use`, com o perfil antigo intacto) e a 3ª amostra conclui a troca")
PYEOF
