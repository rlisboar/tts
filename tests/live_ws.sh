#!/usr/bin/env bash
# Live: ciclo completo pelo WebSocket real, com latência medida (tasks #93/#104).
#
# É o smoke que o gate do MVP usa, e ele é AUTO-CONTIDO: sobe um provedor de LLM
# local (SSE, sem rede) e um servidor próprio (porta livre) apontado para o stub
# por VARIÁVEL DE AMBIENTE (`TTS_CHAT_BACKEND=openai` + `TTS_CHAT_BASE_URL`/
# `TTS_CHAT_MODEL`, #103). Assim ele não escreve no `settings.json` do dono — nem
# quando morre no meio —, não depende do provedor dele estar de pé E não troca de
# rota pelo `chat_backend` que estiver no arquivo (o backend entrou na árvore com o
# DSH #122/#123, e sem o pino o smoke media a rota do dono, não a stub).
#
#   ./tests/live_ws.sh                 # sobe o próprio servidor (porta livre, stub)
#   LIVE_LLM=config ./tests/live_ws.sh # usa o provedor configurado (sem stub)
#   BASE=http://127.0.0.1:7860 ./tests/live_ws.sh   # usa um servidor já de pé
#   TTS_CHAT_BACKEND=dsh ./tests/live_ws.sh         # turno pelo harness dsh (o env
#                                   # do chamador manda; ajuste TTS_CHAT_DSH_BIN se
#                                   # o `chat_dsh_bin` do settings estiver inválido)
#
# Checa: (1) setup/ready; (2) turno completo com transcript dos dois lados;
# (3) fim-de-fala → PRIMEIRO ÁUDIO ≤ 1500 ms (o alvo do MVP); (4) `cancel`
# durante o playback → `interrupted` e o áudio para de chegar; (5) sessão
# fechando limpa.
#
# ESTADO ISOLADO (#463): além do provedor e do `settings.json`, a rodada não encosta
# em NADA do dono — chaves (`.apikeys.json` + `.apikey` legado), `outputs/` e o
# histórico de jobs vão para `/tmp` por `TTS_*_PATH` (#461) e saem no fim, com log
# preservado só em rodada vermelha. A chave do dono só é LIDA no modo servidor
# externo (`BASE=…`), e mesmo aí a suíte não escreve nela.
#
# MEDINDO À MÃO (script de trace, curl, playwright avulso, sem este isolamento): a
# chave de API que você cria é estado DO DONO. Use nome com prefixo que a limpeza
# reconhece — `teste-ui-*`, `probe-papel*`, `probe-adota*` (o `admin_ui_flow.sh`
# varre esses com mais de 2 min na abertura) — e apague no fim. Nome fora do padrão
# (ex.: `trace-speed-191`) fica para sempre, e o dono vê na tela.
#
# O contador de `underrun` (#492) é sensível à carga: com outras suítes rodando
# junto o TTS não acompanha o tempo real e um vão isolado aparece. O critério é
# ZERO; a tolerância (1 vão a cada 5 chunks, como aviso) só vale quando a carga
# por núcleo NO PONTO MEDIDO passa de `LIMITE_CARGA` — a da abertura sai junto na
# mesma linha, para a decisão ser auditável. A/B no mesmo minuto mostrou a versão anterior deste
# arquivo piorando igual ou mais (4 vãos em 11 chunks contra 1 em 8, no #463).
#
# O alvo de 1500 ms vale para ESTE cenário (stub local): com o provedor de chat
# REMOTO do dono o 1º token sozinho custa segundos e o alvo não cabe — quem medir
# com `LIVE_LLM=config` tem de dizer que saiu do cenário do alvo (#173, números no
# LIVE.md). O mesmo vale quando o Live roda no harness `dsh`
# (`TTS_CHAT_BACKEND_LIVE=dsh ./tests/live_ws.sh`): aí o alvo vira AVISO
# qualificado, e o número do provedor sai separado — não ✘ por construção (#195).
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Serializa com as outras suítes que carregam modelo — sem isto o alvo de
# latência do live_ws.sh dá falso vermelho sob contenção de Metal/CPU (#134).
source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1
export BASE="${BASE:-}"          # vazio = o driver sobe o próprio servidor
# a raiz do repo vai por ENV: `cwd` pode ser outro (o `test_run_sh` chama com
# cwd=tests/) e caminho relativo dava "sem voz de teste" com o smoke morrendo em 0 s
export LIVE_WS_RAIZ="$RAIZ"

"$PY" - "$@" <<'PY'
import asyncio, json, os, pathlib, shutil, signal, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import soundfile as sf

BASE = os.environ.get("BASE", "").rstrip("/")
RAIZ = pathlib.Path(os.environ.get("LIVE_WS_RAIZ") or pathlib.Path.cwd())
MODO = os.environ.get("LIVE_LLM", "stub")

#: chave usada no HTTP/WS. No servidor PRÓPRIO é a SEMENTE do arquivo sintético —
#: a chave do dono nem é lida. Só com um servidor EXTERNO (`BASE=…`) a chave real
#: entra, porque aí quem autentica é a instalação dele (#463).
CHAVE = ""
H: dict = {}


def define_chave(chave):
    """Fixa a chave da rodada (a do arquivo sintético ou, no modo externo, a real)."""
    global CHAVE
    CHAVE = chave
    H.clear()
    H["X-API-Key"] = chave
ALVO_MS = 1500
#: carga por núcleo acima da qual o `underrun` tolera vão (ver 2b): com a máquina
#: de 18 núcleos parada a 1m dá ~0,1; com o time de agentes rodando passa de 0,5.
LIMITE_CARGA = 0.5
#: carga por núcleo na ABERTURA (informativa) — o valor que DECIDE é o do turno
CARGA_ABERTURA = 0.0


def carga_por_nucleo() -> float:
    """Load average de 1 min por núcleo. Serve só para rotular a decisão: o
    loadavg já é uma média, então ele sempre fala do último minuto."""
    return os.getloadavg()[0] / max(1, os.cpu_count() or 1)
# Backend do Live desta rodada (o env do chamador manda; sem ele, stub). O alvo de
# 1,5 s é do cenário STUB (#173): com o Live no harness dsh, o 1º token DELE domina
# e o alvo não cabe — aí o número é AVISO qualificado, não falha.
BACKEND_LIVE = (os.environ.get("TTS_CHAT_BACKEND_LIVE")
                or os.environ.get("TTS_CHAT_BACKEND") or "openai")
ALVO_VALE = BACKEND_LIVE == "openai"
falhas, avisos = [], []

#: caminho do log do servidor desta rodada (o `sobe_servidor` preenche). É global
#: porque a mensagem de falha e a limpeza acontecem em OUTRO escopo, onde `log`
#: local não existe (`NameError: name 'log' is not defined`).
LOG = None
CHAVES = None
LEGADO = None
SAIDAS = None


def ok(msg):
    print(f"  ✔ {msg}")

def falha(msg):
    falhas.append(msg)
    print(f"  ✘ {msg}")

def aviso(msg):
    avisos.append(msg)
    print(f"  ! {msg}")

# ---------------------------------------------------------------- HTTP helpers
def http_get(caminho):
    req = urllib.request.Request(BASE + caminho, headers=H)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def registra_voz_de_teste(nome, pcm, segundos=4.0):
    """Cadastra a fala de teste como VOZ do servidor da rodada e devolve o id.

    Num worktree não há `voices/` e sem voz registrada o turno não gera áudio. O
    cadastro é no servidor PRÓPRIO da rodada (o do dono não é tocado)."""
    import uuid
    wav = pathlib.Path("/tmp") / f"live_ws_voz_{uuid.uuid4().hex[:8]}.wav"
    dados = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
    if len(dados) < int(segundos * 16000):    # o app exige 3 s+
        dados = np.tile(dados, int(np.ceil(segundos * 16000 / max(len(dados), 1))))
    sf.write(str(wav), dados[: int(segundos * 16000)], 16000, subtype="PCM_16")
    fronteira = uuid.uuid4().hex
    corpo = b""
    for campo, valor in (("name", nome), ("denoise", "0")):
        corpo += (f"--{fronteira}\r\nContent-Disposition: form-data; name=\"{campo}\"\r\n\r\n"
                  f"{valor}\r\n").encode()
    corpo += (f"--{fronteira}\r\nContent-Disposition: form-data; name=\"audio\"; "
              f"filename=\"v.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
    corpo += wav.read_bytes() + f"\r\n--{fronteira}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/voices", data=corpo, method="POST",
                                 headers={**H, "Content-Type":
                                          f"multipart/form-data; boundary={fronteira}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        criado = json.loads(r.read())
    wav.unlink(missing_ok=True)
    return criado["id"]


def http_post(caminho, corpo, timeout=60):
    req = urllib.request.Request(BASE + caminho, method="POST",
                                 data=json.dumps(corpo).encode(),
                                 headers={**H, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ---------------------------------------------------------------- stub de LLM
class Stub(BaseHTTPRequestHandler):
    """OpenAI-compatível com SSE. Resposta em 3 sentenças para exercitar o chunking."""
    FRASES = ["Claro, ", "o dia está bonito hoje. ", "Quer que eu conte mais alguma coisa?"]

    def log_message(self, *a):
        pass

    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if corpo.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for f in self.FRASES:
                linha = json.dumps({"choices": [{"delta": {"content": f}}]})
                self.wfile.write(f"data: {linha}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            corpo = json.dumps({"choices": [{"message": {"content": "".join(self.FRASES)}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(corpo.encode())

def sobe_stub():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    srv = HTTPServer(("127.0.0.1", porta), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return porta

# ---------------------------------------------------------------- fala de teste
def fala_de_teste(segundos=2.4):
    """PCM16 16 kHz a partir de uma voz (é fala de verdade, não tom).

    Num worktree não existe `voices/` (ela não é versionada): cai na fixture
    versionada, que é voz sintetizada do próprio app."""
    import subprocess
    # ABSOLUTOS a partir de `RAIZ`: o harness pode ser chamado de qualquer cwd (o
    # test_run_sh chama com cwd=tests/) — relativo dava "sem voz de teste" e o
    # smoke morria em 0 s com rc=0 (#688).
    # SEMPRE a fixture: o `voices/` do dono não é lido nem quando existe
    wavs = [RAIZ / "tests" / "fixtures" / "voz_fala_5s_16k.wav"]
    wavs = [w for w in wavs if w.is_file()]
    segundos = max(segundos, 5.0)       # fala de fixture é curta: usa os 5 s dela
    for w in wavs:
        a, sr = sf.read(str(w), dtype="float32")
        if a.ndim > 1:
            a = a.mean(axis=1)
        # corta o silêncio do começo (senão o VAD do turno não abre)
        n = int(0.05 * sr)
        env = np.abs(a[: max(n, 1)]).max()
        inicio = 0
        for i in range(0, len(a) - n, n):
            if np.abs(a[i:i + n]).max() > 4 * max(env, 1e-4):
                inicio = i
                break
        a = a[inicio:inicio + int(segundos * sr)]
        # fixture é curta (0,6 s): repete até o tamanho pedido, com 0,1 s de vão
        # para o VAD ver fala separada — sem isso o worktree fica sem fala
        if len(a) < int(segundos * sr) and len(a):
            vao = np.zeros(int(0.1 * sr), dtype=a.dtype)
            pedaco, partes = a.copy(), [a]
            while sum(len(x) for x in partes) < int(segundos * sr):
                partes += [vao, pedaco]
            a = np.concatenate(partes)[: int(segundos * sr)]
        if len(a) < sr:                      # curto demais: tenta a próxima voz
            continue
        idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
        a16 = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
        return w.stem, (np.clip(a16, -1, 1) * 32767).astype("<i2").tobytes()
    raise SystemExit("nenhuma voices/*.wav serviu como fala de teste")

def limpa_sobras(manter_log=False):
    """Apaga os temporários DESTA rodada — log, arquivo de chaves e `.apikey`.

    Rodada VERMELHA mantém o log (é o que se lê para diagnosticar); as chaves saem
    sempre, que é o motivo do isolamento."""
    for sobra in ((() if manter_log else (LOG,)) + (CHAVES, LEGADO)):
        try: sobra.unlink()
        except Exception: pass
    if SAIDAS:
        try: shutil.rmtree(SAIDAS, ignore_errors=True)
        except Exception: pass


def _chave_da_instalacao() -> str:
    """Chave para o servidor EXTERNO (`BASE=…`) vinda só do AMBIENTE (#720).

    Nada de ler `.apikey`/`.apikeys.json` da raiz: quem aponta para uma instalação
    de pé passa `LIVE_WS_KEY`. Sem ela a rodada segue desautenticada — de loopback
    a instalação aceita, e é o caso normal."""
    return (os.environ.get("LIVE_WS_KEY") or os.environ.get("TTS_CHAVE") or "").strip()


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sobe_servidor(porta, stub_porta):
    """Servidor próprio com o stub por ENV — o settings.json do dono fica intocado.

    Sem isto o smoke tinha de gravar via POST /api/settings e, se morresse no meio,
    deixava o provedor de teste no arquivo do dono (incidente de 2026-09-25).

    `TTS_CHAT_BACKEND` PRECISA ser pinado junto: sem ele o backend vem do
    `settings.json` e, com o dono em `chat_backend: "dsh"`, a suíte saía do stub e
    media outra rota (achado do gate #135/#137) — e caía quando o dsh não subia, o
    que virava vermelho FALSO do smoke. O env do CHAMADOR ainda manda (é o que
    permite `TTS_CHAT_BACKEND=dsh ./tests/live_ws.sh` medir o harness); o pino só
    impede o settings do dono de decidir sozinho.

    `TTS_CHAT_BACKEND_LIVE` é o MESMO caso, e ficou de fora por um tempo: ele manda
    sobre `chat_backend_live` e `TTS_CHAT_BACKEND=openai` só governa a Conversa —
    com o Live em dsh (por env do chamador OU por `chat_backend_live` no settings)
    a suíte saía do stub sem querer (#195)."""
    env = {**os.environ,
           "TTS_CHAT_BACKEND": os.environ.get("TTS_CHAT_BACKEND") or "openai",
           "TTS_CHAT_BACKEND_LIVE": BACKEND_LIVE,
           "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
           "TTS_CHAT_MODEL": "stub-live"}
    global LOG
    log = LOG = pathlib.Path("/tmp") / f"live_ws_servidor_{porta}.log"
    # ARQUIVO DE CHAVES PRÓPRIO (#435/#463): a suíte cria chave para conectar e, sem
    # isto, a gravação caía no `.apikeys.json` do dono (o conftest não alcança shell).
    # Os DOIS caminhos vão por AMBIENTE (#461) — o `.apikey` legado inclusive, porque
    # `_save_apikeys` espelha nele — e o arquivo nasce com uma chave, não vazio
    # (vazio faria `_load_apikeys` gerar uma e gravar por conta própria).
    chaves = pathlib.Path("/tmp") / f"live_ws_apikeys_{porta}.json"
    legado = chaves.with_suffix(".apikey")
    semente = "sk-teste-" + os.urandom(8).hex()
    chaves.write_text(json.dumps({"enabled": True, "keys": [
        {"id": "sessao-legado", "name": "smoke", "secret": semente,
         "created_at": "2026-01-01 00:00:00", "role": None}]}))
    legado.write_text(semente + "\n")
    global CHAVES, LEGADO, SAIDAS
    CHAVES, LEGADO = chaves, legado
    define_chave(semente)          # quem conecta é a chave DESTE arquivo
    # saídas e histórico também saem do diretório do dono (#463): a síntese grava
    # `.job-*`/`.wav` e o histórico de jobs é estado — nada disso é nosso
    SAIDAS = pathlib.Path("/tmp") / f"live_ws_saidas_{porta}"
    SAIDAS.mkdir(parents=True, exist_ok=True)
    # o cadastro vai SEMPRE para tmp (o `voices/` do repo não é tocado)
    (SAIDAS / "voices").mkdir(parents=True, exist_ok=True)
    env["TTS_VOICES_DIR"] = str(SAIDAS / "voices")
    # `settings` também: o servidor GRAVA nele (marca d'água, backend do chat) e sem
    # a env ele escreve no arquivo do dono. Sintético (`{}` + defaults do app): o
    # worktree não tem o arquivo e nada do dono é lido (#685/#720).
    settings = SAIDAS / "settings.json"
    settings.write_text("{}\n")
    env.update({"TTS_SETTINGS_PATH": str(settings),
                "TTS_APIKEYS_PATH": str(chaves),
                "TTS_LEGACY_APIKEY_PATH": str(legado),
                "TTS_OUTPUTS_DIR": str(SAIDAS),
                "TTS_JOBS_HIST_PATH": str(SAIDAS / "jobs-history.json")})
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(porta), "--log-level", "warning"],
        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    global _SERVIDOR, _PIDFILE
    _SERVIDOR = proc
    _PIDFILE = pathlib.Path("/tmp") / f"live_ws_servidor_{porta}_{os.getpid()}.pgid"
    try:
        _PIDFILE.write_text(str(os.getpgid(proc.pid)))
    except Exception:                         # noqa: BLE001
        _PIDFILE = None
    for _ in range(300):                      # até 60 s
        if proc.poll() is not None:
            raise SystemExit(f"servidor do smoke morreu na subida — log em {log}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{porta}/health", timeout=2).read()
            return proc
        except Exception:                     # noqa: BLE001
            time.sleep(0.2)
    mata_servidor(proc)
    raise SystemExit(f"servidor do smoke não subiu em 60 s — log em {log}")


#: servidor da rodada (para o trap de saída) e o pidfile do GRUPO dele
_SERVIDOR = None
_PIDFILE = None


def mata_servidor(proc):
    """Mata o GRUPO do servidor — `terminate()` sozinho deixa filho para trás."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:                         # noqa: BLE001 — já morreu
        try:
            proc.terminate()
        except Exception:                     # noqa: BLE001
            pass
    try:
        proc.wait(timeout=15)
    except Exception:                         # noqa: BLE001
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:                     # noqa: BLE001
            pass
    if _PIDFILE is not None:
        try:
            _PIDFILE.unlink()
        except Exception:                     # noqa: BLE001
            pass


def mata_servidores_antigos():
    """SIGTERM nos grupos de rodadas ANTERIORES que ficaram para trás (#688).

    O harness escolhe porta livre, então a rodada nova não sabe a porta da velha:
    o pidfile do grupo (`$$` no nome) é o que sobra. Sem
    isto, um SIGTERM no meio deixava uvicorn órfão (32 achados, 808 MB)."""
    mortos = 0
    for pidfile in pathlib.Path("/tmp").glob("live_ws_servidor_*.pgid"):
        # o nome carrega o PID do HARNESS dono do pidfile (`_{os.getpid()}.pgid`).
        # Harness VIVO é rodada concorrente (outro agente/worktree): não é órfão —
        # sem este filtro, quem começa derrubava o servidor de quem já estava
        # rodando. Órfão de verdade tem o harness morto e o servidor de pé.
        try:
            dono = int(pidfile.stem.rsplit("_", 1)[-1])
            os.kill(dono, 0)
            if dono != os.getpid():
                continue
        except (ValueError, OSError):
            pass
        try:
            pgid = int(pidfile.read_text().strip())
        except Exception:                     # noqa: BLE001
            pidfile.unlink(missing_ok=True)
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)   # sobra de rodada morta: avisa e segue
            print(f"  (matei servidor órfão da rodada anterior, grupo {pgid})")
            mortos += 1
        except OSError:
            pass
        pidfile.unlink(missing_ok=True)
    return mortos


def instala_traps():
    """SIGINT/SIGTERM/SIGHUP e saída normal passam TODOS por `mata_servidor`."""
    def _sai(signum, _frame):
        mata_servidor(_SERVIDOR)
        raise SystemExit(128 + signum)

    for sinal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sinal, _sai)


# (o `settings.json` da raiz não é lido: o estado é sempre sintético — #720)


# ---------------------------------------------------------------- cliente WS
async def um_turno(ws, pcm, medir_latencia=True):
    """Manda a fala e lê o turno. Devolve (eventos, primeiro_audio_ms, audio_bytes)."""
    for i in range(0, len(pcm), 3200):        # frames de 100 ms, como o cliente
        await ws.send(pcm[i:i + 3200])
        await asyncio.sleep(0.005)
    t0 = time.perf_counter()
    await ws.send(json.dumps({"type": "end_of_speech"}))
    eventos, primeiro, audio = [], None, 0
    while True:
        msg = await asyncio.wait_for(ws.recv(), 90)
        if isinstance(msg, (bytes, bytearray)):
            if primeiro is None:
                primeiro = (time.perf_counter() - t0) * 1000
            audio += len(msg)
            continue
        ev = json.loads(msg)
        eventos.append(ev)
        if ev["type"] in ("turn_complete", "error", "interrupted"):
            return eventos, primeiro, audio

BASE_UND = [0]          # `underruns` no fim do aquecimento (#492): o alvo é o delta


async def espera_ocioso(ws, limite=6.0):
    """Espera o motor voltar a `ocioso` depois do turno descartado (#489).

    Sem isto a injeção seguinte cai DURANTE o turno anterior e vira pendente — e o
    turno pendente não abre por `_live_abre_turno`, então o `decisao_para_audio_ms`
    sai `None` e o #315 reprova."""
    fim = time.perf_counter() + limite
    ocioso_seguidas = 0
    while time.perf_counter() < fim:
        try:
            msg = await asyncio.wait_for(ws.recv(), 1.0)
        except asyncio.TimeoutError:
            return
        if isinstance(msg, (bytes, bytearray)):
            ocioso_seguidas = 0        # áudio no fio = ainda tocando
            continue
        try:
            ev = json.loads(msg)
        except Exception:
            continue
        if ev.get("type") != "stats":
            continue
        BASE_UND[0] = (ev.get("playback") or {}).get("underruns", BASE_UND[0])
        # DUAS leituras seguidas de `ocioso`: uma só pode ser o respiro entre
        # chunks do turno que acabou
        motor = ev.get("motor") or {}
        # `turno_assistente_aberto` é o sinal de APP (o motor pode estar `ocioso`
        # com o turno ainda aberto no servidor e a injeção seguinte entra no MESMO
        # turno — foi o que fez o `decisao_para_audio_ms` sumir)
        if (motor.get("estado") == "ocioso" and not motor.get("tocando")
                and not motor.get("turno_assistente_aberto")):
            ocioso_seguidas += 1
            if ocioso_seguidas >= 2:
                await asyncio.sleep(0.5)
                return
        else:
            ocioso_seguidas = 0


async def principal():
    global BASE
    import websockets

    global CARGA_ABERTURA
    print("Live — smoke do ciclo completo (tasks #93/#104)")
    CARGA_ABERTURA = carga_por_nucleo()
    # Servidor EXTERNO: a chave vem do ambiente (`LIVE_WS_KEY`). No servidor
    # próprio quem define a chave é o `sobe_servidor`, com o arquivo sintético.
    if BASE:
        define_chave(_chave_da_instalacao())
        if not CHAVE:
            print("  (sem LIVE_WS_KEY: rodando sem chave — só passa de loopback)")
    voz, pcm = fala_de_teste()

    servidor, stub_porta = None, None
    if MODO != "config":
        stub_porta = sobe_stub()
        if not BASE:
            # servidor PRÓPRIO: o stub entra por env (nada é escrito no settings)
            porta_app = porta_livre()
            servidor = sobe_servidor(porta_app, stub_porta)
            BASE = f"http://127.0.0.1:{porta_app}"
            ok(f"servidor próprio em {BASE} + stub de LLM em 127.0.0.1:{stub_porta} "
               f"(por TTS_CHAT_*, sem tocar o settings.json)")
            if voz == "voz":          # fala veio da FIXTURE: cadastra como voz
                voz = registra_voz_de_teste("fixture", pcm)
                ok(f"voz de teste cadastrada no servidor da rodada ({voz})")
        else:
            ok(f"servidor já de pé em {BASE}; stub em 127.0.0.1:{stub_porta} "
               f"— só vale se o servidor foi subido com TTS_CHAT_BASE_URL (senão veja o aviso)")
    else:
        ok("usando o provedor de chat CONFIGURADO (precisa estar de pé)")
    try:
        http_get("/api/status")
    except Exception as e:                    # noqa: BLE001
        raise SystemExit(f"servidor não respondeu em {BASE} ({e})")
    print(f"  voz de teste: {voz} · fala de {len(pcm)/2/16000:.1f}s\n")

    uri = f"{BASE.replace('http', 'ws')}/api/live/ws?key={CHAVE}"
    try:
        async with websockets.connect(uri, max_size=None, ping_interval=None) as ws:
            # 1) handshake
            await ws.send(json.dumps({"type": "setup", "voice_id": voz,
                                      "system": "responda curto em pt-br"}))
            pronto = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if pronto.get("type") != "ready":
                falha(f"setup não devolveu ready: {pronto}")
                return
            ok(f"handshake: ready (sessão {pronto.get('session_id')}, "
               f"áudio {pronto.get('audio', {}).get('format')}@{pronto.get('audio', {}).get('sr')}Hz)")

            # 1.1) isolamento da ESCRITA (#463): a chave criada aqui tem de cair no
            # arquivo sintético, nunca no do dono. É a mordida do card: apontar de
            # volta para o caminho real faz este bloco reprovar.
            if CHAVES is not None:
                criada = http_post("/api/apikeys",
                                   {"name": "probe-live-ws", "role": "use"}
                                   )["key"]["secret"]
                if criada in CHAVES.read_text():
                    ok("chave da rodada gravada no arquivo sintético (dono intocado)")
                else:
                    falha("a chave criada NÃO caiu no arquivo sintético: isolamento furado")

            # 1.5) espera o pre-warm: antes dele o 1º turno paga a compilação dos
            # kernels dos modelos (medido: 7,3 s de 1º áudio contra 0,7 s depois)
            t_prev = time.perf_counter()
            while True:
                msg = await asyncio.wait_for(ws.recv(), 180)
                if isinstance(msg, (bytes, bytearray)):
                    continue
                ev = json.loads(msg)
                if ev.get("type") == "prewarm":
                    if ev.get("ok"):
                        ok(f"pre-warm concluído em {time.perf_counter()-t_prev:.1f}s "
                           f"(modelos quentes antes do 1º turno)")
                    else:
                        aviso(f"pre-warm falhou: {ev.get('message')}")
                    break
                if ev.get("type") == "error":
                    aviso(f"evento antes do turno: {ev}")
                    break

            # 1.6) TURNO DE AQUECIMENTO, descartado (#489): o alvo de 1,5 s só
            # vale com modelo QUENTE. Numa instância recém-subida ou recém-ociosa
            # (o dono tem `idle_unload_minutes`), o 1º turno paga a carga inteira —
            # medido: 2,3 s e até 17 s — e a suíte reprovava por AMBIENTE. O
            # descartado NÃO mede nada; se ele vier lento, sai como AVISO para o
            # número frio não virar mistério.
            t_aq = time.perf_counter()
            _, primeiro_aq, audio_aq = await asyncio.wait_for(um_turno(ws, pcm, False), 180)
            gasto_aq = time.perf_counter() - t_aq
            if audio_aq == 0:
                falha("turno de aquecimento não devolveu áudio (modelo não ficou quente)")
            elif gasto_aq > 3.0 or (primeiro_aq or 0) > ALVO_MS:
                aviso(f"aquecimento lento: {gasto_aq:.1f}s de turno, "
                      f"{primeiro_aq or 0:.0f} ms até o 1º áudio — modelo estava frio;"
                      " o alvo abaixo é medido no turno SEGUINTE")
            else:
                ok(f"aquecimento: turno descartado em {gasto_aq:.1f}s (modelo já quente)")
            await espera_ocioso(ws)

            # 2) turno completo + 3) latência até o 1º áudio — MEDIÇÃO com até 3
            # turnos quentes, fica o MELHOR (#489). Um turno só ainda reprovava por
            # máquina saturada (medido aqui: load 40, STT 1303 ms); latência é mínimo,
            # não média, e a sabotagem de latência continua reprovando nas 3.
            amostras, melhor = [], None
            for tentativa in range(1, 4):
                ev_t, pr_t, au_t = await asyncio.wait_for(um_turno(ws, pcm), 180)
                lat_t = next((e for e in reversed(ev_t) if e["type"] == "latency"), None)
                val = (lat_t or {}).get("decisao_para_audio_ms")
                amostras.append((pr_t, val))
                if au_t == 0:
                    continue
                if melhor is None or (val is not None and val < (melhor[3] if melhor[3] is not None else 10**9)):
                    melhor = (ev_t, pr_t, au_t, val)
                if val is not None and val <= ALVO_MS and pr_t <= ALVO_MS:
                    break
            if melhor is None:
                falha("nenhuma das tentativas devolveu áudio para medir latência")
                melhor = (ev_t, pr_t, au_t, None)
            eventos, primeiro, audio, _melhor_srv = melhor
            print("     amostras (cliente, servidor) ms: "
                  + " · ".join(f"({p if p is not None else '-'}, {v if v is not None else '-'})"
                               for p, v in amostras))
            tipos = [e["type"] for e in eventos]
            transcricao = next((e.get("text") for e in eventos if e["type"] == "transcript_user"), "")
            resposta = "".join(e.get("delta", "") for e in eventos if e["type"] == "assistant_text")
            if transcricao:
                ok(f"STT do turno: {transcricao[:60]!r}")
            else:
                falha("sem transcript_user (STT não transcreveu a fala de teste)")
            if resposta:
                ok(f"LLM em stream: {len(resposta)} chars em {sum(1 for t in tipos if t == 'assistant_text')} deltas")
            else:
                falha(f"sem assistant_text (deltas) — eventos: {tipos}")
            if audio > 0:
                ok(f"áudio do TTS no fio: {audio} bytes ({audio/2/24000:.1f}s)")
            else:
                falha("nenhum frame BINÁRIO de áudio recebido")
            if "turn_complete" in tipos:
                ok("turn_complete no fim do turno")
            else:
                falha(f"turno não fechou com turn_complete: {tipos}")
            # 2b) fome do buffer de playback (#492): o critério é ZERO vão novo num
            # turno longo. Máquina CARREGADA é a exceção declarada: se a carga por
            # núcleo no momento da medida passa de LIMITE_CARGA, o turno tolera até
            # 1 vão a cada 5 chunks como AVISO — o TTS não acompanha o tempo real
            # quando outras suítes disputam CPU (A/B do #463: a versão anterior
            # deste arquivo dava 4 vãos em 11 chunks contra 1 em 8, mesmo minuto).
            # Sem carga alta, 1 vão já reprova. A carga sai no texto da linha, para
            # ninguém precisar adivinhar por que a tolerância valeu.
            stats_ev = [e for e in eventos if e.get("type") == "stats"]
            if stats_ev:
                pb = (stats_ev[-1].get("playback") or {})
                n_chunks, n_und = pb.get("chunks", 0), pb.get("underruns", 0)
                novo = n_und - BASE_UND[0]     # delta desde o aquecimento
                # A carga que DECIDE é a do ponto medido, não a da abertura: a
                # pergunta é "a máquina estava disputada DURANTE o turno?". Com a
                # da abertura, máquina que esfriou no meio continuaria tolerando
                # regressão. As duas saem na linha para a decisão ser auditável.
                carga = carga_por_nucleo()
                limite = max(1, n_chunks // 5) if carga > LIMITE_CARGA else 0
                rotulo = (f"carga {carga:.2f}/núcleo agora, {CARGA_ABERTURA:.2f} na "
                          f"abertura")
                if n_chunks < 5:
                    falha(f"turno curto para medir underrun: {n_chunks} chunk(s), queria ≥5")
                elif novo == 0:
                    ok(f"playback sem underrun novo em {n_chunks} chunks "
                       f"(total {n_und}; {rotulo})")
                elif novo <= limite:
                    aviso(f"{novo} underrun(s) em {n_chunks} chunks — tolerado por CARGA "
                          f"({rotulo}; limite {limite}; buffer vazio)")
                else:
                    porque = (f"{rotulo}, limite {limite}" if limite
                              else f"máquina folgada ({rotulo}), critério é zero")
                    falha(f"{novo} underrun(s) NOVOS em {n_chunks} chunks ({porque})")
            if primeiro is not None:
                if primeiro <= ALVO_MS:
                    ok(f"fim-de-fala → 1º áudio: {primeiro:.0f} ms (alvo ≤ {ALVO_MS} ms) — OK")
                elif ALVO_VALE:
                    falha(f"fim-de-fala → 1º áudio: {primeiro:.0f} ms (alvo ≤ {ALVO_MS} ms)"
                          f" — ACIMA DO ALVO (segundo turno, após aquecimento de"
                          f" {gasto_aq:.1f}s: não é carga fria)")
                else:
                    print(f"     ⚠ fim-de-fala → 1º áudio: {primeiro:.0f} ms — acima do alvo do"
                          f" stub, mas o backend do Live é {BACKEND_LIVE!r}: o alvo de 1,5 s é do"
                          " cenário local/stub (#173), não deste. AVISO, não falha.")
                # o ÚLTIMO `latency` do turno: o pipeline emite um por chunk e o
                # primeiro vem com a conta de um estágio que já não é o começo
                lat = next((e for e in reversed(eventos) if e["type"] == "latency"), None)
                if lat:
                    print("     orçamento por estágio (ms): "
                          + " · ".join(f"{k}={lat.get(k)}" for k in
                                       ("stt_ms", "first_token_ms", "first_chunk_ms",
                                        "first_audio_ms", "decisao_para_audio_ms",
                                        "lock_espera_ms", "total_ms")))
                # #315: o número AUDITÁVEL é o do SERVIDOR, com a decisão do VAD
                # incluída (o `primeiro` acima é medido pelo cliente a partir do
                # `end_of_speech`, que não paga a decisão). Sem o campo o alvo do
                # épico não é cobrável, e daí o gate tem de saber.
                if lat and lat.get("decisao_para_audio_ms") is not None:
                    d = int(lat["decisao_para_audio_ms"])
                    if d <= ALVO_MS:
                        ok(f"fim-de-fala → 1º áudio pelo SERVIDOR, decisão incluída: "
                           f"{d} ms (alvo ≤ {ALVO_MS} ms) — OK")
                    elif ALVO_VALE:
                        falha(f"fim-de-fala → 1º áudio pelo SERVIDOR, decisão incluída: "
                              f"{d} ms (alvo ≤ {ALVO_MS} ms) — ACIMA DO ALVO")
                    else:
                        print(f"     ⚠ servidor (decisão incluída): {d} ms — acima do alvo,"
                              " mas o backend não é o stub (AVISO, ver #173).")
                elif ALVO_VALE:
                    falha("o evento `latency` não trouxe `decisao_para_audio_ms` (#315)")
            else:
                falha("nenhum áudio chegou para medir latência")

            # 4) barge-in: cancel no meio do playback
            eventos2, _, audio2 = await asyncio.wait_for(um_turno(ws, pcm), 120)
            lat2 = next((e for e in eventos2 if e["type"] == "latency"), None)
            if lat2:
                print("     turno 2 (diagnóstico): "
                      + " · ".join(f"{k}={lat2.get(k)}" for k in
                                   ("stt_ms", "first_token_ms", "first_chunk_ms",
                                    "first_audio_ms")))
            await ws.send(json.dumps({"type": "cancel"}))
            t0 = time.perf_counter()
            interrompido, depois = False, 0
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), 5)
                    if isinstance(msg, (bytes, bytearray)):
                        depois += len(msg)
                        continue
                    if json.loads(msg)["type"] == "interrupted":
                        interrompido = True
                        break
            except asyncio.TimeoutError:
                pass
            if interrompido:
                ok(f"cancel → interrupted em {(time.perf_counter()-t0)*1000:.0f} ms")
            else:
                falha("cancel não devolveu interrupted")
            await asyncio.sleep(0.4)
            ok(f"(turno anterior tinha {audio2} bytes; após o cancel chegaram {depois} bytes)")
    finally:
        if servidor is not None:
            mata_servidor(servidor)
            ok("servidor do smoke encerrado (e o stub com ele)")

    limpa_sobras(manter_log=bool(falhas))
    print()
    if falhas:
        print(f"✘ {len(falhas)} falha(s) (log do servidor: {LOG}):")
        for f in falhas:
            print(f"   - {f}")
        sys.exit(1)
    print("✔ OK — ciclo completo, latência medida e barge-in respondendo"
      + ("" if ALVO_VALE else f" (alvo de {ALVO_MS} ms é do cenário stub; Live em {BACKEND_LIVE!r})"))
    if avisos:
        print(f"({len(avisos)} aviso(s))")


if "--mata-orfaos" in sys.argv[1:]:
    # sem rodar a suíte: serve para limpar sobra a qualquer momento (um SIGKILL no
    # script não roda trap nenhum e o servidor fica até alguém varrer) (#688)
    quantos = mata_servidores_antigos()
    print(f"✔ {quantos} servidor(es) órfão(s) do harness encerrado(s)")
    sys.exit(0)

mata_servidores_antigos()             # #688: rodada anterior não pode ficar de pé
instala_traps()
try:
    asyncio.run(principal())
except BaseException:                 # crash do driver (ex.: TypeError) também
    mata_servidor(_SERVIDOR)          # servidor some INTEIRO, inclusive por sinal
    limpa_sobras(manter_log=True)     # tem de limpar: o log fica, o resto não
    raise
else:
    mata_servidor(_SERVIDOR)
PY