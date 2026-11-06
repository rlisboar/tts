#!/usr/bin/env bash
# Interrupção inteligente na UI do Live (#502) — a metade do CLIENTE do contrato
# do #501. Prova, por EFEITO e com o app de pé:
#
#   · `barge_suspeito` ABAIXA o playback ~18 dB em ≤50 ms sem parar a fila;
#   · `barge_retoma` devolve o volume (~150 ms) e a resposta segue;
#   · `interrupted` corta (e o volume não fica abaixado para o próximo turno);
#   · o TETO de 2 s devolve o volume sozinho quando o motor não decide nada;
#   · `barge_decisao` entra no log com tipo, texto, ms E o filtro que decidiu;
#   · `speech_start` durante o playback NÃO corta no modo `inteligente` (abaixa e
#     espera) e MANTÉM o corte em `imediata` — a #225 continua valendo, agora
#     dependendo do modo.
#   · o PAINEL DE FILTROS (adendo do dono): oito toggles, três números, dois
#     editores de chips, `sem_decisao` e restaurar padrões. Cobra o MERGE PARCIAL
#     (mandar um filtro não pode zerar os outros), a normalização de palavra, os
#     limites (40 chars, duplicata) e o painel TRAVADO no modo `imediata`.
#
# O motor ainda não emite estes eventos (o card irmão é o #501): o teste injeta os
# frames no MESMO `lxRecebe` que o WebSocket usa — mesmo precedente do
# `turno_pendente`/`latency` do `live_ui.sh`. O que se cobra aqui é o contrato do
# CLIENTE, não a classificação (essa é do `smoke_barge_intencao.py`).
#
# `live_interrupcao`, `live_barge_filtros` e `live_barge_sem_decisao` também ainda
# não existem no `_apply_settings`: o launcher ENVOLVE a função, valida, faz o merge
# e GRAVA O CORPO de cada POST — é o que permite cobrar o parcial e a ida-e-volta
# (POST, GET, reload). Quando o app JÁ conhece as chaves (o #501 entrou), o mesmo
# launcher só grava os corpos e o teste passa a bater no `_apply_settings` REAL.
#
# ESTADO PRÓPRIO (#541): todos os caminhos de estado vão para tmp pelas variáveis do
# #461 (`TTS_SETTINGS_PATH`, `TTS_APIKEYS_PATH`, `TTS_VOICES_DIR`, `TTS_OUTPUTS_DIR`
# e afins) — o `settings.json`/`.apikeys.json` do dono não são abertos. A suíte
# imprime sha256 e mtime deles ANTES e DEPOIS e reprova se mudarem, para a rodada
# nunca mais mexer no estado dele (antes era ele quem ganhava `live_barge_*` e podia
# ficar com o Live em `dsh`, mandando texto para fora sem o dono ter escolhido).
# Desde o #721 esse estado saiu do repositório: a guarda resolve o MESMO default do
# app (`TTS_STATE_DIR`; worktree/cópia = `<árvore>/.state-local`; raiz principal =
# Application Support) e cobre ainda o par legado da raiz. Guardar só a raiz, como
# antes, deixava a célula MUDA num worktree limpo (o arquivo nem existe lá) e o
# `antes[0]` estourava com `None` no fim; arquivo ausente nas duas pontas não é
# regressão, mas se a rodada CRIAR um deles o veredito é vermelho.
# O diretório da rodada (chaves sintéticas dentro) morre no VERDE e fica na VERMELHA
# para leitura; na abertura, uma varredura por idade recolhe o que rodadas mortas
# deixaram (#562 — antes ficavam 117 dirs e 57 `.jsonl` no /tmp até no verde).
#
# O volume é medido no ANALISADOR DA SAÍDA (o mesmo do painel OBS), não só no
# ganho: prova atenuação de áudio de verdade, não um número solto.
#
#   ./tests/live_interrupcao_ui.sh        # sobe servidor próprio em porta livre
#
# MORDIDA: tirar a chamada de `lxAbaixaVolume` do `barge_suspeito`/`speech_start`
# derruba as asserções de duck (ganho e RMS ficam em 1); mandar o objeto de filtros
# INTEIRO no lugar do parcial derruba as asserções de merge.
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

# Fora da faixa de controle do mic-router (7861-7865, #898): se o servidor do
# teste cair ali, a sonda do painel OBS fala com ele no lugar do roteador e
# leva CORS — vermelho que não existe.
def porta_livre():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]
        if not 7861 <= porta <= 7865:
            return porta

stub_porta = porta_livre()
threading.Thread(target=HTTPServer(("127.0.0.1", stub_porta), Stub).serve_forever, daemon=True).start()

# ─── servidor próprio, com ESTADO PRÓPRIO (#541/#461) ───────────────────────
# A suíte grava em `/api/settings`; sem override, cada rodada reescrevia o
# `settings.json` do DONO (e o dono num rev com o #509 sairia falando por `dsh`,
# ou seja, com o texto do trecho indo para fora durante a rodada). Todos os
# caminhos de estado vão para tmp — o arquivo do dono nem é aberto.
# #562: o diretório da rodada (chaves sintéticas dentro) e o gravador de POSTs
# ficavam no /tmp até no verde — 117 dirs e 57 jsonl acumulados. Agora tudo mora
# em DIR_TMP e o verde apaga; a varredura por IDADE recolhe o que ficou de
# rodadas mortas, sem tocar em rodada viva (que acabou de criar o diretório).
import shutil, tempfile
for _velho in pathlib.Path("/tmp").glob("live_interrupcao_*"):
    try:
        if time.time() - _velho.stat().st_mtime > 7200:      # 2 h
            shutil.rmtree(_velho) if _velho.is_dir() else _velho.unlink()
    except Exception:
        pass
porta = porta_livre()
DIR_TMP = pathlib.Path("/tmp") / f"live_interrupcao_{porta}"
(DIR_TMP / "voices").mkdir(parents=True, exist_ok=True)
(DIR_TMP / "outputs").mkdir(parents=True, exist_ok=True)
SETTINGS_TESTE = DIR_TMP / "settings.json"
SETTINGS_TESTE.write_text("{}")     # vazio: valem os defaults do app
RAIZ_REPO = pathlib.Path.cwd()

# Onde o app escreveria SEM os overrides acima (#721) — a MESMA regra do
# `_dir_de_estado_padrao` do app: `TTS_STATE_DIR` vence; worktree/cópia (`.git`
# ARQUIVO) usa `<árvore>/.state-local`; só a RAIZ PRINCIPAL usa o Application
# Support. Apontar para a raiz do repo (como antes) deixava a guarda cega no
# worktree, onde o arquivo não existe.
def _estado_padrao():
    bruto = (os.environ.get("TTS_STATE_DIR") or "").strip()
    if bruto:
        return pathlib.Path(bruto).expanduser()
    if not (RAIZ_REPO / ".git").is_dir():
        return RAIZ_REPO / ".state-local"
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "tts-studio"
    return pathlib.Path(os.environ.get("XDG_STATE_HOME")
                        or pathlib.Path.home() / ".local" / "state") / "tts-studio"

ESTADO_PADRAO = _estado_padrao()
# (rótulo, caminho, reprova?) — `settings.json` no default é o arquivo que a
# suíte escreveria se o isolamento falhasse; `.apikeys.json` também é escrito por
# outras suítes, então só avisa. O par LEGADO na raiz fica coberto enquanto um
# checkout antigo o tiver.
GUARDADOS = [(f"settings.json ({ESTADO_PADRAO})", ESTADO_PADRAO / "settings.json", True),
             (f".apikeys.json ({ESTADO_PADRAO})", ESTADO_PADRAO / ".apikeys.json", False),
             ("settings.json (legado na raiz)", RAIZ_REPO / "settings.json", True),
             (".apikeys.json (legado na raiz)", RAIZ_REPO / ".apikeys.json", False)]

def _impressao(caminho):
    """sha256 + mtime — é a prova de que o arquivo do dono não foi tocado."""
    import hashlib
    if not caminho.exists():
        return None
    st = caminho.stat()
    return (hashlib.sha256(caminho.read_bytes()).hexdigest(), int(st.st_mtime_ns))

ANTES = {str(c): _impressao(c) for _r, c, _f in GUARDADOS}
print("  dono antes:", ANTES)

env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
       "TTS_SETTINGS_PATH": str(SETTINGS_TESTE),
       "TTS_APIKEYS_PATH": str(DIR_TMP / "apikeys.json"),
       "TTS_LEGACY_APIKEY_PATH": str(DIR_TMP / "apikey"),
       "TTS_VOICES_DIR": str(DIR_TMP / "voices"),
       "TTS_OUTPUTS_DIR": str(DIR_TMP / "outputs"),
       "TTS_SPEAKER_PATH": str(DIR_TMP / "speaker.json"),
       "TTS_JOBS_HIST_PATH": str(DIR_TMP / "jobs-history.json"),
       "TTS_CHAT_MODEL": "stub-int",
       # pinados: o dono pode estar em `chat_backend: "dsh"` — e o do LIVE tem
       # variável própria, não governada por esta (#143/#195)
       "TTS_CHAT_BACKEND": "openai",
       "TTS_CHAT_BACKEND_LIVE": os.environ.get("TTS_CHAT_BACKEND_LIVE") or "openai",
       "LIVE_INT_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1",
       "LIVE_INT_POSTS": str(DIR_TMP / "posts.jsonl")}
log = pathlib.Path("/tmp") / f"live_interrupcao_servidor-{porta}.log"
# STUB DO LADO DO SERVIDOR para o que ainda não existe no app: `live_interrupcao`,
# `live_barge_filtros` e `live_barge_sem_decisao` (são do card irmão, #501).
# Envolver `_apply_settings` faz o POST da tela VALER — é o que permite provar
# ida-e-volta (grava, recarrega, a tela volta no estado) sem esperar o motor, e
# GUARDAR O CORPO de cada POST para conferir o MERGE PARCIAL (mandar
# `{pergunta:{on:false}}` não pode zerar os outros filtros). Validação e clamp
# aqui são do stub; os definitivos são do `_apply_settings` do #501.
_launcher = '''
import inspect, json, os, unicodedata, uvicorn, app
_MODOS = ("inteligente", "imediata", "so_comando")
_SEM = ("continua", "interrompe")     # é o que o motor lê (app.py: "interrompe")
_NUM = {"frase_longa": ("min_palavras", 1, 20), "fala_sustentada": ("min_ms", 400, 5000),
        "fim_de_resposta": ("restante_ms", 0, 3000)}
_LISTA = ("palavras_corte", "backchannel")
_POSTS = os.environ["LIVE_INT_POSTS"]

def _norm(t):
    t = " ".join(str(t or "").split()).strip()[:40]
    return unicodedata.normalize("NFD", t.lower()).encode("ascii", "ignore").decode()

def _merge_filtros(patch):
    atual = app._settings.setdefault("live_barge_filtros", {})
    for nome, cfg in (patch or {}).items():
        if nome not in _NUM and nome not in _LISTA and nome not in (
                "pergunta", "ruido", "eco", "modelo"):
            continue
        alvo = atual.setdefault(nome, {})
        if "on" in cfg or nome == "modelo":     # `modelo` nasce DESLIGADO (#509)
            alvo["on"] = bool(cfg.get("on", False))
        if nome == "modelo":
            alvo["provedor"] = "dsh" if str(cfg.get("provedor") or "local") == "dsh" else "local"
            try: alvo["limiar"] = min(0.95, max(0.05, float(cfg.get("limiar", 0.5))))
            except (TypeError, ValueError): alvo["limiar"] = 0.5
            try: alvo["timeout_ms"] = min(3000, max(100, int(cfg.get("timeout_ms", 400))))
            except (TypeError, ValueError): alvo["timeout_ms"] = 400
        if nome in _NUM and _NUM[nome][0] in cfg:
            campo, lo, hi = _NUM[nome]
            try: alvo[campo] = min(hi, max(lo, int(cfg[campo])))
            except (TypeError, ValueError): pass
        if nome in _LISTA and "palavras" in cfg:
            vistos = []
            for item in list(cfg["palavras"])[:100]:
                n = _norm(item)
                if n and n not in vistos: vistos.append(n)
            alvo["palavras"] = vistos

# O stub só SERVE as chaves se o app ainda não as conhecer (o card irmão #501 pode
# já ter entrado). O gravador de corpos fica SEMPRE, porque o merge parcial precisa
# ser conferido nos dois casos.
_SUPORTE = "live_barge_filtros" in inspect.getsource(app._apply_settings)
_orig = app._apply_settings
def _com_modo(payload):
    r = _orig(payload)
    with open(_POSTS, "a") as fh:      # o corpo CRU de cada POST, para o teste
        fh.write(json.dumps(payload) + "\\n")
    if not _SUPORTE:
        if "live_interrupcao" in payload:
            v = payload.get("live_interrupcao")
            app._settings["live_interrupcao"] = v if v in _MODOS else "inteligente"
        if "live_barge_sem_decisao" in payload:
            v = payload.get("live_barge_sem_decisao")
            app._settings["live_barge_sem_decisao"] = v if v in _SEM else "continua"
        if "live_barge_filtros" in payload:
            _merge_filtros(payload.get("live_barge_filtros"))
    if app._settings.get("__sem_modelo"):     # simula servidor SEM o filtro `modelo`
        (app._settings.get("live_barge_filtros") or {}).pop("modelo", None)
    return r
app._apply_settings = _com_modo

# escreve direto nas settings, sem validação. Vai INSERIDO na frente das rotas:
# o mount estático em "/" é anterior e responderia 405 a um POST novo.
from starlette.responses import JSONResponse
from starlette.routing import Route
async def _set(request):
    app._settings.update(await request.json() or {})
    return JSONResponse({})
app.app.router.routes.insert(0, Route("/__set", _set, methods=["POST"]))

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
    print("  settings do barge: "
          + ("o app JÁ conhece as chaves (só o gravador de corpo é do teste)"
             if "live_barge_filtros" in pathlib.Path("app.py").read_text()
             else "app antigo — o teste SERVE as chaves"))
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

        # ─── o SELETOR: só dois modos (`so_comando` virou preset de filtros) ──
        sel = pg.evaluate("""() => {
            const s = document.getElementById('lxInterrupcao');
            return s ? { valor: s.value, opcoes: [...s.options].map(o => o.value),
                         titulo: s.title, rotulos: [...s.options].map(o => o.textContent) } : null;
        }""")
        print("  seletor:", sel)
        cobrar(sel is not None, "seletor #lxInterrupcao não existe no painel do Live")
        if sel:
            cobrar(sel["opcoes"] == ["inteligente", "imediata"],
                   f"opções do seletor fora do contrato de dois modos: {sel['opcoes']}")
            cobrar("Inteligente" in sel["titulo"] and "Imediata" in sel["titulo"]
                   and "filtros" in sel["titulo"],
                   f"tooltip não explica as opções: {sel['titulo']!r}")

        # ─── o PAINEL DE FILTROS: um toggle por filtro, números e chips ──────
        # Baseline determinístico: quando o app conhece as chaves, rodadas
        # anteriores deixam valores no settings.json real — o painel volta ao
        # padrão aqui, antes de qualquer medição (o servidor é alinhado no bloco 7).
        pg.evaluate("() => lxFiltrosAplica()")
        painel = pg.evaluate("""() => {
            const p = document.getElementById('lxFiltros');
            if (!p) return null;
            const toggles = [...p.querySelectorAll('input[type=checkbox]')].map(i => i.id);
            return { toggles, nums: [...p.querySelectorAll('input[type=number]')].map(i => i.id),
                     chips: [...p.querySelectorAll('.lx-chips-edit')].map(c => c.id),
                     sem: (document.getElementById('lxSemDecisao') || {}).value,
                     padrao: !!document.getElementById('lxFiltrosPadrao'),
                     preset: !!document.getElementById('lxFiltrosPreset'),
                     recolhido: !p.open,
                     palavras_corte: LX.filtros.palavras_corte.palavras.length,
                     backchannel: LX.filtros.backchannel.palavras.length,
                     modelo: LX.filtros.modelo.on,
                     modelo_visivel: !document.getElementById('lxFiltroModelo').hidden,
                     modelo_campos: [document.getElementById('lxProv_modelo').value,
                                     document.getElementById('lxNum_modelo_limiar').value,
                                     document.getElementById('lxNum_modelo_timeout').value],
                     egress: !document.getElementById('lxEgress_modelo').hidden };
        }""")
        print(f"  painel de filtros: {painel}")
        cobrar(painel is not None, "painel #lxFiltros não existe")
        if painel:
            cobrar(len(painel["toggles"]) == 9, f"faltam toggles de filtro: {painel['toggles']}")
            cobrar(sorted(painel["toggles"]) == sorted(
                "lxLiga_" + f for f in ("palavras_corte", "pergunta", "frase_longa",
                                        "fala_sustentada", "backchannel", "ruido", "eco",
                                        "modelo", "fim_de_resposta")),
                f"toggles fora do schema: {painel['toggles']}")
            cobrar(painel["modelo"] is False, "o filtro modelo deveria nascer DESLIGADO")
            cobrar(painel["modelo_visivel"], "o filtro `modelo` não apareceu com o servidor servindo o campo")
            cobrar(painel["modelo_campos"] == ["local", "0.5", "400"],
                   f"padrões do modelo errados (provedor/limiar/timeout): {painel['modelo_campos']}")
            cobrar(not painel["egress"], "aviso de egress apareceu com provedor local")
            cobrar(len(painel["nums"]) == 5, f"números editáveis faltando: {painel['nums']}")
            cobrar(len(painel["chips"]) == 2, f"editores de chips faltando: {painel['chips']}")
            cobrar(painel["sem"] == "continua", f"sem_decisao não nasce em 'continua': {painel['sem']}")
            cobrar(painel["padrao"], "botão de restaurar padrões ausente")
            cobrar(painel["preset"], "preset 'só com comando' ausente")
            cobrar(painel["recolhido"], "painel deveria nascer recolhido")
            cobrar(painel["palavras_corte"] > 0 and painel["backchannel"] > 0,
                   "listas padrão não vieram preenchidas")

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

        def espera_ganho(alvo, prazo_ms=3500, abaixo=False):
            """Espera o ganho CRUZAR o alvo e devolve o tempo. Sob contenção o
            `currentTime` do AudioContext anda por saltos, então amostrar um
            intervalo fixo dava falso vermelho (medido 0,157 e 0,405 em 400 ms)."""
            return pg.evaluate("""async ([alvo, prazo, abaixo]) => {
                const t0 = performance.now();
                while (performance.now() - t0 < prazo) {
                    const g = LX.duck.gain.value;
                    if (abaixo ? g <= alvo : g >= alvo) return performance.now() - t0;
                    await new Promise(r => setTimeout(r, 5));
                }
                return null;
            }""", [alvo, prazo_ms, abaixo])

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
        # O relógio começa no PRÓPRIO duck (medir depois da rodada anterior daria
        # menos que 2 s, porque o prazo já estava correndo).
        t_teto = pg.evaluate("""async () => {
            lxCancelaPlayback();
            const n = 24000 * 6, i = new Int16Array(n);
            for (let k = 0; k < n; k++) i[k] = Math.round(Math.sin(2 * Math.PI * 440 * k / 24000) * 3200);
            lxToca(i);
            lxRecebe({ data: JSON.stringify({ type: 'barge_suspeito', turn: 99 }) });
            // espera o duck se ESTABELECER primeiro (o ganho ainda está em 1 no
            // instante do evento) e só então conta o prazo do teto
            const limite = performance.now() + 300;
            while (performance.now() < limite && LX.duck.gain.value > 0.2)
                await new Promise(r => setTimeout(r, 5));
            const t0 = performance.now();
            while (performance.now() - t0 < 4000) {
                if (LX.duck.gain.value >= 0.99) return performance.now() - t0;
                await new Promise(r => setTimeout(r, 5));
            }
            return null;
        }""")
        log_teto = pg.evaluate("() => document.getElementById('lxLog').textContent")
        print(f"  teto de 2 s: volume voltou em {t_teto} ms · aviso no log={'2 s sem decisão' in log_teto}")
        cobrar(t_teto is not None, "volume não voltou sozinho depois do teto de 2 s")
        cobrar(t_teto is not None and t_teto >= 1900,
               f"volume voltou ANTES do teto de 2 s ({t_teto} ms)")
        cobrar("2 s sem decisão" in log_teto, "o teto de 2 s não avisa no log")
        cobrar(pg.evaluate("() => document.getElementById('lxDuck').hidden"),
               "chip de volume abaixado ficou aceso depois do teto")

        # ─── 3) `barge_suspeito` + `barge_retoma`: abaixa e DEVOLVE ───────────
        prepara(); pg.wait_for_timeout(250)
        r3 = dispara({"type": "barge_suspeito", "turn": 7}, 200)
        t3 = primeiro(r3["am"], 0.2)
        cobrar(t3 is not None and t3 <= 70, f"barge_suspeito não abaixou em ≤50 ms (medido {t3} ms)")
        cobrar(r3["ativos"] > 0, "barge_suspeito parou a fila (devia manter tocando)")
        r4 = dispara({"type": "barge_retoma", "turn": 7, "tipo": "backchannel"}, 1200)
        t4 = primeiro(r4["am"], 0.99, abaixo=False)
        log3 = pg.evaluate("() => document.getElementById('lxLog').textContent")
        print(f"  barge_suspeito/retoma: duck em {t3} ms · retoma em {t4} ms · log='↩ continuei' {'in' if '↩ continuei — era backchannel' in log3 else 'NÃO'} · fila {r4['ativos']}")
        cobrar(t4 is not None and t4 <= 400, f"barge_retoma não devolveu o volume (medido {t4} ms)")
        cobrar("↩ continuei — era backchannel" in log3,
               "log do barge_retoma não diz o que era (esperado '↩ continuei — era backchannel')")
        cobrar(r4["ativos"] > 0, "barge_retoma não deixou a resposta tocando")

        # ─── 4) `barge_decisao` no log: tipo, texto e ms ─────────────────────
        pg.evaluate("""() => lxRecebe({ data: JSON.stringify({ type: 'barge_decisao', turn: 7,
            tipo: 'backchannel', filtro: 'backchannel', texto: 'uhum', fala_ms: 380, decisao_ms: 240 }) })""")
        decisao = pg.evaluate("() => document.getElementById('lxLog').textContent")
        print("  log da decisão:",
              [ln for ln in decisao.split("decisão")[-1:]][0].splitlines()[0] if "decisão" in decisao else "(ausente)")
        cobrar("backchannel" in decisao and "uhum" in decisao and "240 ms" in decisao,
               f"barge_decisao não entra no log com tipo/texto/ms: {decisao[-160:]!r}")
        cobrar("filtro backchannel" in decisao,
               f"barge_decisao não diz QUAL filtro decidiu: {decisao[-160:]!r}")

        # ─── 5) `interrupted` corta e o volume NÃO fica abaixado ─────────────
        prepara(); pg.wait_for_timeout(250)
        dispara({"type": "barge_suspeito", "turn": 8}, 120)
        r5 = pg.evaluate("""() => { lxRecebe({ data: JSON.stringify({ type: 'interrupted', turn: 8 }) });
                                     return LX.ativos.length; }""")
        t5 = espera_ganho(0.99, 3000)
        print(f"  interrupted: fila {r5} · volume devolvido em {t5} ms")
        cobrar(r5 == 0, "interrupted não cortou a fila")
        cobrar(t5 is not None,
               f"volume ficou abaixado depois do corte (ganho {pg.evaluate('() => LX.duck.gain.value')}) — o próximo turno nasceria mudo")

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
        # no imediata os filtros não decidem nada: o painel tem de dizer isso
        travado = pg.evaluate("""() => ({ classe: document.getElementById('lxFiltros').classList.contains('travado'),
            desligado: document.getElementById('lxLiga_pergunta').disabled,
            dica: document.getElementById('lxFiltrosDica').textContent })""")
        print("  painel no modo imediata:", travado)
        cobrar(travado["classe"] and travado["desligado"], "painel de filtros não travou no modo imediata")
        cobrar("não decidem nada" in travado["dica"], f"dica não explica o travamento: {travado['dica']!r}")

        # ─── 7) FILTROS: edição, merge parcial, normalização e limites ───────
        # o painel é recolhido por padrão: abrir é preciso para clicar de verdade
        # (o que `select_option`/`click` exigem — os toggles daqui em diante são
        # acionados por JS só onde o alvo é o handler, não a visibilidade)
        pg.evaluate("() => { document.getElementById('lxFiltros').open = true; }")
        pg.select_option("#lxInterrupcao", "inteligente")
        pg.wait_for_timeout(200)

        def posts():
            """Corpos CRUS recebidos pelo stub — é o merge parcial que se cobra."""
            arq = pathlib.Path(env["LIVE_INT_POSTS"])
            if not arq.exists(): return []
            return [json.loads(l) for l in arq.read_text().splitlines() if l.strip()]

        def settings():
            with urllib.request.urlopen(base + "/api/settings", timeout=5) as r:
                return json.loads(r.read() or b"{}")

        def limpa_posts():
            arq = pathlib.Path(env["LIVE_INT_POSTS"])
            if arq.exists(): arq.unlink()

        # estado conhecido: grava os PADRÕES inteiros no servidor primeiro. Sem
        # isso o servidor teria só o que o parcial mandasse e "não zerou o resto"
        # não estaria sendo medido.
        pg.evaluate("() => document.getElementById('lxFiltrosPadrao').click()")
        pg.wait_for_timeout(800)
        limpa_posts()
        # desligar UM filtro manda só ele
        pg.evaluate("() => { const c = document.getElementById('lxLiga_pergunta'); c.checked = false; c.onchange(); }")
        pg.wait_for_timeout(700)
        corpo = posts()[-1] if posts() else {}
        estado = settings()
        print("  toggle pergunta off ->", corpo.get("live_barge_filtros"))
        cobrar(list((corpo.get("live_barge_filtros") or {}).keys()) == ["pergunta"],
               f"falta de merge parcial: o POST levou {corpo.get('live_barge_filtros')}")
        filtros_srv = estado.get("live_barge_filtros") or {}
        cobrar(filtros_srv.get("pergunta", {}).get("on") is False, "o filtro não ficou desligado no servidor")
        for outro in ("frase_longa", "palavras_corte", "backchannel", "eco", "ruido"):
            cobrar(filtros_srv.get(outro, {}).get("on") is True, f"o filtro {outro} foi zerado junto")
        cobrar(len((filtros_srv.get("palavras_corte") or {}).get("palavras") or []) > 0,
               "a lista de palavras_corte se perdeu no merge")

        # número: manda só o campo dele e o clamp vale
        limpa_posts()
        pg.evaluate("""() => { const c = document.getElementById('lxNum_frase_longa_min_palavras');
                                c.value = 9; c.onchange(); }""")
        pg.wait_for_timeout(700)
        corpo = posts()[-1] if posts() else {}
        print("  número frase_longa ->", corpo.get("live_barge_filtros"))
        cobrar((corpo.get("live_barge_filtros") or {}).get("frase_longa") == {"min_palavras": 9},
               f"número não mandou só o próprio campo: {corpo.get('live_barge_filtros')}")
        cobrar((settings().get("live_barge_filtros") or {}).get("frase_longa", {}).get("min_palavras") == 9,
               "min_palavras não chegou ao servidor")
        pg.evaluate("""() => { const c = document.getElementById('lxNum_frase_longa_min_palavras');
                                c.value = 99; c.onchange(); }""")
        pg.wait_for_timeout(400)
        print("  clamp de 99 ->", pg.evaluate("() => document.getElementById('lxNum_frase_longa_min_palavras').value"))
        cobrar(pg.evaluate("() => document.getElementById('lxNum_frase_longa_min_palavras').value") == "20",
               "valor acima do teto não foi limitado a 20")

        # palavra nova: normalizada (maiúscula/acento), entra no filtro e o liga
        pg.evaluate("""() => { const c = document.getElementById('lxLiga_palavras_corte');
                                c.checked = false; c.onchange(); }""")
        pg.wait_for_timeout(400)
        limpa_posts()
        pg.evaluate("""() => { const c = document.getElementById('lxNova_palavras_corte');
                                c.value = '  BASTA '; c.onkeydown({ key: 'Enter', preventDefault(){} }); }""")
        pg.wait_for_timeout(700)
        corpo = posts()[-1] if posts() else {}
        lista = (corpo.get("live_barge_filtros") or {}).get("palavras_corte") or {}
        print("  palavra nova ->", lista)
        cobrar("basta" in (lista.get("palavras") or []), f"palavra não normalizada/gravada: {lista}")
        cobrar(lista.get("on") is True, "cadastrar palavra não religou o filtro")
        servida = (settings().get("live_barge_filtros") or {}).get("palavras_corte") or {}
        cobrar("basta" in (servida.get("palavras") or []), "a palavra nova não chegou ao servidor")
        cobrar(servida.get("palavras", []).count("basta") == 1, "palavra duplicada no servidor")

        # duplicata não entra duas vezes
        quantas = len(servida.get("palavras") or [])
        pg.evaluate("""() => { const c = document.getElementById('lxNova_palavras_corte');
                                c.value = 'basta'; c.onkeydown({ key: 'Enter', preventDefault(){} }); }""")
        pg.wait_for_timeout(500)
        print("  duplicata:", len((settings().get("live_barge_filtros") or {})
                                  .get("palavras_corte", {}).get("palavras") or []), "itens")
        cobrar(len((settings().get("live_barge_filtros") or {}).get("palavras_corte", {}).get("palavras") or [])
               == quantas, "a duplicata entrou na lista")

        # limite: palavra de 50 chars vira 40 na tela e no servidor
        pg.evaluate("""() => { const c = document.getElementById('lxNova_palavras_corte');
                                c.value = 'a'.repeat(50); c.onkeydown({ key: 'Enter', preventDefault(){} }); }""")
        pg.wait_for_timeout(500)
        longas = ((settings().get("live_barge_filtros") or {})
                  .get("palavras_corte", {}).get("palavras") or [])
        print("  palavras de 40+:", [p for p in longas if len(p) >= 40])
        cobrar(any(len(p) == 40 for p in longas), "palavra gigante não foi cortada em 40 chars")

        # tirar pelo × do chip
        antes = len(longas)
        pg.evaluate("""() => { const c = document.querySelector('#lxChips_palavras_corte button[data-palavra="basta"]');
                                c.click(); }""")
        pg.wait_for_timeout(500)
        depois = (settings().get("live_barge_filtros") or {}).get("palavras_corte", {}).get("palavras") or []
        print(f"  remoção pelo ×: {antes} -> {len(depois)}")
        cobrar("basta" not in depois, "o × do chip não removeu a palavra")

        # dois patches no MESMO filtro dentro da janela de 350 ms viram UM corpo,
        # sem perder o primeiro. Campos DISJUNTOS de propósito (número e depois
        # `on`): com merge raso o segundo apagaria o primeiro e o número nunca
        # chegaria ao servidor.
        limpa_posts()
        pg.evaluate("""() => {
            const n = document.getElementById('lxNum_frase_longa_min_palavras');
            n.value = 7; n.onchange();
            const c = document.getElementById('lxLiga_frase_longa');
            c.checked = false; c.onchange();
        }""")
        pg.wait_for_timeout(800)
        corpo = posts()[-1] if posts() else {}
        juntos = (corpo.get("live_barge_filtros") or {}).get("frase_longa") or {}
        print("  dois patches juntos ->", juntos)
        cobrar(juntos.get("min_palavras") == 7, f"o patch do 'on' apagou o número: {juntos}")
        cobrar(juntos.get("on") is False, f"o patch do número perdeu o 'on': {juntos}")
        no_srv = (settings().get("live_barge_filtros") or {}).get("frase_longa") or {}
        cobrar(no_srv.get("min_palavras") == 7 and no_srv.get("on") is False,
               f"o servidor não recebeu os dois campos juntos: {no_srv}")

        # ─── o filtro `modelo`: provedor, aviso de egress e prazos ───────────
        limpa_posts()
        pg.evaluate("""() => { const c = document.getElementById('lxLiga_modelo');
                                c.checked = true; c.onchange(); }""")
        pg.wait_for_timeout(600)
        corpo = posts()[-1] if posts() else {}
        print("  modelo ligado ->", (corpo.get("live_barge_filtros") or {}).get("modelo"))
        cobrar((corpo.get("live_barge_filtros") or {}).get("modelo") == {"on": True},
               f"ligar o modelo não mandou só o `on`: {corpo.get('live_barge_filtros')}")
        limpa_posts()
        pg.select_option("#lxProv_modelo", "dsh")
        pg.wait_for_timeout(600)
        corpo = posts()[-1] if posts() else {}
        egress = pg.evaluate("""() => ({ visivel: !document.getElementById('lxEgress_modelo').hidden,
                                          texto: document.getElementById('lxEgress_modelo').textContent })""")
        print("  provedor dsh ->", (corpo.get("live_barge_filtros") or {}).get("modelo"), "· egress:", egress)
        cobrar((corpo.get("live_barge_filtros") or {}).get("modelo") == {"provedor": "dsh"},
               f"trocar o provedor não mandou só o `provedor`: {corpo.get('live_barge_filtros')}")
        cobrar(egress["visivel"] and "sai da máquina" in egress["texto"],
               f"aviso de egress não apareceu com dsh: {egress}")
        # limiar é float e tem teto
        pg.evaluate("""() => { const c = document.getElementById('lxNum_modelo_limiar');
                                c.value = 0.85; c.onchange(); }""")
        pg.wait_for_timeout(500)
        pg.evaluate("""() => { const c = document.getElementById('lxNum_modelo_limiar');
                                c.value = 3; c.onchange(); }""")
        pg.wait_for_timeout(500)
        limiar = pg.evaluate("() => document.getElementById('lxNum_modelo_limiar').value")
        servido = (settings().get("live_barge_filtros") or {}).get("modelo") or {}
        print("  limiar 0.85 e depois 3 (teto) ->", limiar, "· no servidor:", servido.get("limiar"))
        cobrar(limiar == "0.95" and servido.get("limiar") == 0.95,
               f"limiar não foi clampado/gravado como float: tela={limiar} servidor={servido.get('limiar')}")
        # clamps do SERVIDOR nos outros dois campos (o GET não publica limites;
        # os números da tela espelham o `_normaliza_filtro_barge`)
        pg.evaluate("""() => { const c = document.getElementById('lxNum_fala_sustentada_min_ms');
                                c.value = 100; c.onchange(); }""")
        pg.wait_for_timeout(500)
        cobrar(pg.evaluate("() => document.getElementById('lxNum_fala_sustentada_min_ms').value") == "150",
               "min_ms abaixo do piso do servidor (150) não foi limitado")
        pg.evaluate("""() => { const c = document.getElementById('lxNum_fim_de_resposta_restante_ms');
                                c.value = 6000; c.onchange(); }""")
        pg.wait_for_timeout(500)
        cobrar(pg.evaluate("() => document.getElementById('lxNum_fim_de_resposta_restante_ms').value") == "5000",
               "restante_ms acima do teto do servidor (5000) não foi limitado")
        # timeout: teto de 3000
        pg.evaluate("""() => { const c = document.getElementById('lxNum_modelo_timeout');
                                c.value = 9999; c.onchange(); }""")
        pg.wait_for_timeout(500)
        cobrar(pg.evaluate("() => document.getElementById('lxNum_modelo_timeout').value") == "3000",
               "timeout acima do teto não foi limitado a 3000")

        # ─── FEATURE-DETECT: servidor sem o campo `modelo` esconde o ajuste ───
        pg.evaluate("""() => fetch('/__set', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ __sem_modelo: 1 }) })""")
        pg.wait_for_timeout(300)
        pg.evaluate("""() => { const c = document.getElementById('lxLiga_modelo');
                                c.checked = true; c.onchange(); }""")
        pg.wait_for_timeout(800)
        oculto = pg.evaluate("""() => ({ escondido: document.getElementById('lxFiltroModelo').hidden,
                                          status: document.getElementById('lxFiltrosStatus').textContent })""")
        print("  servidor sem o campo modelo ->", oculto)
        cobrar(oculto["escondido"], f"o ajuste não sumiu com servidor sem o campo: {oculto}")
        cobrar("escondi" in oculto["status"], f"a tela não disse que escondeu: {oculto['status']!r}")
        # e o payload deixa de levar o `modelo`
        limpa_posts()
        pg.evaluate("() => document.getElementById('lxFiltrosPadrao').click()")
        pg.wait_for_timeout(800)
        corpo = posts()[-1] if posts() else {}
        print("  payload do restaurar sem o modelo:", "modelo" not in (corpo.get("live_barge_filtros") or {}))
        cobrar("modelo" not in (corpo.get("live_barge_filtros") or {}),
               "payload continuou mandando `modelo` com o ajuste escondido")
        # servidor de volta: o load reexibe
        pg.evaluate("""() => fetch('/__set', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ __sem_modelo: 0 }) })""")
        pg.reload(wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.wait_for_function("() => typeof LX === 'object'", timeout=20000)
        pg.wait_for_timeout(900)
        de_volta = pg.evaluate("() => !document.getElementById('lxFiltroModelo').hidden")
        print("  modelo de volta no load:", de_volta)
        cobrar(de_volta, "o ajuste não voltou quando o servidor passou a servir o campo")
        pg.evaluate("() => { document.getElementById('lxFiltros').open = true; }")   # o reload fecha o details

        # ─── PRESET `so_comando` (o modo que saiu do enum): só palavra corta ──
        limpa_posts()
        pg.evaluate("() => document.getElementById('lxFiltrosPreset').click()")
        pg.wait_for_timeout(800)
        corpo = posts()[-1] if posts() else {}
        filtros_preset = corpo.get("live_barge_filtros") or {}
        print("  preset só-com-comando ->",
              {k: v.get("on") for k, v in filtros_preset.items()}, "· sem_decisao:", corpo.get("live_barge_sem_decisao"))
        for corte in ("pergunta", "frase_longa", "fala_sustentada", "modelo"):
            cobrar(filtros_preset.get(corte, {}).get("on") is False, f"o preset deixou o corte {corte} ligado")
        cobrar(filtros_preset.get("palavras_corte", {}).get("on") is True,
               "o preset desligou o único corte que devia sobrar (palavras_corte)")
        for continua in ("backchannel", "ruido", "eco", "fim_de_resposta"):
            cobrar(filtros_preset.get(continua, {}).get("on") is True, f"o preset desligou o filtro {continua}")
        cobrar(corpo.get("live_barge_sem_decisao") == "continua", "o preset não deixou o 'sem decisão' em continuar")
        servido_preset = (settings().get("live_barge_filtros") or {})
        cobrar(servido_preset.get("pergunta", {}).get("on") is False
               and servido_preset.get("palavras_corte", {}).get("on") is True,
               f"o preset não chegou ao servidor: {servido_preset}")
        cobrar(len(servido_preset.get("palavras_corte", {}).get("palavras") or []) > 0,
               "o preset apagou as palavras cadastradas")

        # sem decisão
        limpa_posts()
        pg.select_option("#lxSemDecisao", "interrompe")
        pg.wait_for_timeout(700)
        corpo = posts()[-1] if posts() else {}
        print("  sem_decisao ->", corpo.get("live_barge_sem_decisao"))
        cobrar(corpo.get("live_barge_sem_decisao") == "interrompe",
               f"sem_decisao não foi mandado sozinho: {corpo}")
        cobrar(settings().get("live_barge_sem_decisao") == "interrompe",
               "sem_decisao não chegou ao servidor com o valor que o motor lê")
        cobrar("corta" in pg.evaluate("() => document.getElementById('lxFiltrosResumo').textContent"),
               "resumo do painel não mostra o 'sem decisão'")

        # restaurar padrões devolve o schema e o padrão de sem_decisao
        pg.evaluate("() => document.getElementById('lxFiltrosPadrao').click()")
        pg.wait_for_timeout(800)
        restaurado = settings()
        print("  padrões: sem_decisao=", restaurado.get("live_barge_sem_decisao"),
              "· pergunta.on=", (restaurado.get("live_barge_filtros") or {}).get("pergunta", {}).get("on"),
              "· frase_longa.min_palavras=",
              (restaurado.get("live_barge_filtros") or {}).get("frase_longa", {}).get("min_palavras"))
        cobrar(restaurado.get("live_barge_sem_decisao") == "continua", "restaurar padrões não voltou o sem_decisao")
        cobrar((restaurado.get("live_barge_filtros") or {}).get("pergunta", {}).get("on") is True,
               "restaurar padrões não religou o filtro pergunta")
        cobrar((restaurado.get("live_barge_filtros") or {}).get("frase_longa", {}).get("min_palavras") == 3,
               "restaurar padrões não voltou o min_palavras")
        cobrar("basta" not in ((restaurado.get("live_barge_filtros") or {})
                              .get("palavras_corte", {}).get("palavras") or []),
               "restaurar padrões não tirou a palavra cadastrada")

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
        # os filtros também voltam do servidor (não só o modo)
        recarga = pg.evaluate("""() => ({ sem: document.getElementById('lxSemDecisao').value,
            pergunta: document.getElementById('lxLiga_pergunta').checked,
            min: document.getElementById('lxNum_frase_longa_min_palavras').value,
            chips: document.querySelectorAll('#lxChips_palavras_corte .lx-chipw').length })""")
        print("  filtros depois do reload:", recarga)
        cobrar(recarga["sem"] == "continua" and recarga["pergunta"] is True and recarga["min"] == "3",
               f"os filtros não voltaram do load: {recarga}")
        cobrar(recarga["chips"] > 0, "a lista de palavras voltou vazia no load")

        # valor inválido no servidor: a tela cai no PADRÃO, nunca num modo fantasma
        req = urllib.request.Request(base + "/api/settings", method="POST",
                                     data=json.dumps({"live_interrupcao": "banana"}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        pg.reload(wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.wait_for_function("() => typeof LX === 'object'", timeout=20000)
        pg.wait_for_timeout(900)
        # e o valor do ADENDO (`corta`) tem de ser entendido como corte na leitura.
        # Vai pelo `/__set`, sem validação: serve um servidor que fale o vocabulário
        # do adendo (o de hoje normaliza para `continua`, e aí o alias não apareceria).
        req = urllib.request.Request(base + "/__set", method="POST",
                                     data=json.dumps({"live_barge_sem_decisao": "corta"}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        pg.reload(wait_until="networkidle")
        pg.locator('.nav-item[data-view="live"]').click()
        pg.wait_for_function("() => typeof LX === 'object'", timeout=20000)
        pg.wait_for_timeout(900)
        alias = pg.evaluate("() => document.getElementById('lxSemDecisao').value")
        print("  'corta' do adendo lido como:", alias)
        cobrar(alias == "interrompe", f"o valor 'corta' não foi entendido como corte: {alias!r}")
        padrao = pg.evaluate("() => ({ sel: document.getElementById('lxInterrupcao').value, lx: LX.interrupcao })")
        print("  valor inválido no servidor:", padrao)
        cobrar(padrao["sel"] == "inteligente", f"modo inválido não caiu no padrão: {padrao}")

        cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))

        # devolve o estado do dono: filtros nos PADRÕES e o modo que estava lá
        # (os dois vão para o settings.json de verdade; sem isto o arquivo ficaria
        # com o `corta` do teste de alias, que o motor não lê)
        try:
            pg.evaluate("() => lxRestauraPadroes()")
            pg.wait_for_timeout(600)
            pg.evaluate("(m) => lxDefineInterrupcao(m)", modo_inicial or "inteligente")
            pg.wait_for_timeout(600)
            print("  devolvido ao dono:", modo_servidor(), "·", settings().get("live_barge_sem_decisao"))
        except Exception as e:
            print("  ⚠ não consegui devolver o estado do dono:", e)
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

DEPOIS = {str(c): _impressao(c) for _r, c, _f in GUARDADOS}
print("  dono depois:", DEPOIS)
for rotulo, caminho, reprova in GUARDADOS:
    antes, depois = ANTES[str(caminho)], DEPOIS[str(caminho)]
    if antes == depois:
        if antes is None:
            print(f"  · intacto: {rotulo} (ausente — nada foi criado)")
        else:
            print(f"  ✔ intacto: {rotulo} sha256={str(antes[0])[:12]} mtime_ns={antes[1]}")
    elif reprova:
        # ESTE é o arquivo que a suíte escreveria se o isolamento falhasse: mudou
        # (ou apareceu) aqui = regressão dura.
        falhas.append(f"o {rotulo} do dono MUDOU na rodada ({antes} -> {depois})")
    else:
        # `.apikeys.json` é escrito por OUTRAS suítes (admin_ui_flow,
        # ultima_chave_ui) que rodam no mesmo estágio: mudança aqui pode não ser
        # nossa, então avisa em vez de reprovar.
        print(f"  ⚠ {rotulo} mudou na rodada (pode ser de outra suíte): {antes} -> {depois}")

print()
if falhas:
    print("✖ FALHOU — contrato do cliente da interrupção inteligente:")
    print("  log do servidor da rodada em", log)
    print("  estado da rodada preservado em", DIR_TMP)   # só a vermelha guarda
    for f in falhas: print("  ·", f)
    sys.exit(1)
try: log.unlink()
except Exception: pass
try: shutil.rmtree(DIR_TMP)          # verde não deixa diretório nem jsonl
except Exception: pass
print("✔ OK — duck ≤50 ms sem cortar a fila, retoma ~150 ms, corte no interrupted,"
      " teto de 2 s devolvendo o volume e o modo imediata preservando o corte da #225")
PYEOF
