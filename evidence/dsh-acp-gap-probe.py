"""DSH-4b: assinatura de INTERVALOS entre deltas, nas duas pontas.

(a) fonte: SSE direto no provedor (controle que já existia, agora medindo o GAP);
(b) cliente: caminho ACP do dsh (a ponta que o Live usa), com timestamp na chegada
    de cada session/update, sem editar o dsh_client.

Uso: python3 /tmp/dsh4b-probe.py [curta|longa] [rodadas]
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/Users/lisboa/Documents/tts-rod")

PROMPTS = {
    "curta": "Responda em 2 frases, conversando: por que o ceu e azul?",
    "longa": "Escreva um texto de uns 1200 caracteres sobre o mar, sem listas.",
}
QUAL = sys.argv[1] if len(sys.argv) > 1 else "curta"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3
PROMPT = PROMPTS[QUAL]


def resumo(nome, stamps):
    """stamps: lista de t (s) do 1º delta em diante."""
    if len(stamps) < 2:
        print(f"  [{nome}] {len(stamps)} delta(s) — sem intervalos")
        return
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    grandes = [g for g in gaps if g > 1.0]
    print(f"  [{nome}] deltas={len(stamps)} 1º={stamps[0]*1000:.0f} ms "
          f"total={stamps[-1]*1000:.0f} ms · gaps>1s={len(grandes)} "
          f"maior={max(gaps):.2f}s · fração<=50ms={sum(1 for g in gaps if g <= .05)/len(gaps):.0%} "
          f"· fração<=1s={sum(1 for g in gaps if g <= 1.0)/len(gaps):.0%}")


# ---------------------------------------------------------------- (a) provedor
def prova_ssse():
    cred = Path.home() / ".dsh/.credentials.yaml"
    key = re.search(r"DSFLASH_API_KEY:\s*(\S+)", cred.read_text()).group(1).strip().strip('"\'')
    base = re.search(r"baseURL:\s*(\S+)", (Path.home() / ".dsh/settings.yaml").read_text()).group(1).strip()
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    corpo = json.dumps({"model": "deepseek-flash-41", "stream": True,
                        "messages": [{"role": "user", "content": PROMPT}]}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=corpo, method="POST",
                                headers={"Content-Type": "application/json",
                                         "Authorization": f"Bearer {key}",
                                         "User-Agent": "Mozilla/5.0 (compatible; tts-studio/1.0)"})
    p0 = time.perf_counter()
    stamps = []
    with urllib.request.urlopen(req, timeout=300, context=ctx) as r:
        for linha in r:
            if not linha.startswith(b"data:"):
                continue
            p = linha[5:].strip()
            if p == b"[DONE]":
                break
            try:
                d = json.loads(p)["choices"][0].get("delta", {}).get("content")
            except Exception:
                continue
            if d:
                stamps.append(time.perf_counter() - p0)
    resumo("PROVEDOR (SSE direto)", stamps)


# ------------------------------------------------------------------ (b) ACP
def prova_acp():
    import dsh_client
    from dsh_client import DshClient

    stamps = []
    novo = {"t0": 0.0}

    def rota(self, update, sessao=""):
        if str(update.get("sessionUpdate")) == "agent_message_chunk":
            stamps.append(time.perf_counter() - novo["t0"])
        return original(self, update, sessao)

    original = dsh_client.DshClient._rota_update
    dsh_client.DshClient._rota_update = rota

    c = DshClient(profile="tts-studio", effort="off")
    c.prewarm()
    try:
        stamps.clear()                      # o prewarm já streama; não conta aqui
        novo["t0"] = time.perf_counter()
        n = 0
        for _ in c.stream(PROMPT, turno_id="t"):
            n += 1
        resumo("CLIENTE (ACP dsh)", stamps)
        print(f"     (deltas emitidos={n} duplicados={c.chunks_duplicados})")
    finally:
        c.close()
        dsh_client.DshClient._rota_update = original


print(f"== prompt {QUAL!r}, {N} rodada(s), load={os.getloadavg()[0]:.1f} ==")
for i in range(N):
    print(f"-- rodada {i+1} provedor --")
    prova_ssse()
for i in range(N):
    print(f"-- rodada {i+1} cliente ACP --")
    prova_acp()