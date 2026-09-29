#!/usr/bin/env bash
# E2E do componente único de observabilidade OBS-3 (task #120): os QUATRO painéis
# (gancho de mic, primitiva de log, fábrica do painel) renderizam nas telas com
# áudio/geração e são alimentados por dados VIVOS — mic falso de verdade no
# ditado/STT, espelho da Conversa e wiring do progresso do job de TTS.
#
#   ./tests/obs_ui.sh             # sobe servidor próprio em porta livre e derruba no fim
#
# O provedor de chat é STUBADO por env (`TTS_CHAT_BACKEND=openai` + BASE/MODEL):
# o settings.json do dono não manda no teste, nem se ele estiver em `dsh` (#143).
#
# Paralelo-seguro (#83): arquivos temporários com sufixo por execução.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Serializa com as outras suítes que carregam modelo — sem isto o alvo de
# latência do live_ws.sh dá falso vermelho sob contenção de Metal/CPU (#134).
source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - "$RAIZ" <<'PYEOF'
import base64, json, os, pathlib, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

RAIZ = pathlib.Path(sys.argv[1])
SUF = str(os.getpid())
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

# ─── stub OpenAI-compatível (o settings.json do dono fica intocado, #103) ────
class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

stub_porta = porta_livre()
threading.Thread(target=HTTPServer(("127.0.0.1", stub_porta), Stub).serve_forever, daemon=True).start()

# ─── fala de teste -> WAV 16k, alimenta o mic falso (o medidor precisa de sinal) ─
import numpy as np, soundfile as sf
wav_entrada = None
for w in sorted(pathlib.Path("voices").glob("*.wav")):
    a, sr = sf.read(str(w), dtype="float32")
    if a.ndim > 1: a = a.mean(axis=1)
    if len(a) < sr: continue
    idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
    a16 = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    picoA = float(np.abs(a16).max()) or 1.0
    a16 = np.clip(a16 / picoA * 0.95, -1.0, 1.0)
    wav_entrada = pathlib.Path("/tmp") / f"obs_ui_fala_{SUF}.wav"
    audio16 = np.concatenate([(a16 * 32767), np.zeros(int(3.0 * 16000))])
    sf.write(str(wav_entrada), audio16.astype("<i2"), 16000, subtype="PCM_16")
    break
if not wav_entrada: raise SystemExit("sem voices/*.wav para alimentar o mic falso")

# ─── servidor próprio ────────────────────────────────────────────────────────
porta = porta_livre()
env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
       "TTS_CHAT_MODEL": "stub-obs", "OBS_UI_PORT": str(porta),
       # pinado: env > settings — o dono pode ter deixado `chat_backend: "dsh"` (#143)
       "TTS_CHAT_BACKEND": "openai",
       "ORT_DISABLE_TELEMETRY": "1"}
log = pathlib.Path("/tmp") / f"obs_ui_servidor_{SUF}.log"
_launcher = '''
import os, uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["OBS_UI_PORT"]))
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
    print(f"servidor de teste em {base} (stub :{stub_porta})")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-fake-device-for-media-stream",
                                    "--use-fake-ui-for-media-stream",
                                    f"--use-file-for-fake-audio-capture={wav_entrada}"])
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=base)
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        cons = []; pg.on("console", lambda m: cons.append(m.text)
                         if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        ruins = []
        pg.on("requestfailed", lambda r: ruins.append((r.url[:110], r.failure)))
        pg.on("response", lambda r: ruins.append((r.url[:110], r.status)) if r.status >= 400 else None)
        pg.goto(base, wait_until="networkidle")

        # ─── 1) os quatro painéis existem, na tela certa e com as peças certas ──
        # medidor de mic SÓ onde o hook de captura alimenta (stt/vozes); conversa e
        # gerar declaram no próprio painel por que não têm medidor.
        telas = [("gerar", "obsTts", False), ("vozes", "obsVozes", True),
                 ("conversa", "obsCv", False), ("tradutor", "obsStt", True)]
        for tela, mount, temMedidor in telas:
            pg.evaluate("(t) => showView(t)", tela)
            pg.wait_for_timeout(120)
            info = pg.evaluate("""(m) => {
                const r = document.getElementById(m);
                if (!r) return { erro: 'mount ausente' };
                const vis = !!r.offsetParent;
                const box = r.querySelector('.lx-obs');
                return { vis, temCaixa: !!box, chip: !!r.querySelector('.lx-chip.big'),
                         log: !!r.querySelector('.lx-log-mini'), medidor: !!r.querySelector('.lx-meters') };
            }""", mount)
            print(f"  painel {mount} em #{tela}: {info}")
            cobrar(info.get("vis"), f"painel {mount} não renderiza na tela {tela}")
            cobrar(info.get("temCaixa") and info.get("chip") and info.get("log"),
                   f"painel {mount} sem peças (caixa/chip/log): {info}")
            cobrar(bool(info.get("medidor")) == temMedidor,
                   f"medidor do {mount} esperado={temMedidor}, veio={info.get('medidor')}")

        # ─── 2) engine de RAF ÚNICA: parada em ocioso, viva só com barra ────────
        ocioso = pg.evaluate("() => ({ n: OBS.barras.size, raf: OBS.raf })")
        print("  engine em ocioso:", ocioso)
        cobrar(ocioso["n"] == 0 and ocioso["raf"] == 0, f"engine de RAF não está ociosa: {ocioso}")

        # ─── 3) STT/ditado com mic REAL: registro, medidor movendo, log, estado ─
        pg.evaluate("() => showView('tradutor')")
        pg.wait_for_timeout(200)
        # click por JS (o harness às vezes pega o botão em reflow e desiste da ação)
        pg.evaluate("() => document.getElementById('sttRecBtn').click()")
        pg.wait_for_timeout(300)
        lig = pg.evaluate("""() => ({ estado: document.querySelector('#obsStt .lx-chip.big').textContent,
                                      barras: OBS.barras.size, raf: OBS.raf })""")
        print("  STT gravando:", lig)
        cobrar("gravando" in lig["estado"], f"estado do painel STT não vira gravando ({lig['estado']!r})")
        cobrar(lig["barras"] >= 1 and lig["raf"] != 0, f"engine não ligou com o mic do STT: {lig}")

        largura = pg.evaluate("""async () => {
            let max = 0, maxBancada = 0, viuBancada = false;
            // Uma malha = UM requestAnimationFrame por volta do loop. Se algum
            // medidor trouxer RAF própria, cada volta agenda DOIS (é o defeito da
            // #120: duas malhas pintando o mesmo sinal). Compara-se chamadas x
            // voltas do loop — não se assume 60 Hz (headless não é vsync).
            const orig = window.requestAnimationFrame;
            const ol = window.obsLoop;
            let chamadas = 0, voltas = 0;
            window.requestAnimationFrame = function (cb) { chamadas++; return orig.call(window, cb); };
            window.obsLoop = function () { voltas++; return ol.apply(this, arguments); };
            for (let i = 0; i < 40; i++) {
                const b = document.querySelector('#obsStt .lx-meterbar .bar');
                if (b) max = Math.max(max, parseFloat(b.style.width) || 0);
                const mb = document.querySelector('#micMeterBar');
                if (mb) maxBancada = Math.max(maxBancada, parseFloat(mb.style.width) || 0);
                if (OBS.barras.has('bancada:mic')) viuBancada = true;
                await new Promise(r => setTimeout(r, 15));
            }
            window.requestAnimationFrame = orig; window.obsLoop = ol;
            return { max, maxBancada, viuBancada, chamadas, voltas };
        }""")
        print(f"  pico da barra do mic no STT: {largura['max']:.1f}% · bancada: {largura['maxBancada']:.1f}% "
              f"· bancada na malha única: {largura['viuBancada']}")
        print(f"  loop: {largura['voltas']} voltas x {largura['chamadas']} agendamentos de rAF — 1 por volta")
        cobrar(largura["max"] > 2, f"medidor do STT não reage ao sinal (máx {largura['max']:.1f}%)")
        cobrar(largura["viuBancada"], "medidor da bancada não entrou na malha OBS (startMeter com RAF própria)")
        cobrar(largura["maxBancada"] > 2, f"medidor da bancada parou de reagir (máx {largura['maxBancada']:.1f}%)")
        cobrar(largura["voltas"] > 20, f"malha de RAF não está rodando ({largura['voltas']} voltas)")
        cobrar(largura["chamadas"] - largura["voltas"] <= 2,
               f"cada volta agenda mais de um rAF — malha duplicada "
               f"({largura['chamadas']} agendamentos / {largura['voltas']} voltas)")

        pg.evaluate("() => document.getElementById('sttRecBtn').click()")   # encerra a gravação
        pg.wait_for_timeout(800)
        pos = pg.evaluate("""() => ({ estado: document.querySelector('#obsStt .lx-chip.big').textContent,
                                     linhas: document.querySelectorAll('#obsStt .lx-log-mini > div').length })""")
        print("  STT pós-parada:", pos)
        cobrar(pos["linhas"] >= 3, f"log do STT não registrou o ciclo (linhas={pos['linhas']})")
        # transcrever pode carregar o modelo local: espera o estado FINAL
        try:
            pg.wait_for_function(
                "() => /✔|✖/.test(document.querySelector('#obsStt .lx-chip.big').textContent)",
                timeout=240000)
        except Exception:
            pass
        fim = pg.evaluate("""() => ({ estado: document.querySelector('#obsStt .lx-chip.big').textContent,
                                     ultimo: document.querySelector('#obsStt .lx-log-mini').lastElementChild.textContent })""")
        print("  STT final:", fim)
        cobrar("✔" in fim["estado"] or "✖" in fim["estado"],
               f"STT não fechou o ciclo no painel ({fim['estado']!r}) — erro silencioso")
        cobrar("transcri" in fim["ultimo"] or "erro" in fim["ultimo"].lower(),
               f"log do STT não fechou o ciclo: {fim['ultimo']!r}")

        # ─── 4) Conversa: estado e cvStatus espelhados no painel ────────────────
        pg.evaluate("() => showView('conversa')")
        pg.evaluate("() => { cvSetMic(true); cvStatus('escuta de teste'); cvSetMic(false); }")
        cv = pg.evaluate("""() => ({ estado: document.querySelector('#obsCv .lx-chip.big').textContent,
                                     log: [...document.querySelectorAll('#obsCv .lx-log-mini > div')].map(d => d.textContent) })""")
        print("  painel da Conversa:", cv)
        cobrar(cv["estado"] == "desligado", f"estado da Conversa não reflecte cvSetMic ({cv['estado']!r})")
        cobrar(any("escuta de teste" in l for l in cv["log"]), "cvStatus não foi espelhado no log da Conversa")

        # ─── 5) Gerar/job de TTS: wiring REAL do streamJob (sem carregar Metal) ─
        pg.evaluate("() => showView('gerar')")
        res = pg.evaluate("""async () => {
            const orig = window.pollPieces;
            window.pollPieces = async (id, meu, opts) => {
                for (let i = 1; i <= 3; i++) { opts.onProgress({ status: 'running', pieces: 3, progress: { current: i, total: 3 } }, i); await new Promise(r => setTimeout(r, 30)); }
                return { pieces: 3, status: 'done' };
            };
            const ler = () => { const r = document.getElementById('obsTts');
                return { estado: r.querySelector('.lx-chip.big').textContent,
                         chips: [...r.querySelectorAll('.lx-chips .lx-chip')].map(c => c.textContent),
                         log: [...r.querySelectorAll('.lx-log-mini > div')].map(d => d.textContent) }; };
            try { await streamJob('deadbeef1234', document.getElementById('ttsStatus')); }
            finally { window.pollPieces = orig; }
            const ok = ler();
            // caminho de ERRO: o painel não pode terminar dizendo "running" nem
            // gardar o troféu de "concluído" do job anterior (#142)
            window.pollPieces = async (id, meu, opts) => {
                opts.onProgress({ status: 'running', pieces: 2, progress: { current: 1, total: 2 } }, 1);
                throw new Error('stub quebrou');
            };
            try { await streamJob('cafebabe9999', document.getElementById('ttsStatus')); }
            catch (e) { /* esperado */ }
            finally { window.pollPieces = orig; }
            return { ok, err: ler() };
        }""")
        ok, err = res["ok"], res["err"]
        print("  painel do job (ok):", {"estado": ok["estado"], "chips": ok["chips"]})
        print("  painel do job (erro):", {"estado": err["estado"], "chips": err["chips"]})
        cobrar("concluído" in ok["estado"], f"estado do painel do job não fecha em concluído ({ok['estado']!r})")
        cobrar(any(c.startswith("trecho 3/3") for c in ok["chips"]),
               f"contador de trecho do job não acompanhou o progresso ({ok['chips']})")
        # o chip `status` NÃO pode ficar no último poll ("running") contradizendo o
        # estado grande — foi o achado #142
        cobrar(not any(c.startswith("status running") for c in ok["chips"]),
               f"chip de status ficou 'running' com o job concluído: {ok['chips']}")
        cobrar(any(c.startswith("status done") for c in ok["chips"]),
               f"desfecho do job não chegou ao chip de status: {ok['chips']}")
        cobrar(any("concluído" in l for l in ok["log"]), f"log do job sem linha de conclusão: {ok['log']}")
        # e no erro: estado de erro, status de erro, sem herdar o "concluído" de antes
        cobrar("erro" in err["estado"], f"painel não foi para erro ({err['estado']!r})")
        cobrar(any(c.startswith("status error") for c in err["chips"]),
               f"chip de status não virou erro: {err['chips']}")
        cobrar(any("falhou" in l for l in err["log"]), f"log sem a falha: {err['log']}")

        # ─── 6) malha sem vazamento: nada registrado e o loop PAROU ───────────
        # O medidor da bancada entra e sai da MESMA malha; se algo ficar
        # registrado, o app pinta um RAF para sempre com o mic fechado.
        pg.wait_for_timeout(600)
        vazio = pg.evaluate("() => ({ n: OBS.barras.size, raf: OBS.raf, bancada: OBS.barras.has('bancada:mic') })")
        print("  engine depois do ciclo:", vazio)
        cobrar(not vazio["bancada"], "medidor da bancada ficou registrado com o mic fechado")
        cobrar(vazio["n"] == 0 and vazio["raf"] == 0,
               f"malha de RAF vazou depois do ciclo (n={vazio['n']}, raf={vazio['raf']})")

        # ─── 6b) ramo TRANSPARENTE (hp=0/gain=1): tap em ctx PRÓPRIO ─────────
        # O default do app é hp>0 (ramo filtrado), então o outro caminho ficava
        # sem cobertura — foi o que o gate apontou. Aqui: ligar, medir, e conferir
        # que o tap saiu num AudioContext SEPARADO (a gravação segue no stream cru)
        # e que nada vaza depois.
        pg.evaluate("() => showView('tradutor')")
        pg.evaluate("() => { const d = document.querySelector('#view-tradutor details'); if (d) d.open = true; }")
        hp_antes, ganho_antes = pg.evaluate("() => [micFx.hp, micFx.gain]")
        pg.evaluate("() => { micFx.hp = 0; micFx.gain = 1; }")
        pg.evaluate("() => document.getElementById('sttRecBtn').click()")
        pg.wait_for_timeout(300)
        tr = pg.evaluate("""async () => {
            let max = 0;
            for (let i = 0; i < 30; i++) {
                const b = document.querySelector('#obsStt .lx-meterbar .bar');
                if (b) max = Math.max(max, parseFloat(b.style.width) || 0);
                await new Promise(r => setTimeout(r, 15));
            }
            return { max, estado: document.querySelector('#obsStt .lx-chip.big').textContent,
                     ctxProprio: !!ctxObsMed, ctxRate: ctxObsMed ? ctxObsMed.sampleRate : 0,
                     barras: OBS.barras.size };
        }""")
        print("  ramo transparente:", tr)
        cobrar("gravando" in tr["estado"], f"STT não grava no modo transparente ({tr['estado']!r})")
        cobrar(tr["max"] > 2, f"medidor não reage no modo transparente (máx {tr['max']:.1f}%)")
        cobrar(tr["ctxProprio"], "tap do modo transparente não usou o ctx próprio")
        cobrar(tr["barras"] >= 1, "barra do painel não entrou na malha no modo transparente")
        pg.evaluate("() => document.getElementById('sttRecBtn').click()")
        pg.wait_for_timeout(900)
        pg.evaluate("(v) => { [micFx.hp, micFx.gain] = v; }", [hp_antes, ganho_antes])
        pg.wait_for_timeout(300)
        cobrar(pg.evaluate("() => OBS.barras.has('obsStt:in')") is False,
               "barra do painel ficou registrada depois de fechar no modo transparente")

        # ─── 7) console limpo ─────────────────────────────────────────────────
        # "Failed to load resource" é ruído de RECURSO e é pré-existente ao OBS-3:
        # probes do mic-router em :7861-7865 (serviços opcionais), 404 da voz-probe
        # do xss e os previews de áudio das vozes (blob abortado no load). O que
        # este teste cobra é erro de JS (pageerror / console.error de script).
        print(f"  pageerror: {erros} · console.error de script: {cons}")
        print(f"  requisições falhas/4xx (pré-existentes): {sorted(set(ruins))}")
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
print("\n✔ OK — painéis OBS-3 renderizam nas 4 telas e respondem a dados vivos (mic real, Conversa, job).")
PYEOF