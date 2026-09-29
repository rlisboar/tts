"""Mede TTFT do provedor dsflash DIRETO (SSE), para saber o ganho se o ACP streamasse."""
import json, re, ssl, time, urllib.request
from pathlib import Path

cred = Path.home()/".dsh/.credentials.yaml"
txt = cred.read_text()
m = re.search(r"DSFLASH_API_KEY:\s*(\S+)", txt)
key = m.group(1).strip().strip('"\'')
cfg = Path.home()/".dsh/settings.yaml"
m2 = re.search(r"baseURL:\s*(\S+)", cfg.read_text())
base = m2.group(1).strip()

try:
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    ctx = ssl.create_default_context()
for rotulo, texto in [("curta", "Responda em uma frase curta: quanto e 2+2?"),
                      ("longa", "Escreva um texto de uns 1200 caracteres sobre o mar, sem listas.")]:
    corpo = json.dumps({"model": "deepseek-flash-41", "stream": True,
                        "messages": [{"role": "user", "content": texto}]}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=corpo, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}",
                                 "User-Agent": "Mozilla/5.0 (compatible; tts-studio/1.0)"})
    p0 = time.perf_counter(); n = 0; chars = 0; ttft = None
    with urllib.request.urlopen(req, timeout=300, context=ctx) as r:
        for linha in r:
            if not linha.startswith(b"data:"): continue
            p = linha[5:].strip()
            if p == b"[DONE]": break
            try: d = json.loads(p)["choices"][0].get("delta", {}).get("content")
            except Exception: continue
            if d:
                n += 1; chars += len(d)
                if ttft is None: ttft = time.perf_counter() - p0
    print(f"[{rotulo}] TTFT={ttft*1000 if ttft else -1:.0f} ms  deltas={n} chars={chars} total={(time.perf_counter()-p0)*1000:.0f} ms")
