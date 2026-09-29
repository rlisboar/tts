"""GATE #215 — observação do item 3 (#210): o id do cliente passa a ser ADOTADO, e
o registro é chaveado por ele. Reconectar com o MESMO id sobrescreve a entrada
(`_live_sessions[sid] = sess`), deixando a sessão ANTIGA viva fora do registro.

Consequência medida aqui (o teto é decidido por `len(_live_sessions)`):

  A) com `TTS_LIVE_MAX_SESSIONS=1`, três sockets com o MESMO id são todos aceitos
     (o teto de 1 sessão não barra o 2º/3º) — cada um com engine/pipeline próprio;
  B) um socket com id DIFERENTE, no mesmo momento, leva `busy`.

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-210-teto.py
"""
import logging
import os
import sys

os.environ.setdefault("TTS_LIVE_WORKER", "0")
os.environ["TTS_ROD_API_KEY"] = "chave-do-gate-215"
os.environ["TTS_LIVE_MAX_SESSIONS"] = "1"        # o teto MAIS apertado

import app                                     # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

logging.getLogger("live").setLevel(logging.WARNING)
cli = TestClient(app.app)
HEADERS = {"x-api-key": "chave-do-gate-215"}
falhas = []


def conecta():
    return cli.websocket_connect("/api/live/ws", headers=HEADERS).__enter__()


def setup(ws, sid):
    ws.send_json({"type": "setup", "session_id": sid})
    return ws.receive_json()


# três sockets, MESMO id, teto=1
prontos = []
for _ in range(3):
    ws = conecta()
    prontos.append((ws, setup(ws, "mesmo-id")))

aceitos = sum(1 for _, r in prontos if r.get("type") == "ready")
print(f"  mesmo id ×3 com TTS_LIVE_MAX_SESSIONS=1 → {aceitos} ready(s), "
      f"registro={len(app._live_sessions)}")
if aceitos == 3:
    print("  ✔ os 3 foram aceitos (o teto não barra id repetido)")
else:
    falhas.append(f"esperava 3 aceitos, veio {aceitos}: {[r for _, r in prontos]}")

# socket com id DIFERENTE: o teto barra (é a semântica do busy, preservada)
ws4 = conecta()
r4 = setup(ws4, "outro-id")
print(f"  id diferente com o teto cheio → {r4.get('type')}")
if r4.get("type") == "error" and "busy" in str(r4.get("error", r4)):
    print("  ✔ o `busy` segue valendo para id NOVO")
else:
    falhas.append(f"esperava error/busy para id novo, veio {r4}")

for ws, _ in prontos:
    ws.close()

print(f"\nfalhas: {len(falhas)}")
for f in falhas:
    print(f"  - {f}")
sys.exit(1 if falhas else 0)