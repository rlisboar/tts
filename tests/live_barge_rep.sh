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
#   MODO=vao REP=20 ./tests/live_barge_rep.sh     # injeta no VÃO (ver abaixo)
#   AMP=0.2 MODO=vao REP=20 ./tests/live_barge_rep.sh   # vão + fala BAIXA (#216)
#
# MODO=vao (#216): em vez de injetar com o playback ATIVO, espera o `turn_complete`
# e injeta `VAO_MS` (900 ms por padrão) depois, quando a janela de playback
# dimensionada pela fila já expirou. É o cenário do #167; o modo padrão satura
# perto de 20/20 e não distingue configurações do motor.
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
# CORTE (#225): por iteração sai também a FILA (ms de áudio que o cliente ainda vai
# tocar no instante do onset) e se o playback foi CORTADO, com latência. O corte é
# do CLIENTE (`lxCancelaPlayback`) e chega por dois caminhos: `interrupted` (barge)
# ou `speech_start` com fila (#225) — o servidor não sabe o que o cliente bufferiza,
# então o cliente é a única ponta que pode decidir. O resumo `=== CORTE (#225)` conta
# os casos com fila que seguiram tocando por cima (o defeito).
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
import base64, json, math, os, pathlib, socket, statistics, subprocess, sys, threading, time, urllib.request
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

def _dbfs(x: float) -> float:
    return 20.0 * math.log10(x) if x > 1e-9 else -120.0

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
    # AMP (#216): a MESMA fala, mais baixa. É a única forma de medir a alavanca que
    # baixa o LIMIAR no vão (`eco_so_tocando`): com a injeção em escala cheia o
    # estímulo passa do limiar em qualquer configuração e a alavanca não aparece.
    # A fala cheia continua sendo usada pelo `garante_turno` (o STT precisa dela).
    amp = float(os.environ.get("AMP", "1") or 1)
    if amp != 1.0:
        pcm_b = np.clip(np.frombuffer(pcm, dtype="<i2").astype(np.float32) * amp,
                        -32768, 32767).astype("<i2").tobytes()
        fala_baixa = base64.b64encode(pcm_b).decode()
        v = np.frombuffer(pcm_b, dtype="<i2").astype(np.float32) / 32768.0
        voz = v[:int(16000 * (len(v) / 16000 - 3))]     # tira a cauda de 3 s
        print(f"  AMP={amp}: injeção em {_dbfs(float(np.sqrt(np.mean(voz ** 2)))):.1f} dBFS RMS")
    else:
        fala_baixa = fala
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
    motor_final = {}
    contagem_final = {}
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
            window.__ev = []; window.__sp = []; window.__audio = 0; window.__motor = {};
            window.__barges = 0; window.__interr = 0;
            // #225: mede o CORTE do playback no onset — é o que faz o assistente
            // calar quando o usuário fala. A porta única é `lxCancelaPlayback`
            // (barge via `interrupted` OU corte no `speech_start`, #225); sem o
            // wrapper, "o áudio seguiu tocando por cima" não tem número.
            window.__cortes = [];
            const _canc = window.lxCancelaPlayback;
            window.lxCancelaPlayback = function () {
                try {
                    const c = LX.ctxPlay;
                    window.__cortes.push({ t: performance.now(), ativos: LX.ativos.length,
                        restante_ms: c ? Math.max(0, Math.round((LX.proximo - c.currentTime) * 1000)) : 0 });
                } catch (e) {}
                return _canc.apply(this, arguments);
            };
            window.__mk = () => {
                if (!LX.ws) { setTimeout(window.__mk, 50); return; }
                LX.ws.addEventListener("message", ev => {
                    if (typeof ev.data !== "string") { window.__audio++; window.__ev.push({ tipo: "<audio>", t: performance.now() }); return; }
                    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
                    window.__ev.push({ tipo: m.type, t: performance.now() });
                    if (["speech_start", "speech_end", "barge_in"].includes(m.type)) window.__sp.push(m);
                    // #216: no sentido do eco a contagem CUMULATIVA de `barge_in` é a
                    // medida de "o motor se interrompe sozinho?" — ela não depende de
                    // a injeção pegar a janela aberta (o ✔ por iteração depende).
                    if (m.type === "barge_in") window.__barges++;
                    if (m.type === "interrupted") window.__interr++;
                    // #216: contadores do MOTOR (cumulativos) — diagnóstico
                    if (m.type === "stats" && m.motor) window.__motor = m.motor;
                });
            };
            window.__mk();
        }""")
        pg.locator("#lxLigar").click()
        pg.wait_for_function("() => LX.ws && LX.ws.readyState === 1", timeout=20000)

        def injeta(b64: str | None = None):
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
            }""", b64 or fala)

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
        # #216: MODO=vao — injeta `VAO_MS` DEPOIS de o turno do assistente terminar
        # (`turn_complete`), com o cliente ainda com áudio na fila. É o VÃO de
        # geração: a janela de playback dimensionada pela FILA já expirou (900 ms
        # após o último envio) e é o único cenário em que as alavancas do #167 têm o
        # que entregar. No modo padrão o harness injeta com playback ATIVO, o que
        # satura perto do teto e não distingue as configurações.
        MODO = (os.environ.get("MODO") or "").strip().lower()
        VAO_MS = int(os.environ.get("VAO_MS", "900"))
        for i in range(1, REP + 1):
            if MODO == "vao":
                if not garante_turno():
                    resultados.append({"i": i, "ok": False, "motivo": "sem áudio do servidor", "assinatura": {}})
                    print(f"  [{i}/{REP}] ✖ sem áudio do servidor — tail:\n{tail(6)}")
                    continue
                pg.evaluate("() => { window.__ev = []; }")
                try:
                    pg.wait_for_function(
                        "() => window.__ev.some(e => e.tipo === 'turn_complete' || e.tipo === 'interrupted')",
                        timeout=30000)
                except Exception:
                    pass
                pg.wait_for_timeout(VAO_MS)
                print(f"  [{i}/{REP}] MODO=vao: {VAO_MS} ms depois do fim do turno ·"
                      f" playback ativo={pg.evaluate('() => LX.ativos.length')}")
            # playback ativo? se já parou, injeta um turno novo e espera o áudio
            elif not pg.evaluate("() => LX.ativos.length > 0"):
                if not garante_turno():
                    resultados.append({"i": i, "ok": False, "motivo": "sem áudio do servidor", "assinatura": {}})
                    print(f"  [{i}/{REP}] ✖ sem áudio do servidor — tail:\n{tail(6)}")
                    continue
            print(f"  [{i}/{REP}] injetando em {time.strftime('%H:%M:%S')} (playback ativo={pg.evaluate('() => LX.ativos.length')})")
            marco = pg.evaluate("() => window.__ev.length")
            est0 = pg.evaluate("() => ({ estado: LX.estado, ativos: LX.ativos.length, chunks: (LX.obs.stats||{}).playback ? LX.obs.stats.playback.chunks : null })")
            # #225: FILA = o que o cliente ainda vai tocar AGORA (segundos de áudio
            # agendado). É a medida do "fala por cima": se o onset não corta, o
            # assistente segue audível por estes ms enquanto o usuário fala.
            fila_ms = pg.evaluate("() => { const c = LX.ctxPlay; return c ? Math.max(0, Math.round((LX.proximo - c.currentTime) * 1000)) : 0; }")
            cortes0 = pg.evaluate("() => (window.__cortes || []).length")
            pg.evaluate("() => { window.__inj = performance.now(); }")
            # a injeção de MEDIÇÃO usa a fala em AMP (o `garante_turno` usa a cheia)
            injeta(fala_baixa)
            ok = True
            try:
                pg.wait_for_function("() => window.__ev.some(e => e.tipo === 'interrupted')", timeout=15000)
            except Exception:
                ok = False
            corte = None
            if ok:
                corte = pg.evaluate("() => { const t = window.__ev.find(e => e.tipo === 'interrupted'); return t ? Math.round(performance.now() - t.t) : null; }")
            # #225: o corte do playback pode vir do `interrupted` (barge) OU do
            # `speech_start` (onset fora da janela). O que importa é: cortou?
            novo_corte = pg.evaluate("(n) => { const c = (window.__cortes || []).slice(n); return c.length ? c[0] : null; }", cortes0)
            corte_ms = (round(novo_corte["t"] - pg.evaluate("() => window.__inj"))
                        if novo_corte else None)
            novos = pg.evaluate("(m) => window.__ev.slice(m).map(e => e.tipo)", marco)
            assin = pg.evaluate("(m) => (window.__sp || []).slice(-2)", marco)
            motor_agora = pg.evaluate("() => window.__motor || {}")
            print(f"  [{i}/{REP}] {'✔ barge' if ok else '✖ SEM barge'} · antes={est0} · eventos={novos[:6]}"
                  f" · fila={fila_ms}ms · " + (f"cortou em {corte_ms}ms" if corte_ms is not None
                                               else ("✖ SEM CORTE (tocou por cima)" if fila_ms >= 400 else "sem fila"))
                  + f" · motor: barge_ativos={motor_agora.get('barge_ativos')}"
                  f" barge_falsos={motor_agora.get('barge_falsos')}"
                  f" limiar={motor_agora.get('limiar_dbfs')}")
            for s in assin:
                print("        motor:", {k: s.get(k) for k in ("type", "barge_in", "barge_falso", "curto", "prob", "rms_dbfs", "fala_ms", "t_decisao_ms", "detalhe") if k in s})
            resultados.append({"i": i, "ok": ok, "antes": est0, "eventos": novos, "assinatura": assin,
                               "fila_ms": fila_ms, "corte_ms": corte_ms})
            if not ok:
                print(f"        tail do servidor:\n{tail(6)}")
            # deixa assentar antes da próxima (senão a injeção pega o turno anterior)
            pg.wait_for_timeout(2500)
            pg.evaluate("() => { window.__ev = []; window.__sp = []; }")

        motor_final = pg.evaluate("() => window.__motor || {}")
        contagem_final = pg.evaluate(
            "() => ({barge_in: window.__barges || 0, interrupted: window.__interr || 0, "
            "audio: window.__audio || 0})")
        b.close()

    print("\n=== JANELA DE PLAYBACK (speaking) — para correlacionar com as injeções:")
    for l in log.read_text().splitlines():
        if "[JANELA]" in l: print("   ", l)
    ok_n = sum(1 for r in resultados if r["ok"])
    print(f"\n=== RESUMO: {ok_n}/{len(resultados)} barges dispararam")
    # #225: o alvo aqui é o CORTE, não o barge. "Com fila" = o cliente tinha áudio
    # para tocar no instante do onset (>=400 ms, acima do ruído de medição): nesses
    # casos o assistente TEM de calar — pelo `interrupted` (barge) ou pelo corte no
    # `speech_start` (onset fora da janela). Sem corte, ele fala por cima.
    com_fila = [r for r in resultados if (r.get("fila_ms") or 0) >= 400]
    cortados = [r for r in com_fila if r.get("corte_ms") is not None]
    lats = sorted(r["corte_ms"] for r in cortados)
    print(f"=== CORTE (#225): com fila>=400ms: {len(com_fila)} · cortou: {len(cortados)}"
          f" · tocando por cima: {len(com_fila) - len(cortados)}"
          + (f" · latência do corte mediana={lats[len(lats) // 2]}ms" if lats else ""))
    falhas = [r for r in resultados if not r["ok"]]
    for r in falhas:
        print(f"  falha #{r['i']} · antes={r.get('antes')} · eventos={r.get('eventos')}")
        for s in r.get("assinatura") or []:
            print("        motor:", {k: s.get(k) for k in ("type", "barge_in", "barge_falso", "curto", "prob", "rms_dbfs", "fala_ms", "t_decisao_ms", "detalhe") if k in s})
    # #216: contadores do MOTOR no fim da rodada. No sentido do eco eles são a
    # medida objetiva do "barge falso": `barge_ativos` conta TODA interrupção que o
    # motor armou (a maioria ali não tem humano falando) e `barge_falsos` as que
    # fecharam sem fala além da janela de confirmação.
    print(f"\n=== MOTOR (cumulativo da rodada): {json.dumps(motor_final, sort_keys=True)}")
    print(f"=== CONTAGEM (cumulativa): {json.dumps(contagem_final, sort_keys=True)}")
    if ok_n == 0:
        print("✖ NENHUM barge — não é cauda, é quebrado")
        sys.exit(1)
    print("✔ investigação concluída (não falha por cauda)")
finally:
    try: os.killpg(os.getpgid(proc.pid), 15)
    except Exception: proc.terminate()
PYEOF