"""DSH-4b: o giro de 16–42 s acontece NO FIO ou é buffering de biblioteca?

Lê o SSE do provedor por socket TLS CRU, carimbando cada recv() (nível TCP) e cada
delta parseado. Se o TCP já entrega em rajada, o gargalo é upstream do nosso código
(provedor/agregador/CDN) e nenhum patch nosso resolve.

Uso: python3 /tmp/dsh4b-wire.py [curta|longa] [rodadas]
"""
import json
import re
import socket
import ssl
import sys
import time
from pathlib import Path

PROMPTS = {
    "curta": "Responda em 2 frases, conversando: por que o ceu e azul?",
    "longa": "Escreva um texto de uns 1200 caracteres sobre o mar, sem listas.",
    "media": ("Você é um assistente. Responda em 1 ou 2 frases. Contexto: app de voz. "
               "Agora responda: por que o ceu e azul?"),
    "persona": ("Você é um assistente conversacional em português do Brasil. Responda curto, "
                "em 1 ou 2 frases, sem listas e sem markdown. Contexto: o usuário usa um app de "
                "voz com TTS local e quer respostas rápidas. " + "Detalhe irrelevante. " * 60 +
                "Agora responda: por que o ceu e azul?"),
    "enorme": "Escreva um texto de uns 5000 caracteres sobre o mar, sem listas.",
}
QUAL = sys.argv[1] if len(sys.argv) > 1 else "curta"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3
ESFORCO = sys.argv[3] if len(sys.argv) > 3 else "ausente"   # ausente|none|max
PROMPT = PROMPTS[QUAL]

cred = Path.home() / ".dsh/.credentials.yaml"
KEY = re.search(r"DSFLASH_API_KEY:\s*(\S+)", cred.read_text()).group(1).strip().strip('"\'')
BASE = re.search(r"baseURL:\s*(\S+)", (Path.home() / ".dsh/settings.yaml").read_text()).group(1).strip()
HOST = BASE.split("//", 1)[1].split("/")[0]
CAMINHO = "/" + BASE.split("//", 1)[1].split("/", 1)[1] + "/chat/completions"

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()


def uma_rodada(i):
    corpo_d = {"model": "deepseek-flash-41", "stream": True,
               "messages": [{"role": "user", "content": PROMPT}]}
    if ESFORCO != "ausente":
        corpo_d["reasoning_effort"] = ESFORCO
    corpo = json.dumps(corpo_d).encode()
    req = (f"POST {CAMINHO} HTTP/1.1\r\nHost: {HOST}\r\n"
           f"Authorization: Bearer {KEY}\r\nContent-Type: application/json\r\n"
           f"Content-Length: {len(corpo)}\r\n"
           "Accept: text/event-stream\r\nConnection: close\r\n"
           "User-Agent: Mozilla/5.0 (compatible; tts-studio/1.0)\r\n\r\n").encode()

    p0 = time.perf_counter()
    recvs, deltas, buf = [], [], b""
    with socket.create_connection((HOST, 443), timeout=30) as s:
        with CTX.wrap_socket(s, server_hostname=HOST) as tls:
            tls.settimeout(150)
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
                    if p == b"[DONE]":
                        continue
                    try:
                        c = json.loads(p)["choices"][0].get("delta", {}).get("content")
                    except Exception:
                        continue
                    if c:
                        deltas.append(time.perf_counter() - p0)
    fim = time.perf_counter() - p0
    print(f"  rodada {i}: recvs={len(recvs)} bytes={sum(n for _, n in recvs)} "
          f"deltas={len(deltas)} total={fim:.2f}s")
    if len(deltas) > 1:
        gaps = [deltas[j + 1] - deltas[j] for j in range(len(deltas) - 1)]
        print(f"    1º delta={deltas[0]*1000:.0f} ms · maior gap={max(gaps):.2f}s "
              f"· gaps>1s={sum(1 for g in gaps if g > 1)} "
              f"· deltas depois do maior gap={sum(1 for d in deltas if d > deltas[0] + max(gaps))}")
    if len(recvs) > 1:
        gr = [recvs[j + 1][0] - recvs[j][0] for j in range(len(recvs) - 1)]
        print(f"    recv: 1º={recvs[0][0]*1000:.0f} ms maior gap={max(gr):.2f}s "
              f"recvs com gap>1s={sum(1 for g in gr if g > 1)}")
        maiores = sorted(range(len(gr)), key=lambda j: -gr[j])[:3]
        for j in maiores:
            if gr[j] <= 1:
                break
            print(f"      gap {gr[j]:6.2f}s entre os recvs #{j+1} e #{j+2} — "
                  f"em t={recvs[j][0]:.2f}s já tinham chegado {sum(recvs[k][1] for k in range(j+1))} bytes")
        # taxa: quanto do tempo total está em gaps > 1 s
        print(f"    tempo em gaps>1s: {sum(g for g in gr if g > 1):.1f}s de {fim:.1f}s")


print(f"== TCP cru no provedor · {QUAL} · {N} rodada(s) · host={HOST} · effort={ESFORCO} ==")
for i in range(1, N + 1):
    try:
        uma_rodada(i)
    except Exception as e:
        print(f"  rodada {i}: ERRO {e!r}")