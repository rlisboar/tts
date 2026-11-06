#!/usr/bin/env bash
# Pausa e retomada do playback do Live (#881, corte rápido S3) — a metade do
# CLIENTE. Prova, por EFEITO e com o app de pé:
#
#   · `barge_pausa` PARA o som (RMS da saída ~0) sem descartar nada: a fila
#     (`LX.ativos`/`LX.proximo`) fica intacta e o relógio do contexto congela;
#   · trecho que chega DURANTE a pausa entra na fila atrás dos outros;
#   · `barge_retoma` continua do ponto: o tempo de ÁUDIO tocado (relógio do
#     contexto, que só anda tocando) bate com a soma dos trechos — nem repete
#     (sobraria), nem pula (faltaria) — enquanto o tempo de PAREDE soma a pausa;
#   · `interrupted` com o playback pausado corta E solta o contexto: o próximo
#     turno toca (não nasce mudo);
#   · `interrupted` com `motivo:"continuacao"` (#877) NÃO é corte: a fila fica, o
#     contexto pausado volta e a tela não anuncia "interrompi" — o MESMO frame
#     sem `motivo` continua cortando (contraprova);
#   · sem decisão do motor, o teto local (3 s) continua sozinho;
#   · sem `ctxPlay` o episódio de medida não fica órfão (o próximo nasce com `t0`
#     novo, não herda o velho);
#   · `window.lxCortes` expõe t_duck, t_pausa e t_corte (ms desde a fala) para a
#     bancada, com o desfecho de cada episódio.
#
# O motor ainda não emite `barge_pausa` (contrato no card): o teste injeta os
# frames no MESMO `lxRecebe` que o WebSocket usa — precedente do
# `live_interrupcao_ui.sh`.
#
# Estado próprio (#541): todos os caminhos de estado vão para tmp; o
# settings.json do dono não é aberto.
#
#   ./tests/live_pausa_ui.sh
#
# MORDIDA: trocar o `ctx.suspend()` de `lxPausaPlayback` por `lxCancelaPlayback()`
# derruba "fila intacta" e "tempo tocado = soma dos trechos"; tirar o `resume` do
# `lxCancelaPlayback` derruba "próximo turno toca depois do corte"; tirar o
# `if (m.motivo === "continuacao")` derruba a fila preservada, o estado e o
# desfecho do episódio de continuação.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - <<'PYEOF'
import json, os, pathlib, shutil, signal, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

falhas = []
def cobrar(c, m):
    print(("  ✔ " if c else "  ✖ ") + m)
    if not c: falhas.append(m)

class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

stub_porta = porta_livre()
threading.Thread(target=HTTPServer(("127.0.0.1", stub_porta), Stub).serve_forever, daemon=True).start()

for _velho in pathlib.Path("/tmp").glob("live_pausa_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 7200:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass
porta = porta_livre()
DIR_TMP = pathlib.Path("/tmp") / f"live_pausa_{porta}"
for d in ("voices", "outputs"): (DIR_TMP / d).mkdir(parents=True, exist_ok=True)
(DIR_TMP / "settings.json").write_text("{}")
env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
       "TTS_SETTINGS_PATH": str(DIR_TMP / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR_TMP / "apikeys.json"),
       "TTS_LEGACY_APIKEY_PATH": str(DIR_TMP / "apikey"),
       "TTS_VOICES_DIR": str(DIR_TMP / "voices"),
       "TTS_OUTPUTS_DIR": str(DIR_TMP / "outputs"),
       "TTS_SPEAKER_PATH": str(DIR_TMP / "speaker.json"),
       "TTS_JOBS_HIST_PATH": str(DIR_TMP / "jobs-history.json"),
       "TTS_CHAT_MODEL": "stub-pausa", "TTS_CHAT_BACKEND": "openai",
       "TTS_CHAT_BACKEND_LIVE": "openai", "ORT_DISABLE_TELEMETRY": "1"}
log = DIR_TMP / "servidor.log"
proc = subprocess.Popen([str(pathlib.Path.cwd() / ".venv-mlx" / "bin" / "python"), "-c",
                         f"import uvicorn, app; uvicorn.run(app.app, host='127.0.0.1', port={porta})"],
                        env=env, stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
base = f"http://127.0.0.1:{porta}"
try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu na subida (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor de teste em {base}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-fake-device-for-media-stream",
                                    "--use-fake-ui-for-media-stream",
                                    "--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=base)
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(base, wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.wait_for_function("() => typeof LX === 'object'", timeout=20000)
        pg.locator("#lxLigar").click()
        pg.wait_for_function("() => !!LX.ctxPlay && !!LX.duck", timeout=25000)
        # o mic falso é um bipe e abriria turnos no meio da medição
        pg.evaluate("""() => {
            try { LX.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
            window.lxEnviaBin = () => {};
            LX.ctxPlay.resume();
            LX.interrupcao = 'inteligente';
        }""")
        pg.wait_for_timeout(1200)
        pg.evaluate("() => { lxCancelaPlayback(); LX.cortes.length = 0; LX.corte = null; }")

        # utilidades no navegador: tom e RMS da SAÍDA (o que se ouve)
        pg.evaluate("""() => {
            window.__tom = (s) => { const n = Math.round(24000 * s), i = new Int16Array(n);
                for (let k = 0; k < n; k++) i[k] = Math.round(Math.sin(2 * Math.PI * 440 * k / 24000) * 3200);
                return i; };
            window.__rms = () => { const a = LX.anOut; if (!a) return -1;
                const b = new Float32Array(a.fftSize); a.getFloatTimeDomainData(b);
                let s = 0; for (const v of b) s += v * v; return Math.sqrt(s / b.length); };
            window.__ev = (o) => lxRecebe({ data: JSON.stringify(o) });
            window.__dorme = (ms) => new Promise(r => setTimeout(r, ms));
        }""")

        # ─── 1. pausa → trecho novo na pausa → retoma: nada repete nem pula ───
        r = pg.evaluate("""async () => {
            const c = LX.ctxPlay, o = {};
            lxToca(__tom(1.2));
            const tc0 = c.currentTime, tw0 = performance.now();
            await __dorme(400);
            o.rms_tocando = __rms();
            __ev({ type: 'speech_start' });              // t0 do episódio (há fila)
            __ev({ type: 'barge_suspeito' });
            await __dorme(120);
            __ev({ type: 'barge_pausa', turn: 1 });
            await __dorme(150);
            o.estado_ctx = c.state;
            o.ativos_pausa = LX.ativos.length;
            o.proximo_pausa = LX.proximo;
            const tcP = c.currentTime;
            lxToca(__tom(0.5));                           // chegou durante a pausa
            o.ativos_com_novo = LX.ativos.length;
            await __dorme(600);
            o.rms_pausado = __rms();
            o.medidor_pausado = lxLe(LX.pausado ? null : LX.anOut, 'teste').rms;
            o.relogio_andou_ms = (c.currentTime - tcP) * 1000;
            o.pausa_ms = 750;
            // sem trecho na fila (a mordida) não há fim a esperar: o prazo fecha
            const ult = LX.ativos[LX.ativos.length - 1];
            const fim = Promise.race([__dorme(5000), new Promise(res => {
                if (!ult) return res();
                const ant = ult.onended;
                ult.onended = (e) => { ant && ant(e); res(); }; })]);
            __ev({ type: 'barge_retoma', turn: 1, tipo: 'backchannel' });
            await __dorme(200);
            o.estado_ctx_retoma = c.state;
            o.rms_retomado = __rms();
            await fim;
            o.tocado_ms = (c.currentTime - tc0) * 1000;   // relógio só anda tocando
            o.parede_ms = performance.now() - tw0;
            o.corte = LX.cortes[LX.cortes.length - 1] || null;
            return o;
        }""")
        print("  1:", json.dumps(r))
        cobrar(r["rms_tocando"] > 0.02, f"tom tocando antes da pausa (rms {r['rms_tocando']:.3f})")
        cobrar(r["estado_ctx"] == "suspended", f"barge_pausa suspende o contexto ({r['estado_ctx']})")
        cobrar(r["ativos_pausa"] == 1, f"fila intacta na pausa ({r['ativos_pausa']} trecho)")
        cobrar(r["ativos_com_novo"] == 2, f"trecho chegado na pausa entra na fila ({r['ativos_com_novo']})")
        # o analisador de um contexto SUSPENSO congela no último quadro (mede
        # ~0,01, o tom já abaixado) — a prova de que nada sai é o relógio do
        # contexto parado: sem render, não há amostra no alto-falante.
        cobrar(abs(r["relogio_andou_ms"]) < 5,
               f"relógio do áudio congelado na pausa = nada renderiza ({r['relogio_andou_ms']:.1f} ms)")
        cobrar(r["medidor_pausado"] == 0, f"medidor de saída do painel zera na pausa ({r['medidor_pausado']})")
        cobrar(r["estado_ctx_retoma"] == "running", f"barge_retoma solta o contexto ({r['estado_ctx_retoma']})")
        cobrar(r["rms_retomado"] > 0.02, f"som de volta na retomada (rms {r['rms_retomado']:.3f})")
        # 1,7 s de áudio na fila; +20 ms do agendamento inicial
        cobrar(1650 <= r["tocado_ms"] <= 1800,
               f"tempo TOCADO = soma dos trechos, nem repete nem pula ({r['tocado_ms']:.0f} ms ≈ 1700)")
        cobrar(r["parede_ms"] >= r["tocado_ms"] + 600,
               f"tempo de parede soma a pausa ({r['parede_ms']:.0f} ms)")
        c = r["corte"] or {}
        cobrar(c.get("desfecho") == "retoma" and c.get("t0_origem") == "speech_start",
               f"episódio fechado como retoma, t0 no speech_start ({c.get('desfecho')}, {c.get('t0_origem')})")
        cobrar(c.get("t_duck") is not None and c["t_duck"] <= 300, f"t_duck medido ({c.get('t_duck')} ms)")
        cobrar(c.get("t_pausa") is not None and 100 <= c["t_pausa"] <= 400,
               f"t_pausa medido (~120 ms injetados + suspend: {c.get('t_pausa')} ms)")
        cobrar(c.get("t_retoma") is not None and c.get("t_corte") is None, "t_retoma marcado, sem t_corte")

        # ─── 2. pausa → interrupted: corta E o próximo turno toca ─────────────
        r = pg.evaluate("""async () => {
            const c = LX.ctxPlay, o = {};
            lxToca(__tom(2));
            await __dorme(300);
            __ev({ type: 'speech_start' });
            __ev({ type: 'barge_pausa', turn: 2 });
            await __dorme(200);
            o.estado_pausa = c.state;
            __ev({ type: 'interrupted', turn: 2 });
            await __dorme(150);
            o.estado_corte = c.state; o.ativos = LX.ativos.length; o.pausado = LX.pausado;
            o.ganho = LX.duck.gain.value;
            o.corte = LX.cortes[LX.cortes.length - 1] || null;
            lxToca(__tom(0.6));                           // próximo turno
            await __dorme(250);
            o.rms_proximo = __rms();
            lxCancelaPlayback();
            return o;
        }""")
        print("  2:", json.dumps(r))
        cobrar(r["estado_pausa"] == "suspended", "pausado antes do corte")
        cobrar(r["estado_corte"] == "running" and r["ativos"] == 0 and not r["pausado"],
               f"interrupted corta e solta o contexto ({r['estado_corte']}, {r['ativos']} na fila)")
        cobrar(r["rms_proximo"] > 0.02, f"próximo turno toca depois do corte (rms {r['rms_proximo']:.3f})")
        c = r["corte"] or {}
        cobrar(c.get("desfecho") == "corte" and c.get("t_pausa") is not None and c.get("t_corte") is not None
               and c["t_corte"] >= c["t_pausa"],
               f"episódio de corte com t_pausa ≤ t_corte ({c.get('t_pausa')} / {c.get('t_corte')})")

        # ─── 3. teto local: pausa sem decisão continua sozinha ────────────────
        r = pg.evaluate("""async () => {
            const c = LX.ctxPlay, o = {};
            lxToca(__tom(5));
            await __dorme(200);
            __ev({ type: 'barge_pausa', turn: 3 });
            await __dorme(2600);
            o.ainda = c.state;
            await __dorme(800);
            o.depois = c.state; o.pausado = LX.pausado;
            o.corte = LX.cortes[LX.cortes.length - 1] || null;
            lxCancelaPlayback();
            return o;
        }""")
        print("  3:", json.dumps(r))
        cobrar(r["ainda"] == "suspended", "pausa segura antes do teto local (o do servidor é 2,5 s)")
        cobrar(r["depois"] == "running" and not r["pausado"], "teto local de 3 s continua sozinho")
        cobrar((r["corte"] or {}).get("desfecho") == "teto_local"
               and (r["corte"] or {}).get("t0_origem") == "barge_pausa",
               f"episódio do teto registrado ({r['corte']})")

        # ─── 4. barge_retoma sem pausa (motor antigo) ainda devolve o volume ─
        r = pg.evaluate("""async () => {
            lxToca(__tom(2)); await __dorme(200);
            __ev({ type: 'barge_suspeito' }); await __dorme(120);
            const g1 = LX.duck.gain.value;
            __ev({ type: 'barge_retoma', tipo: 'eco' }); await __dorme(300);
            const o = { g1, g2: LX.duck.gain.value, estado: LX.ctxPlay.state };
            lxCancelaPlayback(); return o;
        }""")
        print("  4:", json.dumps(r))
        cobrar(r["g1"] < 0.2 and r["g2"] > 0.95 and r["estado"] == "running",
               f"retoma sem pausa devolve o volume ({r['g1']:.3f} → {r['g2']:.3f})")

        # ─── 5. `interrupted` de CONTINUAÇÃO (#877): não é corte ─────────────
        # A fala emendou na pausa e o motor cancelou o turno ANTES de qualquer
        # chunk. Chegando com fila, o frame não pode descartá-la nem anunciar
        # "interrompi": só devolve volume/relógio e a tela segue em "pensando".
        r = pg.evaluate("""async () => {
            const c = LX.ctxPlay, o = {};
            lxToca(__tom(2));
            await __dorme(250);
            __ev({ type: 'speech_start' });
            __ev({ type: 'barge_pausa', turn: 5 });
            await __dorme(200);
            o.antes = LX.ativos.length;
            __ev({ type: 'interrupted', turn: 5, motivo: 'continuacao' });
            await __dorme(250);
            o.ativos = LX.ativos.length; o.ctx = c.state; o.pausado = LX.pausado;
            o.estado = LX.estado; o.log = document.getElementById('lxLog').textContent;
            o.corte = LX.cortes[LX.cortes.length - 1] || null;
            lxCancelaPlayback(); return o;
        }""")
        print("  5:", json.dumps(r))
        cobrar(r["antes"] == 1, f"havia trecho na fila antes da continuação ({r['antes']})")
        cobrar(r["ativos"] == 1, f"continuação NÃO descarta a fila ({r['ativos']} na fila)")
        cobrar(r["ctx"] == "running" and not r["pausado"],
               f"continuação solta o contexto pausado ({r['ctx']}, pausado={r['pausado']})")
        cobrar(r["estado"] != "interrompido",
               f"continuação não pinta 'interrompido' (estado {r['estado']!r})")
        cobrar("continuação" in r["log"], "continuação aparece no log")
        c = r["corte"] or {}
        cobrar(c.get("desfecho") == "continuacao" and c.get("t_corte") is None,
               f"episódio fechado como continuação, sem t_corte ({c.get('desfecho')}, {c.get('t_corte')})")

        # contraprova: o MESMO frame sem `motivo` volta a cortar e a dizer "interrompido"
        r = pg.evaluate("""async () => {
            lxToca(__tom(2)); await __dorme(200);
            __ev({ type: 'interrupted', turn: 6 }); await __dorme(200);
            const o = { ativos: LX.ativos.length, estado: LX.estado };
            lxCancelaPlayback(); return o;
        }""")
        print("  5b:", json.dumps(r))
        cobrar(r["ativos"] == 0 and r["estado"] == "interrompido",
               f"interrupted comum continua cortando ({r['ativos']}, {r['estado']!r})")

        # ─── 6. sem `ctxPlay` o episódio não fica órfão (P3 do gate #893) ────
        # `lxPausaPlayback` sai cedo sem contexto; se o episódio ficasse aberto, o
        # `lxCorteInicia` do próximo sinal passaria pelo early return e o `t0` novo
        # nunca nasceria — as medidas seguintes herdariam o `t0` velho.
        r = pg.evaluate("""async () => {
            const o = {};
            lxToca(__tom(1)); await __dorme(150);
            __ev({ type: 'speech_start' }); await __dorme(50);
            o.aberto = !!LX.corte;
            const ctx = LX.ctxPlay; LX.ctxPlay = null;     // contexto morto
            __ev({ type: 'barge_pausa', turn: 7 }); await __dorme(100);
            o.orfao = !!LX.corte;
            o.desfecho = (LX.cortes[LX.cortes.length - 1] || {}).desfecho;
            LX.ctxPlay = ctx;
            __ev({ type: 'speech_start' }); await __dorme(50);
            o.novo = LX.corte ? LX.corte.t0_origem : null;
            lxCancelaPlayback(); lxCorteFecha('fim'); return o;
        }""")
        print("  6:", json.dumps(r))
        cobrar(r["aberto"], "o primeiro speech_start abriu episódio")
        cobrar(not r["orfao"], f"episódio ficou órfão sem ctxPlay ({r})")
        cobrar(r["desfecho"] == "sem_ctx", f"órfão fechado como sem_ctx ({r['desfecho']!r})")
        cobrar(r["novo"] == "speech_start", f"o episódio seguinte nasceu com t0 novo ({r['novo']!r})")

        cobrar(not erros, f"console sem pageerror ({erros[:3]})")
        try: pg.evaluate("() => lxDesligar()")
        except Exception: pass
        b.close()
finally:
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception: pass
    try: proc.wait(timeout=10)
    except Exception:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass

print()
if falhas:
    print("✖ FALHOU — pausa/retomada do playback do Live (#881); estado em", DIR_TMP)
    for f in falhas: print("  ·", f)
    sys.exit(1)
shutil.rmtree(DIR_TMP, ignore_errors=True)
print("✔ OK — pausa no ponto, fila intacta, retoma sem repetir nem pular, corte solta o contexto, teto local e medidas expostas")
PYEOF
