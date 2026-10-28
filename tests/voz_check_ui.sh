#!/usr/bin/env bash
# "Testar" do perfil (#590): o veredito tem de ser o do SERVIDOR.
#
# O botão comparava `sim` (escore CRU) com o slider de rigor. Com alvo de FAR
# ligado (`speaker_far_alvo`) o gate AS-normaliza o escore e compara com o limiar
# calibrado do extrator (~2,4 em unidade normalizada contra 0,6 cru): a tela dizia
# "abaixo do rigor" para quem passava e "passa" para quem era barrado.
#
# O que se cobra aqui:
#   · com o veredito no corpo (`passaria`, ou o anterior `autorizado`) a tela usa
#     ELE — não faz conta com o slider;
#   · servidor que não manda veredito: mostra o número e NÃO afirma aprovação;
#   · modo `asnorm`: diz que o número é normalizado e que não se compara ao slider;
#   · `speaker_far_alvo` ativo: o slider fica rotulado como "só vale no modo CRU".
#
# Áudio real (microfone falso do Chromium sobre a fixture de fala) nas fases reais;
# as combinações de resposta que o servidor de hoje ainda não emite são roteadas
# (`pg.route`) para o MESMO caminho de tela. Estado em tmp (#461/#541), nada do
# dono; diretório morre no verde e sobra por idade se a rodada morrer (#562).
#
#   ./tests/voz_check_ui.sh       # instância própria, porta livre
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
FIX="$RAIZ/tests/fixtures/voz_fala_5s_16k.wav"
[ -f "$FIX" ] || { echo "✖ fixture de fala ausente ($FIX)" >&2; exit 1; }

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

for _velho in pathlib.Path("/tmp").glob("voz_check_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 7200:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"voz_check_{porta}"
(DIR / "outputs").mkdir(parents=True, exist_ok=True)
(DIR / "voices").mkdir(exist_ok=True)
SETTINGS = DIR / "settings.json"
SETTINGS.write_text("{}")
porta_stub = porta_livre()

env = {**os.environ, "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai",
       "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{porta_stub}/v1", "TTS_CHAT_MODEL": "stub-voz",
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(SETTINGS),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(DIR / "speaker.json"), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_VOICES_DIR": str(DIR / "voices"), "VOZ_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = pathlib.Path("/tmp") / f"voz_check_servidor-{porta}.log"
proc = subprocess.Popen(
    [sys.executable, "-c",
     "import os, uvicorn, app\nuvicorn.run(app.app, host='127.0.0.1', port=int(os.environ['VOZ_PORT']))"],
    cwd=str(pathlib.Path.cwd()), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
    start_new_session=True)
base = f"http://127.0.0.1:{porta}"
FIX = pathlib.Path("tests/fixtures/voz_fala_5s_16k.wav")

def espera_saude(prazo=90):
    for _ in range(prazo * 5):
        if proc.poll() is not None:
            raise SystemExit(f"servidor morreu na subida (log {log})")
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read(); return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"servidor não subiu em {prazo}s (log {log})")

def enroll(nome, wav):
    """Cadastra o perfil pela rota real (multipart) — 1 amostra basta para o check."""
    lim = "----probe590"
    corpo = (f"--{lim}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n{nome}\r\n"
             f"--{lim}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode() + pathlib.Path(wav).read_bytes() \
            + f"\r\n--{lim}--\r\n".encode()
    r = urllib.request.Request(base + "/api/speaker/enroll", data=corpo, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={lim}"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read())

def settings(corpo):
    """Grava settings pela rota (o app guarda em MEMÓRIA; escrever o arquivo não vale)."""
    r = urllib.request.Request(base + "/api/settings", data=json.dumps(corpo).encode(),
                               method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status == 200

def esperar(fn, msg, timeout=30000):
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

    # ── estático: a tela não pode voltar a comparar o cru com o slider ────────
    fonte = pathlib.Path("static/index.html").read_text()
    i = fonte.find('$("spTest")')
    bloco = fonte[i:i + 2600] if i > 0 else ""
    cobrar(bool(bloco), "o handler do spTest sumiu")
    for trecho, oque in (
        ("r.passaria", "o veredito `passaria`"),
        ("r.autorizado", "o veredito anterior `autorizado` (compatibilidade)"),
        ("não manda o veredito", "a ressalva de servidor sem veredito"),
        ("spRigorNota", "o rótulo do rigor no modo normalizado"),
    ):
        cobrar(trecho in fonte, f"a UI não tem {oque}")
    cobrar("r.sim >= lim" not in bloco,
           "o spTest voltou a comparar o escore cru com o slider")

    # perfil de verdade, cadastrado pela rota real
    r = enroll("Dono", FIX)
    cobrar(r.get("samples") == 1, f"não consegui cadastrar a amostra: {r}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        def abre(wav, rota_check=None):
            b = pw.chromium.launch(args=["--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                f"--use-file-for-fake-audio-capture={pathlib.Path(wav).resolve()}"])
            ctx = b.new_context(permissions=["microphone"])
            pg = ctx.new_page()
            erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
            if rota_check is not None:
                # 1 s por gravação: a resposta é canned, o áudio não importa aqui
                pg.route("**/api/speaker/check",
                         lambda route: route.fulfill(status=200, content_type="application/json",
                                                     body=json.dumps(rota_check)))
                pg.add_init_script(
                    "window.addEventListener('DOMContentLoaded', () => {"
                    " const o = window.spGravar; window.spGravar = (s, st) => o(1, st); });")
            pg.goto(base, wait_until="domcontentloaded")
            pg.click('.nav-item[data-view="config"]')
            pg.click('[data-stab="rede"]')
            pg.wait_for_selector("#spTest", state="visible", timeout=20000)
            return b, ctx, pg, erros

        def testar(pg):
            pg.click("#spTest")
            esperar(lambda: pg.wait_for_function(
                "() => !/gravando|comparando/.test("
                "document.getElementById('spStatus').textContent || '')",
                timeout=60000), "o resultado do Testar aparecer")
            return (pg.text_content("#spStatus") or "").strip()

        # ── 1) servidor REAL: veredito do servidor, sem conta na tela ─────────
        b, ctx, pg, erros = abre(FIX)
        pg.evaluate("() => { const o = window.spGravar; window.spGravar = (s, st) => o(5, st); }")
        txt = testar(pg)
        print(f"  servidor real: {txt[:110]!r}")
        cobrar("passaria no gate" in txt or "ficaria de fora" in txt,
               f"a tela não usou o veredito do servidor ({txt[:110]!r})")
        cobrar("rigor" in txt and "modo cru" in txt, f"o rigor/modo não foi dito ({txt[:110]!r})")
        cobrar("abaixo do rigor" not in txt and "passa no rigor atual" not in txt,
               f"sobrou o texto antigo ({txt[:110]!r})")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

        # ── 2) servidor ANTIGO (sem veredito): mostra o número, não afirma ────
        b, ctx, pg, erros = abre(FIX, {"speaker": "Dono", "sim": 0.71})
        txt = testar(pg)
        print(f"  sem veredito: {txt[:110]!r}")
        cobrar("0.71" in txt, f"o escore não apareceu ({txt[:110]!r})")
        cobrar("não manda o veredito" in txt, f"faltou a ressalva ({txt[:110]!r})")
        cobrar("passaria no gate" not in txt and "ficaria de fora" not in txt,
               f"afirmou aprovação sem veredito ({txt[:110]!r})")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

        # ── 3) modo normalizado: escala dita, limiar do servidor usado ────────
        b, ctx, pg, erros = abre(FIX, {"speaker": "Dono", "sim": 2.91, "modo": "asnorm",
                                       "limiar": 2.434, "passaria": True})
        txt = testar(pg)
        print(f"  asnorm passa: {txt[:110]!r}")
        cobrar("normalizado" in txt, f"a tela não avisou da escala normalizada ({txt[:110]!r})")
        cobrar("2.43" in txt, f"o limiar do servidor não apareceu ({txt[:110]!r})")
        cobrar("passaria no gate" in txt, f"não seguiu o veredito do servidor ({txt[:110]!r})")
        # 2.91 < slider(0.60)? não — o que importa é a comparação errada clássica:
        # sim 2.91 com slider 0.60 "passaria" de qualquer jeito, então este caso
        # também prova que o modo não é inferido do número.
        cobrar("modo asnorm" in txt, f"o modo não foi dito ({txt[:110]!r})")
        ctx.close(); b.close()

        # ── 4) modo normalizado REPROVANDO (o caso do bug: 2.0 < 2.434 mas > 0.6)
        b, ctx, pg, erros = abre(FIX, {"speaker": "Outro", "sim": 2.0, "modo": "asnorm",
                                       "limiar": 2.434, "passaria": False})
        txt = testar(pg)
        print(f"  asnorm barra: {txt[:110]!r}")
        cobrar("ficaria de fora" in txt, f"seguiu o slider em vez do servidor ({txt[:110]!r})")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

        # ── 5) `speaker_far_alvo` ativo rotula o slider como "só no modo cru" ─
        b, ctx, pg, erros = abre(FIX)
        cobrar(not pg.is_visible("#spThrNota"), "o rótulo do rigor aparece sem FAR alvo")
        cobrar(settings({"speaker_far_alvo": "1"}), "o POST das settings com FAR alvo falhou")
        pg.reload(wait_until="domcontentloaded")
        pg.click('.nav-item[data-view="config"]')
        pg.click('[data-stab="rede"]')
        esperar(lambda: pg.wait_for_selector("#spThrNota:not([hidden])", timeout=20000),
                "o rótulo do rigor com FAR alvo ativo")
        nota = (pg.text_content("#spThrNota") or "").strip()
        print(f"  rótulo com FAR alvo: {nota[:100]!r}")
        cobrar("modo CRU" in nota, f"o rótulo não diz que só vale no modo cru ({nota[:100]!r})")
        cobrar("1 %" in nota, f"o alvo de FAR não aparece no rótulo ({nota[:100]!r})")
        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
        ctx.close(); b.close()

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
    print("✖ Testar do perfil:")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: log.unlink()
except Exception: pass
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — o veredito do Testar vem do servidor (passaria/autorizado), sem veredito "
      "ele não afirma aprovação, modo normalizado é dito e o slider se rotula com FAR alvo")
PYEOF
