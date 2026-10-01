"""#222 — id do cliente JÁ VIVO: a sessão antiga saía do registro e ficava viva.

Repro do achado (gate #215, `evidence/215-210-teto.py`): com o sid adotado do
cliente (#210), o registro é chaveado por ele. Um id REPETIDO sobrescreve a
entrada e a sessão antiga continua viva (engine/pipeline próprios) mas FORA do
registro — fora do sweep por TTL (nunca vencida, só morria se o cliente
fechasse) e fora da contagem do teto.

Mede o ANTES e o DEPOIS no mesmo script (o app é o do disco):

  A) 3 sockets com o MESMO id, teto=1 → quantos `ready`, tamanho do registro;
  B) os sockets ANTIGOS recebem fechamento do servidor? (espera com teto)
  C) id DIFERENTE com o teto cheio segue levando `busy` (semântica preservada).

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/222-id-repetido.py
"""
import logging
import os
import queue
import sys
import threading

os.environ.setdefault("TTS_LIVE_WORKER", "0")
os.environ["TTS_ROD_API_KEY"] = "chave-do-222"
os.environ["TTS_LIVE_MAX_SESSIONS"] = "1"        # o teto MAIS apertado

import app                                     # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

logging.getLogger("live").setLevel(logging.WARNING)
cli = TestClient(app.app)
HEADERS = {"x-api-key": "chave-do-222"}
falhas = []


def conecta():
    return cli.websocket_connect("/api/live/ws", headers=HEADERS).__enter__()


def setup(ws, sid):
    ws.send_json({"type": "setup", "session_id": sid})
    return ws.receive_json()


def recebe_em(ws, teto_s=3.0):
    """Próximo frame com teto (o receive do TestClient não tem timeout)."""
    caixa = queue.Queue()

    def le():
        try:
            caixa.put(ws.receive_json())
        except Exception as exc:               # noqa: BLE001 — fechou: é o dado
            caixa.put(exc)
    t = threading.Thread(target=le, daemon=True)
    t.start()
    try:
        return caixa.get(timeout=teto_s)
    except queue.Empty:
        return None


# A) três sockets, MESMO id, teto=1
prontos = []
for _ in range(3):
    ws = conecta()
    prontos.append((ws, setup(ws, "mesmo-id")))

aceitos = sum(1 for _, r in prontos if r.get("type") == "ready")
print(f"A) mesmo id x3 com TTS_LIVE_MAX_SESSIONS=1 -> {aceitos} ready(s), "
      f"registro={len(app._live_sessions)}")
if aceitos == 3:
    print("   ✔ os 3 abriram (o teto NÃO barra id repetido — desenho do #210)")
else:
    falhas.append(f"esperava 3 aceitos, veio {aceitos}")

# B) os dois ANTIGOS têm de ser fechados pelo servidor (antes: ficavam vivos)
antigos = prontos[:2]
recebidos = [recebe_em(ws) for ws, _ in antigos]
codigos = [r.get("code") if isinstance(r, dict) else repr(r) for r in recebidos]
print(f"B) fechamento nos sockets antigos -> {codigos}")
if codigos == ["session_substituida", "session_substituida"]:
    print("   ✔ os antigos saíram pelo caminho do fechamento, com motivo próprio")
else:
    falhas.append(f"antigos não receberam `session_substituida`: {codigos}")

# o último segue no registro e ATENDENDO
novo = app._live_sessions.get("mesmo-id")
prontos[2][0].send_json({"type": "ping"})
pong = recebe_em(prontos[2][0])
print(f"   último socket vivo -> {pong}")
if not (isinstance(pong, dict) and pong.get("type") == "pong"):
    falhas.append(f"o socket novo não respondeu ping: {pong}")
if novo is not None and app._live_sessions.get("mesmo-id") is novo:
    print("   ✔ o registro é do socket NOVO")
else:
    falhas.append("registro não é do socket novo")

# C) id DIFERENTE com o teto cheio: `busy` preservado
ws4 = conecta()
r4 = setup(ws4, "outro-id")
print(f"C) id diferente com o teto cheio -> {r4.get('type')} / {r4.get('code')}")
if r4.get("type") == "error" and r4.get("code") == "busy":
    print("   ✔ o `busy` segue valendo para id NOVO")
else:
    falhas.append(f"esperava error/busy para id novo, veio {r4}")

for ws, _ in prontos:
    ws.close()

print(f"\nfalhas: {len(falhas)}")
for f in falhas:
    print(f"  - {f}")
sys.exit(1 if falhas else 0)
