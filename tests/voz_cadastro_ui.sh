#!/usr/bin/env bash
# Cadastro de voz com qualidade (#585, parte de UI do #525): o servidor passou a
# RECUSAR amostra ruim (400 + motivo), a exigir 3 amostras antes de considerar o
# perfil pronto e a marcar perfil de outro extrator. A tela tem de:
#
#   · dizer o critério ANTES da gravação (3 s de FALA, SNR ≥ 10 dB, ambiente quieto);
#   · mostrar o MOTIVO da recusa e oferecer "usar assim mesmo" (`?forcar=true`) com
#     a MESMA gravação — sem obrigar a regravar para descobrir o critério;
#   · guiar as amostras pelo que o SERVIDOR responde (`samples`/`faltando`/`completo`),
#     sem tratar a 1ª como fim do cadastro;
#   · mostrar `modelo` e `precisa_recadastrar` por perfil e oferecer a regravação.
#
# Áudio real, sem dublê da rota: microfone FALSO do Chromium tocando fixtures
# VERSIONADAS e o servidor de verdade decidindo (VAD, SNR, extrator). O que se mede
# é a TELA. As fixtures:
#   · voz_curta_16k.wav  — 2,9 s, quase toda silêncio com um blip: em 8 s gravados o
#     VAD acha < 3 s de fala ⇒ recusa "fala curta" (do #443, já existia);
#   · voz_fala_5s_16k.wav — 5 s de FALA real, 16 kHz mono: passa (4,9 s de voz, SNR
#     ~36 dB). Regenerar: `say -o /tmp/fala-$$.aiff "<frase>"` + `ffmpeg -i /tmp/fala-$$.aiff
#     -af silenceremove=… -t 5 -ac 1 -ar 16000 -c:a pcm_s16le <destino>`.
#
# Estado próprio (#461/#541): TTS_SPEAKER_PATH/OUTPUTS/SETTINGS/APIKEYS em tmp —
# o `.speaker-profiles.json` e o `outputs/` do dono não são tocados. Diretório da
# rodada morre no verde; fica na vermelha; as sobras de rodadas mortas são
# recolhidas por idade (#562).
#
#   ./tests/voz_cadastro_ui.sh        # instância própria, porta livre
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
# microfone falso + captura de arquivo: só existe no Chromium
[ -f "$RAIZ/tests/fixtures/voz_curta_16k.wav" ] && [ -f "$RAIZ/tests/fixtures/voz_fala_5s_16k.wav" ] || {
  echo "✖ fixtures de áudio ausentes em tests/fixtures/" >&2; exit 1; }

source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - <<'PYEOF'
import json, os, pathlib, shutil, socket, subprocess, sys, threading, time, traceback, urllib.request

falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

for _velho in pathlib.Path("/tmp").glob("voz_cadastro_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 7200:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"voz_cadastro_{porta}"
(DIR / "outputs").mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")
SPEAKER = DIR / "speaker.json"
porta_stub = porta_livre()

env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_stub}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(DIR / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(SPEAKER), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_VOICES_DIR": str(DIR / "voices"), "VOZ_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
(DIR / "voices").mkdir(exist_ok=True)
log = pathlib.Path("/tmp") / f"voz_cadastro_servidor-{porta}.log"
proc = subprocess.Popen(
    [sys.executable, "-c",
     "import os, uvicorn, app\nuvicorn.run(app.app, host='127.0.0.1', port=int(os.environ['VOZ_PORT']))"],
    cwd=str(pathlib.Path.cwd()), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
    start_new_session=True)
base = f"http://127.0.0.1:{porta}"
FIX = pathlib.Path("tests/fixtures")

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

class Stub(threading.Thread):
    """prewarm do backend de chat: um 200 vazio basta, o cadastro não usa LLM."""
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

def abre_cadastro(pw, wav):
    b = pw.chromium.launch(args=[
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        f"--use-file-for-fake-audio-capture={pathlib.Path(wav).resolve()}"])
    ctx = b.new_context(permissions=["microphone"])
    pg = ctx.new_page()
    erros = []
    pg.on("pageerror", lambda e: erros.append(str(e)))
    pg.goto(base, wait_until="domcontentloaded")
    # o cadastro mora na aba "Rede & memória" das Configurações
    pg.click('.nav-item[data-view="config"]')
    pg.click('[data-stab="rede"]')
    esperar(lambda: pg.wait_for_selector("#spEnroll", state="visible", timeout=20000),
            "o cadastro não abriu na tela")
    return b, ctx, pg, erros


def esperar(fn, msg, timeout=30000):
    """Espera sem derrubar a rodada: timeout vira FALHA com o nome do que faltou."""
    try:
        fn()
        return True
    except Exception as e:
        if type(e).__name__ != "TimeoutError":
            raise
        cobrar(False, f"tempo esgotado esperando: {msg}")
        return False

try:
    espera_saude()
    print(f"servidor de teste em {base}")

    # ── estático: o contrato do servidor está de fato sendo usado (barato; se
    # alguém "resumir" o fetch de volta, o caminho de runtime abaixo some e os
    # testes de tela continuariam verdes por acidente)
    fonte = pathlib.Path("static/index.html").read_text()
    for trecho, oque in (
        ('(forcar ? "?forcar=true" : "")', "o `forcar=true` no POST"),
        ("spGravar(8", "a gravação de 8 s"),
        ("r.faltando", "a leitura de `faltando`"),
        ("r.completo", "a leitura de `completo`"),
        ("p.precisa_recadastrar", "a leitura de `precisa_recadastrar`"),
    ):
        cobrar(trecho in fonte, f"a UI não usa {oque} ({trecho!r})")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        # ── 0) o critério aparece ANTES de gravar ─────────────────────────────
        b, ctx, pg, erros = abre_cadastro(pw, FIX / "voz_curta_16k.wav")
        dica = (pg.text_content("#spDica") or "").strip()
        print(f"  dica antes da gravação: {dica[:70]!r}…")
        cobrar("3 s de FALA" in dica or "3 s de fala" in dica.lower(),
               f"a dica não diz o mínimo de fala ({dica[:80]!r})")
        cobrar("SNR" in dica and "10 dB" in dica, "a dica não diz o critério de ruído")
        cobrar("3 amostras" in dica, "a dica não diz que o perfil precisa de 3 amostras")
        cobrar(pg.is_visible("#spDica"), "a dica não está visível antes da gravação")
        cobrar(not pg.is_visible("#spForcar"), "o box de 'usar assim mesmo' nasce visível")

        # ── 1) amostra RUIM: motivo na tela + "usar assim mesmo" sem regravar ─
        pg.fill("#spName", "perfil-curto")
        pg.click("#spEnroll")
        esperar(lambda: pg.wait_for_selector("#spForcar", state="visible", timeout=60000),
                "o motivo da recusa aparecer na tela")
        motivo = (pg.text_content("#spForcarMotivo") or "").strip()
        print(f"  motivo da recusa: {motivo[:90]!r}")
        cobrar("fala curta" in motivo.lower(), f"o motivo da recusa não foi mostrado ({motivo[:90]!r})")
        cobrar("3s" in motivo or "3 s" in motivo, "o motivo não traz o mínimo (3s)")
        cobrar(pg.is_visible("#spForcarBtn"), "a saída 'usar assim mesmo' não foi oferecida")
        cobrar(not perfis(), "o perfil entrou mesmo com a amostra recusada")

        # o clique tem de REUSAR a gravação (sem outra captura de 8 s)
        t0 = time.time()
        pg.click("#spForcarBtn")
        esperar(lambda: pg.wait_for_function(
            "() => document.getElementById('spForcar').hidden", timeout=30000),
            "o box da recusa fechar depois do 'usar assim mesmo'")
        pg.wait_for_timeout(400)
        gasto = time.time() - t0
        print(f"  'usar assim mesmo' resolveu em {gasto:.1f}s")
        cobrar(gasto < 6, f"o 'usar assim mesmo' regravou (levou {gasto:.1f}s)")
        prog1 = pg.text_content("#spProgresso") or ""
        cobrar("aceito assim mesmo" in prog1, f"a tela não repetiu o problema aceito ({prog1[:90]!r})")
        cobrar("fala curta" in prog1.lower(), "o problema aceito perdeu o texto do servidor")
        cobrar("amostra 1 registrada" in prog1, f"a tela não numerou a amostra ({prog1[:90]!r})")
        cobrar("perfil completo" not in prog1, "a 1ª amostra foi tratada como perfil pronto")
        cobrar(not pg.is_visible("#spForcar"), "o box da recusa ficou aberto depois de aceitar")
        p1 = [p for p in perfis() if p["name"] == "perfil-curto"]
        cobrar(bool(p1) and p1[0]["samples"] == 1, f"o servidor não guardou a amostra forçada: {p1}")
        cobrar(bool(p1) and p1[0].get("precisa_recadastrar") is False,
               "perfil novo nasceu marcado para recadastro")
        cobrar("regravar" not in (pg.text_content("#spProfiles") or ""),
               "perfil atual ganhou 'regravar'")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

        # ── 2) 3 amostras boas: a tela guia e só fecha no 3º ─────────────────
        b, ctx, pg, erros = abre_cadastro(pw, FIX / "voz_fala_5s_16k.wav")
        # 5 s por amostra: a fixture tem 5 s de fala e o teto do runner é curto.
        # A captura e o envio são os de produção (o default de 8 s é cobrado acima).
        pg.evaluate("() => { const o = window.spGravar; window.spGravar = (s, st) => o(5, st); }")
        pg.fill("#spName", "perfil-fala")
        rotulos = []
        for i in (1, 2, 3):
            pg.click("#spEnroll")
            esperar(lambda: pg.wait_for_function(
                f"() => /amostra {i} registrada|perfil completo/.test("
                "document.getElementById('spProgresso').textContent)", timeout=90000),
                f"a amostra {i} ser confirmada")
            prog = pg.text_content("#spProgresso") or ""
            rotulos.append((pg.text_content("#spEnroll") or "").strip())
            print(f"  amostra {i}: {prog[:80]!r} · botão {rotulos[-1]!r}")
            cobrar(f"amostra {i}" in prog or f"{i} amostra" in prog,
                   f"amostra {i} não foi confirmada ({prog[:80]!r})")
            if i < 3:
                cobrar("perfil completo" not in prog,
                       f"a tela fechou o cadastro na amostra {i} ({prog[:80]!r})")
                cobrar("faltando" not in prog.lower() or "amostra" in prog.lower(),
                       "a tela não diz o que falta")
        prog3 = pg.text_content("#spProgresso") or ""
        cobrar("perfil completo" in prog3, f"o perfil não foi declarado completo na 3ª ({prog3[:90]!r})")
        cobrar(rotulos[0] != rotulos[2], "o botão não mudou de rótulo ao longo do cadastro")
        p2 = [p for p in perfis() if p["name"] == "perfil-fala"]
        cobrar(bool(p2) and p2[0]["samples"] == 3, f"o servidor não acumulou 3 amostras: {p2}")
        cobrar("regravar" not in (pg.text_content("#spProfiles") or ""),
               "perfil completo ganhou 'regravar'")
        cobrar("(1)" in (pg.text_content("#spProfiles") or "") or "1/3" in (pg.text_content("#spProfiles") or ""),
               "a lista não mostra o perfil 'perfil-curto' com 1 amostra")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))

        # ── 3) perfil de OUTRO extrator: aviso + regravação ──────────────────
        # (recarrega a MESMA aba: nada de gravar aqui, então o microfone falso não
        # precisa de outro navegador; o perfil antigo é semeado no estado da rodada)
        d = json.loads(SPEAKER.read_text()) if SPEAKER.exists() else {}
        d["perfil-antigo"] = {"vecs": [[0.0] * 256], "updated": 1.0, "modelo": "extrator:v0"}
        SPEAKER.write_text(json.dumps(d))
        pg.reload(wait_until="domcontentloaded")
        pg.click('.nav-item[data-view="config"]')
        pg.click('[data-stab="rede"]')
        esperar(lambda: pg.wait_for_selector(".sp-chip.recadastrar", timeout=30000),
                "a lista marcar o perfil do extrator antigo")
        chip = pg.query_selector(".sp-chip.recadastrar")
        txt = (chip.text_content() or "").strip() if chip else ""
        print(f"  perfil antigo: {txt!r}")
        cobrar("extrator:v0" in txt, f"o extrator do perfil não aparece ({txt!r})")
        cobrar("regrave" in (pg.text_content("#spProfiles") or "").lower(),
               "a lista não explica o recadastro")
        btn_reg = chip.query_selector("[data-regravar]") if chip else None
        cobrar(bool(btn_reg), "o perfil antigo não ofereceu regravar")
        if btn_reg:
            btn_reg.click()
            pg.wait_for_timeout(200)
            cobrar(pg.input_value("#spName") == "perfil-antigo",
                   f"regravar não preencheu o perfil ({pg.input_value('#spName')!r})")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

except BaseException as exc:          # exceção no meio: também guarda a rodada
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
    print("✖ cadastro de voz:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)      # só a vermelha guarda
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: log.unlink()
except Exception: pass
try: shutil.rmtree(DIR)             # verde não deixa diretório para trás
except Exception: pass
print("✔ OK — dica antes de gravar, motivo da recusa, 'usar assim mesmo' sem regravar, "
      "guia até a 3ª amostra e regravação do perfil antigo")
PYEOF
