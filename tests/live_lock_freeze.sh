#!/usr/bin/env bash
# PROVA DE EFEITO do item 2 do gate #215 (`await` com `_live_lock` preso no caminho
# `busy`, fix #209): com o envio do `error{busy}` PENDURADO, o processo inteiro tem
# de continuar atendendo — `/health` não pode parar de responder.
#
# POR QUE UM SERVIDOR DE VERDADE: o `TestClient` abre um portal/event loop POR
# conexão (`_TestClientTransport.handle_request` → `with self.portal_factory()`), e
# então os handlers nunca dividem o loop — segurar o `_live_lock` durante o `send`
# não trava ninguém ali. O congelamento é fenômeno de loop ÚNICO (uvicorn), e é isso
# que este script mede. O teste de invariante (`test_busy_e_mandado_fora_do_live_lock`,
# `locked()` no instante do envio) FICA e é o par determinístico e barato deste aqui;
# ele NÃO substitui esta medida e vice-versa.
#
# AS DUAS CENAS (é o que impede o verde de fachada — lição do #182):
#   A) `preso=False` — o `error{busy}` sai com o lock LIVRE (é o fix). TEM de passar.
#      Se a linha do fix for revertida (`await` de volta para dentro do `with`), esta
#      cena CONGELA e o script FALHA. É a prova de mordida.
#   B) `preso=True` — o launcher segura o `_live_lock` em volta do envio, que é
#      literalmente o estado revertido. TEM de congelar. É o CONTROLE: prova que a
#      medida consegue detectar um congelamento (senão o verde da cena A seria só
#      "o probe não olhou").
# A cena B é emulação de runtime (o `acquire` do launcher, não a linha do app);
# a mordida REAL — reverter a linha e ver a cena A falhar — está documentada abaixo
# e foi medida nas duas direções.
#
# COMO O ENVIO FICA PENDURADO (determinístico, sem loteria de buffer do kernel):
#   1. o cliente que vai receber o `busy` é um socket CRU que faz o handshake e
#      depois NÃO lê mais nada (o buffer dele só cresce);
#   2. o servidor sobe com um launcher que envolve `WebSocket.send_json` e, SÓ no
#      frame `busy`, dorme `FREEZE_SONO` antes de enviar — e escreve um ARQUIVO-MARCA
#      no início do sono. O script só mede DEPOIS da marca, então a medida é sempre
#      "com o envio pendurado" e nunca "antes de o handler chegar lá".
#   3. a 3ª conexão (a que morde) não manda `setup`: o handler dela chama `_live_sweep()`
#      — código SÍNCRONO, na THREAD do event loop — no instante em que é aceita. Com o
#      lock preso, ela bloqueia a thread do loop; e como o lock é `threading.Lock`
#      (não reentrante) e quem o segura é a MESMA thread, o loop não roda mais: o
#      `asyncio.sleep` do envio nunca retoma. É um auto-deadlock, não uma lentidão.
#
# MORDIDA (reverter a linha do fix = `app.py`, bloco do caminho `busy`):
#   o `await ws.send_json(...)` volta para DENTRO do `with _live_lock:`; a cena A
#   passa a congelar e o script sai 1. Com o fix (o `await` FORA), sai 0.
#
# O QUE ELE **NÃO** PROVA: latência do Live, corretude do teto de sessões, nem o
# caminho de áudio — só que um `await` sob o lock congela (ou não) o processo.
# Também não prova nada sobre `TestClient` (ver acima: lá o fenômeno não existe).
#
# LIMIAR (RELATIVO, não ms absoluto — o runner roda sob `nice 19` + hogs): o probe
# durante o congelamento tem de ficar abaixo de `max(0.25s, 25× o tempo OCIOSO do
# /health medido antes)`. Números medidos nesta máquina, nas duas direções:
#   · fix (cena A)   → ocioso ~3 ms, probe ~3 ms (≈ 1×) → PASSA
#   · revertido      → ocioso ~3 ms, probe ~2,0 s (o `curl`/urlopen de 2 s estoura;
#                      o loop está preso em `Lock.acquire()`) → FALHA
#   · controle (B)   → congelado e PERMANENTE (2º probe também estoura)
#
#   ./tests/live_lock_freeze.sh
#   FREEZE_SONO=1.0 ./tests/live_lock_freeze.sh     # janela maior (máquina lenta)
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Serializa com as suítes que carregam modelo: aqui o prewarm do pipeline roda em
# thread no connect e pode disputar Metal/CPU com outra suíte (#134).
source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

cd "$RAIZ" || exit 1
"$PY" - <<'PY'
import base64, json, os, pathlib, shutil, socket, subprocess, sys, tempfile, time
import urllib.request

RAIZ = pathlib.Path.cwd()
PY = sys.executable
SONO = float(os.environ.get("FREEZE_SONO", "0.5"))   # janela do send "pendurado"
FATOR = 25.0                                         # limiar relativo ao ocioso
PISO_S = 0.25                                        # e um piso, p/ máquina rápida demais
PROBE_TIMEOUT = 2.0                                  # o probe estoura => congelou
ESPERA_JANELA = 0.2                                  # 3ª conexão chegar ao `_live_sweep`
falhas = []

def ok(m):
    print(f"  ✔ {m}")

def falha(m):
    falhas.append(m)
    print(f"  ✘ {m}")

def porta_livre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

# ------------------------------------------------- launcher do servidor (patched)
# Arquivo próprio porque o patch tem de viver no processo do SERVIDOR.
LAUNCHER = '''
import asyncio, os, pathlib, sys
sys.path.insert(0, {raiz!r})
import app
from starlette.websockets import WebSocket
MARCA = pathlib.Path({marca!r})
PRESO = {preso!r}
_orig = WebSocket.send_json

async def lento(self, dado, *a, **k):
    if isinstance(dado, dict) and dado.get("code") == "busy":
        # PRESO: emula o estado revertido — o lock é segurado em volta do envio.
        # `blocking=False` porque, se a linha do app já o segura (fonte revertida),
        # o acquire não pode bloquear (threading.Lock não é reentrante).
        meu = bool(PRESO) and app._live_lock.acquire(blocking=False)
        MARCA.write_text("1")                      # o sono começa: envio pendurado
        await asyncio.sleep({sono})
        if meu:
            app._live_lock.release()
    return await _orig(self, dado, *a, **k)

WebSocket.send_json = lento
import uvicorn
uvicorn.run(app.app, host="127.0.0.1", port={porta}, log_level="warning")
'''

def health(base, tempo_limite=PROBE_TIMEOUT):
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(base + "/health", timeout=tempo_limite) as r:
            r.read()
        return time.monotonic() - t0, True
    except Exception:                                   # noqa: BLE001
        return time.monotonic() - t0, False

def conecta(porta):
    """Handshake WS na mão e, depois, NENHUMA leitura (buffer só cresce)."""
    s = socket.create_connection(("127.0.0.1", porta), timeout=5)
    chave = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /api/live/ws HTTP/1.1\r\nHost: 127.0.0.1:{porta}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {chave}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        pedaco = s.recv(1)
        if not pedaco:
            raise RuntimeError("handshake morreu")
        buf += pedaco
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError(f"handshake recusado: {buf[:60]!r}")
    return s

def setup(s):
    """Frame de texto MASCARADO (cliente→servidor) com o `setup`."""
    carga = json.dumps({"type": "setup"}).encode()
    mascara = os.urandom(4)
    s.sendall(bytes([0x81, 0x80 | len(carga)]) + mascara
              + bytes(b ^ mascara[i % 4] for i, b in enumerate(carga)))

def cena(preso):
    """Sobe um servidor próprio e mede o /health com o `busy` pendurado."""
    porta = porta_livre()
    base = f"http://127.0.0.1:{porta}"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="freeze-"))
    marca = tmp / "no_send"
    (tmp / "servidor.py").write_text(LAUNCHER.format(raiz=str(RAIZ), marca=str(marca),
                                                     preso=preso, sono=SONO, porta=porta))
    ambiente = {**os.environ, "TTS_LIVE_MAX_SESSIONS": "1",   # N=1: a 2ª conexão leva busy
                "TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai"}
    proc = subprocess.Popen([PY, str(tmp / "servidor.py")], env=ambiente,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    abertos = []
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except Exception:                           # noqa: BLE001
                time.sleep(0.5)
        else:
            raise RuntimeError("servidor não subiu")

        ocioso_s, _ = health(base)
        a = conecta(porta); abertos.append(a); setup(a)      # sessão 1 (registrada)
        time.sleep(0.4)
        b = conecta(porta); abertos.append(b); setup(b)      # sessão 2 -> busy

        for _ in range(400):                                 # a marca: sono em curso
            if marca.exists():
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("o `busy` nunca chegou a ser enviado (sem marca)")

        c = conecta(porta); abertos.append(c)                # 3ª: `_live_sweep` no loop
        time.sleep(ESPERA_JANELA)                            # ela já tentou o lock
        probe_s, respondeu = health(base)
        # 2º probe: separa CONGELAMENTO de lentidão (o sleep já teria acabado)
        time.sleep(SONO)
        probe2_s, respondeu2 = health(base)
        # o `busy` ainda sai? (medido AQUI: o finally fecha o socket do `b`)
        recebeu = False
        if not preso:
            b.settimeout(3)
            try:
                recebeu = bool(b.recv(4096))
            except Exception:                               # noqa: BLE001
                recebeu = False
        return {"ocioso_s": ocioso_s, "probe_s": probe_s, "respondeu": respondeu,
                "probe2_s": probe2_s, "respondeu2": respondeu2, "recebeu": recebeu}
    finally:
        for s in abertos:
            try:
                s.close()
            except Exception:                               # noqa: BLE001
                pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                   # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

def limiar(ocioso_s):
    return max(PISO_S, FATOR * ocioso_s)

# ---------------------------------------------------------------- cena A (o fix)
print("  · cena A — `await` com o lock LIVRE (estado do fix)")
ca = cena(preso=False)
la = limiar(ca["ocioso_s"])
print(f"    ocioso {ca['ocioso_s'] * 1000:.0f} ms · probe {ca['probe_s'] * 1000:.0f} ms "
      f"(limiar {la * 1000:.0f} ms)")
if not ca["respondeu"]:
    falha(f"cena A CONGELOU: /health não respondeu em {PROBE_TIMEOUT:.0f} s com o `busy` "
          f"pendurado (limiar {la * 1000:.0f} ms) — o `await` está sob o lock?")
elif ca["probe_s"] > la:
    falha(f"cena A: /health levou {ca['probe_s'] * 1000:.0f} ms (limiar {la * 1000:.0f} ms): "
          f"o envio está segurando o loop")
else:
    ok(f"cena A: o processo seguiu atendendo com o `busy` pendurado "
       f"({ca['probe_s'] * 1000:.0f} ms vs limiar {la * 1000:.0f} ms)")

# e o busy ainda sai (o sono não virou "não envia")
if ca["recebeu"]:
    ok("cena A: o cliente do `busy` recebeu o frame depois da janela")
else:
    falha("cena A: o cliente do `busy` não recebeu o frame (o envio não completou)")

# ------------------------------------------------------------ cena B (controle)
print("  · cena B (controle) — lock SEGURADO em volta do envio (estado revertido)")
cb = cena(preso=True)
lb = limiar(cb["ocioso_s"])
print(f"    ocioso {cb['ocioso_s'] * 1000:.0f} ms · probe {cb['probe_s'] * 1000:.0f} ms "
      f"· 2º probe {cb['probe2_s'] * 1000:.0f} ms (limiar {lb * 1000:.0f} ms)")
if cb["respondeu"] and cb["probe_s"] <= lb:
    falha(f"cena B NÃO congelou ({cb['probe_s'] * 1000:.0f} ms ≤ limiar {lb * 1000:.0f} ms): "
          f"a medida não distingue os dois mundos")
else:
    ok(f"cena B: congelou como esperado ({cb['probe_s'] * 1000:.0f} ms vs "
       f"limiar {lb * 1000:.0f} ms)")
    if cb["respondeu2"] or cb["probe2_s"] <= lb:
        falha("cena B: descongelou sozinho — era lentidão, não auto-deadlock")
    else:
        ok("cena B: congelamento PERMANENTE (o 2º probe também estourou)")

print(f"\n{'✘ ' + str(len(falhas)) + ' falha(s)' if falhas else '✔ sem congelamento'}")
sys.exit(1 if falhas else 0)
PY