#!/usr/bin/env bash
# E2E da UI Live (task #94) contra um servidor PRÓPRIO com o stub de chat por ENV
# (#103) — assim o settings.json do dono fica intocado. O BACKEND também vai por
# env (`TTS_CHAT_BACKEND=openai`): com o dono em `chat_backend: "dsh"` a suíte
# deixava o stub e falava com o harness (#143).
#
# Exercita o caminho REAL do cliente: mic (dispositivo falso do Chromium, alimentado
# com fala de verdade de voices/*.wav) -> AudioWorklet 16 kHz -> frames de 100 ms ->
# WS -> turno com áudio de volta -> playback -> barge-in com corte medido.
#
#   ./tests/live_ui.sh            # sobe stub+servidor em portas livres e derruba no fim
#
# BARGE: o corte do playback é exigência DURA por padrão. Escapes, para quando se
# está medindo OUTRA coisa:
#   BARGE_ESTRITO=0     volta ao regime tolerante (barge ausente = aviso, não falha)
#   BARGE_SIMULA_SEM=1  força a rodada a se comportar como a que não teve barge —
#                       é o controle que prova os dois lados sem depender da janela
# Vazio/ausente/qualquer outro valor de BARGE_ESTRITO = ESTRITO (`= cmd` não relaxa).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Serializa com as outras suítes que carregam modelo — sem isto o alvo de
# latência do live_ws.sh dá falso vermelho sob contenção de Metal/CPU (#134).
source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - <<'PYEOF'
import base64, json, os, pathlib, signal, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

RAIZ = pathlib.Path.cwd()
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

# ─── stub OpenAI-compatível (SSE) ────────────────────────────────────────────
class Stub(BaseHTTPRequestHandler):
    FRASES = ["Claro, ", "o dia está bonito hoje. ", "Quer que eu conte mais alguma coisa?"]
    def log_message(self, *a): pass
    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if os.environ.get("STUB_FALHA"):        # simula o provedor fora (o 530 do dono)
            self.send_response(500); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"error":{"message":"provedor fora"}}'); return
        if corpo.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for f in self.FRASES:
                self.wfile.write(f"data: {json.dumps({'choices':[{'delta':{'content':f}}]})}\n\n".encode())
                self.wfile.flush(); time.sleep(0.02)
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"choices":[{"message":{"content":"".join(self.FRASES)}}]}).encode())

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

stub_porta = porta_livre()
srv = HTTPServer(("127.0.0.1", stub_porta), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ─── fala de teste -> WAV 16k mono (alimenta o mic falso) ────────────────────
import numpy as np, soundfile as sf
wav_entrada = None
for w in sorted(pathlib.Path("voices").glob("*.wav")):
    a, sr = sf.read(str(w), dtype="float32")
    if a.ndim > 1: a = a.mean(axis=1)
    if len(a) < sr: continue
    idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
    a16 = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    # Normaliza o PICO (sem ganho extra, sem clipping). O nível NÃO é o que faz o
    # barge-in disparar — o motor é invariante de escala aqui, e isso está medido
    # dos dois lados: dispara igual a -17 e a -9 dBFS RMS (audio-ml, no fix do
    # #115) e também sem ganho nenhum, a ~-21 dBFS (eu, com este harness). A
    # normalização fica só para o teste não depender de qual voz de `voices/` caiu
    # na ordenação.
    # Leitura que induzia a erro: o regime ANTIGO (flag de speaking pulsada por
    # chunk) fazia parecer que faltava ~+10 dB — o ganho era sintoma, não causa.
    picoA = float(np.abs(a16).max()) or 1.0
    a16 = np.clip(a16 / picoA * 0.95, -1.0, 1.0)
    wav_entrada = pathlib.Path("/tmp") / "live_ui_fala.wav"
    # SILÊNCIO de 5 s depois da fala, DE PROPÓSITO: o Chromium toca este arquivo em
    # loop, e sem a pausa o VAD nunca fecha — e sem fechar não existe "início de
    # fala" para o motor avaliar como barge-in. Foi isso (não o nível) que fez os
    # primeiros testes não dispararem `interrupted`.
    audio16 = np.concatenate([(np.clip(a16, -1, 1) * 32767), np.zeros(int(5.0 * 16000))])
    sf.write(str(wav_entrada), audio16.astype("<i2"), 16000, subtype="PCM_16")
    break
if not wav_entrada: raise SystemExit("sem voices/*.wav para alimentar o mic falso")

# ─── servidor próprio com TTS_CHAT_* (settings do dono intocado) ─────────────
porta = porta_livre()
env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1", "TTS_CHAT_MODEL": "stub-live",
       # BACKEND PINADO: sem isto a suíte fica refém do settings.json do dono —
       # com `chat_backend: "dsh"` ela deixaria o stub e falaria com o harness (#143).
       # O do LIVE também: ele MANDA sobre `chat_backend_live` e o pin da Conversa
       # não o governa, então o dono com o Live em dsh saía do stub sem querer (#195).
       # O do LIVE é o ÚNICO que respeita o env do chamador, de propósito: é a
       # porta para medir esta tela com o harness (`TTS_CHAT_BACKEND_LIVE=dsh`).
       # Como a rota muda o que se mede, ela vai IMPRESSA abaixo — env exportado
       # não pode mudar a rodada em silêncio.
       "TTS_CHAT_BACKEND": "openai",
       "TTS_CHAT_BACKEND_LIVE": os.environ.get("TTS_CHAT_BACKEND_LIVE") or "openai"}
ROTA_LIVE = env["TTS_CHAT_BACKEND_LIVE"]

def _barge_estrito():
    """Duro por padrão (#167 fechou); `0`/`false`/`no` relaxam. Vazio = estrito."""
    return (os.environ.get("BARGE_ESTRITO") or "").strip().lower() not in ("0", "false", "no", "nao")
BARGE_ESTRITO = _barge_estrito()
log = pathlib.Path("/tmp/live_ui_servidor.log")
# Instrumenta SEM editar arquivo de outro agente: envolve `set_speaking` para
# registrar o nível que vem do payload do TTS e o limiar resultante. É o número
# que falta para saber se o alvo (estímulo >= limiar) é alcançável pelo mic.
env["LIVE_UI_PORT"] = str(porta)
_launcher = '''
import os, sys, time, uvicorn, live_turns, app
_orig = live_turns.TurnEngine.set_speaking
def _patched(self, ligado, nivel_dbfs=None, **kw):
    # `**kw`: o call site do app passou a mandar `duracao_ms` (#167). Sem isto o
    # espelho quebra com TypeError no MEIO do handler do WS (mesma classe do
    # dublê do test_api) — e só quando `TTS_LIVE_PLAYBACK_DURACAO=1`.
    _orig(self, ligado, nivel_dbfs, **kw)
    print(f"[DBG-eco] t={time.time():.2f} ligado={ligado} nivel_payload={nivel_dbfs} "
          f"duracao_ms={kw.get('duracao_ms')} "
          f"limiar={self.limiar_energia_dbfs:.1f}", file=sys.stderr, flush=True)
live_turns.TurnEngine.set_speaking = _patched
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["LIVE_UI_PORT"]))
'''
proc = subprocess.Popen([str(RAIZ / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
base = f"http://127.0.0.1:{porta}"
try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu na subida (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor de teste em {base} (stub :{stub_porta})")
    print(f"  rota do Live: {ROTA_LIVE}" + ("  ← pedida pelo env do chamador" if ROTA_LIVE != "openai" else " (stub)"))

    # ─── UI no navegador, com mic falso ─────────────────────────────────────
    with sf.SoundFile(str(wav_entrada)) as f:
        pcm16 = f.read(dtype="int16").tobytes()
    _PCM_B64 = base64.b64encode(pcm16).decode()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-fake-device-for-media-stream",
                                    "--use-fake-ui-for-media-stream",
                                    f"--use-file-for-fake-audio-capture={wav_entrada}"])
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=base)
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        eventos = []
        # Playwright observa o WS por fora do JS: registrar aqui é imune à corrida
        # que fazia o teste perder o `ready` (o motor pegava, o instrumento não)
        def _guarda(f):
            p = getattr(f, "payload", f)          # a API entrega o frame ou o payload
            if isinstance(p, str):
                eventos.append(p)
            else:
                try: eventos.append(bytes(p).decode())
                except Exception: eventos.append("<bin>")
        pg.on("websocket", lambda w: w.on("framereceived", _guarda))
        pg.goto(base, wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()

        # instrumenta: timestamp de eventos + medição do corte no `interrupted`
        pg.evaluate("""() => {
            window.__ev = []; window.__corte = null;
            const carimba = (nome) => window.__ev.push({ tipo: nome, t: performance.now() });
            window.__hook = () => {
                if (!LX.ws) { setTimeout(window.__hook, 100); return; }
                LX.ws.addEventListener("message", ev => {
                    if (typeof ev.data !== "string") { carimba("<audio>"); return; }
                    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
                    carimba(m.type); window.__ev[window.__ev.length - 1].epoch = Date.now() / 1000;
                    if (m.type === "error") window.__erro = m;
                    if (m.type === "speech_start" || m.type === "speech_end" || m.type === "barge_in") {
                        (window.__sp = window.__sp || []).push(m);
                    }
                    if (m.type === "interrupted") {
                        const t0 = performance.now();
                        const tick = () => { LX.ativos.length === 0 ? (window.__corte = performance.now() - t0)
                                                                    : setTimeout(tick, 1); };
                        tick();
                    }
                });
            };
            window.__hook();
        }""")
        pg.locator("#lxLigar").click()
        try:
            pg.wait_for_function("() => window.__ev.length > 0", timeout=20000)
        except Exception:
            diag = pg.evaluate("""() => ({ estado: LX.estado, status: document.getElementById('lxStatus').textContent,
                                          ws: LX.ws ? LX.ws.readyState : -1, eventos: window.__ev,
                                          temKey: !!KEY })""")
            print("  DIAG:", diag)
            raise
        try:
            pg.wait_for_function("() => !!LX.ctxCap && !!LX.noCap", timeout=15000)
        except Exception:
            pass
        captura = pg.evaluate("() => !!LX.ctxCap && !!LX.noCap")
        print(f"  sessão pronta · captura (worklet+mic): {captura}")
        # conta QUADROS que o worklet entrega (mede o fluxo, não o efeito): o bug
        # relatado ("grava só o 1º trecho e para") é do fluxo, não da transcrição
        pg.evaluate("""() => {
            const orig = lxEnviaBin;
            window.__quadros = 0;
            window.lxEnviaBin = (f) => { window.__quadros++; return orig(f); };
        }""")
        cobrar(captura, "captura não subiu (worklet/mic falso) — o resto do teste não vale")

        # espera um turno COMPLETO (transcrição + áudio) e, se vier, um barge-in
        try:
            pg.wait_for_function("() => window.__ev.some(e => e.tipo === '<audio>')", timeout=120000)
        except Exception:
            pass
        def injeta(segundos=None):
            """Solta a fala de teste em quadros de 100 ms, como o cliente faz.

            `segundos` limita o disparo (usado nas retentativas do barge): um estímulo
            curto tem onset mais limpo e não invade o turno seguinte."""
            pg.evaluate("""async ([pcmB64, seg]) => {
                const bin = atob(pcmB64); const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                const i16 = new Int16Array(bytes.buffer);
                const fim = seg ? Math.min(i16.length, seg * 16000) : i16.length;
                for (let i = 0; i + 1600 <= fim; i += 1600) {
                    if (!LX.ws || LX.ws.readyState !== 1) break;
                    LX.ws.send(i16.slice(i, i + 1600).buffer);
                    await new Promise(r => setTimeout(r, 30));
                }
                if (LX.ws && LX.ws.readyState === 1) LX.ws.send(JSON.stringify({ type: "end_of_speech" }));
            }""", [_PCM_B64, segundos])

        # provoca o barge-in de forma determinística: com áudio do servidor tocando,
        # injeta fala e fecha o turno (o mic falso é loop, dependeria de sorte)
        injeta()
        # Se o servidor ficar MUDO (visto em 1 de ~6 rodadas: `ready` e mais nada,
        # com quadros fluindo), o defeito é do turno, não da injeção — em vez de
        # seguir e falhar 3 asserções depois, avisa e injeta UMA vez de novo.
        try:
            pg.wait_for_function("() => window.__ev.some(e => e.tipo === 'speech_start')", timeout=25000)
        except Exception:
            print("  ⚠ servidor mudo no 1º turno — re-injetando (diag abaixo)")
            print("  servidor (tail):", "".join(open("/tmp/live_ui_servidor.log").readlines()[-6:]) if os.path.exists("/tmp/live_ui_servidor.log") else "-")
            injeta()
        # Barge: injeta fala com áudio do servidor tocando e fecha o turno.
        # Com a janela de playback colada ao TURNO a 1ª tentativa basta; a 2ª
        # continua aqui como rede do servidor mudo, não como desculpa para o barge
        # não vir. O corte do alvo (<50 ms) é exigência DURA — quem quer o regime
        # antigo pede BARGE_ESTRITO=0 (cabeçalho).
        def tentar_barge(timeout=25000):
            injeta()
            try:
                pg.wait_for_function("() => window.__ev.some(e => e.tipo === 'interrupted')", timeout=timeout)
                return True
            except Exception:
                return False

        barge_ok = tentar_barge()
        pg.wait_for_timeout(1500)

        # ─── CONTINUIDADE (bug do dono: para depois do 1º trecho) ────────────
        q1 = pg.evaluate("() => window.__quadros")
        for _ in range(2):
            injeta()
            pg.wait_for_timeout(9000)
        q2 = pg.evaluate("() => window.__quadros")
        # 2ª chance: as injeções da continuidade acabaram de produzir áudio novo, então
        # há janela de playback para interromper (se a 1ª tentativa não pegou)
        if not barge_ok:
            try:
                pg.wait_for_function("() => LX.ativos.length > 0", timeout=20000)
            except Exception:
                pass
            barge_ok = tentar_barge(120000)
            print(f"  barge: {'disparou na 2ª tentativa' if barge_ok else 'NÃO disparou (2 tentativas)'}"
                  " — a rede da 2ª tentativa foi usada")
        starts = len([e for e in (pg.evaluate("() => window.__sp || []") or [])
                      if e.get("type") == "speech_start"])
        print(f"  continuidade: quadros de mic {q1} -> {q2} · speech_start acumulados: {starts}")

        # ─── PAINEL OBS (#119): contadores ligados em dados VIVOS ────────────
        # Medidor visual em si é foto (evidence/119-*.png); o que o teste COBRA
        # é o painel estar RECEBENDO estado: contadores do cliente, stats do
        # servidor e os chips preenchidos. (Amostrar a barra do mic é flaky —
        # o mic falso fica 5 s em silêncio entre voltas do loop.)
        pg.wait_for_timeout(300)
        painel = pg.evaluate("""() => ({
            stats: !!LX.obs.stats,
            cEnv: (document.getElementById('lxCEnv')||{}).textContent,
            rec:  (document.getElementById('lxCRec')||{}).textContent,
            micServ: (document.getElementById('lxMicServ')||{}).textContent,
            motor: (document.getElementById('lxMotor')||{}).textContent,
            ws: (document.getElementById('lxWs')||{}).textContent
        })""")
        print("  painel OBS:", painel)
        cobrar(painel["stats"], "painel sem stats do servidor (LX.obs.stats vazio)")
        cobrar(painel["cEnv"] not in (None, "", "0"), f"contador de frames enviados não reage ({painel['cEnv']!r})")
        cobrar(painel["rec"] not in (None, "", "0"), f"contador de chunks recebidos não reage ({painel['rec']!r})")
        cobrar(painel["micServ"] not in (None, "", "—"), f"mic no servidor não aparece no painel ({painel['micServ']!r})")
        cobrar(painel["motor"] not in (None, "", "—"), f"estado do motor não aparece no painel ({painel['motor']!r})")

        # estado NORMAL da IA (#151): sem fallback o selo mostra o backend em vigor
        # e NÃO ganha a aparência de aviso
        ia_normal = pg.evaluate("""() => ({ txt: (document.getElementById('lxProv')||{}).textContent || '',
            warn: !!(document.getElementById('lxProv').parentElement||{}).classList.contains('warn') })""")
        print("  selo de IA (normal):", ia_normal)
        cobrar(ia_normal["txt"].strip() not in ("", "—"), f"selo de IA vazio no estado normal: {ia_normal['txt']!r}")
        if ROTA_LIVE == "openai":
            cobrar(not ia_normal["warn"], "selo de IA marcado como aviso sem fallback")
        else:
            # fora do stub o selo pode estar marcado por fallback LEGÍTIMO (o dsh
            # não acha o modelo/endpoint do stub e a sessão segue no openai): o que
            # se cobra é o aviso ser VISÍVEL e coerente com o que a sessão usa
            cobrar(ia_normal["warn"] == bool(pg.evaluate("() => LX.obs.dshFallback")),
                   f"selo de IA e fallback do painel discordam: selo={ia_normal!r}")
            print("  (rota dsh: aviso de fallback é esperado, não falha)")

        # ─── CONTRATO `turno_pendente` (#133/#136): contadores no log e no chip ─
        # Determinístico: injeta o evento do servidor no MESMO `lxRecebe` do
        # caminho real (o teste do servidor prova que ele emite estes campos; aqui
        # se prova que o cliente não engole nem inventa).
        # Num ÚNICO task do JS (evento real não interleava no meio): injeta no mesmo
        # `lxRecebe` do caminho real e força o render, em vez de esperar o tick.
        r = pg.evaluate("""() => {
            const enviar = (p) => lxRecebe({ data: JSON.stringify(Object.assign(
                { type: 'turno_pendente', buffer_bytes: 40960, trechos: 1,
                  descartados_ms: 0, barge_in: false, truncado: false }, p)) });
            const anotada = () => { const ls = [...document.querySelectorAll('#lxLog > div')].reverse();
                const d = ls.find(x => x.textContent.includes('fala anotada')); return d ? d.textContent : ''; };
            const chip = () => (document.getElementById('lxTurno') || {}).textContent || '';
            enviar({}); lxObsRender();
            const um = { log: anotada(), chip: chip(), aceso: LX.obs.pendente };
            enviar({ trechos: 3 }); lxObsRender();
            const tres = { log: anotada(), chip: chip() };
            enviar({ trechos: 4, truncado: true, descartados_ms: 1500, barge_in: true }); lxObsRender();
            const teto = { log: anotada(), chip: chip() };
            lxRecebe({ data: JSON.stringify({ type: 'speech_start' }) }); lxObsRender();
            return { um, tres, teto, limpou: !LX.obs.pendente, chipLimpo: chip() };
        }""")
        um, tres, teto = r["um"], r["tres"], r["teto"]
        print(f"  pendente 1 trecho: {um['log']!r} · chip {um['chip']!r}")
        print(f"  pendente 3 trechos: {tres['log']!r} · chip {tres['chip']!r}")
        print(f"  pendente truncado: {teto['log']!r} · chip {teto['chip']!r}")
        cobrar("fala anotada" in um["log"], f"evento turno_pendente não entra no log: {um['log']!r}")
        cobrar(um["aceso"], "turno_pendente não acendeu o flag de pendente")
        cobrar("trechos" not in um["log"], f"1 trecho virou contador (ruído): {um['log']!r}")
        cobrar("3 trechos" in tres["log"], f"contador `trechos` não aparece no log: {tres['log']!r}")
        cobrar("1500 ms" in teto["log"], f"`descartados_ms` não aparece no log: {teto['log']!r}")
        cobrar("teto de 30 s" in teto["log"], f"aviso de truncado sumiu: {teto['log']!r}")
        cobrar("trechos" in teto["chip"], f"chip do turno não mostra os trechos retidos: {teto['chip']!r}")
        # barge-in do pendente continua visível (flag pegajoso do servidor)
        cobrar("playback" in teto["log"], f"marca de barge-in do pendente sumiu: {teto['log']!r}")
        # `speech_start` limpa: o pendente está abrindo, não fica fantasma no chip
        cobrar(r["limpou"], "speech_start não limpou o pendente do cliente")
        cobrar("pendente" not in r["chipLimpo"], f"chip ficou com pendente fantasma: {r['chipLimpo']!r}")

        # ─── latência: o 1º TOKEN do provedor vira MARCA quando domina ──────────
        # O alvo de 1,5 s é do cenário local/stub; com provedor remoto o primeiro
        # token dele é que explica a espera — sem a marca a pessoa caça fantasma.
        def latencia(**campos):
            base = {"type": "latency", "stt_ms": 300, "first_token_ms": 200,
                    "first_chunk_ms": 250, "first_audio_ms": 400, "total_ms": 900}
            base.update(campos)
            pg.evaluate("(m) => lxRecebe({ data: JSON.stringify(m) })", base)
            pg.evaluate("() => lxObsRender()")
            return pg.evaluate("""() => ({ log: document.querySelector('#lxLog').lastElementChild.textContent,
                                         painel: (document.getElementById('lxLat')||{}).textContent || '' })""")
        rapido = latencia()
        lento = latencia(first_token_ms=4200, first_audio_ms=4600, total_ms=5200)
        print(f"  latência (sem provedor): {rapido['painel']!r}")
        print(f"  latência (provedor dominante): {lento['painel']!r}")
        cobrar("provedor" not in rapido["log"], f"marca de provedor apareceu sem necessidade: {rapido['log']!r}")
        cobrar("4200 ms" in lento["log"] and "provedor" in lento["log"],
               f"log de latência não marca o 1º token do provedor: {lento['log']!r}")
        cobrar("provedor" in lento["painel"] and "4200" in lento["painel"],
               f"painel de latência não marca o provedor: {lento['painel']!r}")

        # ─── #192: os dois ajustes de MOMENTO do servidor ───────────────────────
        # 1) `error{busy}` + close 1013 chegam DEPOIS do setup e ANTES do ready: a
        #    tela mostra o motivo e NÃO pinta "WebSocket caiu" (close limpo não é
        #    falha de transporte — `LX.erro` só liga em `onerror`).
        # 2) `error{pipeline}` chega logo DEPOIS do ready com a sessão VIVA: o
        #    "ouvindo" não pode ficar prometendo fala que não vem.
        r192 = pg.evaluate("""() => {
            const estadoTxt = () => document.getElementById('lxEstado').textContent;
            const status = () => document.getElementById('lxStatus').textContent;
            const ia = () => document.getElementById('lxIA').textContent;
            LX.estado = 'conectando'; lxEstado('conectando', 'abrindo sessão…');
            lxRecebe({ data: JSON.stringify({ type: 'error', code: 'busy', message: 'teto de sessões' }) });
            const a = { estado: estadoTxt(), status: status(), ia: ia() };
            if (LX.ws) LX.ws.onclose({ code: 1013 });      // o servidor fecha logo atrás
            const b = { estado: estadoTxt(), status: status() };
            LX.estado = 'conectando'; lxEstado('conectando', 'abrindo sessão…');
            lxRecebe({ data: JSON.stringify({ type: 'ready' }) });
            const c = { estado: estadoTxt() };
            lxRecebe({ data: JSON.stringify({ type: 'error', code: 'pipeline', message: 'pipeline não subiu' }) });
            const d = { estado: estadoTxt(), ia: ia() };
            return { a, b, c, d };
        }""")
        print(f"  error{{busy}} antes do ready → ia={r192['a']['ia'][:46]!r} estado={r192['a']['estado']!r}"
              f" · depois do close 1013: estado={r192['b']['estado']!r} status={r192['b']['status']!r}")
        print(f"  ready → {r192['c']['estado']!r} · error{{pipeline}} depois dele →"
              f" estado={r192['d']['estado']!r} ia={r192['d']['ia'][:46]!r}")
        cobrar("teto de sessões" in r192["a"]["ia"], f"motivo do busy não aparece na coluna IA: {r192['a']['ia']!r}")
        cobrar("conectando" not in r192["a"]["estado"], f"erro antes do ready deixou a tela em conectando: {r192['a']['estado']!r}")
        cobrar("caiu" not in r192["b"]["status"], f"close 1013 limpo virou 'WebSocket caiu': {r192['b']['status']!r}")
        cobrar("ouvindo" in r192["c"]["estado"], f"ready não abriu a sessão: {r192['c']['estado']!r}")
        cobrar("pipeline não subiu" in r192["d"]["ia"], f"motivo do pipeline não aparece na coluna IA: {r192['d']['ia']!r}")
        cobrar("ouvindo" not in r192["d"]["estado"],
               f"com o pipeline morto a tela segue prometendo fala (estado {r192['d']['estado']!r})")

        ev = [((json.loads(x).get("type") if x.startswith("{") else x) if x != "<bin>" else "<audio>") for x in eventos]
        for e in (pg.evaluate("() => window.__sp || []") or []):
            print("  evento VAD:", {k: e.get(k) for k in ("type", "barge_in", "prob", "rms_dbfs", "fala_ms", "curto", "barge_falso", "detalhe", "t_decisao_ms")})
        erro_ev = pg.evaluate("() => window.__erro || null")
        if erro_ev: print("  erro do servidor:", erro_ev)
        corte = pg.evaluate("() => window.__corte")
        estado = pg.inner_text("#lxEstado")
        user = pg.inner_text("#lxUser"); ia = pg.inner_text("#lxIA")
        audio_evs = ev.count("<audio>")
        print(f"  eventos: {ev[:14]}")
        print(f"  transcrição você={user[:40]!r} ia={ia[:40]!r} · quadros de áudio: {audio_evs}")
        print(f"  corte no interrupted: {corte if corte is None else round(corte, 2)} ms · estado final: {estado!r}")

        cobrar("ready" in ev, "não veio `ready`")
        cobrar(q1 > 0, f"nenhum quadro de mic contado ({q1}) — o contador não pegou o fluxo")
        cobrar(q2 > q1 + 50, f"CAPTURA PAROU depois do 1º turno (quadros {q1} -> {q2})")
        cobrar(starts >= 3, f"esperava >=3 turnos seguidos, vieram {starts} speech_start")
        cobrar(bool(user.strip()), "não veio transcrição do usuário")
        cobrar(bool(ia.strip()), "não veio texto do assistente")
        cobrar(audio_evs > 0, "nenhum áudio do servidor")
        # A janela de barge colada ao TURNO (com o eco valendo só como referência
        # enquanto há áudio tocando) faz o onset no vão voltar a ser interrupção:
        # o corte é exigência DURA por padrão, e `BARGE_ESTRITO=0` é o escape.
        if os.environ.get("BARGE_SIMULA_SEM"):
            # controle do MODO: força a rodada a se comportar como a que não teve
            # barge (sem depender da janela) — é o que prova que o `=0` AINDA
            # tolera e que o estrito REPROVA a MESMA rodada. O corte real é
            # IMPRESSO antes de sumir, senão o par `=0`/`=1` prova só que o
            # simulador simulou.
            print(f"  [controle] rodada simulada SEM barge (BARGE_SIMULA_SEM=1;"
                  f" corte real da rodada={corte if corte is None else round(corte, 2)} ms)")
            corte = None
        if corte is not None:
            cobrar(corte < 50, f"corte do playback demorou {corte:.1f} ms (alvo <50 ms)")
        elif BARGE_ESTRITO:
            cobrar(False,
                   "barge-in não aconteceu nem com fala injetada — sem medição do alvo <50 ms")
        else:
            print("  ⚠ sem barge nesta rodada, tolerado por BARGE_ESTRITO=0"
                  " (o corte não foi medido)")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        janelas = [l for l in log.read_text().splitlines() if "DBG-eco" in l]
        for l in janelas: print("  " + l)
        for e in (pg.evaluate("() => window.__ev") or []):
            if e["tipo"] in ("speech_start", "speech_end") and e.get("epoch"):
                print(f"  VAD {e['tipo']} epoch={e['epoch']:.2f}")
        try: pg.evaluate("() => lxDesligar()")
        except Exception: pass
        b.close()
finally:
    # Mata o GRUPO inteiro, não só o líder: o servidor sobe com `start_new_session`
    # (grupo próprio) e o uvicorn tem filhos — sem isso sobra servidor de teste vivo
    # segurando porta (e o modelo carregado), o que atrapalha rodadas seguintes.
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try: proc.terminate()
        except Exception: pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass

print()
if falhas:
    print("✖ FALHOU")
    for f in falhas: print("  ·", f)
    sys.exit(1)
print("✔ OK — turno real pela UI: mic->worklet->WS->áudio de volta, transcrição e corte do barge-in medido"
      + ("" if ROTA_LIVE == "openai" else f" [rota do Live: {ROTA_LIVE}, não o stub]"))
PYEOF