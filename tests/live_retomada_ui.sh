#!/usr/bin/env bash
# Live: queda explicada pelo servidor tem de RETOMAR a sessão (#616).
#
# `session_ttl` (sessão ociosa) e `busy` (sem vaga) chegam como `error` e o
# servidor FECHA o socket logo atrás. A tela fazia o contrário do necessário:
# caía no ramo genérico, pintava "sessão viva — fale de novo" (mentira: a sessão
# morreu) e parava a captura — o dono falava para o nada.
#
# Aqui o WebSocket é FALSO (`route_web_socket`), então o roteiro é determinístico
# e rápido: nada de esperar 300 s de TTL nem de disputar carga.
#
#   · `session_ttl` → motivo NA TELA (coluna IA + estado), retomada automática e a
#     nova conexão leva `setup.session_id` (o servidor retoma o histórico) e volta
#     a mandar áudio (captura reaberta);
#   · `busy` → mesma retomada, com espera maior entre tentativas;
#   · o motivo NÃO é sobrescrito pelo ramo genérico (erro de pipeline atrás);
#   · o close ATRASADO do socket antigo (chega depois da retomada) não derruba a
#     conexão nova nem a captura;
#   · `session_substituida` NÃO retoma (outra conexão é a dona do id) e o motivo
#     fica;
#   · `session_fechada` (#628) também não retoma e explica o motivo próprio — o
#     código não pode cair no ramo genérico ("sessão viva");
#   · depois de LX_RETOMA_MAX tentativas (4 conexões contando a do clique) a tela
#     para e pede o clique, sem laço infinito.
#
# Instância isolada em tmp (nada do dono), microfone falso do Chromium.
#
#   ./tests/live_retomada_ui.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

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

for _velho in pathlib.Path("/tmp").glob("live_retoma_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 3600:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"live_retoma_{porta}"
for _sub in ("outputs", "voices", "state"):
    (DIR / _sub).mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")
porta_stub = porta_livre()
env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_stub}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(DIR / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(DIR / "speaker.json"), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_VOICES_DIR": str(DIR / "voices"), "TTS_METRICS_DIR": str(DIR / "state"),
       "VOZ_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = DIR / "servidor.log"
_launcher = '''
import os, uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["VOZ_PORT"]))
'''
proc = subprocess.Popen([sys.executable, "-c", _launcher], cwd=str(pathlib.Path.cwd()), env=env,
                        stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
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

def esperar(fn, msg, timeout=15000):
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
                                     "--use-fake-ui-for-media-stream"])
        ctx = b.new_context(permissions=["microphone"], viewport={"width": 1200, "height": 900})
        pg = ctx.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))

        # ── WebSocket FALSO: guarda cada conexão, o `setup` e os frames de áudio ─
        conns = []           # [{rota, setup, frames}]
        roteiro = {"modo": "ttl"}     # ttl | substituida | busy_sempre
        contador = {"sid": 0}

        def ao_conectar(rota):
            reg = {"rota": rota, "setup": None, "frames": 0}
            conns.append(reg)

            def recebe(msg):
                if isinstance(msg, (bytes, bytearray)):
                    reg["frames"] += 1
                    return
                try:
                    d = json.loads(msg)
                except Exception:
                    return
                if d.get("type") == "setup":
                    reg["setup"] = d
                    if roteiro["modo"] == "busy_sempre":
                        rota.send(json.dumps({"type": "error", "code": "busy",
                                              "message": "máximo de 4 sessões simultâneas"}))
                        rota.close(code=1013)
                        return
                    contador["sid"] += 1
                    # o servidor retoma quando o setup traz o MESMO id
                    sid = d.get("session_id") or f"sid-probe-{contador['sid']}"
                    reg["sid"] = sid
                    rota.send(json.dumps({"type": "ready", "session_id": sid,
                                          "resumed": bool(d.get("session_id")),
                                          "audio": {"format": "pcm16", "sr": 24000}}))
            rota.on_message(recebe)

        pg.route_web_socket("**/api/live/ws", ao_conectar)
        pg.goto(base, wait_until="domcontentloaded")
        pg.click('.nav-item[data-view="live"]')
        pg.wait_for_selector("#lxLigar", state="visible", timeout=20000)

        def estado():
            return pg.evaluate("() => ({ estado: LX.estado, retoma: LX.retoma, explicado: LX.explicado,"
                               " status: document.getElementById('lxStatus').textContent,"
                               " ia: document.getElementById('lxIA').innerText })")

        def religar(rotulo, ouvindo=True):
            """Estado limpo e clica em Ligar: com `LX.ws` de pé o clique DESLIGA.

            `ouvindo=False` para cenários que RECUSAM a conexão (busy): aí não há
            `ouvindo` para esperar."""
            pg.evaluate("""() => { clearTimeout(LX.retomaT); LX.retomaT = 0;
                if (LX.ws) { try { LX.ws.close(); } catch (e) {} LX.ws = null; }
                lxParaCaptura(); LX.sid = ''; LX.retoma = 0; LX.estado = 'desligado';
                LX.substituida = false; }""")
            pg.wait_for_timeout(200)
            pg.click("#lxLigar")
            if ouvindo:
                esperar(lambda: pg.wait_for_function(
                    "() => LX.sid && LX.estado === 'ouvindo'", timeout=20000), rotulo)
            else:
                pg.wait_for_timeout(500)

        # ── 1) `session_ttl`: motivo na tela + retomada automática ───────────
        pg.click("#lxLigar")
        esperar(lambda: pg.wait_for_function("() => LX.sid && LX.estado === 'ouvindo'", timeout=20000),
                "a primeira sessão abrir")
        cobrar(len(conns) == 1, f"esperava 1 conexão, veio {len(conns)}")
        c1 = conns[0]
        cobrar(c1["setup"] is not None, "o cliente não mandou `setup`")
        sid1 = c1.get("sid")
        c1["rota"].send(json.dumps({"type": "error", "code": "session_ttl",
                                    "message": "sessão ociosa por 300s"}))
        # um erro GENÉRICO logo atrás não pode repintar o motivo explicado; o close
        # só vem DEPOIS da leitura, senão o `onclose` mascara o repintado
        # o BARGE repinta a linha ("✋ ouvi você…") e não pode apagar o motivo;
        # leitura cedo (80 ms) — antes de a retomada (250 ms) concluir e limpar o
        # motivo legitimamente com "sessão pronta"
        c1["rota"].send(json.dumps({"type": "speech_start", "turn": 9}))
        pg.wait_for_timeout(80)
        e0 = estado()
        print(f"  ttl + barge: estado={e0['estado']!r} · status={e0['status']!r}")
        cobrar("ouvi você" not in e0["status"],
               f"o barge repintou por cima do motivo ({e0['status']!r})")
        cobrar("inatividade" in e0["status"], f"o motivo sumiu da linha ({e0['status']!r})")
        cobrar("retomando" in e0["status"], f"a retomada não está anunciada ({e0['status']!r})")
        c1["rota"].close(code=1000)
        pg.wait_for_timeout(150)
        e1 = estado()
        print(f"  após session_ttl: estado={e1['estado']!r} · status={e1['status']!r}")
        cobrar("ociosa" in e1["ia"] or "session_ttl" in e1["ia"],
               f"o motivo do TTL não ficou na coluna IA ({e1['ia'][-80:]!r})")
        # a janela de "retomando" é curta (250 ms) e pode já ter concluído aqui:
        # a prova da retomada é a 2ª conexão abaixo, com o mesmo session_id
        print(f"  retoma={e1['retoma']} · estado={e1['estado']!r}")
        cobrar("fale de novo" not in e1["status"], f"o ramo genérico sobrescreveu o motivo ({e1['status']!r})")
        # a retomada é automática: 2ª conexão, com o MESMO session_id
        esperar(lambda: pg.wait_for_function("() => LX.estado === 'ouvindo' && LX.sid", timeout=20000),
                "a sessão voltar depois do TTL")
        cobrar(len(conns) >= 2, f"a tela não reconectou sozinha (conexões: {len(conns)})")
        if len(conns) >= 2:
            c2 = conns[1]
            print(f"  2ª conexão setup.session_id={c2['setup'] and c2['setup'].get('session_id')!r}")
            cobrar((c2["setup"] or {}).get("session_id") == sid1,
                   "a retomada não levou o session_id (perderia o histórico)")
            # captura REABERTA: frames de áudio voltam a chegar no servidor
            esperar(lambda: pg.wait_for_function("() => true", timeout=100), "x", timeout=100)
            pg.wait_for_timeout(1200)
            print(f"  frames na 2ª conexão: {c2['frames']}")
            cobrar(c2["frames"] > 5, f"a captura não voltou depois da retomada ({c2['frames']} frames)")
            cobrar(pg.evaluate("() => !!LX.stream"), "o mic não foi reaberto depois da retomada")

        # ── 1c) close ATRASADO: a retomada abre a nova e o velho fecha depois ─
        # (rede lenta fecha > 250 ms; sem guarda, o `onclose` velho zerava o
        # `LX.ws` novo e derrubava a captura recém-aberta)
        alvo_velho = conns[-1]["rota"]
        n0 = len(conns)
        alvo_velho.send(json.dumps({"type": "error", "code": "session_ttl",
                                    "message": "sessão ociosa por 300s"}))
        pg.wait_for_timeout(600)      # retomada (250 ms) + folga
        cobrar(len(conns) == n0 + 1, f"a retomada não abriu conexão nova ({len(conns) - n0})")
        n_antes_atraso = len(conns)
        alvo_velho.close(code=1000)        # close do ANTIGO, já substituído
        pg.wait_for_timeout(900)
        a = pg.evaluate("() => ({ estado: LX.estado, ws: !!LX.ws && LX.ws.readyState === 1,"
                        " stream: !!LX.stream, conexoes: 0 })")
        frames_agora = conns[-1]["frames"]
        print(f"  close atrasado: estado={a['estado']!r} wsAberto={a['ws']} mic={a['stream']} frames={frames_agora}")
        cobrar(a["ws"] and a["stream"], "o close do socket velho derrubou a conexão/mic novos")
        cobrar(a["estado"] == "ouvindo", f"o close velho pintou por cima ({a['estado']!r})")
        cobrar(len(conns) == n_antes_atraso, "o close velho disparou outra retomada")
        cobrar(frames_agora > 5, f"a captura parou no close velho ({frames_agora} frames)")

        # ── 1d) `pipeline` morto: mostra o erro e RETOMA (a sessão pode estar viva)
        n_antes_pipe = len(conns)
        conns[-1]["rota"].send(json.dumps({"type": "error", "code": "pipeline",
                                           "message": "RuntimeError: provedor caiu"}))
        pg.wait_for_timeout(250)
        ep = estado()
        print(f"  após pipeline: estado={ep['estado']!r} · status={ep['status']!r}")
        cobrar("pipeline" in (ep["status"] + ep["ia"]).lower(),
               f"o erro do pipeline não apareceu ({ep['status']!r})")
        cobrar("fale de novo" not in ep["status"],
               f"a tela voltou para 'fale de novo' com o pipeline morto ({ep['status']!r})")
        esperar(lambda: pg.wait_for_function(
            "() => LX.estado === 'ouvindo'", timeout=20000), "a retomada do pipeline")
        cobrar(len(conns) > n_antes_pipe, "o pipeline morto não retomou a sessão")

        # ── 2b) `session_fechada` (task de envio morta) NÃO retoma e explica ─
        # #628: o código próprio não pode cair no ramo genérico ("sessão viva")
        # nem no de vencimento (que RETOMA).
        n_antes_f = len(conns)
        conns[-1]["rota"].send(json.dumps({"type": "error", "code": "session_fechada",
                                           "message": "a sessão foi encerrada pelo servidor"}))
        conns[-1]["rota"].close(code=1000)
        pg.wait_for_timeout(1200)
        e2f = estado()
        print(f"  após session_fechada: estado={e2f['estado']!r} · status={e2f['status']!r}")
        cobrar(len(conns) == n_antes_f, f"session_fechada reconectou ({len(conns)} conexões)")
        # o que DISTINGUE do ramo genérico é `LX.explicado`: só o ramo próprio
        # fixa o motivo; o genérico deixa o `onclose` pintar por cima e o motivo
        # vira "sessão encerrada" genérico
        cobrar("encerrada pelo servidor" in (e2f["explicado"] or ""),
               f"caiu no ramo genérico (explicado={e2f['explicado']!r})")
        cobrar("encerrada" in (e2f["status"] + e2f["ia"]).lower(),
               f"o motivo do fechamento se perdeu ({e2f['status']!r})")
        cobrar("sessão viva" not in (e2f["status"] + " " + e2f["ia"]).lower(),
               f"caiu no ramo genérico e diz que está viva ({e2f['status']!r})")

        religar("a sessão religar antes do caso da substituída")

        # ── 2) `session_substituida` NÃO retoma e o motivo fica ──────────────
        n_antes = len(conns)
        conns[-1]["rota"].send(json.dumps({"type": "error", "code": "session_substituida",
                                           "message": "outra conexão assumiu este session_id"}))
        conns[-1]["rota"].close(code=1000)
        pg.wait_for_timeout(1500)
        e2 = estado()
        print(f"  após substituída: estado={e2['estado']!r} · status={e2['status']!r}")
        cobrar(len(conns) == n_antes, f"a substituição reconectou ({len(conns)} conexões)")
        cobrar("substituída" in (e2["status"] + e2["ia"]).lower(),
               f"o motivo da substituição se perdeu ({e2['status']!r})")
        cobrar("sessão encerrada" not in e2["status"], f"o close repintou por cima ({e2['status']!r})")

        # ── 3) `busy` retoma (com espera maior) e desiste no teto ────────────
        roteiro["modo"] = "busy_sempre"
        conns.clear()
        religar("a sessão religar antes do caso do busy", ouvindo=False)
        esperar(lambda: pg.wait_for_function(
            "() => /não consegui retomar/.test(document.getElementById('lxStatus').textContent)",
            timeout=25000), "a tela desistir depois do teto de tentativas")
        e3 = estado()
        print(f"  após 3 busy: conexões={len(conns)} · status={e3['status']!r}")
        # 1 tentativa do clique + LX_RETOMA_MAX retomadas automáticas
        cobrar(len(conns) == 4,
               f"o teto de tentativas não foi respeitado (conexões: {len(conns)}, esperado 4)")
        cobrar("não consegui retomar" in e3["status"], f"a tela não pediu o clique ({e3['status']!r})")
        cobrar(e3["estado"] == "erro", f"o estado não virou erro no teto ({e3['estado']!r})")
        # o clique prometido na mensagem tem de RETOMAR (com socket vivo ele
        # desligaria, que é o caminho normal)
        roteiro["modo"] = "normal"
        antes_manual = len(conns)
        cobrar(pg.text_content("#lxLigar").strip().endswith("Ligar"), "o botão não está em 'Ligar'")
        pg.click("#lxLigar")
        esperar(lambda: pg.wait_for_function("() => LX.estado === 'ouvindo'", timeout=20000),
                "o clique retomar a sessão")
        cobrar(len(conns) == antes_manual + 1, "o clique depois da desistência não reconectou")

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
    print("✖ retomada do Live:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — session_fechada e substituída não retomam; session_ttl e busy RETOMAM (mesmo session_id, captura de volta), "
      "o motivo não é sobrescrito, substituição não retoma e o teto de tentativas pede o clique")
PYEOF
