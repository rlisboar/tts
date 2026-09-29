"""GATE #215 item 2 (#209) — INVARIANTE: nenhum `await` com `_live_lock` preso.

Este é o par determinístico do efeito (`tests/live_lock_freeze.sh`, servidor real com
peer que não lê): aqui um espião no `WebSocket.send_json` registra `_live_lock.locked()`
no INSTANTE do envio do `error{busy}` — o único ponto que tinha `await` dentro do
`with _live_lock`.

O que NÃO vale (aviso do PM): medir depois do envio — o lock já soltou nos dois
mundos. E o `TestClient` abre um portal/loop POR conexão, então aqui o fenômeno de
congelamento não aparece: isto é invariante, não efeito.

`--mutacao` emula o desenho antigo (o launcher segura o lock em volta do envio):
o espião passa a ver `True` — é o que prova que o espião distingue os dois mundos.

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-209-invariante.py [--mutacao]
"""
import os
import sys
import time

os.environ.setdefault("TTS_LIVE_WORKER", "0")
os.environ["TTS_ROD_API_KEY"] = "chave-do-gate-215"
os.environ["TTS_LIVE_MAX_SESSIONS"] = "1"        # a 2ª conexão é o caminho do busy

import app                                    # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402
from starlette.websockets import WebSocket    # noqa: E402

MUTACAO = "--mutacao" in sys.argv
KEY = {"x-api-key": "chave-do-gate-215"}
cli = TestClient(app.app)
vistos = []
falhas = []
_original = WebSocket.send_json


async def espiao(self, dado, *a, **k):
    if isinstance(dado, dict) and dado.get("code") == "busy":
        if MUTACAO:                              # desenho antigo: lock preso no envio
            meu = app._live_lock.acquire(blocking=False)
            vistos.append(app._live_lock.locked())
            if meu:
                app._live_lock.release()
        else:
            vistos.append(app._live_lock.locked())
    return await _original(self, dado, *a, **k)


def ok(m):
    print(f"  ✔ {m}")


def falha(m):
    falhas.append(m)
    print(f"  ✘ {m}")


def conecta():
    return cli.websocket_connect("/api/live/ws", headers=KEY).__enter__()


def main():
    WebSocket.send_json = espiao
    with app._live_lock:
        app._live_sessions.clear()

    ws1 = conecta()
    ws1.send_json({"type": "setup"})
    pronto = ws1.receive_json()
    if pronto.get("type") == "ready":
        ok("1ª conexão ocupa o único slot (ready)")
    else:
        falha(f"1ª conexão não abriu: {pronto}")

    ws2 = conecta()
    ws2.send_json({"type": "setup"})             # o teto é checado DEPOIS do setup
    ev = ws2.receive_json()
    fecha = ws2.receive()
    if ev.get("type") == "error" and ev.get("code") == "busy" and fecha.get("code") == 1013:
        ok("2ª conexão recebe `error{busy}` e close 1013 (semântica preservada)")
    else:
        falha(f"busy não saiu como antes: {ev} / {fecha}")
    if vistos and vistos[0] is False:
        ok("no INSTANTE do envio do busy o `_live_lock` está LIVRE (o invariante)")
    else:
        falha(f"`_live_lock` preso no instante do envio: vistos={vistos}")
    ws2.close()

    # o slot não ficou preso: fechando a 1ª, a 3ª entra
    ws1.close()
    limite = time.time() + 3
    while time.time() < limite and app._live_sessions:
        time.sleep(0.02)
    ws3 = conecta()
    ws3.send_json({"type": "setup"})
    if ws3.receive_json().get("type") == "ready":
        ok("depois do busy o slot libera e a conexão seguinte entra (nada ficou preso)")
    else:
        falha("slot/estado preso depois do caminho do busy")
    ws3.close()

    WebSocket.send_json = _original
    print(f"\nfalhas: {len(falhas)}")
    for f in falhas:
        print(f"  - {f}")
    print("VEREDITO:", "OK" if not falhas else "FALHOU")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())