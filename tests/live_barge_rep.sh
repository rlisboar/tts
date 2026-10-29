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
# BLOCO (#547) — cada bloco mede UMA pergunta, com o piso do PM:
#   BLOCO=comando ./tests/live_barge_rep.sh      # "para, para, espera um pouco" no
#                                                # MEIO do playback → corta (≥5/6)
#   BLOCO=backchannel ./tests/live_barge_rep.sh  # "uhum, sim" no meio → NÃO corta (≥5/6)
#   BLOCO=fim ./tests/live_barge_rep.sh          # fala no ÚLTIMO segundo → não corta,
#                                                # é ANOTADA e respondida depois (≥5/6)
#   MODO_INTERRUPCAO=imediata BLOCO=comando ...  # modo antigo (energia), ≥4/6
#   BLOCO=misto                                  # diagnóstico antigo, ≥4/6
# O estímulo é sintetizado (`say`) com o CONTEÚDO do bloco: a amostra de voz do
# repo ("Olá, meu nome é Valentina…") não tem palavra de comando, então o filtro
# `palavras_corte` nunca era exercitado (#547).
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
# REGIME (#503): latência é sensível a carga. Acima de 0,5 de load por núcleo a
# rodada sai INCONCLUSIVA (rc 2) em vez de reprovar por ambiente — o número vira
# medida da máquina, não do produto.
CARGA_LIMITE=$(sysctl -n hw.ncpu 2>/dev/null | awk '{print $1 * 0.5}')
CARGA_AGORA=$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{print $1}')
# `CARGA_FORCA=1` roda mesmo com a máquina carregada (diagnóstico; o veredito
# continua sendo o do regime folgado). Imprime a carga junto.
[ "${CARGA_FORCA:-0}" = "1" ] && CARGA_LIMITE=""
if [ -n "$CARGA_LIMITE" ] && [ -n "$CARGA_AGORA" ] && \
   [ "$(awk -v a="$CARGA_AGORA" -v b="$CARGA_LIMITE" 'BEGIN{print (a > b) ? 1 : 0}')" = "1" ]; then
    echo "! carga $CARGA_AGORA acima de $CARGA_LIMITE (0,5/núcleo): rodada INCONCLUSIVA (CARGA_FORCA=1 fura)" >&2
    exit 2
fi
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
export REP="${REP:-6}"
# VEREDITO POR BLOCO (#547, decisão do PM): cada bloco tem UMA pergunta e um piso
# próprio. `comando`/`backchannel`/`fim` medem o produto (≥5/6, uma falha de
# cauda); `misto`/`imediata` mantêm o piso antigo (≥4/6). O default
# `REP - 2` só valia quando a suíte media uma coisa só — com três blocos, o
# limiar frouxo deixava passar justamente o defeito que o bloco existe p/ achar.
BLOCO_ALVO="${BLOCO:-misto}"
# o modo `imediata` corta por ENERGIA (sem conteúdo): piso frouxo, como antes.
if [ "${MODO_INTERRUPCAO:-inteligente}" = "imediata" ]; then
    export LIMIAR="${LIMIAR:-$(( REP > 3 ? REP - 2 : REP - 1 > 0 ? REP - 1 : REP ))}"
elif [ "$BLOCO_ALVO" = "comando" ] || [ "$BLOCO_ALVO" = "backchannel" ] || \
   [ "$BLOCO_ALVO" = "fim" ]; then
    export LIMIAR="${LIMIAR:-$(( REP > 5 ? REP - 1 : REP ))}"
else
    export LIMIAR="${LIMIAR:-$(( REP > 3 ? REP - 2 : REP - 1 > 0 ? REP - 1 : REP ))}"
fi
export BLOCO="$BLOCO_ALVO"
# Duração injetada por iteração (#469). O wav INTEIRO (8–23 s) estourava o teto de
# 30 s do pendente quando o turno anterior ainda rodava, e o turno era descartado
# por `_stt_ok` no fim — a rodada não media barge, media espera. 3 s produzem turno
# e cabem folgado. ESTÍMULOS (#547) são sintetizados por bloco; sem o `say` do
# macOS cai no wav de voz (o antigo), e aí só vale o alvo do bloco `misto`.
export INJ_S="${INJ_S:-3}"

source "$RAIZ/tests/serial.sh"; serial_pega || exit 1

cd "$RAIZ"
"$PY" - "$RAIZ" <<'PYEOF'
import base64, json, math, os, pathlib, socket, statistics, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

RAIZ = pathlib.Path(sys.argv[1])
REP = int(os.environ.get("REP", "6"))
# 2000 ms de playback RESTANTE no onset (#547, decisão do PM). A decisão sai
# ~700 ms depois (janela de `barge_decisao_ms`); esperando só 1200 ms ela
# aterrissava com <1000 ms no relógio — abaixo do `restante_ms` do
# `fim_de_resposta` — e o bloco `comando` media a regra do ÚLTIMO segundo em vez
# da interrupção no MEIO. Com 2000 ms sobram ~1300 ms na decisão.
PLAYBACK_MIN_MS = int(os.environ.get("PLAYBACK_MIN_MS", "2000"))
# BLOCO (#547): qual família esta rodada mede — a suíte antiga injetava a fala
# sempre no mesmo ponto e com um estímulo arbitrário (a 1ª voz em ordem
# alfabética), então NÃO media o que dizia medir:
#   comando     — fala inequívoca ("para, para, espera um pouco") NO MEIO do
#                 playback → TEM de cortar.
#   backchannel — "uhum, sim" no meio → NÃO pode cortar (só abaixa).
#   fim         — fala no ÚLTIMO segundo → NÃO corta, é ANOTADA e respondida
#                 depois (o `fim_de_resposta`).
#   imediata    — modo antigo; corta por energia, sem olhar conteúdo.
#   misto       — diagnóstico antigo (estímulo de voz, sem contrato de bloco).
BLOCO = (os.environ.get("BLOCO") or "misto").lower()
MODO_INTERRUPCAO = (os.environ.get("MODO_INTERRUPCAO") or "inteligente").lower()
LIMIAR = int(os.environ.get("LIMIAR", "6"))
SUF = str(os.getpid())

# ESTÍMULOS (#547): sintetizados com o `say` do macOS — são o CONTEÚDO que o
# bloco precisa (o wav de voz do repo não tem palavra de comando), e por isso o
# áudio é o mesmo em qualquer máquina. Sem `say`/`afconvert` a rodada avisa e cai
# no wav de voz: aí só o bloco `misto`/`imediata` vale, porque o conteúdo passa a
# ser o da amostra (frase longa, nunca comando).
# `misto` também ganha conteúdo próprio: a amostra de voz do repo entrega
# JANELAS no meio de palavra (o harness pula o ataque), o whisper devolve
# fragmento ("E aí", "Amem") e o resultado mede a sorte do corte, não o motor.
# o comando é repetido no COMEÇO e no FIM: a janela que o STT recebe pode ser só
# a cauda do trecho (o suspeito abre depois do ataque), e "um pouco" sozinho não
# tem palavra de corte
ESTIMULOS = {"comando": "Para, para, espera um pouco, para.",
             "backchannel": "Uhum, sim.",
             "fim": "O dia está bonito hoje.",
             "misto": "Amanhã eu preciso resolver umas coisas na cidade."}

def sintetiza(frase: str, destino: pathlib.Path) -> bool:
    """`say` → AIFF → WAV LEI16 16 kHz mono. False se não der (sem macOS)."""
    try:
        aiff = destino.with_suffix(".aiff")
        subprocess.run(["say", "-o", str(aiff), frase], check=True,
                       timeout=30, capture_output=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                        str(aiff), str(destino)], check=True, timeout=30,
                       capture_output=True)
        aiff.unlink(missing_ok=True)
        return destino.exists() and destino.stat().st_size > 8000
    except Exception:                      # noqa: BLE001 — sem `say` cai no wav de voz
        return False

class Stub(BaseHTTPRequestHandler):
    # RESPOSTA LONGA (#547): o bloco precisa de playback com ≥2 s POR TOCAR no
    # onset e o `fim` precisa CHEGAR ao último segundo com o assistente ainda
    # falando. A resposta curta (~6 s) atingia o 2000 ms só num piscar e o
    # `fim` injetava com o playback já encerrado.
    FRASES = ["Claro, o dia está bonito hoje. Quer que eu conte mais alguma coisa? "] * 5
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

# No bloco `fim` a resposta é CURTA de propósito: o alvo é o ÚLTIMO segundo dela.
# Com 25 s de resposta o cliente fica com dezenas de segundos na fila, o backlog
# do motor infla junto e o "último segundo" real demora ~20 s para chegar (e a
# fila ainda oscila por underrun). ~5 s põem o fim ao alcance em segundos.
# resposta CURTA por bloco: com 25 s de fala o cliente fica com dezenas de
# segundos na fila, cada iteração leva minutos e o `garante_turno` estourava o
# teto esperando o turno anterior terminar de gerar (#547). Meio do playback
# ainda é alcançável com ~10 s — e o `fim` precisa de menos.
if BLOCO == "fim":
    Stub.FRASES = Stub.FRASES[:1]
elif BLOCO in ("comando", "backchannel", "misto"):
    Stub.FRASES = Stub.FRASES[:2]


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

# ESTÍMULO DO BLOCO (#547): o áudio injetado é o do CONTEÚDO do bloco, não a
# amostra de voz. Sem `say` a rodada avisa e mantém o comportamento antigo (a
# amostra, truncada em 3 s), que é o alvo do `misto`/`imediata`.
def pcm_de_wav(caminho: pathlib.Path) -> str:
    a, sr = sf.read(str(caminho), dtype="float32")
    if a.ndim > 1: a = a.mean(axis=1)
    if sr != 16000:
        idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
        a = np.clip(np.interp(idx, np.arange(len(a)), a), -1, 1)
    a = a / (float(np.abs(a).max()) or 1.0) * 0.95
    return base64.b64encode((a * 32767).astype("<i2").tobytes()).decode()

estimulo = fala_baixa            # default: amostra de voz (comportamento antigo)
if BLOCO in ESTIMULOS:
    sint = pathlib.Path("/tmp") / f"live_barge_estim_{BLOCO}_{SUF}.wav"
    if sintetiza(ESTIMULOS[BLOCO], sint):
        estimulo = pcm_de_wav(sint)
        sint.unlink(missing_ok=True)
        print(f"  estímulo do bloco {BLOCO!r}: {ESTIMULOS[BLOCO]!r}")
    else:
        print(f"  ! sem `say`/`afconvert`: estímulo do bloco {BLOCO} cai na amostra de voz")
        if BLOCO != "misto":
            print("    (o bloco mede o CONTEÚDO; sem estímulo próprio o número não vale)")

porta = porta_livre()
settings_tmp = pathlib.Path(f"/tmp/live_barge_settings_{SUF}.json")
# settings ANTES do processo subir (#547): o app lê o arquivo no IMPORT, e o
# patch de `SETTINGS_PATH` do launcher vem depois — sem passar `TTS_SETTINGS_PATH`
# o boot ainda lia o arquivo do DONO. Aqui o modo do bloco entra no arquivo.
settings_tmp.write_text(json.dumps({"live_interrupcao": MODO_INTERRUPCAO}))
env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
       "TTS_CHAT_MODEL": "stub-barge", "TTS_CHAT_BACKEND": "openai",
       # o Live tem backend PRÓPRIO: sem este pin o dono com `chat_backend_live: dsh`
       # tirava o harness do stub e a contagem de barge media outra rota (#195)
       "TTS_CHAT_BACKEND_LIVE": os.environ.get("TTS_CHAT_BACKEND_LIVE") or "openai",
       "BARGE_PORT": str(porta), "ORT_DISABLE_TELEMETRY": "1",
       "BARGE_SETTINGS": str(settings_tmp),
       # os CAMINHOS vão por env ANTES do import (#591): `import app` já roda
       # `_save_settings()`/`_load_apikeys()`, e caminho por atributo DEPOIS do
       # import deixa essas escritas no arquivo do DONO. `BARGE_*` fica só para o
       # launcher conferir o que recebeu.
       "TTS_SETTINGS_PATH": str(settings_tmp),
       "BARGE_APIKEYS": f"/tmp/live_barge_apikeys_{SUF}.json",
       # `.apikey` legado é espelho de `_save_apikeys`: sem ele a rodada reescrevia
       # o arquivo do DONO mesmo com o JSON de chaves em /tmp (#535)
       "BARGE_LEGADO": f"/tmp/live_barge_apikey_{SUF}",
       "TTS_APIKEYS_PATH": f"/tmp/live_barge_apikeys_{SUF}.json",
       "TTS_LEGACY_APIKEY_PATH": f"/tmp/live_barge_apikey_{SUF}"}
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
# settings/chaves já vieram por ENV antes deste import (`TTS_*_PATH`): os arquivos
# do dono não são lidos nem escritos aqui — nem pelo `_save_settings()` do import
# (#591). Só a auth é desligada: este harness fala sempre por loopback.
app.API_KEY = None
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["BARGE_PORT"]), log_level="info")
'''

proc = subprocess.Popen([str(RAIZ / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                        start_new_session=True)
base = f"http://127.0.0.1:{porta}"

def tail(n=8):
    try: return "".join(log.read_text().splitlines(keepends=True)[-n:])
    except Exception: return "-"

ok_n = -1                          # -1 = morreu antes do veredito: log fica
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
                    if (["speech_start", "speech_end", "barge_in", "turno_pendente", "barge_decisao"]
                        .includes(m.type)) window.__sp.push(m);   // inteiro: o `__ev` só guarda o tipo
                    // #216: no sentido do eco a contagem CUMULATIVA de `barge_in` é a
                    // medida de "o motor se interrompe sozinho?" — ela não depende de
                    // a injeção pegar a janela aberta (o ✔ por iteração depende).
                    if (m.type === "barge_in") window.__barges++;
                    if (m.type === "interrupted") window.__interr++;
                    // DECISÕES cumulativas (nunca zeradas): a iteração correlaciona
                    // pelo ÍNDICE do onset, o que sobrevive ao `__ev` zerado
                    if (m.type === "barge_decisao") {
                        (window.__decisoes = window.__decisoes || []).push(
                            { t: performance.now(), tipo: m.tipo_barge, texto: m.texto,
                              filtro: m.filtro });
                    }
                    // #216: contadores do MOTOR (cumulativos) — diagnóstico
                    if (m.type === "stats" && m.motor) window.__motor = m.motor;
                    // #547: `restante_ms` é o que o MOTOR acha que falta tocar —
                    // é a ENTRADA do `fim_de_resposta`, e é nela (não no relógio
                    // do cliente) que o bloco `fim` tem de se orientar
                    if (m.type === "stats" && m.playback) window.__playback = m.playback;
                    // nível do MIC (dBFS do último frame): é como se sabe que os
                    // frames da injeção ANTERIOR ainda estão em voo (hipótese 2)
                    if (m.type === "stats" && m.mic) {
                        window.__mic = m.mic;
                        // PICO do nível do mic na iteração: é a prova de que os
                        // frames da injeção CHEGARAM (hipótese 1/2 do PM)
                        window.__micPico = Math.max(window.__micPico ?? -120,
                                                    m.mic.dbfs ?? -120);
                    }
                    // e o ESTÁGIO do pipeline: com ele ocupado o `interrupted` é do
                    // pipeline, não do handler — o onset precisa de ambos ociosos
                    if (m.type === "stats" && m.turno) window.__turno = m.turno;
                });
            };
            window.__mk();
        }""")
        pg.locator("#lxLigar").click()
        pg.wait_for_function("() => LX.ws && LX.ws.readyState === 1", timeout=20000)

        INJ_S = float(os.environ.get("INJ_S", "3"))

        def injeta(b64: str | None = None, segundos: float | None = None):
            # RITMO REAL (#547): 1600 amostras = 100 ms de áudio, enviadas a cada
            # 100 ms. A injeção antiga mandava a cada 25 ms — 4x mais rápido que o
            # microfone —, então o `fala_ms` do motor (relógio do STREAM) saía 4x
            # maior que o tempo de parede e o `speech_end` chegava junto do prazo
            # de decisão: `fala_sustentada` ganhava a corrida e a cadeia nem rodava.
            pg.evaluate("""async ([b64, INJ_S, SEG]) => {
                const bin = atob(b64); const by = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) by[i] = bin.charCodeAt(i);
                const i16 = new Int16Array(by.buffer);
                const ini = Math.min(i16.length, 16000 * 0.5);          // pula o ataque
                const dur = (typeof SEG !== "undefined" && SEG) ? SEG : INJ_S;
                const fim = Math.min(i16.length, ini + 16000 * dur);
                for (let i = ini; i + 1600 <= fim; i += 1600) {
                    if (!LX.ws || LX.ws.readyState !== 1) break;
                    LX.ws.send(i16.slice(i, i + 1600).buffer);
                    await new Promise(r => setTimeout(r, 100));
                }
                if (LX.ws && LX.ws.readyState === 1) LX.ws.send(JSON.stringify({ type: "end_of_speech" }));
            }""", [b64 or fala, INJ_S, segundos])

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
            """Injeta um turno e espera o áudio — o barge precisa de playback ATIVO.

            O ESTÍMULO DO BLOCO, e curto (1 s): antes usava a amostra de voz
            inteira (3 s) e o áudio dela ficava no buffer do turno, então o STT da
            suspeita transcrevia o RABO DA AMOSTRA ("E aí") em vez do comando —
            e os frames da injeção de medição chegavam atrasados, clumpeados, na
            carona dos 3 s anteriores (#547, hipótese 2 do PM)."""
            # o PIPELINE tem de estar livre: injetar com o turno anterior no STT
            # virava um turno de 64 ms sem áudio e o teto de 90 s estourava
            # (medido: `prewarm_em_curso` e um `speech_end` vazio a cada 4 s)
            limite_s = time.monotonic() + 25
            while time.monotonic() < limite_s:
                if pg.evaluate("() => ((window.__turno||{}).stage)") in ("idle", None):
                    break
                pg.wait_for_timeout(100)
            # RE-TENTA a injeção: depois de um corte o app pode levar um minuto
            # para voltar a falar; uma injeção perdida não pode custar a iteração
            ok = False
            limite_t = time.monotonic() + 90
            while time.monotonic() < limite_t:
                n0 = audio_n()
                injeta(estimulo, segundos=2.0)
                if espera_audio(n0, timeout=15000):
                    ok = True
                    break
            # e o MIC tem de estar quieto: frames do turno anterior ainda em voo
            # fariam a fala de medição dividir o buffer com eles
            limite = time.monotonic() + 6
            while time.monotonic() < limite:
                dbfs = pg.evaluate("() => ((window.__mic||{}).dbfs)")
                if dbfs is None or dbfs < -45:
                    break
                pg.wait_for_timeout(60)
            return ok

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
        def espera_settle(timeout=25000):
            """Espera o turno ANTERIOR fechar antes de injetar de novo (#469).

            Sem isto a injeção caía no `stage=stt`/prewarm do turno anterior, virava
            PENDENTE e a rodada media espera: era a causa das falhas "sem áudio do
            servidor" e do pendente truncado no teto de 30 s."""
            pg.evaluate("() => { window.__ev = []; }")
            try:
                pg.wait_for_function(
                    "() => window.__ev.some(e => e.tipo === 'turn_complete'"
                    " || e.tipo === 'interrupted')", timeout=timeout)
            except Exception:
                pass
            pg.wait_for_timeout(150)

        for i in range(1, REP + 1):
            if i > 1:
                espera_settle()
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
            # INJEÇÃO INEQUÍVOCA (#547): só injeta com >= `PLAYBACK_MIN_MS` de
            # playback AINDA POR TOCAR. Sem isto a fala podia cair no fim do
            # chunk e o `fim_de_resposta` (correto) era lido como falha.
            try:
                if MODO != "vao" and BLOCO in ("comando", "backchannel", "fim", "misto"):
                    # PONTO DE ONSET (#547): a fala tem de cair com o TURNO do
                    # assistente já FECHADO no motor e a JANELA de playback ABERTA
                    # — é a janela que ARMA o barge, e com o turno aberto a fala
                    # entraria no turno em voo como se fosse fala normal (medido:
                    # `turn_complete` chega muito antes do fim do áudio e o motor
                    # ainda estava em `falando`). Por isso cada iteração força um
                    # turno NOVO e espera `ocioso` + `janela`.
                    #   `comando`/`backchannel`/`misto`: MEIO do playback (restante alto)
                    #   `fim`: ÚLTIMO segundo (restante ≤ 2,2 s)
                    # REUSO: se o turno anterior ainda está TOCANDO com fila
                    # suficiente, não gasta outro (cada turno novo custa STT+TTS
                    # e, sob carga, o teto de espera do áudio estourava)
                    tem_fila = pg.evaluate(
                        "() => (LX.ativos.length > 0) && (((window.__playback||{}).restante_ms)||0) >= 6000")
                    if not tem_fila:
                        pg.evaluate("() => { try { lxCancelaPlayback(); } catch (e) {} }")
                        pg.wait_for_timeout(400)
                    if not tem_fila and not garante_turno():
                        resultados.append({"i": i, "ok": False, "motivo": "sem áudio",
                                           "assinatura": {}})
                        print(f"  [{i}/{REP}] ✖ sem áudio do servidor — tail:\n{tail(6)}")
                        continue
                    if tem_fila:
                        pg.wait_for_timeout(300)
                    pg.evaluate("() => { window.__ev = []; }")
                    pg.wait_for_function(
                        "() => window.__ev.some(e => e.tipo === 'turn_complete')",
                        timeout=40000)
                    # stats saem ~1/s: sem esta espera o laço decide com o retrato
                    # ANTERIOR (medido: `ocioso` e 24 s de backlog do turno velho)
                    pg.wait_for_timeout(1300)
                    limite = time.monotonic() + 60
                    curva = []
                    motivo_saida = "timeout"
                    while time.monotonic() < limite:
                        r = pg.evaluate(
                            "() => { const p = window.__playback || {};"
                            " const m = window.__motor || {};"
                            " const t = window.__turno || {};"
                            " return [p.restante_ms, p.janela, m.estado, t.stage]; }")
                        curva.append(r)
                        restante = (r[0] or 0) if r else 0
                        # `comando`/`backchannel`/`misto` exigem folga sobre o
                        # limiar do `fim_de_resposta` (2000 ms default): a decisão
                        # sai ~700 ms depois do onset, então esperar só 2000 punha
                        # o MEIO do playback já dentro da regra do último segundo
                        # (medido: onset em 2400, decisão em 1696 → retoma). O
                        # backlog ainda escoa em DOBRO enquanto a pessoa fala (os
                        # frames injetados descontam o mesmo contador), então a
                        # folga tem de ser bem maior que o limiar da regra.
                        pronto = bool(r and r[1] and r[2] == "ocioso"
                                      and r[3] == "idle") and (
                            (0 < restante <= 2200) if BLOCO == "fim"
                            else restante >= 6000)
                        if pronto:
                            motivo_saida = "ok"
                            break
                        # backlog transitório em 0 não serve: o cliente ainda tem
                        # fila. Só sai por timeout (o veredito diz o motivo)
                        if r and restante == 0 and r[2] == "ocioso" and r[3] == "idle":
                            motivo_saida = "backlog 0"
                            break
                        pg.wait_for_timeout(200)
                    print(f"  [{i}/{REP}] antes do onset ({motivo_saida}) "
                          f"[restante, janela, estado, estágio]: {curva[-6:]}")
                else:
                    pg.wait_for_function(
                        "(m) => { const c = LX.ctxPlay; if (!c) return false;"
                        " return (LX.proximo - c.currentTime) * 1000 >= m; }",
                        arg=PLAYBACK_MIN_MS, timeout=8000)
            except Exception:
                print(f"  [{i}/{REP}] ⚠ janela de playback não atingida")
            marco = pg.evaluate("() => window.__ev.length")
            est0 = pg.evaluate("() => ({ estado: LX.estado, ativos: LX.ativos.length, chunks: (LX.obs.stats||{}).playback ? LX.obs.stats.playback.chunks : null })")
            # #225: FILA = o que o cliente ainda vai tocar AGORA (segundos de áudio
            # agendado). É a medida do "fala por cima": se o onset não corta, o
            # assistente segue audível por estes ms enquanto o usuário fala.
            fila_ms = pg.evaluate("() => { const c = LX.ctxPlay; return c ? Math.max(0, Math.round((LX.proximo - c.currentTime) * 1000)) : 0; }")
            cortes0 = pg.evaluate("() => (window.__cortes || []).length")
            # índice das DECISÕES: correlação exata com ESTA injeção
            decisoes0 = pg.evaluate("() => (window.__decisoes || []).length")
            pg.evaluate("() => { window.__inj = performance.now(); window.__micPico = -120; }")
            # a injeção de MEDIÇÃO usa o ESTÍMULO DO BLOCO (#547). No bloco
            # `backchannel` ele é CURTO (<=0,6 s) — a intenção é justamente NÃO
            # cortar — e no `fim` também é curto: a fala do ÚLTIMO segundo é uma
            # frase, não um monólogo, e precisa caber no que resta da fila.
            # `garante_turno` continua usando a amostra de voz.
            injeta(estimulo, segundos=(0.6 if BLOCO == "backchannel"
                                       else (1.2 if BLOCO == "fim" else None)))
            print(f"  [{i}/{REP}] pico do mic na injeção: "
                  f"{pg.evaluate('() => Math.round(window.__micPico ?? -120)')} dBFS"
                  f" · limiar {pg.evaluate('() => (window.__motor||{}).limiar_dbfs')}"
                  f" · barge_ativos {pg.evaluate('() => (window.__motor||{}).barge_ativos')}")
            fim_resp = False
            respondida = False
            if BLOCO == "fim":
                # o alvo é o CONTRÁRIO do corte: a fala NÃO corta e é respondida
                # depois (preferencialmente pelo PENDENTE, com `fim_de_resposta`).
                # O pendente só existe se o playback ainda estava de pé; com a fila
                # chocha (TTS mais lento que 1x, medido aqui) a mesma fala abre um
                # turno NORMAL — é o mesmo contrato visto pelo usuário, e contar
                # isso como falha seria reprovar por underrun da máquina.
                try:
                    pg.wait_for_function(
                        "() => window.__ev.some(e => e.tipo === 'turno_pendente')",
                        timeout=6000)
                except Exception:
                    pass
                # o evento INTEIRO está em `__sp` (o `__ev` guarda só o tipo)
                fim_resp = bool(pg.evaluate(
                    "() => (window.__sp || []).some(e => e.type === 'turno_pendente'"
                    " && e.fim_de_resposta)"))
                try:
                    pg.wait_for_function(
                        "() => window.__ev.some(e => e.tipo === 'transcript_user'"
                        " || e.tipo === 'assistant_text' || e.tipo === 'speech_start')",
                        timeout=20000)
                    respondida = True
                except Exception:
                    respondida = False
                cortou = bool(pg.evaluate(
                    "() => window.__ev.some(e => e.tipo === 'interrupted')"))
                ok = (not cortou) and respondida
            else:
                # O acerto do bloco é a DECISÃO de cortar. Se o `interrupted` não
                # chegar (com turno já fechado no pipeline ele pode não sair),
                # `barge_decisao{tipo_barge=interrupcao}` é a mesma prova — medir só
                # o frame do cliente reprovava por contabilidade de turno, não por
                # decisão (#547, sob carga a decisão chega depois do teto antigo).
                try:
                    pg.wait_for_function(
                        "(n) => window.__ev.some(e => e.tipo === 'interrupted')"
                        " || (window.__decisoes || []).slice(n)"
                        "      .some(d => d.tipo === 'interrupcao')",
                        arg=decisoes0, timeout=90000)
                except Exception:
                    pass
                ok = bool(pg.evaluate(
                    "(n) => window.__ev.some(e => e.tipo === 'interrupted')"
                    " || (window.__decisoes || []).slice(n)"
                    "      .some(d => d.tipo === 'interrupcao')", decisoes0))
                print(f"  [{i}/{REP}] decisões desta injeção: "
                      f"{pg.evaluate('(n) => JSON.stringify((window.__decisoes||[]).slice(n))', decisoes0)[:160]}")
            corte = None
            if ok:
                corte = pg.evaluate("() => { const t = window.__ev.find(e => e.tipo === 'interrupted'); return t ? Math.round(performance.now() - t.t) : null; }")
            # PÓS-CORTE (#674): do `speech_start` do TURNO SEGUINTE ao 1º áudio
            # dele. Contar do corte pegava o áudio do turno cortado ainda no fio
            # ("0 ms" falso); o tempo de a pessoa falar não entra.
            if ok and os.environ.get("MEDE_POS_CORTE"):
                injeta(estimulo, segundos=2.0)
                t0 = time.monotonic()
                abriu = False
                while time.monotonic() - t0 < 30:
                    if pg.evaluate(
                            "() => window.__ev.some(e => e.tipo === 'speech_start')"):
                        abriu = True
                        break
                    pg.wait_for_timeout(50)
                if not abriu:
                    print(f"  [{i}/{REP}] pós-corte: SEM turno novo em 30 s")
                else:
                    t1 = time.monotonic()
                    n_a = audio_n()
                    chegou = False
                    while time.monotonic() - t1 < 30:
                        if audio_n() > n_a:
                            chegou = True
                            break
                        pg.wait_for_timeout(50)
                    print(f"  [{i}/{REP}] pós-corte: abertura "
                          f"{int((t1 - t0) * 1000)} ms · 1º áudio "
                          + (f"{int((time.monotonic() - t1) * 1000)} ms"
                             if chegou else "AUSENTE em 30 s"))
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
                  + (f" · anotada={fim_resp} respondida={respondida}" if BLOCO == "fim" else "")
                  + f" · motor: barge_ativos={motor_agora.get('barge_ativos')}"
                  f" barge_falsos={motor_agora.get('barge_falsos')}"
                  f" limiar={motor_agora.get('limiar_dbfs')}")
            for s in assin:
                print("        motor:", {k: s.get(k) for k in ("type", "barge_in", "barge_falso", "curto", "prob", "rms_dbfs", "fala_ms", "t_decisao_ms", "detalhe") if k in s})
            resultados.append({"i": i, "ok": ok, "antes": est0, "eventos": novos, "assinatura": assin,
                               "fila_ms": fila_ms, "corte_ms": corte_ms,
                               "fim": bool(fim_resp), "respondida": respondida})
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
    # BLOCO (#547): cada bloco tem UMA pergunta; o acerto é o contrário dela
    # quando a pergunta é "não corte".
    if BLOCO == "backchannel":
        acertos = REP - ok_n
        print(f"\n=== BLOCO {BLOCO}: {acertos}/{REP} NÃO cortaram (limiar {LIMIAR})")
    elif BLOCO == "fim":
        # o aceite do `fim` é: NÃO cortar e a fala ser respondida depois. O
        # PENDENTE com `fim_de_resposta` é o caminho preferido e vai no número
        # (`anotadas`), mas exigi-lo reprovaria por underrun da máquina.
        acertos = ok_n
        print(f"\n=== BLOCO {BLOCO}: {acertos}/{REP} não cortaram E foram respondidas"
              f" · anotadas no pendente={sum(1 for r in resultados if r.get('fim'))}"
              f" · respondidas={sum(1 for r in resultados if r.get('respondida'))}"
              f" (limiar {LIMIAR})")
    else:
        acertos = ok_n
        print(f"\n=== BLOCO {BLOCO}: {acertos}/{REP} cortaram (limiar {LIMIAR})")
    if BLOCO == "misto" and ok_n == 0:
        print(f"✖ NENHUM barge — não é cauda, é quebrado (log: {log})")
        sys.exit(1)
    if acertos < LIMIAR:
        print(f"✖ REPROVADO: {acertos}/{REP} abaixo do limiar {LIMIAR} (log: {log})")
        sys.exit(1)
    print(f"✔ APROVADO: {acertos}/{REP} ≥ {LIMIAR} (bloco {BLOCO})")
finally:
    try: os.killpg(os.getpgid(proc.pid), 15)
    except Exception: proc.terminate()
    # temporários saem SEMPRE — e DENTRO do `finally`: o `sys.exit` do veredito
    # propaga e o código depois do `finally` nunca roda (o wav ficava para trás).
    # O LOG fica só na reprovação, porque é o que se lê.
    if ok_n >= LIMIAR:                 # verde não deixa nem o log
        try: log.unlink()
        except Exception: pass
    for _sobra in (wav_path,
                   settings_tmp,
                   pathlib.Path(f"/tmp/live_barge_apikeys_{SUF}.json")):
        try: _sobra.unlink()
        except Exception: pass
PYEOF
