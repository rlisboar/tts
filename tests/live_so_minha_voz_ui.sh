#!/usr/bin/env bash
# "Entender só a minha voz" no Live e "só a voz de" nas transcrições (#963) — a
# TELA do I5 (#533) e do I6 (#534). Prova, no navegador, o que a UI faz com o
# servidor:
#   · no Live o bloco do modo nasce DESLIGADO, com rigor = os valores do servidor
#     (joelho / 1 / 0,1) e, sem perfil cadastrado, mostra o caminho ao cadastro;
#   · ligar sem perfil leva ao assistente de cadastro e grava `live_so_minha_voz`;
#     o rigor escolhido vai ao servidor;
#   · o `stats.motor.voz` chega ao OBS: "é você? ○ 0,21" + "outra voz recusada N"
#     quando recusado; "● 0,63" quando ok; some ao desligar o modo;
#   · o seletor "só a voz de" manda `speaker_alvo` no envio do STT, e sem filtro
#     o campo nem vai; separação caída aparece na tela (o texto saiu inteiro).
#
# Instância própria em tmp (estado do dono intocado). Nenhum modelo é carregado:
# o envio do STT é interceptado por `page.route`.
#
#   ./tests/live_so_minha_voz_ui.sh
#   SO_VOZ_EVIDENCIA=1 ./tests/live_so_minha_voz_ui.sh   # grava os PNGs em evidence/
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
SO_VOZ_EVIDENCIA="${SO_VOZ_EVIDENCIA:-0}" "$PY" - <<'PYEOF'
import json, os, pathlib, shutil, socket, subprocess, sys, time, traceback, urllib.request, wave

falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

# Fora da faixa do mic-router (7861-7865, #898), como nas outras suítes de UI.
def porta_livre():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]
        if not 7861 <= porta <= 7865:
            return porta

for _velho in pathlib.Path("/tmp").glob("so_voz_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 3600:
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass

porta = porta_livre()
DIR = pathlib.Path("/tmp") / f"so_voz_{porta}"
for _sub in ("voices", "outputs", "state"):
    (DIR / _sub).mkdir(parents=True, exist_ok=True)
(DIR / "settings.json").write_text("{}")
env = {**os.environ, "TTS_VOICES_DIR": str(DIR / "voices"),
       "TTS_OUTPUTS_DIR": str(DIR / "outputs"), "TTS_SETTINGS_PATH": str(DIR / "settings.json"),
       "TTS_APIKEYS_PATH": str(DIR / "apikeys.json"), "TTS_LEGACY_APIKEY_PATH": str(DIR / "apikey"),
       "TTS_SPEAKER_PATH": str(DIR / "speaker.json"), "TTS_JOBS_HIST_PATH": str(DIR / "jobs.json"),
       "TTS_METRICS_DIR": str(DIR / "state"), "VOZ_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1"}
log = DIR / "servidor.log"
proc = subprocess.Popen([sys.executable, "-c",
    "import os, uvicorn, app\nuvicorn.run(app.app, host='127.0.0.1', port=int(os.environ['VOZ_PORT']))"],
    cwd=str(pathlib.Path.cwd()), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
    start_new_session=True)
base = f"http://127.0.0.1:{porta}"

def espera_saude(prazo=120):
    for _ in range(prazo * 5):
        if proc.poll() is not None:
            raise SystemExit(f"servidor morreu na subida (log {log})")
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read(); return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"servidor não subiu em {prazo}s (log {log})")

def ajustes():
    return json.loads(urllib.request.urlopen(base + "/api/settings", timeout=10).read())

# WAV mudo de 0,5 s: o envio é interceptado, então o conteúdo só precisa ser um WAV válido
WAV = DIR / "fala.wav"
with wave.open(str(WAV), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x00" * 8000)

EVID = pathlib.Path(os.environ.get("SO_VOZ_EVIDENCIA_DIR", pathlib.Path.cwd() / "evidence"))
GRAVA_EVIDENCIA = os.environ.get("SO_VOZ_EVIDENCIA") == "1"

try:
    espera_saude()
    print(f"servidor de teste em {base}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()

        # ---- 1) Live sem perfil: bloco desligado, rigor do servidor, caminho ao cadastro
        pg = b.new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(base, wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)
        pg.click('.nav-item[data-view="live"]'); pg.wait_for_timeout(300)

        cobrar(pg.is_visible("#lxSoVozBloco"), "o bloco 'entender só a minha voz' não aparece no Live")
        cobrar(pg.is_checked("#lxSoVoz_on") is False, "o modo nasceu LIGADO")
        valores = pg.eval_on_selector_all("#lxSoVoz_rigor option", "els => els.map(e => e.value)")
        cobrar(valores == ["joelho", "1", "0.1"], f"o rigor não tem os valores do servidor: {valores}")
        cobrar(pg.is_visible("#lxSoVozSemPerfil"), "sem perfil, o modo não mostra o caminho ao cadastro")
        for sel in ("sttSoVoz", "tvSoVoz", "cvSoVoz"):
            n = pg.eval_on_selector_all(f"#{sel} option", "els => els.length")
            cobrar(n == 1, f"{sel} sem perfil devia ter só 'sem filtro de voz' e tem {n} opções")

        # ---- 2) ligar sem perfil: leva ao cadastro e grava
        pg.check("#lxSoVoz_on"); pg.wait_for_timeout(900)
        cobrar((ajustes().get("live_so_minha_voz") or {}).get("on") is True,
               "ligar o modo não gravou live_so_minha_voz.on no servidor")
        cobrar(pg.is_visible("#view-config"), "ligar sem perfil não levou ao cadastro")
        pg.click('.nav-item[data-view="live"]'); pg.wait_for_timeout(300)

        # ---- 3) rigor escolhido vai ao servidor
        pg.select_option("#lxSoVoz_rigor", "0.1"); pg.wait_for_timeout(900)
        cobrar((ajustes().get("live_so_minha_voz") or {}).get("rigor_far") == "0.1",
               "o rigor escolhido não foi gravado no servidor")

        # ---- 4) veredito do I5 no OBS (stats.motor.voz)
        def stats(ultimo, recusados):
            # o tick do OBS só existe com a sessão do Live ligada (LX.obs.tick): aqui
            # chamamos a mesma função que ele chama, depois do frame de `stats`
            pg.evaluate("""([u, n]) => {
                lxRecebe({ data: JSON.stringify({ type: "stats",
                    motor: { estado: "ok", voz: { ultimo: u, recusados: n } }, barge: { tipos: {} } }) });
                lxObsRender(); }""", [ultimo, recusados])
            pg.wait_for_timeout(200)
        def chips():
            return pg.evaluate("""() => ({
                vis: !document.getElementById('lxVozChip').hidden,
                txt: document.getElementById('lxVozChip').textContent.replace(/\\s+/g, ' ').trim(),
                warn: document.getElementById('lxVozChip').classList.contains('warn'),
                recVis: !document.getElementById('lxVozRecusChip').hidden,
                rec: document.getElementById('lxVozRecus').textContent.trim() })""")

        stats({"estado": "recusado", "score": 0.21, "janelas": 3, "latencia_ms": 40,
               "motivo": "outra voz (melhor 0.21 < 0.44)"}, 3)
        c = chips()
        cobrar(c["vis"] and "○" in c["txt"] and "0,21" in c["txt"],
               f"o veredito 'recusado' não chegou ao chip 'é você?': {c}")
        cobrar(c["recVis"] and c["rec"] == "3", f"a contagem de recusas não chegou: {c}")
        cobrar(c["warn"], "veredito 'recusado' sem tom de aviso")
        if GRAVA_EVIDENCIA:
            EVID.mkdir(parents=True, exist_ok=True)
            pg.locator("#lxSoVozBloco").screenshot(path=str(EVID / "963-so-minha-voz-bloco.png"))
            pg.locator("#view-live .lx-chips").screenshot(path=str(EVID / "963-so-minha-voz-chips.png"))

        stats({"estado": "ok", "score": 0.63, "janelas": 4, "latencia_ms": 35, "motivo": ""}, 3)
        c = chips()
        cobrar(c["vis"] and "●" in c["txt"] and "0,63" in c["txt"] and not c["warn"],
               f"o veredito 'ok' não chegou ao chip: {c}")

        # desligar o modo esconde o veredito (a contagem de recusas continua se houver)
        pg.uncheck("#lxSoVoz_on"); pg.wait_for_timeout(900)
        stats({"estado": "ok", "score": 0.63, "janelas": 4, "latencia_ms": 35, "motivo": ""}, 3)
        cobrar(chips()["vis"] is False, "com o modo desligado o chip 'é você?' continuou visível")
        cobrar((ajustes().get("live_so_minha_voz") or {}).get("on") is False,
               "desligar o modo não gravou live_so_minha_voz.on = false")

        cobrar(not erros, "erros de JS no Live: " + "; ".join(erros[:3]))

        # ---- 5) "só a voz de" manda speaker_alvo no envio do STT (página com um perfil)
        pg2 = b.new_page()
        erros2 = []; pg2.on("pageerror", lambda e: erros2.append(str(e)))
        pg2.route("**/api/speaker/profiles", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps([{"name": "dono"}])))
        envios = []
        def ao_transcrever(route, request):
            envios.append(request.post_data_buffer or b"")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "text": "oi", "language": "pt", "segments": [],
                "separacao": "indisponivel", "separacao_motivo": "modelo ausente"}))
        pg2.route("**/api/transcribe", ao_transcrever)
        pg2.goto(base, wait_until="domcontentloaded"); pg2.wait_for_timeout(1500)
        pg2.click('.nav-item[data-view="tradutor"]'); pg2.wait_for_timeout(300)

        opcoes = pg2.eval_on_selector_all("#sttSoVoz option", "els => els.map(e => e.value)")
        cobrar("dono" in opcoes, f"o seletor 'só a voz de' não lista o perfil: {opcoes}")
        # o STT mora no <details> "Transcrever arquivo · Gravar · YouTube" (fechado por padrão)
        pg2.evaluate("() => { document.querySelector('#sttSoVoz').closest('details').open = true; }")
        pg2.select_option("#sttSoVoz", "dono")
        pg2.set_input_files("#sttFile", str(WAV)); pg2.wait_for_timeout(2500)
        cobrar(len(envios) >= 1, "o envio do STT não chegou ao /api/transcribe")
        corpo = envios[-1] if envios else b""
        cobrar(b'name="speaker_alvo"' in corpo and b"dono" in corpo,
               "com 'só a voz de: dono', o envio do STT não mandou speaker_alvo=dono")
        cobrar("separação indisponivel" in pg2.evaluate("() => document.body.innerText"),
               "a separação indisponível não foi avisada na tela (o texto saiu da fala inteira)")

        pg2.select_option("#sttSoVoz", ""); n0 = len(envios)
        pg2.set_input_files("#sttFile", str(WAV)); pg2.wait_for_timeout(2500)
        cobrar(len(envios) > n0 and b'name="speaker_alvo"' not in envios[-1],
               "sem filtro de voz, o campo speaker_alvo não devia ir no envio")

        cobrar(not erros2, "erros de JS no Tradutor/STT: " + "; ".join(erros2[:3]))
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
    print("✖ só a minha voz (#963):")
    print("  log do servidor:", log)
    print("  estado da rodada preservado em", DIR)
    for f in falhas: print("  ·", f)
    raise SystemExit(1)
try: shutil.rmtree(DIR)
except Exception: pass
print("✔ OK — o modo nasce desligado com o rigor do servidor, ligar sem perfil leva ao cadastro "
      "e grava; o veredito 'é você?' chega ao OBS; 'só a voz de' manda speaker_alvo e a separação caída aparece")
PYEOF
