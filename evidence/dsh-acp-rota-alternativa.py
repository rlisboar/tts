"""DSH-4b: o giro existe também em OUTRA rota? (contornar de produto)

Mesma medição de intervalo do `dsh-acp-wire-probe.py`, agora contra o OpenRouter
(as duas rotas de lá estão no settings.yaml). Se aqui o stream for contínuo, trocar
de rota é um contornar real; se girar igual, é o comportamento do nosso consumo.

Uso: python3 evidence/dsh-acp-rota-alternativa.py [modelo] [rodadas]
"""
import json
import re
import socket
import ssl
import sys
import time
from pathlib import Path

MODELO = sys.argv[1] if len(sys.argv) > 1 else "z-ai/glm-5.3-flash"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3
PROMPT = "Responda em 2 frases, conversando: por que o ceu e azul?"
HOST = "openrouter.ai"
CAMINHO = "/api/v1/chat/completions"

cred = Path.home() / ".dsh/.credentials.yaml"
KEY = re.search(r"OPENROUTER_API_KEY:\s*(\S+)", cred.read_text()).group(1).strip().strip('"\'')
try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()


def rodada(i):
    corpo = json.dumps({"model": MODELO, "stream": True,
                        "messages": [{"role": "user", "content": PROMPT}]}).encode()
    req = (f"POST {CAMINHO} HTTP/1.1\r\nHost: {HOST}\r\nAuthorization: Bearer {KEY}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(corpo)}\r\n"
           "Accept: text/event-stream\r\nUser-Agent: tts-studio/1.0\r\nConnection: close\r\n\r\n").encode()
    p0 = time.perf_counter()
    recvs, deltas, buf = [], [], b""
    with socket.create_connection((HOST, 443), timeout=30) as s:
        with CTX.wrap_socket(s, server_hostname=HOST) as tls:
            tls.settimeout(180)
            tls.sendall(req + corpo)
            while True:
                d = tls.recv(65536)
                if not d:
                    break
                recvs.append((time.perf_counter() - p0, len(d)))
                buf += d
                while b"\n" in buf:
                    linha, buf = buf.split(b"\n", 1)
                    if not linha.startswith(b"data:"):
                        continue
                    p = linha[5:].strip()
                    if p in (b"[DONE]", b""):
                        continue
                    try:
                        c = json.loads(p)["choices"][0].get("delta", {}).get("content")
                    except Exception:
                        continue
                    if c:
                        deltas.append(time.perf_counter() - p0)
    total = time.perf_counter() - p0
    if len(deltas) > 1:
        g = [deltas[j + 1] - deltas[j] for j in range(len(deltas) - 1)]
        print(f"  [{i}] total={total:.2f}s deltas={len(deltas)} 1º={deltas[0]*1000:.0f} ms "
              f"maior gap={max(g):.2f}s gaps>1s={sum(1 for x in g if x > 1)}")
    else:
        print(f"  [{i}] total={total:.2f}s deltas={len(deltas)} (sem stream?)")


print(f"== rota alternativa (OpenRouter) · {MODELO} · {N} rodada(s) ==")
for i in range(1, N + 1):
    try:
        rodada(i)
    except Exception as e:
        print(f"  [{i}] ERRO {e!r}")