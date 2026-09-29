"""GATE #215 item 2 (#209) — PROVA DE EFEITO, independente do harness do autor.

`tests/live_lock_freeze.sh` é do api-backend; este script é a minha re-derivação,
com código próprio e SEM a trava de modelo (servidor com `TTS_LIVE_WORKER=0`, então
nada de Metal e nada de esperar a fila).

O MECANISMO (o que faz o congelamento existir):
  · `_live_lock` é `threading.Lock`, pego por código SÍNCRONO no handler
    (`_live_sweep()`, chamado assim que a conexão é aceita) — ou seja, na THREAD do
    event loop;
  · se o `await ws.send_json(error{busy})` acontece com o lock preso e o send
    suspende, a conexão que chega depois bloqueia a thread do loop em
    `Lock.acquire()`; como quem segura é a MESMA thread, o loop não roda mais e a
    corrotina que dormia não retoma — auto-deadlock, não lentidão.

CENAS (é o par que impede verde de fachada):
  A) `preso=False` — o envio do `busy` acontece com o lock LIVRE (é o fix).
     `/health` TEM de responder.
  B) `preso=True`  — o launcher segura o lock em volta do envio (estado revertido).
     `/health` TEM de estourar o timeout. Prova que a medida enxerga o fenômeno.

O envio fica pendurado de propósito: um patch no `WebSocket.send_json` do SERVIDOR
espera um arquivo-marca e dorme `SONO` no frame `busy`. A 3ª conexão só é aberta
depois da marca, então a medida é sempre "com o envio preso" — nunca antes.

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-209-efeito.py
"""
import base64
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SONO = float(os.environ.get("SONO", "0.9"))
TIMEOUT = 2.0
falhas = []


def ok(m):
    print(f"  ✔ {m}")


def falha(m):
    falhas.append(m)
    print(f"  ✘ {m}")


def porta_livre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LAUNCHER = '''
import asyncio, os, pathlib, sys, time
sys.path.insert(0, {raiz!r})
import app
from starlette.websockets import WebSocket
MARCA = pathlib.Path({marca!r})
PRESO = {preso!r}
_orig = WebSocket.send_json

async def lento(self, dado, *a, **k):
    if isinstance(dado, dict) and dado.get("code") == "busy":
        meu = False
        if PRESO:                       # emula o estado revertido
            meu = app._live_lock.acquire(blocking=False)
        MARCA.write_text("1")           # a partir daqui o envio está pendurado
        await asyncio.sleep({sono})
        if meu:
            app._live_lock.release()
    return await _orig(self, dado, *a, **k)

WebSocket.send_json = lento
import uvicorn
uvicorn.run(app.app, host="127.0.0.1", port={porta}, log_level="warning")
'''


def health(base, limite=TIMEOUT):
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(base + "/health", timeout=limite) as r:
            r.read()
        return time.monotonic() - t0, True
    except Exception:                                   # noqa: BLE001
        return time.monotonic() - t0, False


def conecta(porta, exige=True, tempo=5.0):
    """Handshake WS na mão; depois NENHUMA leitura (o buffer do peer só cresce).

    `exige=False` é da 3ª conexão: com o loop CONGELADO o 101 pode não sair — o
    `accept()` só enfileira a resposta e quem a escreve é outra task, que o
    congelamento não deixa rodar. O socket segue servindo: o handler do servidor já
    está parado no `_live_sweep()`, que é o que a cena mede."""
    s = socket.create_connection(("127.0.0.1", porta), timeout=tempo)
    chave = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /api/live/ws HTTP/1.1\r\nHost: 127.0.0.1:{porta}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {chave}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            pedaco = s.recv(1)
            if not pedaco:
                raise RuntimeError("handshake morreu")
            buf += pedaco
    except TimeoutError:
        if exige:
            raise
    return s


def setup(s):
    carga = json.dumps({"type": "setup"}).encode()
    mascara = os.urandom(4)
    s.sendall(bytes([0x81, 0x80 | len(carga)]) + mascara
              + bytes(b ^ mascara[i % 4] for i, b in enumerate(carga)))


def cena(preso):
    porta = porta_livre()
    base = f"http://127.0.0.1:{porta}"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="efeito209-"))
    marca = tmp / "no_send"
    (tmp / "servidor.py").write_text(LAUNCHER.format(raiz=str(RAIZ), marca=str(marca),
                                                     preso=preso, sono=SONO, porta=porta))
    ambiente = {**os.environ, "TTS_LIVE_WORKER": "0", "TTS_LIVE_MAX_SESSIONS": "1",
                "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai"}
    log = open(tmp / "servidor.log", "w")
    proc = subprocess.Popen([sys.executable, str(tmp / "servidor.py")], env=ambiente,
                            stdout=log, stderr=subprocess.STDOUT)
    abertos = []
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except Exception:                           # noqa: BLE001
                time.sleep(0.25)
        else:
            raise RuntimeError("servidor não subiu")
        ocioso_s, _ = health(base)
        a = conecta(porta); abertos.append(a); setup(a)
        time.sleep(0.4)
        b = conecta(porta); abertos.append(b); setup(b)      # -> busy (teto = 1)
        for _ in range(400):
            if marca.exists():
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("o `busy` nunca foi enviado")
        c = conecta(porta, exige=False, tempo=1.5); abertos.append(c)   # `_live_sweep`
        time.sleep(0.25)
        probe_s, respondeu = health(base)
        return {"ocioso_s": ocioso_s, "probe_s": probe_s, "respondeu": respondeu}
    except Exception:                                       # noqa: BLE001
        log.flush()
        print("    ── log do servidor ──")
        print("      " + "\n      ".join((tmp / "servidor.log").read_text().splitlines()[-15:]))
        raise
    finally:
        log.close()
        for s in abertos:
            try:
                s.close()
            except Exception:                               # noqa: BLE001
                pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                   # noqa: BLE001
            proc.kill()


def main():
    print(f"  · cena A — `await` com o lock LIVRE (o fix): /health TEM de responder")
    ca = cena(preso=False)
    print(f"    ocioso {ca['ocioso_s'] * 1000:.0f} ms · probe {ca['probe_s'] * 1000:.0f} ms "
          f"respondeu={ca['respondeu']}")
    if ca["respondeu"]:
        ok("cena A: o processo seguiu atendendo com o `busy` pendurado")
    else:
        falha("cena A congelou — o `await` está sob o lock?")

    print("  · cena B (controle) — lock SEGURADO em volta do envio (revertido): "
          "TEM de congelar")
    cb = cena(preso=True)
    print(f"    ocioso {cb['ocioso_s'] * 1000:.0f} ms · probe {cb['probe_s'] * 1000:.0f} ms "
          f"respondeu={cb['respondeu']}")
    if not cb["respondeu"]:
        ok(f"cena B: congelou (o probe de {TIMEOUT:.0f} s estourou) — a medida "
           f"distingue os dois mundos")
    else:
        falha(f"cena B NÃO congelou ({cb['probe_s'] * 1000:.0f} ms): a medida não "
              f"distingue os dois mundos")

    print(f"\nfalhas: {len(falhas)}")
    for f in falhas:
        print(f"  - {f}")
    print("VEREDITO:", "OK" if not falhas else "FALHOU")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())