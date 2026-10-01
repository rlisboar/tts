#!/usr/bin/env python3
"""GATE #194 — repro POR FORA (processo real, cliente websockets real, sem TestClient):

  · #187: handshake do Live SEM voz gravada → frame `error{setup_invalido}` com
    mensagem e close 4400 (o cliente não pendura); a instância continua viva.
  · #188 (contrato de MOMENTO, medido na ordem do FIO): com o teto cheio, o socket
    aberto sem `setup` recebe `setup_timeout` (não `busy`) — a checagem saiu do
    connect; mandando `setup`, aí sim `busy` + close 1013.
"""
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

RAIZ = pathlib.Path("/Users/lisboa/Documents/tts-rod")
PY = str(RAIZ / ".venv-mlx/bin/python")
falhas = []


def cobrar(c, m):
    print(("  ok   " if c else "  FALHA ") + m)
    if not c:
        falhas.append(m)


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sobe(extra: str, porta: int):
    launcher = f"""
import pathlib, app
{extra}
import uvicorn
uvicorn.run(app.app, host="127.0.0.1", port={porta}, log_level="warning")
"""
    env = {**os.environ, "ORT_DISABLE_TELEMETRY": "1"}
    log = pathlib.Path(f"/tmp/gate194-{os.getpid()}/srv_{porta}.log")
    p = subprocess.Popen([PY, "-c", launcher], cwd=str(RAIZ), env=env,
                         stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(300):
        if p.poll() is not None:
            raise SystemExit(f"servidor morreu (log {log})")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{porta}/health", timeout=2).read()
            return p
        except Exception:
            time.sleep(0.2)
    raise SystemExit("servidor não subiu")


async def cenario_187(porta):
    import websockets
    uri = f"ws://127.0.0.1:{porta}/api/live/ws"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "setup"}))
        t0 = time.monotonic()
        frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
        dt = time.monotonic() - t0
        print(f"  frame do handshake sem voz: {frame} ({dt:.2f}s)")
        cobrar(frame.get("type") == "error" and frame.get("code") == "setup_invalido",
               f"veio frame de erro com o código do contrato: {frame}")
        cobrar("voz" in (frame.get("message") or "").lower(),
               f"a mensagem diz o que falta (voz): {frame.get('message')!r}")
        try:
            await asyncio.wait_for(ws.recv(), 10)
            cobrar(False, "o socket não fechou depois do erro")
        except Exception as exc:
            print(f"  fecho depois do erro: {type(exc).__name__} {exc}")
            cobrar("4400" in str(exc), f"o close tem de ser 4400: {exc}")
    # a instância não ficou pendurada: outra conexão responde o mesmo
    async with websockets.connect(uri) as ws2:
        await ws2.send(json.dumps({"type": "setup"}))
        f2 = json.loads(await asyncio.wait_for(ws2.recv(), 10))
        cobrar(f2.get("code") == "setup_invalido", "a 2ª conexão também recebe o erro (não pendura)")


async def cenario_188(porta):
    import websockets
    uri = f"ws://127.0.0.1:{porta}/api/live/ws"
    async with websockets.connect(uri) as ws1:
        await ws1.send(json.dumps({"type": "setup"}))
        pronto = json.loads(await asyncio.wait_for(ws1.recv(), 15))
        cobrar(pronto.get("type") == "ready", f"1ª sessão pronta (teto=1): {pronto.get('type')}")
        # (a) socket aberto com o teto cheio e SEM setup → o 1º frame é o timeout
        async with websockets.connect(uri) as ws2:
            f = json.loads(await asyncio.wait_for(ws2.recv(), 15))
            cobrar(f.get("code") == "setup_timeout",
                   f"antes do setup não pode vir `busy` (veio {f.get('code')!r})")
        # (b) mandando o setup: aí sim busy, com 1013
        async with websockets.connect(uri) as ws3:
            await ws3.send(json.dumps({"type": "setup"}))
            f3 = json.loads(await asyncio.wait_for(ws3.recv(), 15))
            cobrar(f3.get("code") == "busy", f"teto cheio + setup → busy (veio {f3.get('code')!r})")
            try:
                await asyncio.wait_for(ws3.recv(), 10)
                cobrar(False, "o busy não fechou o socket")
            except Exception as exc:
                cobrar("1013" in str(exc), f"o close do busy tem de ser 1013: {exc}")


procs = []
try:
    # ─── servidor A: SEM voz (VOICES_DIR vazio e sem presets) ────────────────
    vazio = pathlib.Path(f"/tmp/gate194-{os.getpid()}/voices-vazio")
    vazio.mkdir(parents=True, exist_ok=True)
    pa = porta_livre()
    procs.append(sobe(f'app.VOICES_DIR = pathlib.Path("{vazio}")\napp.OMNI_PRESETS = {{}}', pa))
    print(f"servidor A (sem voz) em :{pa}")
    asyncio.run(cenario_187(pa))
    cobrar(urllib.request.urlopen(f"http://127.0.0.1:{pa}/health", timeout=5).status == 200,
           "a instância segue viva depois dos handshakes recusados")

    # ─── servidor B: normal, teto=1, timeout de setup curto ─────────────────
    pb = porta_livre()
    procs.append(sobe('app._LIVE_MAX_SESSIONS = 1\napp._LIVE_SETUP_TIMEOUT_S = 2.0\n'
                      'app._settings["chat_backend"] = "openai"\n'
                      'app._settings["chat_backend_live"] = ""', pb))
    print(f"servidor B (teto=1) em :{pb}")
    asyncio.run(cenario_188(pb))
finally:
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), 15)
        except Exception:
            p.terminate()

print("\n✖ FALHAS:" if falhas else "\n✔ #187 e #188 (momento) OK por fora")
for f in falhas:
    print("  -", f)
sys.exit(1 if falhas else 0)