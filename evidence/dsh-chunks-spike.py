"""Spike: o ACP entrega chunk incremental? Mede timestamps por chunk, 2 perfis."""
import json, os, subprocess, sys, threading, time

DSH = "/opt/homebrew/bin/dsh"
MODEL = '["dsflash","deepseek-flash-41"]'

class C:
    def __init__(self, perfil):
        self.perfil = perfil
        self.proc = subprocess.Popen([DSH, "--profile", perfil],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd="/tmp", env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": "/tmp"})
        self.lk = threading.Lock(); self.nid = 1; self.pend = {}
        self.chunks = []; self.sid = None; self.tipos = {}
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _err(self):
        for l in self.proc.stderr:
            t = l.decode("utf-8", "replace").rstrip()
            if t and ("error" in t.lower() or "warn" in t.lower()): print("   [stderr]", t[:160], flush=True)

    def _w(self, o): self.proc.stdin.write((json.dumps(o)+"\n").encode()); self.proc.stdin.flush()

    def req(self, m, p, t=180):
        with self.lk:
            i = self.nid; self.nid += 1; ev = threading.Event(); box = {}
            self.pend[i] = (ev, box)
        self._w({"jsonrpc":"2.0","id":i,"method":m,"params":p})
        if not ev.wait(t): raise TimeoutError(m)
        if "error" in box: raise RuntimeError(f"{m}: {box['error']}")
        return box.get("result")

    def _read(self):
        for raw in self.proc.stdout:
            try: msg = json.loads(raw.decode())
            except Exception: continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lk: p = self.pend.pop(msg["id"], None)
                if p: p[1].update(msg); p[0].set()
                continue
            m = msg.get("method","")
            if m == "session/update":
                u = (msg.get("params") or {}).get("update") or {}
                k = u.get("sessionUpdate")
                self.tipos[k] = self.tipos.get(k, 0) + 1
                if k in ("agent_message_chunk","agent_thought_chunk"):
                    txt = (u.get("content") or {}).get("text") or ""
                    self.chunks.append((time.perf_counter(), k, len(txt)))
            elif "id" in msg and m == "session/request_permission":
                o = next((x for x in (msg.get("params") or {}).get("options") or [] if x.get("kind")=="reject_once"), None)
                self._w({"jsonrpc":"2.0","id":msg["id"],"result":{"outcome":{"outcome":"selected","optionId":o["optionId"]}}})
            elif "id" in msg and m:
                self._w({"jsonrpc":"2.0","id":msg["id"],"result":{}})

    def kill(self):
        try: self.proc.terminate(); self.proc.wait(timeout=3)
        except Exception: self.proc.kill()


def teste(perfil, texto, rotulo):
    c = C(perfil)
    try:
        c.req("initialize", {"protocolVersion":1,"clientCapabilities":{"fs":{}}})
        r = c.req("session/new", {"cwd":"/tmp","mcpServers":[]}); c.sid = r["sessionId"]
        c.req("session/set_config_option", {"sessionId":c.sid,"configId":"model","value":MODEL})
        c.req("session/set_config_option", {"sessionId":c.sid,"configId":"reasoning_effort","value":"off"})
        c.req("session/prompt", {"sessionId":c.sid,"prompt":[{"type":"text","text":"oi"}]})  # prewarm
        c.chunks.clear(); c.tipos.clear()
        p0 = time.perf_counter()
        c.req("session/prompt", {"sessionId":c.sid,"prompt":[{"type":"text","text":texto}]}, t=300)
        fim = time.perf_counter()
        total_chars = sum(n for _,_,n in c.chunks)
        print(f"\n[{perfil} / {rotulo}] chars={total_chars} chunks={len(c.chunks)} total={(fim-p0)*1000:.0f} ms")
        for t,k,n in c.chunks:
            print(f"    +{(t-p0)*1000:8.0f} ms  {k}  {n} chars")
        print(f"    tipos: {c.tipos}")
    finally: c.kill()

print("== A. resposta CURTA ==", flush=True)
teste("tts-studio", "Responda em uma frase curta: quanto e 2+2?", "curta")
print("\n== B. resposta LONGA (~1,4k chars) ==", flush=True)
teste("tts-studio", "Escreva um texto de uns 1200 caracteres sobre o mar, sem listas.", "longa")
print("\n== C. perfil acp PURO, mesma resposta longa ==", flush=True)
teste("acp", "Escreva um texto de uns 1200 caracteres sobre o mar, sem listas.", "longa")
