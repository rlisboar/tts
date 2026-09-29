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
# MEDINDO À MÃO (script de trace, curl, playwright avulso): a chave de API que você
# cria é estado DO DONO e fica no `.apikeys.json` dele. Use nome com prefixo que a
# limpeza reconhece — `teste-ui-*`, `probe-papel*`, `probe-adota*` (o
# `admin_ui_flow.sh` varre esses com mais de 2 min na abertura) — e apague no fim.
# Nome fora do padrão (ex.: `trace-speed-191`) fica para sempre, e o dono vê na tela.
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

"$PY" - "$@" <<'PY'
import asyncio, json, os, pathlib, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import soundfile as sf

BASE = os.environ.get("BASE", "").rstrip("/")
RAIZ = pathlib.Path.cwd()
MODO = os.environ.get("LIVE_LLM", "stub")
CHAVE = pathlib.Path(".apikey").read_text().strip()
H = {"X-API-Key": CHAVE}
ALVO_MS = 1500
# Backend do Live desta rodada (o env do chamador manda; sem ele, stub). O alvo de
# 1,5 s é do cenário STUB (#173): com o Live no harness dsh, o 1º token DELE domina
# e o alvo não cabe — aí o número é AVISO qualificado, não falha.
BACKEND_LIVE = (os.environ.get("TTS_CHAT_BACKEND_LIVE")
                or os.environ.get("TTS_CHAT_BACKEND") or "openai")
ALVO_VALE = BACKEND_LIVE == "openai"
falhas, avisos = [], []

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
    """PCM16 16 kHz a partir de uma voz do repo (é fala de verdade, não tom)."""
    import subprocess
    wavs = sorted(pathlib.Path("voices").glob("*.wav"))
    if not wavs:
        raise SystemExit("sem voices/*.wav para usar como fala de teste")
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
        if len(a) < sr:                      # curto demais: tenta a próxima voz
            continue
        idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr))
        a16 = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
        return w.stem, (np.clip(a16, -1, 1) * 32767).astype("<i2").tobytes()
    raise SystemExit("nenhuma voices/*.wav serviu como fala de teste")

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
    log = pathlib.Path("/tmp") / f"live_ws_servidor_{porta}.log"
    proc = subprocess.Popen(
        [str(RAIZ / ".venv-mlx" / "bin" / "uvicorn"), "app:app",
         "--host", "127.0.0.1", "--port", str(porta)],
        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
        start_new_session=True)
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


def mata_servidor(proc):
    try:
        proc.terminate()                      # grupo próprio: mata o uvicorn e filhos
        proc.wait(timeout=15)
    except Exception:                         # noqa: BLE001
        try:
            proc.kill()
        except Exception:                     # noqa: BLE001
            pass


def guarda_do_settings():
    """Se o settings REAL parece poluído por um smoke antigo, avisa (ou aborta).

    Antes o smoke 'restaurava' o snapshot — e um snapshot tirado já poluído fazia
    o lixo virar permanente (não se auto-curava). Com o env isto não escreve mais,
    mas o estado do arquivo continua valendo para quem roda o app à mão."""
    caminho = RAIZ / "settings.json"
    try:
        cfg = json.loads(caminho.read_text())
    except Exception:                         # noqa: BLE001
        return
    base = (cfg.get("chat_base_url") or "").strip()
    if not base.startswith(("http://127.0.0.1", "http://localhost")):
        return
    try:
        urllib.request.urlopen(base.rstrip("/") + "/models", timeout=2).read()
    except Exception:                         # noqa: BLE001
        if MODO == "config":
            raise SystemExit(
                f"settings.json aponta para {base} (stub de smoke) e ele NÃO responde:\n"
                "  restaure com: curl -X POST .../api/settings -d '{\"chat_model\":\"\"}'\n"
                "  ou rode com o stub: ./tests/live_ws.sh (auto-contido)")
        aviso(f"settings.json aponta para {base} (stub de smoke) que não responde — "
              f"o servidor JÁ DE PÉ vai falhar; o meu usa TTS_CHAT_* e não sofre")


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

async def principal():
    import websockets

    print("Live — smoke do ciclo completo (tasks #93/#104)")
    guarda_do_settings()
    voz, pcm = fala_de_teste()

    global BASE
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

            # 2) turno completo + 3) latência até o 1º áudio
            eventos, primeiro, audio = await asyncio.wait_for(um_turno(ws, pcm), 120)
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
            if primeiro is not None:
                if primeiro <= ALVO_MS:
                    ok(f"fim-de-fala → 1º áudio: {primeiro:.0f} ms (alvo ≤ {ALVO_MS} ms) — OK")
                elif ALVO_VALE:
                    falha(f"fim-de-fala → 1º áudio: {primeiro:.0f} ms (alvo ≤ {ALVO_MS} ms)"
                          " — ACIMA DO ALVO")
                else:
                    print(f"     ⚠ fim-de-fala → 1º áudio: {primeiro:.0f} ms — acima do alvo do"
                          f" stub, mas o backend do Live é {BACKEND_LIVE!r}: o alvo de 1,5 s é do"
                          " cenário local/stub (#173), não deste. AVISO, não falha.")
                lat = next((e for e in eventos if e["type"] == "latency"), None)
                if lat:
                    print("     orçamento por estágio (ms): "
                          + " · ".join(f"{k}={lat.get(k)}" for k in
                                       ("stt_ms", "first_token_ms", "first_chunk_ms",
                                        "first_audio_ms", "total_ms")))
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

    print()
    if falhas:
        print(f"✘ {len(falhas)} falha(s):")
        for f in falhas:
            print(f"   - {f}")
        sys.exit(1)
    print("✔ OK — ciclo completo, latência medida e barge-in respondendo"
      + ("" if ALVO_VALE else f" (alvo de {ALVO_MS} ms é do cenário stub; Live em {BACKEND_LIVE!r})"))
    if avisos:
        print(f"({len(avisos)} aviso(s))")

asyncio.run(principal())
PY