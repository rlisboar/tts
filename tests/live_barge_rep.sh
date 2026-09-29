#!/usr/bin/env bash
# Barge-in em RAJADA (task_91ced1b7 / resíduo do #125): repete SÓ o cenário de
# barge-in N vezes contra o mesmo servidor, para medir a frequência do falso
# vermelho visto ("barge-in não aconteceu nem com fala injetada", 1x em 3) e
# capturar a ASSINATURA da decisão do motor quando falha — é o que decide de quem
# é o fix (motor/VAD = audio-ml, pipeline = speech-pipeline).
#
#   ./tests/live_barge_rep.sh          # N=6 por padrão
#   REP=10 ./tests/live_barge_rep.sh   # N explícito
#   MIC_FILE=1 REP=20 ./tests/live_barge_rep.sh   # mic FALSO tocando o wav em loop
#
# DOIS SENTIDOS, e eles precisam de setups diferentes:
#   • barge VERDADEIRO (sem MIC_FILE): o mic falso do Chromium fica no padrão dele e
#     os turnos são dirigidos pela injeção de quadros. Mede "onset durante playback
#     vira interrupção?" — é o modo que reproduz o falso vermelho do #161 (3/6).
#   • barge FALSO por eco (MIC_FILE=1): o mic falso passa a tocar o MESMO wav em
#     loop, o que é a melhor aproximação de "o mic ouvindo o próprio TTS" num
#     browser headless (não há caminho acústico entre a saída do app e o mic). Aqui
#     o alvo é o oposto: medir barge disparando SEM ninguém falar — é o tradeoff
#     que uma janela de playback mais generosa pode piorar.
# O harness imprime a taxa de barge; para o sentido FALSO olhe também `barge_falso`
# no payload do `speech_end` (é o campo que o motor usa para o eco).
#
# NÃO falha por si (é investigação): imprime um resumo com taxa e assinaturas e
# sai 1 só se NENHUM barge funcionou (aí sim há algo quebrado, não cauda).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
export REP="${REP:-6}"

source "$RAIZ/tests/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - "$RAIZ" <<'PYEOF'
import base64, json, os, pathlib, socket, statistics, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

RAIZ = pathlib.Path(sys.argv[1])
REP = int(os.environ.get("REP", "6"))
SUF = str(os.getpid())

class Stub(BaseHTTPRequestHandler):
    FRASES = ["Claro, ", "o dia está bonito hoje. ", "Quer que eu conte mais alguma coisa?"]
    def log_message(self, *a): pass
    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if corpo.get("stream"):
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                for f in self.FRASES:
                    self.wfile.write(f"data: {json.dumps({'choices':[{'delta':{'content':f}}]})}\n\n".encode())
                    self.wfile.flush(); time.sleep(0.02)
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "".join(self.FRASES)}}]}).encode())
        except BrokenPipeError:
            pass

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

stub_porta = porta_livre()
threading.Thread(target=HTTPServer(("127.0.0.1", stub_porta), Stub).serve_forever, daemon=True).start()

import numpy as np, soundfile as sf
fala = None
for w in sorted(pathlib.Path("voices").glob("*.wav")):
    a, sr = sf.read(str(w), dtype="float32")
    if a.ndim > 1: a = a.mean(axis=1)
    if len(a) < sr: continue
    idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
    a16 = np.clip(np.interp(idx, np.arange(len(a)), a), -1, 1)
    a16 = a16 / (float(np.abs(a16).max()) or 1.0) * 0.95
    pcm = np.concatenate([(a16 * 32767), np.zeros(int(3 * 16000))]).astype("<i2").tobytes()
    fala = base64.b64encode(pcm).decode()
    wav_path = pathlib.Path("/tmp") / f"live_barge_fala_{SUF}.wav"
    sf.write(str(wav_path), np.frombuffer(pcm, dtype="<i2"), 16000, subtype="PCM_16")
    break
if not fala: raise SystemExit("sem voices/*.wav")

porta = porta_livre()
env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
       "TTS_CHAT_MODEL": "stub-barge", "TTS_CHAT_BACKEND": "openai",
       # o Live tem backend PRÓPRIO: sem este pin o dono com `chat_backend_live: dsh`
       # tirava o harness do stub e a contagem de barge media outra rota (#195)
       "TTS_CHAT_BACKEND_LIVE": os.environ.get("TTS_CHAT_BACKEND_LIVE") or "openai",
       "BARGE_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = pathlib.Path("/tmp") / f"live_barge_rep_{SUF}.log"
# Instrumenta a JANELA DE PLAYBACK do servidor (sem editar arquivo de outro dono):
# é ela que arma o barge. Imprime quando abre/fecha, para correlacionar com a injeção.
_launcher = '''
import os, sys, time, uvicorn, live_turns, app
_orig = live_turns.TurnEngine.set_speaking
def _patched(self, ligado, nivel_dbfs=None, **kw):
    _orig(self, ligado, nivel_dbfs, **kw)   # **kw: duracao_ms do #167
    print(f"[JANELA] {time.time():.3f} speaking={ligado}", file=sys.stderr, flush=True)
live_turns.TurnEngine.set_speaking = _patched
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["BARGE_PORT"]), log_level="info")
'''

proc = subprocess.Popen([str(RAIZ / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                        start_new_session=True)
base = f"http://127.0.0.1:{porta}"

def tail(n=8):
    try: return "".join(log.read_text().splitlines(keepends=True)[-n:])
    except Exception: return "-"

try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor {base} · stub :{stub_porta} · repetições={REP}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        args = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
        if os.environ.get("MIC_FILE"):
            args.append(f"--use-file-for-fake-audio-capture={wav_path}")
            print("  MIC_FILE=1: mic falso tocando o wav em loop (aproxima o sentido do eco)")
        b = p.chromium.launch(args=args)
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=base)
        pg = ctx.new_page()
        pg.goto(base, wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        # instrumenta: eventos do WS com carimbo (o que o motor DECIDIU fica no
        # payload de speech_start/speech_end/barge_in)
        pg.evaluate("""() => {
            window.__ev = []; window.__sp = []; window.__audio = 0;
            window.__mk = () => {
                if (!LX.ws) { setTimeout(window.__mk, 50); return; }
                LX.ws.addEventListener("message", ev => {
                    if (typeof ev.data !== "string") { window.__audio++; window.__ev.push({ tipo: "<audio>", t: performance.now() }); return; }
                    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
                    window.__ev.push({ tipo: m.type, t: performance.now() });
                    if (["speech_start", "speech_end", "barge_in"].includes(m.type)) window.__sp.push(m);
                });
            };
            window.__mk();
        }""")
        pg.locator("#lxLigar").click()
        pg.wait_for_function("() => LX.ws && LX.ws.readyState === 1", timeout=20000)

        def injeta():
            pg.evaluate("""async (b64) => {
                const bin = atob(b64); const by = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) by[i] = bin.charCodeAt(i);
                const i16 = new Int16Array(by.buffer);
                for (let i = 0; i + 1600 <= i16.length; i += 1600) {
                    if (!LX.ws || LX.ws.readyState !== 1) break;
                    LX.ws.send(i16.slice(i, i + 1600).buffer);
                    await new Promise(r => setTimeout(r, 25));
                }
                if (LX.ws && LX.ws.readyState === 1) LX.ws.send(JSON.stringify({ type: "end_of_speech" }));
            }""", fala)

        def audio_n():
            return pg.evaluate("() => window.__audio || 0")

        def espera_audio(n0, timeout=90000):
            """Espera um áudio NOVO do servidor (playback tocando: é o que se interrompe)."""
            try:
                pg.wait_for_function("(n) => (window.__audio || 0) > n", arg=n0, timeout=timeout)
                return True
            except Exception:
                return False

        def garante_turno():
            """Injeta um turno e espera o áudio — o barge precisa de playback ATIVO."""
            n0 = audio_n()
            injeta()
            return espera_audio(n0)

        resultados = []
        # 1º turno da sessão: sem ele não há áudio para interromper
        if not garante_turno():
            print("  ⚠ o 1º turno não devolveu áudio — tail do servidor:\n" + tail(12))
        for i in range(1, REP + 1):
            # playback ativo? se já parou, injeta um turno novo e espera o áudio
            if not pg.evaluate("() => LX.ativos.length > 0"):
                if not garante_turno():
                    resultados.append({"i": i, "ok": False, "motivo": "sem áudio do servidor", "assinatura": {}})
                    print(f"  [{i}/{REP}] ✖ sem áudio do servidor — tail:\n{tail(6)}")
                    continue
            print(f"  [{i}/{REP}] injetando em {time.strftime('%H:%M:%S')} (playback ativo={pg.evaluate('() => LX.ativos.length')})")
            marco = pg.evaluate("() => window.__ev.length")
            est0 = pg.evaluate("() => ({ estado: LX.estado, ativos: LX.ativos.length, chunks: (LX.obs.stats||{}).playback ? LX.obs.stats.playback.chunks : null })")
            injeta()
            ok = True
            try:
                pg.wait_for_function("() => window.__ev.some(e => e.tipo === 'interrupted')", timeout=15000)
            except Exception:
                ok = False
            corte = None
            if ok:
                corte = pg.evaluate("() => { const t = window.__ev.find(e => e.tipo === 'interrupted'); return t ? Math.round(performance.now() - t.t) : null; }")
            novos = pg.evaluate("(m) => window.__ev.slice(m).map(e => e.tipo)", marco)
            assin = pg.evaluate("(m) => (window.__sp || []).slice(-2)", marco)
            print(f"  [{i}/{REP}] {'✔ barge' if ok else '✖ SEM barge'} · antes={est0} · eventos={novos[:6]}")
            for s in assin:
                print("        motor:", {k: s.get(k) for k in ("type", "barge_in", "barge_falso", "curto", "prob", "rms_dbfs", "fala_ms", "t_decisao_ms", "detalhe") if k in s})
            resultados.append({"i": i, "ok": ok, "antes": est0, "eventos": novos, "assinatura": assin})
            if not ok:
                print(f"        tail do servidor:\n{tail(6)}")
            # deixa assentar antes da próxima (senão a injeção pega o turno anterior)
            pg.wait_for_timeout(2500)
            pg.evaluate("() => { window.__ev = []; window.__sp = []; }")

        b.close()

    print("\n=== JANELA DE PLAYBACK (speaking) — para correlacionar com as injeções:")
    for l in log.read_text().splitlines():
        if "[JANELA]" in l: print("   ", l)
    ok_n = sum(1 for r in resultados if r["ok"])
    print(f"\n=== RESUMO: {ok_n}/{len(resultados)} barges dispararam")
    falhas = [r for r in resultados if not r["ok"]]
    for r in falhas:
        print(f"  falha #{r['i']} · antes={r.get('antes')} · eventos={r.get('eventos')}")
        for s in r.get("assinatura") or []:
            print("        motor:", {k: s.get(k) for k in ("type", "barge_in", "barge_falso", "curto", "prob", "rms_dbfs", "fala_ms", "t_decisao_ms", "detalhe") if k in s})
    if ok_n == 0:
        print("✖ NENHUM barge — não é cauda, é quebrado")
        sys.exit(1)
    print("✔ investigação concluída (não falha por cauda)")
finally:
    try: os.killpg(os.getpgid(proc.pid), 15)
    except Exception: proc.terminate()
PYEOF