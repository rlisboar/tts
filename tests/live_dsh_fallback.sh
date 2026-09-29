#!/usr/bin/env bash
# Live: fallback quando o harness `dsh` NÃO sobe (#146) — a sessão segue no
# endpoint e a TELA TEM DE DIZER ISSO (#150).
#
# Sobe instância própria com `TTS_CHAT_BACKEND=dsh` e `TTS_CHAT_DSH_BIN=/bin/false`
# (binário que sai na hora: o handshake morre antes de qualquer token), com um
# stub de chat para o caminho de fallback. Conecta o WS real, faz UM turno e cobra:
# evento recebido, aviso PERSISTENTE na tela com o motivo e o backend em vigor, o
# selo do painel deixando de dizer "dsh", e áudio voltando (resposta pelo openai).
#
#   ./tests/live_dsh_fallback.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Não carrega modelo de áudio (o turno usa stub de chat e o TTS é do servidor) —
# ainda assim entra na trava para não competir com as suítes de modelo.
source "$RAIZ/tests/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - "$RAIZ" <<'PYEOF'
import base64, json, os, pathlib, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

RAIZ = pathlib.Path(sys.argv[1])
SUF = str(os.getpid())
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

class Stub(BaseHTTPRequestHandler):
    FRASES = ["Claro, ", "o dia está bonito hoje. "]
    def log_message(self, *a): pass
    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if corpo.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            try:                       # o cliente pode fechar no meio (BrokenPipe é normal)
                for f in self.FRASES:
                    self.wfile.write(f"data: {json.dumps({'choices':[{'delta':{'content':f}}]})}\n\n".encode())
                    self.wfile.flush(); time.sleep(0.02)
                self.wfile.write(b"data: [DONE]\n\n")
            except BrokenPipeError:
                pass
        else:
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": "".join(self.FRASES)}}]}).encode())

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

stub_porta = porta_livre()
threading.Thread(target=HTTPServer(("127.0.0.1", stub_porta), Stub).serve_forever, daemon=True).start()

import numpy as np, soundfile as sf
wav = None
for w in sorted(pathlib.Path("voices").glob("*.wav")):
    a, sr = sf.read(str(w), dtype="float32")
    if a.ndim > 1: a = a.mean(axis=1)
    if len(a) < sr: continue
    idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
    a16 = np.clip(np.interp(idx, np.arange(len(a)), a), -1, 1)
    a16 = a16 / (float(np.abs(a16).max()) or 1.0) * 0.95
    pcm = np.concatenate([(a16 * 32767), np.zeros(int(3 * 16000))]).astype("<i2").tobytes()
    wav = base64.b64encode(pcm).decode()
    break
if not wav: raise SystemExit("sem voices/*.wav para alimentar o turno")

porta = porta_livre()
env = {**os.environ,
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1", "TTS_CHAT_MODEL": "stub-fallback",
       "TTS_CHAT_BACKEND": "dsh",           # pede o dsh…
       "TTS_CHAT_DSH_BIN": "/bin/false",    # …que morre no handshake
       "FALLBACK_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = pathlib.Path("/tmp") / f"live_dsh_fallback_{SUF}.log"
_launcher = '''
import os, uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["FALLBACK_PORT"]), log_level="warning")
'''
proc = subprocess.Popen([str(RAIZ / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                        start_new_session=True)
base = f"http://127.0.0.1:{porta}"
try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu na subida (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor de teste em {base} (backend pedido=dsh, bin=/bin/false, stub :{stub_porta})")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-fake-device-for-media-stream",
                                    "--use-fake-ui-for-media-stream"])
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=base)
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        cons = []; pg.on("console", lambda m: cons.append(m.text)
                         if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        eventos = []
        def guarda(f):
            dado = getattr(f, "payload", f)
            if isinstance(dado, str):
                try: eventos.append(json.loads(dado).get("type"))
                except Exception: pass
        pg.on("websocket", lambda w: w.on("framereceived", guarda))
        pg.goto(base, wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.locator("#lxLigar").click()
        pg.wait_for_function("() => LX.ws && LX.ws.readyState === 1", timeout=20000)

        # turno: fala de verdade em quadros de 100 ms (o mesmo caminho do cliente)
        pg.evaluate("""async (pcmB64) => {
            const bin = atob(pcmB64); const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const i16 = new Int16Array(bytes.buffer);
            for (let i = 0; i + 1600 <= i16.length; i += 1600) {
                if (!LX.ws || LX.ws.readyState !== 1) break;
                LX.ws.send(i16.slice(i, i + 1600).buffer);
                await new Promise(r => setTimeout(r, 25));
            }
            if (LX.ws && LX.ws.readyState === 1) LX.ws.send(JSON.stringify({ type: "end_of_speech" }));
        }""", wav)

        # o dsh tenta 5 handshakes com backoff antes de desistir (~8 s) — é o preço
        # que o usuário paga UMA vez por sessão, e é o que o aviso explica
        try:
            pg.wait_for_function("() => LX.obs.dshFallback", timeout=120000)
        except Exception:
            print("  DIAG:", pg.evaluate("() => ({estado: LX.estado, ev: Object.keys(LX.obs), log: document.getElementById('lxLog').textContent.slice(-300)})"))
            raise

        tela = pg.evaluate("""() => {
            const a = document.getElementById('lxDshAviso');
            return { visivel: !!a && !a.hidden, aviso: a ? a.textContent : '',
                     backend: document.getElementById('lxProv').textContent,
                     log: document.getElementById('lxLog').textContent }; }""")
        print("  aviso na tela:", tela["aviso"][:110])
        print("  chip backend:", tela["backend"])
        cobrar("dsh_indisponivel" in eventos, f"evento dsh_indisponivel não chegou (eventos: {eventos[:8]})")
        cobrar(tela["visivel"], "aviso do fallback não está visível na tela")
        cobrar("indisponível" in tela["aviso"] and "openai" in tela["aviso"],
               f"aviso não diz que o dsh caiu e por qual backend a sessão segue: {tela['aviso']!r}")
        cobrar("não pelo harness" in tela["aviso"], "aviso não deixa claro que o backend em vigor é OUTRO")
        cobrar("fallback do dsh" in tela["backend"] and tela["backend"].strip() != "dsh",
               f"selo do painel continua dizendo 'dsh': {tela['backend']!r}")
        cobrar(tela["backend"].startswith("openai"), f"selo não nomeia o backend em vigor: {tela['backend']!r}")
        pg.wait_for_timeout(400)   # o title vem do tick do painel
        # o título tem de trazer o MOTIVO (o tick do painel reescreve o do evento —
        # se o motivo só existir no caminho do evento, ele se perde; #151)
        titulo = pg.evaluate("() => (document.getElementById('lxProv').parentElement||{}).title") or ""
        cobrar("não subiu" in titulo.lower(), f"título do selo sem o aviso de fallback: {titulo!r}")
        cobrar("binário" in titulo.lower() or "erro" in titulo.lower(),
               f"título do selo sem o MOTIVO do fallback: {titulo!r}")
        cobrar("dsh indisponível" in tela["log"], "aviso não foi para o log de eventos")

        # a resposta TEM de sair pelo fallback (áudio voltando)
        try:
            pg.wait_for_function("() => LX.obs.rec > 0", timeout=60000)
        except Exception:
            pass
        rec = pg.evaluate("() => LX.obs.rec")
        ia = pg.evaluate("() => document.getElementById('lxIA').textContent")
        print(f"  chunks de áudio recebidos: {rec} · texto da IA: {ia[:40]!r}")
        cobrar(rec > 0, f"nenhum áudio voltou pelo fallback (rec={rec})")
        cobrar(ia.strip(), "coluna da IA ficou vazia (fallback não respondeu)")
        cobrar(pg.evaluate("() => LX.obs.dshFallback.fallback") == "openai",
               "fallback registrado no cliente não é openai")

        # o aviso é DA SESSÃO: encerrando, some
        pg.evaluate("() => document.getElementById('lxLigar').click()")
        pg.wait_for_function("() => !LX.ws || LX.ws.readyState > 1", timeout=15000)
        pg.wait_for_timeout(400)
        cobrar(pg.evaluate("() => document.getElementById('lxDshAviso').hidden"),
               "aviso continuou na tela depois de encerrar a sessão")

        print(f"  pageerror: {erros} · console.error de script: {cons}")
        cobrar(not erros, f"pageerror: {erros}")
        cobrar(not cons, f"console.error de script: {cons}")
        b.close()
finally:
    try: os.killpg(os.getpgid(proc.pid), 15)
    except Exception: proc.terminate()

if falhas:
    print("\n✖ FALHAS:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print("\n✔ OK — dsh morto: sessão segue no openai, aviso persistente na tela, selo corrigido e áudio voltando.")
PYEOF