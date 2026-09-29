"""DSH-4a: o patch do bridge fez o ACP streamar? Mede a MARCA `partial` e o ganho.

Companheiro de `dsh-chunks-spike.py` (que conta chunks brutos). Aqui o foco é o
que o patch introduz: deltas marcados `partial: true` (1º em ~0,4 s) + a mensagem
comitada marcada `partial: false` no mesmo `messageId`, e quantos caracteres o
cliente (`dsh_client._nao_duplicar`) realmente emite depois do dedup.

Uso: python3 evidence/dsh-acp-partial-spike.py [perfil] [rotulo]
Sem o patch: 1 chunk, `partial=None`, dedup == o próprio texto (caso (c)).
Com o patch: N deltas `partial=True` + 1 comitado `partial=False`; dedup == deltas.
"""
import json, os, subprocess, sys, threading, time

DSH = "/opt/homebrew/bin/dsh"
MODEL = '["dsflash","deepseek-flash-41"]'
PERFIL = sys.argv[1] if len(sys.argv) > 1 else "tts-studio"
ROTULO = sys.argv[2] if len(sys.argv) > 2 else "longa (~1,4k chars)"
PROMPT = ("Escreva um texto de uns 1200 caracteres sobre o mar, sem listas."
          if "longa" in ROTULO else "Responda em uma frase curta: quanto e 2+2?")


def _lcp(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


# A função de dedup REAL do cliente (importada, não uma cópia que pode derivar).
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from dsh_client import _nao_duplicar  # noqa: E402


class C:
    def __init__(self):
        self.proc = subprocess.Popen([DSH, "--profile", PERFIL],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd="/tmp", env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": "/tmp"})
        self.lk = threading.Lock(); self.nid = 1; self.pend = {}; self.chunks = []; self.sid = None
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _err(self):
        for l in self.proc.stderr:
            t = l.decode("utf-8", "replace").rstrip()
            if t and ("error" in t.lower() or "warn" in t.lower()):
                print("   [stderr]", t[:160], flush=True)

    def _w(self, o):
        self.proc.stdin.write((json.dumps(o) + "\n").encode()); self.proc.stdin.flush()

    def req(self, m, p, t=300):
        with self.lk:
            i = self.nid; self.nid += 1; ev = threading.Event(); box = {}
            self.pend[i] = (ev, box)
        self._w({"jsonrpc": "2.0", "id": i, "method": m, "params": p})
        if not ev.wait(t):
            raise TimeoutError(m)
        if "error" in box:
            raise RuntimeError(f"{m}: {box['error']}")
        return box.get("result")

    def _read(self):
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode())
            except Exception:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lk:
                    p = self.pend.pop(msg["id"], None)
                if p:
                    p[1].update(msg); p[0].set()
                continue
            m = msg.get("method", "")
            if m == "session/update":
                u = (msg.get("params") or {}).get("update") or {}
                if u.get("sessionUpdate") == "agent_message_chunk":
                    par = u.get("partial")
                    if par is None and isinstance(u.get("_meta"), dict):
                        par = u["_meta"].get("partial")
                    self.chunks.append((time.perf_counter(), par,
                                        (u.get("content") or {}).get("text") or "",
                                        u.get("messageId")))
            elif "id" in msg and m == "session/request_permission":
                o = next((x for x in (msg.get("params") or {}).get("options") or []
                          if x.get("kind") == "reject_once"), None)
                self._w({"jsonrpc": "2.0", "id": msg["id"],
                         "result": {"outcome": {"outcome": "selected",
                                                "optionId": o["optionId"] if o else "reject_once"}}})
            elif "id" in msg and m:
                self._w({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    def kill(self):
        try:
            self.proc.terminate(); self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


c = C()
try:
    c.req("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}})
    r = c.req("session/new", {"cwd": "/tmp", "mcpServers": []}); c.sid = r["sessionId"]
    c.req("session/set_config_option", {"sessionId": c.sid, "configId": "model", "value": MODEL})
    c.req("session/set_config_option", {"sessionId": c.sid, "configId": "reasoning_effort", "value": "off"})
    c.req("session/prompt", {"sessionId": c.sid, "prompt": [{"type": "text", "text": "oi"}]})
    c.chunks.clear()
    p0 = time.perf_counter()
    c.req("session/prompt", {"sessionId": c.sid, "prompt": [{"type": "text", "text": PROMPT}]})
    fim = time.perf_counter()
    deltas = [x for x in c.chunks if x[1] is True]
    comitados = [x for x in c.chunks if x[1] is False]
    emitido = ""
    ultimo_bruto = ""
    for _, par, txt, _mid in c.chunks:
        emitido += _nao_duplicar(txt, par, emitido, ultimo_bruto)
        ultimo_bruto = txt
    ttft = (deltas[0][0] - p0) * 1000 if deltas else None
    print(f"[{PERFIL} / {ROTULO}] chunks={len(c.chunks)} "
          f"deltas(partial=True)={len(deltas)} comitados(partial=False)={len(comitados)} "
          f"sem-marca={len(c.chunks) - len(deltas) - len(comitados)}")
    print(f"    1º delta: {ttft:.0f} ms" if ttft else "    1º delta: — (sem stream)")
    print(f"    total do turno: {(fim - p0) * 1000:.0f} ms · chars entregues ao cliente "
          f"(pós-dedup)={len(emitido)} · chars brutos={sum(len(x[2]) for x in c.chunks)}")
    ids = {x[3] for x in c.chunks}
    print(f"    messageIds distintos: {len(ids)}")
finally:
    c.kill()