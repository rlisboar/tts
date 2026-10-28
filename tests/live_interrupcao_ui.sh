#!/usr/bin/env bash
# Interrupção inteligente na UI do Live (#502) — a metade do CLIENTE do contrato
# do #501. Prova, por EFEITO e com o app de pé:
#
#   · `barge_suspeito` ABAIXA o playback ~18 dB em ≤50 ms sem parar a fila;
#   · `barge_retoma` devolve o volume (~150 ms) e a resposta segue;
#   · `interrupted` corta (e o volume não fica abaixado para o próximo turno);
#   · o TETO de 2 s devolve o volume sozinho quando o motor não decide nada;
#   · `barge_decisao` entra no log com tipo, texto e ms;
#   · `speech_start` durante o playback NÃO corta nos modos `inteligente` e
#     `so_comando` (abaixa e espera) e MANTÉM o corte em `imediata` — a #225
#     continua valendo, só que agora dependendo do modo.
#
# O motor ainda não emite estes eventos (o card irmão é o #501): o teste injeta os
# frames no MESMO `lxRecebe` que o WebSocket usa — mesmo precedente do
# `turno_pendente`/`latency` do `live_ui.sh`. O que se cobra aqui é o contrato do
# CLIENTE, não a classificação (essa é do `smoke_barge_intencao.py`).
#
# `live_interrupcao` também ainda não existe no `_apply_settings`: o launcher
# ENVOLVE a função para servir a chave, e o teste cobra a ida-e-volta (POST, GET,
# reload pintando o select, clamp de valor inválido). Assim o único ponto que falta
# aqui é o clamp definitivo do motor, não o caminho da tela.
#
# O volume é medido no ANALISADOR DA SAÍDA (o mesmo do painel OBS), não só no
# ganho: prova atenuação de áudio de verdade, não um número solto.
#
#   ./tests/live_interrupcao_ui.sh        # sobe servidor próprio em porta livre
#
# MORDIDA: tirar a chamada de `lxAbaixaVolume` do `barge_suspeito`/`speech_start`
# derruba as asserções de duck (ganho e RMS ficam em 1).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# A sessão do Live faz pre-warm do TTS (carrega modelo): serializa com as outras
# suítes de áudio, senão elas se atropelam em Metal/CPU (#134).
source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - <<'PYEOF'
import json, os, pathlib, signal, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

# ─── stub do provedor de chat: a sessão sobe e o prewarm não depende de nuvem ─
class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if json.loads(b"{}" or b"{}") is None: pass
        corpo = {"choices": [{"message": {"content": "ok"}}], "stream": True}
        if self.path.endswith("/chat/completions"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
            return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(corpo).encode())

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

stub_porta = porta_livre()
threading.Thread(target=HTTPServer(("127.0.0.1", stub_porta), Stub).serve_forever, daemon=True).start()

# ─── servidor próprio (settings.json do dono não manda no teste) ─────────────
porta = porta_livre()
env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
       "TTS_CHAT_MODEL": "stub-int",
       # pinados: o dono pode estar em `chat_backend: "dsh"` — e o do LIVE tem
       # variável própria, não governada por esta (#143/#195)
       "TTS_CHAT_BACKEND": "openai",
       "TTS_CHAT_BACKEND_LIVE": os.environ.get("TTS_CHAT_BACKEND_LIVE") or "openai",
       "LIVE_INT_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = pathlib.Path("/tmp") / f"live_interrupcao_servidor-{porta}.log"
# STUB DO LADO DO SERVIDOR para o único ponto que ainda não existe no app: o
# `live_interrupcao` nas settings (é do card irmão, #501). Envolver
# `_apply_settings` faz o POST da tela VALER no GET seguinte — é o que permite
# provar ida-e-volta de verdade (grava, recarrega, a tela volta no modo) sem
# esperar o motor. O clamp aqui é do stub; o definitivo é o do `_apply_settings`.
_launcher = '''
import os, uvicorn, app
_MODOS = ("inteligente", "imediata", "so_comando")
_orig = app._apply_settings
def _com_modo(payload):
    r = _orig(payload)
    if "live_interrupcao" in payload:
        v = payload.get("live_interrupcao")
        app._settings["live_interrupcao"] = v if v in _MODOS else "inteligente"
    return r
app._apply_settings = _com_modo
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["LIVE_INT_PORT"]))
'''
proc = subprocess.Popen([str(pathlib.Path.cwd() / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(pathlib.Path.cwd()), env=env,
                        stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
base = f"http://127.0.0.1:{porta}"

# modo de interrupção LIDO no início, para devolver o que era do dono no fim
modo_inicial = None
try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu na subida (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor de teste em {base} (stub :{stub_porta})")
    with urllib.request.urlopen(base + "/api/settings", timeout=5) as r:
        modo_inicial = (json.loads(r.read() or b"{}") or {}).get("live_interrupcao")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-fake-device-for-media-stream",
                                    "--use-fake-ui-for-media-stream",
                                    # o ganho só é medível com o contexto de áudio
                                    # rodando: sem isto o autoplay policy o deixa
                                    # suspenso e a rampa não anda no relógio real
                                    "--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context()
        ctx.grant_permissions(["microphone"], origin=base)
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(base, wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.wait_for_function("() => typeof LX === 'object'", timeout=20000)

        # ─── o SELETOR: existe, tem as três opções e o título de cada uma ────
        sel = pg.evaluate("""() => {
            const s = document.getElementById('lxInterrupcao');
            return s ? { valor: s.value, opcoes: [...s.options].map(o => o.value),
                         titulo: s.title, rotulos: [...s.options].map(o => o.textContent) } : null;
        }""")
        print("  seletor:", sel)
        cobrar(sel is not None, "seletor #lxInterrupcao não existe no painel do Live")
        if sel:
            cobrar(sel["opcoes"] == ["inteligente", "imediata", "so_comando"],
                   f"opções do seletor fora do contrato: {sel['opcoes']}")
            cobrar("Inteligente" in sel["titulo"] and "Imediata" in sel["titulo"]
                   and "comando" in sel["titulo"],
                   f"tooltip não explica as opções: {sel['titulo']!r}")

        pg.locator("#lxLigar").click()
        try:
            pg.wait_for_function("() => !!LX.ctxPlay && !!LX.duck", timeout=25000)
        except Exception:
            diag = pg.evaluate("""() => ({ estado: LX.estado, ws: LX.ws ? LX.ws.readyState : -1,
                                          ctx: !!LX.ctxPlay, duck: !!LX.duck,
                                          status: document.getElementById('lxStatus').textContent })""")
            print("  DIAG:", diag)
            raise
        # sai da frente a captura: o mic falso é um bipe contínuo e abriria turnos
        # no meio da medição. O playback (que é o que se mede) continua vivo.
        pg.evaluate("""() => {
            try { LX.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
            window.lxEnviaBin = () => {};      // nada mais sobe para o servidor
            LX.ctxPlay.resume();
        }""")
        pg.wait_for_timeout(1200)
        pg.evaluate("() => { LX.interrupcao = 'inteligente'; lxPintaInterrupcao(); lxCancelaPlayback(); }")

        # ─── utilidades de medição ───────────────────────────────────────────
        def prepara(segundos=6):
            """Fila com UM trecho de tom (440 Hz) e contexto rodando."""
            pg.evaluate("""(s) => {
                lxCancelaPlayback();
                const n = 24000 * s, i = new Int16Array(n);
                for (let k = 0; k < n; k++) i[k] = Math.round(Math.sin(2 * Math.PI * 440 * k / 24000) * 3200);
                lxToca(i);
            }""", segundos)

        def rms():
            """RMS na SAÍDA (o analisador do painel): o que de fato se ouve."""
            return pg.evaluate("""() => {
                const a = LX.anOut; if (!a) return -1;
                const b = new Float32Array(a.fftSize); a.getFloatTimeDomainData(b);
                let s = 0; for (const v of b) s += v * v;
                return Math.sqrt(s / b.length);
            }""")

        def dispara(evento, ms=420):
            """Emite o frame e amostra o ganho a cada ~5 ms (rampa medida)."""
            return pg.evaluate("""async ([ev, ms]) => {
                const t0 = performance.now(), am = [], antes = LX.duck.gain.value;
                let parar = false;
                const passo = () => { am.push([performance.now() - t0, LX.duck.gain.value]);
                                      if (!parar) setTimeout(passo, 5); };
                setTimeout(passo, 5);
                lxRecebe({ data: JSON.stringify(ev) });
                await new Promise(r => setTimeout(r, ms));
                parar = true;
                return { antes, am, ativos: LX.ativos.length, abaixado: LX.abaixado };
            }""", [evento, ms])

        def primeiro(am, alvo, abaixo=True):
            for t, g in am:
                if (g <= alvo) if abaixo else (g >= alvo):
                    return t
            return None

        # ─── 1) `speech_start` no modo INTELIGENTE: abaixa, não corta ────────
        prepara(); pg.wait_for_timeout(250)
        rms_antes = rms()
        r1 = dispara({"type": "speech_start", "fala_ms": 400})
        pg.wait_for_timeout(120)
        rms_depois = rms()
        t_duck = primeiro(r1["am"], 0.2)
        print(f"  speech_start/inteligente: ganho {r1['antes']:.3f} -> 0.2 em {t_duck} ms"
              f" · RMS {rms_antes:.4f} -> {rms_depois:.4f} · fila {r1['ativos']}")
        cobrar(r1["ativos"] > 0, "speech_start no modo inteligente CORTOU a fila (devia só abaixar)")
        cobrar(t_duck is not None and t_duck <= 70, f"duck não desceu a -18 dB em ≤50 ms (medido {t_duck} ms)")
        cobrar(r1["abaixado"] and not pg.evaluate("() => document.getElementById('lxDuck').hidden"),
               "chip de volume abaixado não apareceu")
        if rms_antes > 0:
            cobrar(rms_depois <= rms_antes * 0.35,
                   f"áudio não foi atenuado de verdade ({rms_antes:.4f} -> {rms_depois:.4f})")

        # ─── 2) TETO de 2 s: sem decisão, o volume volta sozinho ─────────────
        pg.wait_for_timeout(2400)
        g_teto = pg.evaluate("() => LX.duck.gain.value")
        log_teto = pg.evaluate("() => document.getElementById('lxLog').textContent")
        print(f"  teto de 2 s: ganho voltou a {g_teto:.3f} · aviso no log={'2 s sem decisão' in log_teto}")
        cobrar(g_teto >= 0.99, f"volume não voltou sozinho depois do teto de 2 s (ganho {g_teto:.3f})")
        cobrar("2 s sem decisão" in log_teto, "o teto de 2 s não avisa no log")
        cobrar(pg.evaluate("() => document.getElementById('lxDuck').hidden"),
               "chip de volume abaixado ficou aceso depois do teto")

        # ─── 3) `barge_suspeito` + `barge_retoma`: abaixa e DEVOLVE ───────────
        prepara(); pg.wait_for_timeout(250)
        r3 = dispara({"type": "barge_suspeito", "turn": 7}, 200)
        t3 = primeiro(r3["am"], 0.2)
        cobrar(t3 is not None and t3 <= 70, f"barge_suspeito não abaixou em ≤50 ms (medido {t3} ms)")
        cobrar(r3["ativos"] > 0, "barge_suspeito parou a fila (devia manter tocando)")
        r4 = dispara({"type": "barge_retoma", "turn": 7, "tipo": "backchannel"}, 400)
        t4 = primeiro(r4["am"], 0.99, abaixo=False)
        log3 = pg.evaluate("() => document.getElementById('lxLog').textContent")
        print(f"  barge_suspeito/retoma: duck em {t3} ms · retoma em {t4} ms · log='↩ continuei' {'in' if '↩ continuei — era backchannel' in log3 else 'NÃO'} · fila {r4['ativos']}")
        cobrar(t4 is not None and t4 <= 300, f"barge_retoma não devolveu o volume (medido {t4} ms)")
        cobrar("↩ continuei — era backchannel" in log3,
               "log do barge_retoma não diz o que era (esperado '↩ continuei — era backchannel')")
        cobrar(r4["ativos"] > 0, "barge_retoma não deixou a resposta tocando")

        # ─── 4) `barge_decisao` no log: tipo, texto e ms ─────────────────────
        pg.evaluate("""() => lxRecebe({ data: JSON.stringify({ type: 'barge_decisao', turn: 7,
            tipo: 'backchannel', texto: 'uhum', fala_ms: 380, decisao_ms: 240 }) })""")
        decisao = pg.evaluate("() => document.getElementById('lxLog').textContent")
        print("  log da decisão:",
              [ln for ln in decisao.split("decisão")[-1:]][0].splitlines()[0] if "decisão" in decisao else "(ausente)")
        cobrar("backchannel" in decisao and "uhum" in decisao and "240 ms" in decisao,
               f"barge_decisao não entra no log com tipo/texto/ms: {decisao[-160:]!r}")

        # ─── 5) `interrupted` corta e o volume NÃO fica abaixado ─────────────
        prepara(); pg.wait_for_timeout(250)
        dispara({"type": "barge_suspeito", "turn": 8}, 120)
        r5 = pg.evaluate("""async () => {
            lxRecebe({ data: JSON.stringify({ type: 'interrupted', turn: 8 }) });
            await new Promise(r => setTimeout(r, 400));
            return { ativos: LX.ativos.length, ganho: LX.duck.gain.value };
        }""")
        print(f"  interrupted: fila {r5['ativos']} · ganho {r5['ganho']:.3f}")
        cobrar(r5["ativos"] == 0, "interrupted não cortou a fila")
        cobrar(r5["ganho"] >= 0.99, f"volume ficou abaixado depois do corte ({r5['ganho']:.3f}) — o próximo turno nasceria mudo")

        # ─── 6) modo `imediata` pela TELA: `speech_start` volta a CORTAR ─────
        pg.select_option("#lxInterrupcao", "imediata")
        pg.wait_for_timeout(200)
        modo = pg.evaluate("() => ({ lx: LX.interrupcao, sel: document.getElementById('lxInterrupcao').value })")
        print("  modo pelo seletor:", modo)
        cobrar(modo["lx"] == "imediata" and modo["sel"] == "imediata",
               f"seletor não aplicou o modo imediata: {modo}")
        prepara(); pg.wait_for_timeout(250)
        r6 = dispara({"type": "speech_start", "fala_ms": 300}, 300)
        print(f"  speech_start/imediata: fila {r6['ativos']} · ganho {pg.evaluate('() => LX.duck.gain.value'):.3f}")
        cobrar(r6["ativos"] == 0, "modo imediata não cortou no speech_start (a #225 se perdeu)")

        # ─── 7) modo `so_comando`: pela tela, também só abaixa ──────────────
        pg.select_option("#lxInterrupcao", "so_comando")
        pg.wait_for_timeout(200)
        prepara(); pg.wait_for_timeout(250)
        r7 = dispara({"type": "speech_start", "fala_ms": 300}, 220)
        print(f"  speech_start/so_comando: fila {r7['ativos']}")
        cobrar(r7["ativos"] > 0, "modo so_comando cortou no speech_start (devia abaixar e esperar)")

        # ─── 8) IDA E VOLTA pela settings: grava, recarrega e volta no modo ──
        # O stub do launcher serve a chave (`_apply_settings`); sem ele este bloco
        # provaria só o lado do cliente. Aqui se cobra o caminho real: POST, GET e
        # o load pintando o select — e o clamp de um valor inválido.
        def modo_servidor():
            with urllib.request.urlopen(base + "/api/settings", timeout=5) as r:
                return (json.loads(r.read() or b"{}") or {}).get("live_interrupcao")

        pg.select_option("#lxInterrupcao", "imediata")
        pg.wait_for_timeout(500)
        gravado = modo_servidor()
        log_modo = pg.evaluate("() => document.getElementById('lxLog').textContent")
        print("  gravado no servidor:", gravado, "· avisou desconhecido:",
              "ainda não conhece" in log_modo)
        cobrar(gravado == "imediata", f"o modo escolhido não chegou às settings (servidor: {gravado!r})")
        cobrar("ainda não conhece" not in log_modo,
               "a tela avisou 'ajuste desconhecido' com o servidor servindo a chave")

        pg.reload(wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.wait_for_function("() => typeof LX === 'object'", timeout=20000)
        pg.wait_for_timeout(900)
        carregado = pg.evaluate("() => document.getElementById('lxInterrupcao').value")
        print("  modo depois do reload:", carregado)
        cobrar(carregado == "imediata", f"o modo gravado não voltou no load (select={carregado!r})")

        # valor inválido no servidor: a tela cai no PADRÃO, nunca num modo fantasma
        req = urllib.request.Request(base + "/api/settings", method="POST",
                                     data=json.dumps({"live_interrupcao": "banana"}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        pg.reload(wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.wait_for_function("() => typeof LX === 'object'", timeout=20000)
        pg.wait_for_timeout(900)
        padrao = pg.evaluate("() => ({ sel: document.getElementById('lxInterrupcao').value, lx: LX.interrupcao })")
        print("  valor inválido no servidor:", padrao)
        cobrar(padrao["sel"] == "inteligente", f"modo inválido não caiu no padrão: {padrao}")

        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))

        # devolve o modo do dono (o POST grava no settings.json de verdade)
        if pg.evaluate("() => typeof lxDefineInterrupcao === 'function'"):
            pg.evaluate("(m) => lxDefineInterrupcao(m)", modo_inicial or "inteligente")
            pg.wait_for_timeout(400)
        try: pg.evaluate("() => lxDesligar()")
        except Exception: pass
        b.close()
finally:
    # mata o GRUPO: o servidor sobe com `start_new_session` e o uvicorn tem filhos
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try: proc.terminate()
        except Exception: pass
    try: proc.wait(timeout=10)
    except Exception:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass

print()
if falhas:
    print("✖ FALHOU — contrato do cliente da interrupção inteligente:")
    print("  log do servidor da rodada em", log)
    for f in falhas: print("  ·", f)
    sys.exit(1)
try: log.unlink()
except Exception: pass
print("✔ OK — duck ≤50 ms sem cortar a fila, retoma ~150 ms, corte no interrupted,"
      " teto de 2 s devolvendo o volume e o modo imediata preservando o corte da #225")
PYEOF
