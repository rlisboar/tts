#!/usr/bin/env bash
# "Desfazer edição" da voz (#493): o denoise/replace do #443 guarda
# `voices/<id>.orig.wav` e o admin desfaz com `POST /api/voices/<id>/restore-original`.
# Aqui se prova, na tela, o contrato inteiro:
#
#   · a ação APARECE quando o servidor diz `tem_original` e NÃO aparece sem ele
#     (não se infere pela UI: quem sabe é o servidor);
#   · com chave `use` ela fica travada e diz o motivo (#412) e o 403 explica na faixa;
#   · com admin, o clique devolve o áudio ORIGINAL byte a byte (sha256 conferido no
#     disco), a lista recarrega e o toast de sucesso aparece.
#
# Estado próprio (#541): `TTS_VOICES_DIR`/`TTS_SETTINGS_PATH`/`TTS_APIKEYS_PATH`/…
# em tmp — o `voices/` e o `settings.json` do dono não são tocados. O diretório da
# rodada morre no verde e fica na vermelha; a varredura por idade recolhe as sobras
# de rodadas mortas (#562).
#
#   ./tests/voz_desfazer_ui.sh       # instância própria, porta livre
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# A fase da chave `use` só existe por um IP NÃO-loopback (loopback é admin por
# construção), então a instância escuta em 0.0.0.0 e a UI abre pelo IP de LAN.
LAN="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
if [ -z "$LAN" ]; then
  echo "✖ sem IP de LAN (en0/en1) — a UI só vê chave de uso por um IP não-loopback" >&2
  exit 1
fi

source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - "$LAN" <<'PYEOF'
import hashlib, json, os, pathlib, signal, socket, subprocess, sys, time, urllib.request

LAN = sys.argv[1]

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

# #562 (mesma classe): o diretório da rodada (com chaves sintéticas) morre no
# verde e a varredura por idade recolhe o que ficou de rodadas mortas.
import shutil, time
for _velho in pathlib.Path("/tmp").glob("voz_desfazer_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 7200:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass
porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"voz_desfazer_{porta}"
VOZES = DIR / "voices"
for d in (VOZES, DIR / "outputs"):
    d.mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")

# duas vozes: uma SEM original (nunca editada) e outra que será editada de verdade.
# A editada usa FALA de verdade: desde o #523 a guarda de timbre RECUSA a limpeza
# de um tom puro (`recusado_por: nivel`, nada escrito) e sem recusa não há `.orig`
# — o teste do desfazer ficava impossível com senoide.
import numpy as np, soundfile as sf
FALA = pathlib.Path("tests/fixtures/voz_fala_5s_16k.wav")
def cria(vid, freq):
    n = int(1.2 * 16000)
    a = (np.sin(2 * np.pi * freq * np.arange(n) / 16000) * 0.3 + 0.01 * np.random.randn(n))
    sf.write(str(VOZES / f"{vid}.wav"), np.clip(a, -1, 1).astype("<f4"), 16000, subtype="PCM_16")
    (VOZES / f"{vid}.json").write_text(json.dumps(
        {"id": vid, "name": vid, "created_at": "2026-01-01 00:00:00", "duration": 1.2}))
def cria_fala(vid):
    import shutil as _sh
    _sh.copy(FALA, str(VOZES / f"{vid}.wav"))
    (VOZES / f"{vid}.json").write_text(json.dumps(
        {"id": vid, "name": vid, "created_at": "2026-01-01 00:00:00", "duration": 5.0}))
cria("voz-limpa", 220)
cria_fala("voz-editada")
ORIGINAL = VOZES / "voz-editada.orig.wav"

porta_key = porta_livre()          # sobe depois: a chave `use` só é usada na 2ª fase
env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_key}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_VOICES_DIR": str(VOZES), "TTS_OUTPUTS_DIR": str(DIR / "outputs"),
       "TTS_SETTINGS_PATH": str(DIR / "settings.json"), "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"),
       "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"), "TTS_SPEAKER_PATH": str(DIR / "speaker.json"),
       "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"), "VOZ_PORT": str(porta),
       "ORT_DISABLE_TELEMETRY": "1"}
log = pathlib.Path("/tmp") / f"voz_desfazer_servidor-{porta}.log"
proc = subprocess.Popen(
    [sys.executable, "-c",
     "import os, uvicorn, app\nuvicorn.run(app.app, host='0.0.0.0', port=int(os.environ['VOZ_PORT']))"],
    cwd=str(pathlib.Path.cwd()), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
    start_new_session=True)
base = f"http://127.0.0.1:{porta}"          # API/curl: loopback (admin)
LANURL = f"http://{LAN}:{porta}"            # navegador: IP de LAN (a chave importa)

def espera_saude(prazo=60):
    for _ in range(prazo * 5):
        if proc.poll() is not None:
            raise SystemExit(f"servidor morreu na subida (log {log})")
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read(); return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"servidor não subiu em {prazo}s (log {log})")

def req(metodo, caminho, chave=None, corpo=None):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    cab = {"Content-Type": "application/json"}
    if chave: cab["X-API-Key"] = chave
    r = urllib.request.Request(base + caminho, data=dados, method=metodo, headers=cab)
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status, (json.loads(resp.read() or b"{}") or {})

def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()

try:
    espera_saude()
    print(f"servidor de teste em {base} · UI em {LANURL}")
    # o `use` precisa de um provedor para o prewarm; um stub mínimo basta
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')
    threading.Thread(target=HTTPServer(("127.0.0.1", porta_key), Stub).serve_forever, daemon=True).start()

    # ─── edição de verdade: denoise deixa o `.orig` guardado ───────────────
    sha_antes_edicao = sha(VOZES / "voz-editada.wav")
    st, _ = req("POST", "/api/voices/voz-editada/denoise", None, {"strength": 0.7})
    cobrar(st == 200, f"denoise não rodou (HTTP {st})")
    cobrar(ORIGINAL.exists(), "o denoise não guardou o `.orig` — o teste não vale sem ele")
    if ORIGINAL.exists():
        cobrar(sha(ORIGINAL) == sha_antes_edicao,
               "o `.orig` não é o áudio de antes da edição")
    sha_orig = sha(ORIGINAL) if ORIGINAL.exists() else None
    print("  original guardado:", (sha_orig or "—")[:12])

    _, lv = req("GET", "/api/voices")
    por_id = {v["id"]: v for v in lv}
    cobrar(por_id.get("voz-editada", {}).get("tem_original") is True,
           "o servidor não marca `tem_original` na voz editada")
    cobrar(por_id.get("voz-limpa", {}).get("tem_original") is False,
           "o servidor marcou `tem_original` numa voz nunca editada")

    # Pela LAN o loopback não vale mais: o navegador precisa de chave. Uma admin
    # para as fases 1 e 2 e uma `use` para a fase 3 (criadas pelo loopback).
    _, ka = req("POST", "/api/apikeys", None, {"name": f"probe-undo-adm-{porta}", "role": "admin"})
    chave_adm = (ka.get("key") or {}).get("secret")
    _, k = req("POST", "/api/apikeys", None, {"name": f"probe-undo-{porta}", "role": "use"})
    chave_use = (k.get("key") or {}).get("secret")
    cobrar(bool(chave_adm) and bool(chave_use), f"não consegui criar as chaves: {ka} / {k}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context()
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(LANURL, wait_until="domcontentloaded")
        pg.evaluate("(c) => localStorage.setItem('ttsStudioKey', c)", chave_adm)
        pg.goto(LANURL, wait_until="networkidle")
        pg.evaluate("() => document.querySelector('.nav-item[data-view=\"vozes\"]').click()")
        pg.wait_for_selector("#voiceList .item", timeout=45000)
        pg.wait_for_timeout(600)

        # ─── 1) quem decide é o servidor: a ação só existe onde há `.orig` ──
        alvos = pg.evaluate("""() => [...document.querySelectorAll('#voiceList .item')].map(it => ({
            nome: (it.querySelector('.name') || {}).textContent || '',
            undo: !!it.querySelector('.undo'),
            desab: it.querySelector('.undo') ? it.querySelector('.undo').disabled : null,
            titulo: it.querySelector('.undo') ? it.querySelector('.undo').title : '' }))""")
        com = [a for a in alvos if "editada" in a["nome"]]
        sem = [a for a in alvos if "limpa" in a["nome"]]
        print(f"  ação na voz editada: {com and com[0]['undo']} · na voz sem original: {sem and sem[0]['undo']}")
        cobrar(bool(com) and com[0]["undo"], "a voz com `.orig` não ganhou a ação de desfazer")
        cobrar(bool(sem) and not sem[0]["undo"], "voz sem `.orig` mostrou a ação de desfazer")
        if com:
            cobrar(not com[0]["desab"], "com chave admin (loopback) a ação nasce travada")

        # ─── 2) o clique desfaz de verdade (byte a byte) e recarrega ────────
        pg.evaluate("""() => {
            const u = document.querySelector('#voiceList .item .undo');
            if (!u) return;                       // a ausência já virou falha acima
            const orig = window.confirm; window.confirm = () => true;
            u.click();
            setTimeout(() => { window.confirm = orig; }, 100);
        }""")
        pg.wait_for_timeout(1500)
        cobrar(ORIGINAL.exists(), "depois de desfazer o `.orig` sumiu (dá para desfazer de novo?)")
        cobrar(sha(VOZES / "voz-editada.wav") == sha_orig,
               "o áudio restaurado não é byte-idêntico ao original")
        corpo = pg.evaluate("() => document.body.innerText")
        cobrar("desfeita" in corpo.lower(), "a tela não confirmou o desfazer")
        # a voz segue com `.orig` (dá para desfazer depois de editar de novo)
        _, lv2 = req("GET", "/api/voices")
        cobrar({v["id"]: v for v in lv2}.get("voz-editada", {}).get("tem_original") is True,
               "o `tem_original` sumiu da voz depois do desfazer")

        # ─── 3) chave `use`: ação travada, motivo dito, 403 explicado ───────
        pg.evaluate("(c) => localStorage.setItem('ttsStudioKey', c)", chave_use)
        pg.reload(wait_until="networkidle")
        pg.evaluate("() => document.querySelector('.nav-item[data-view=\"vozes\"]').click()")
        pg.wait_for_selector("#voiceList .item", timeout=45000)
        pg.wait_for_timeout(800)
        travada = pg.evaluate("""() => { const u = document.querySelector('#voiceList .item .undo');
            return u ? { desab: u.disabled, titulo: u.title || '' } : null; }""")
        print(f"  com chave use -> desab={travada and travada['desab']} · titulo={(travada or {}).get('titulo')!r}")
        cobrar(travada is not None, "a ação de desfazer não apareceu para a chave `use`")
        if travada:
            cobrar(travada["desab"], "a ação devia estar travada com chave `use`")
            cobrar("administrativa" in travada["titulo"].lower(),
                   f"a ação travada não diz o motivo ({travada['titulo']!r})")
        pg.evaluate("""() => {
            const u = document.querySelector('#voiceList .item .undo');
            if (!u) return;
            const orig = window.confirm; window.confirm = () => true;
            u.disabled = false; u.click();
            setTimeout(() => { window.confirm = orig; }, 100);
        }""")
        pg.wait_for_timeout(1200)
        faixa = pg.text_content("#keyBarText") or ""
        corpo_use = pg.evaluate("() => document.body.innerText")
        print(f"  força do clique com use -> faixa={faixa[:45]!r}")
        cobrar("administrativa" in faixa.lower(), f"o 403 do desfazer não foi explicado ({faixa[:60]!r})")
        cobrar("desfeita" not in corpo_use.lower(), "com chave use a tela disse que desfez (devia recusar)")

        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
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

print()
if falhas:
    print("✖ FALHOU — desfazer edição da voz:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)     # só a vermelha guarda
    for f in falhas: print("  ·", f)
    sys.exit(1)
try: log.unlink()
except Exception: pass
try: shutil.rmtree(DIR)              # verde não deixa diretório para trás
except Exception: pass
print("✔ OK — a ação só existe com `.orig`, restaura byte a byte, recarrega e trava para chave `use`")
PYEOF
