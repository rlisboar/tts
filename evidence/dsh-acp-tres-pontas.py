"""DSH-4b: correlaciona as TRÊS pontas no mesmo turno.

Fonte (provedor): `dsh-acp-wire-probe.py` (socket cru, evidência separada).
Aqui, as duas pontas NOSSAS, no mesmo turno e no mesmo relógio de parede:
  (1) bridge — cada `agent/assistant-stream` recebido e cada `session/update` emitido
      (trace do patch, ligado pela presença de /tmp/dsh4a-trace.on);
  (2) cliente — cada `session/update` que chega ao leitor do `dsh_client`.

Se o bridge RECEBE em bloco, o bloqueio é acima dele (provedor/harness);
se recebe contínuo e o cliente vê bloco, é do nosso lado (stdout/leitura).

Uso: python3 evidence/dsh-acp-tres-pontas.py [rodadas]
"""
import json
import pathlib
import sys
import time

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import dsh_client  # noqa: E402
from dsh_client import DshClient  # noqa: E402

TRACE = pathlib.Path("/tmp/dsh4a-trace.jsonl")
LIGA = pathlib.Path("/tmp/dsh4a-trace.on")
PROMPTS = {
    "curta": "Você é um assistente. Responda em 1 ou 2 frases. Agora: por que o ceu e azul?",
    "persona": ("Você é um assistente conversacional em português do Brasil. Responda curto, "
                "em 1 ou 2 frases, sem listas e sem markdown. Contexto: o usuário usa um app de "
                "voz com TTS local e quer respostas rápidas. " + "Detalhe irrelevante. " * 60 +
                "Agora responda: por que o ceu e azul?"),
}
PROMPT = PROMPTS[sys.argv[2]] if len(sys.argv) > 2 else PROMPTS["curta"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 4


def gaps(ts):
    return [ts[i + 1] - ts[i] for i in range(len(ts) - 1)] or [0.0]


def maior(ts):
    if len(ts) < 2:
        return 0.0, -1
    g = gaps(ts)
    j = max(range(len(g)), key=lambda i: g[i])
    return g[j], j


def uma_rodada(i):
    LIGA.write_text("")
    if TRACE.exists():
        TRACE.unlink()
    chegada = []

    original = dsh_client.DshClient._rota_update

    def rota(self, update, sessao=""):
        if str(update.get("sessionUpdate")) == "agent_message_chunk":
            chegada.append(time.time() * 1000.0)
        return original(self, update, sessao)

    dsh_client.DshClient._rota_update = rota
    c = DshClient(profile="tts-studio", effort="off")
    try:
        c.prewarm()
        time.sleep(0.3)   # deixa o slot de prompt do prewarm assentar (corrida do dsh_client)
        chegada.clear()
        # o prewarm JÁ streama: o fd do filho aponta para o arquivo vivo, então
        # marco a contagem de linhas e ignoro tudo o que veio antes (não dá para
        # apagar o arquivo: o fd do filho continuaria apontando para o inode morto).
        corte = len(TRACE.read_text().splitlines()) if TRACE.exists() else 0
        for tentativa in range(3):
            chegada.clear()
            t0 = time.time() * 1000.0
            try:
                for _ in c.stream(PROMPT, turno_id="t"):
                    pass
                t1 = time.time() * 1000.0
                break
            except Exception as exc:                      # corrida do slot de prompt
                if "in flight" not in str(exc) or tentativa == 2:
                    raise
                print(f"    (slot ocupado, retry: {exc})")
                time.sleep(0.5)
    finally:
        dsh_client.DshClient._rota_update = original
        c.close()

    todas = ([json.loads(l) for l in TRACE.read_text().splitlines() if l.strip()]
              if TRACE.exists() else [])
    linhas = todas[corte:]
    frames = [x["t"] for x in linhas if x["k"] == "frame" and x.get("tipo") == "text-delta"]
    emits = [x["t"] for x in linhas if x["k"] == "emit"]
    comitados = [x["t"] for x in linhas if x["k"] == "commit"]
    if not frames or not chegada:
        print(f"  rodada {i}: sem trace (frames={len(frames)} chegada={len(chegada)})")
        return
    gf, _ = maior(frames)
    gc, _ = maior(chegada)
    atraso = chegada[0] - frames[0]
    print(f"  rodada {i}: total={(t1 - t0)/1000:.2f}s · frames-tex={len(frames)} emits={len(emits)} commit={len(comitados)} "
          f"chegadas={len(chegada)}")
    print(f"    bridge: 1º frame t+{frames[0] - t0:.0f} ms · maior gap interno={gf/1000:.2f}s")
    print(f"    cliente: 1ª chegada t+{chegada[0] - t0:.0f} ms · maior gap={gc/1000:.2f}s · "
          f"atraso bridge→cliente={atraso:.0f} ms")


ROTULO = sys.argv[2] if len(sys.argv) > 2 else "curta"
print(f"== {N} rodada(s) · prompt {ROTULO} · trace do bridge ligado ==")
for i in range(1, N + 1):
    uma_rodada(i)