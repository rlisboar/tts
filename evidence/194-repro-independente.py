#!/usr/bin/env python3
"""GATE #194 — re-derivação POR FORA (não re-roda os testes do autor).

Diferente do 194-mordida.py (mutação) e do 194-fora-187-188.py (handshake), este
script ataca o que os testes NÃO provam, pelos ângulos que sobraram:

  A) #185 no POOL REAL (fake_acp, sem monkeypatch do `_chat_dsh_novo`): o carimbo
     tem de estar na ENTREGA e a devolução fora de ordem não pode entregar o
     cliente do modelo velho.
  B) #189: com Live=dsh e Conversa no endpoint, o resumo NÃO pode egressar; sem
     endpoint (só dsh) tem de funcionar em vez de dar 400; o #146 continua valendo;
     e o resumo passa pelo POOL, não pelo cliente da sessão.
  C) #186 na ORDEM DO FIO com TTL curto: a sessão retomada tem de continuar visível
     para o sweep — antes do fix, o close do socket ANTIGO tirava a nova do
     registry e ela nunca era vencida (o cliente ficava pendurado).
  D) #187/#188: setup inválido não pode CONSUMIR slot do teto (com teto=1, o erro
     seguido de um setup válido tem de dar `ready`).
"""
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

RAIZ = pathlib.Path("/Users/lisboa/Documents/tts-rod")
PY = str(RAIZ / ".venv-mlx/bin/python")
os.chdir(RAIZ)
sys.path.insert(0, str(RAIZ))
falhas = []


def cobrar(c, m):
    print(("  ok   " if c else "  FALHA ") + m)
    if not c:
        falhas.append(m)


# ---------------------------------------------------------------- A) #185
def parte_a_pool_real():
    print("\n[A] #185 — pool REAL (fake_acp), carimbo na entrega")
    fake = pathlib.Path("/tmp/gate194/fake-acp")
    fake.write_text(f'#!/bin/sh\nexec "{PY}" "{RAIZ}/tests/fake_acp.py" "$@"\n')
    fake.chmod(0o755)
    os.environ["TTS_CHAT_DSH_BIN"] = str(fake)
    import app

    for _k, c in app._chat_dsh_livres:                 # limpa o prewarm do import
        c.close()
    app._chat_dsh_livres.clear()
    th = app._chat_dsh_prewarm_thread
    if th is not None:
        th.join(30)

    app._settings["chat_dsh_bin"] = str(fake)
    app._settings["chat_backend"] = "dsh"              # habilita o prewarm
    app._settings["chat_dsh_model"] = '["openai","modelo-A"]'
    cfg_a = app._chat_dsh_cfg()
    cli_a = app._chat_dsh_cliente()                    # em voo (turno da Conversa)
    cobrar(getattr(cli_a, "_pool_chave", None) == app._chat_dsh_chave_do(cfg_a),
           "o carimbo da chave acontece na ENTREGA")
    cobrar(cli_a.modelo == cfg_a["model"], f"cliente A no modelo A ({cli_a.modelo})")

    app._settings["chat_dsh_model"] = '["openai","modelo-B"]'
    cfg_b = app._chat_dsh_cfg()
    cobrar(app._chat_dsh_chave_do(cfg_b) != app._chat_dsh_chave_do(cfg_a),
           "as duas chaves do teste são distintas (senão o caso é vazio)")
    th = app._chat_dsh_prewarm("troca-de-config")      # prewarm da config NOVA
    th.join(60)
    app._chat_dsh_devolve(cli_a)                       # devolução DEPOIS da troca

    entregue = app._chat_dsh_cliente()
    cobrar(entregue.modelo == cfg_b["model"],
           f"o próximo turno usa o modelo NOVO ({entregue.modelo})")
    cobrar(getattr(entregue, "_pool_chave", None) == app._chat_dsh_chave_do(cfg_b),
           "a chave do cliente entregue é a da config de agora")
    cobrar(cli_a.alive is False, "o cliente da config velha foi FECHADO (sem leak)")
    entregue.close()
    app._chat_dsh_livres.clear()


# ---------------------------------------------------------------- B) #189
class _PipeFake:
    def __init__(self, n=40):
        self.history = [{"role": "user", "content": "x" * 400} for _ in range(n)]
        self._dsh = object()          # cliente da sessão presente -> backend dsh
        self._dsh_indisponivel = False


def parte_b_compressao():
    print("\n[B] #189 — compressão do Live: egress zero, endpoint ausente e POOL")
    import app
    from fastapi import HTTPException

    app._live_resume_fn = None
    chamados = []
    app._chat_llm = lambda msgs: chamados.append("endpoint") or "resumo-endpoint"
    app._chat_llm_dsh = lambda msgs: chamados.append("dsh") or "resumo-dsh"

    app._settings["chat_backend"] = "openai"
    app._settings["chat_backend_live"] = "dsh"
    sess = {"pipe": _PipeFake(), "resumo": "", "id": "s1", "geracao": 1,
            "voice_id": "v", "system": "", "history": []}

    cobrar(app._live_resumidor(sess) is app._chat_llm_dsh,
           "o resumidor é o do dsh (backend EFETIVO do Live)")
    cobrar(app._live_hist_comprime(sess) is True, "comprimiu")
    cobrar(chamados == ["dsh"], f"com Live=dsh e Conversa no endpoint, NÃO egressou: {chamados}")
    cobrar(sess["pipe"].history[0]["content"].startswith("Resumo do que já foi dito:"),
           "o resumo entrou no histórico")

    # endpoint AUSENTE (só dsh): o caminho da Conversa daria 400 — não pode derrubar
    def endpoint_400(msgs):
        raise HTTPException(400, "Nenhum endpoint de chat configurado")

    app._chat_llm = endpoint_400
    sess["pipe"].history = _PipeFake().history
    chamados.clear()
    cobrar(app._live_hist_comprime(sess) is True,
           "sem endpoint configurado a compressão do Live funciona (não 400)")
    cobrar(chamados == ["dsh"], f"seguiu pelo dsh: {chamados}")

    # #146: dsh marcado INDISPONÍVEL -> volta para o backend efetivo (endpoint)
    app._chat_llm = lambda msgs: chamados.append("endpoint") or "R"
    sess["pipe"]._dsh_indisponivel = True
    cobrar(app._live_resumidor(sess) is app._chat_llm,
           "dsh indisponível -> resumidor do endpoint (fallback do #146)")
    sess["pipe"].history = _PipeFake().history
    chamados.clear()
    cobrar(app._live_hist_comprime(sess) is True and chamados == ["endpoint"],
           f"o fallback do #146 vale na compressão: {chamados}")

    # e o resumo sai pelo POOL (processo separado), não pelo cliente da sessão
    usados = []

    class PoolFake:
        def collect(self, msgs):
            usados.append("pool")
            return "R"

    class ClienteDaSessao:
        def collect(self, msgs):
            usados.append("sessao")
            return "R"

    app._chat_dsh_cliente = lambda: PoolFake()
    app._chat_llm_dsh = lambda msgs: app._chat_dsh_cliente().collect(msgs)
    sess["pipe"]._dsh_indisponivel = False
    sess["dsh"] = ClienteDaSessao()
    sess["pipe"].history = _PipeFake().history
    cobrar(app._live_hist_comprime(sess) is True and usados == ["pool"],
           f"o resumo usa o POOL e não o cliente da sessão: {usados}")


# ---------------------------------------------------------------- C) e D) fio
def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sobe(extra: str, porta: int):
    launcher = f"""
import app
{extra}
import uvicorn
uvicorn.run(app.app, host="127.0.0.1", port={porta}, log_level="warning")
"""
    log = pathlib.Path(f"/tmp/gate194/srv194_{porta}.log")
    p = subprocess.Popen([PY, "-c", launcher], cwd=str(RAIZ), env=dict(os.environ),
                         stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(300):
        if p.poll() is not None:
            raise SystemExit(f"servidor morreu (log {log})")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{porta}/health", timeout=2).read()
            return p
        except Exception:
            time.sleep(0.2)
    raise SystemExit("servidor não subiu")


async def _ate(ws, pred, timeout=15.0):
    """Próximo frame que satisfaz `pred` (o servidor manda `stats` no meio)."""
    fim = time.monotonic() + timeout
    visto = None
    while time.monotonic() < fim:
        visto = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if pred(visto):
            return visto
    return visto


async def cenario_186(porta):
    """C) o registro de retomada é PRÉ-SEMEADO pelo launcher (não depende de turno
    real). Com TTL de 1 s, a sessão RETOMADA tem de continuar no registry e ser
    vencida pelo sweep depois de o socket ANTIGO morrer. Antes do fix ela saía no
    finally do antigo e o sweep nunca a via — o cliente ficava pendurado."""
    import websockets
    uri = f"ws://127.0.0.1:{porta}/api/live/ws"
    async with websockets.connect(uri) as ws1:
        await ws1.send(json.dumps({"type": "setup", "session_id": "gate194"}))
        p1 = json.loads(await asyncio.wait_for(ws1.recv(), 15))
        cobrar(p1["type"] == "ready" and p1["resumed"] is True,
               f"a 1ª conexão retoma o registro semeado: {p1.get('type')}")
        async with websockets.connect(uri) as ws2:
            await ws2.send(json.dumps({"type": "setup", "session_id": "gate194"}))
            r2 = json.loads(await asyncio.wait_for(ws2.recv(), 15))
            cobrar(r2["type"] == "ready" and r2["resumed"] is True,
                   f"a retomada abriu por cima da viva (resumed): {r2.get('type')}")
            await ws1.close()                            # o ANTIGO morre com a nova VIVA
            await asyncio.sleep(3.0)                     # teardown do antigo (sender+pipe)
            await ws2.send(json.dumps({"type": "ping", "t": 1}))
            p = await _ate(ws2, lambda d: d.get("type") == "pong")
            cobrar(p.get("type") == "pong",
                   f"a sessão nova responde ping depois da morte do antigo: {str(p)[:80]}")
            # TTL vencido: um connect novo dispara o sweep; a nova TEM de ser vista
            await asyncio.sleep(1.6)
            async with websockets.connect(uri) as ws3:
                await ws3.send(json.dumps({"type": "setup"}))
                await asyncio.wait_for(ws3.recv(), 15)
                try:
                    ttl = await _ate(ws2, lambda d: d.get("code") == "session_ttl", 8.0)
                except Exception as exc:                 # noqa: BLE001
                    ttl = {"erro": repr(exc)}
                cobrar(ttl.get("code") == "session_ttl",
                       f"o sweep VÊ a sessão retomada (TTL a fecha): {str(ttl)[:100]}")


async def cenario_teto_retomada(porta):
    """A retomada SUBSTITUI a entrada: com teto=1 ela não pode levar `busy`."""
    import websockets
    uri = f"ws://127.0.0.1:{porta}/api/live/ws"
    async with websockets.connect(uri) as ws1:
        await ws1.send(json.dumps({"type": "setup", "session_id": "gate194"}))
        await asyncio.wait_for(ws1.recv(), 15)
        async with websockets.connect(uri) as ws2:
            await ws2.send(json.dumps({"type": "setup", "session_id": "gate194"}))
            f = await _ate(ws2, lambda d: d.get("type") in ("ready", "error"))
            cobrar(f["type"] == "ready",
                   f"retomada com o teto cheio NÃO leva busy (teto=1): {f.get('code')}")
            st = await _ate(ws2, lambda d: "sessao" in d and "criadas" in d["sessao"])
            cobrar(st["sessao"]["criadas"] == 1,
                   f"a retomada SUBSTITUIU a entrada (registry com 1): {st['sessao']}")


async def cenario_187_slot(porta):
    """D) teto=1: um setup INVÁLIDO não pode consumir o slot (ele é inserido só no
    registro, depois da validação) — e o erro tem de chegar com mensagem."""
    import websockets
    uri = f"ws://127.0.0.1:{porta}/api/live/ws"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "setup",
                                  "history": [{"role": "chefe", "text": "oi"}]}))
        erro = json.loads(await asyncio.wait_for(ws.recv(), 10))
        cobrar(erro.get("code") == "setup_invalido" and erro.get("message"),
               f"setup inválido -> frame de erro com mensagem: {erro}")
        try:
            await asyncio.wait_for(ws.recv(), 5)
            cobrar(False, "o socket não fechou depois do erro")
        except Exception as exc:                         # noqa: BLE001
            cobrar("4400" in str(exc), f"close 4400: {exc}")
    async with websockets.connect(uri) as ws2:            # o slot tem de estar livre
        await ws2.send(json.dumps({"type": "setup"}))
        f = json.loads(await asyncio.wait_for(ws2.recv(), 15))
        cobrar(f["type"] == "ready",
               f"o setup inválido NÃO consumiu o slot do teto (teto=1): {f.get('code')}")


procs = []
try:
    parte_a_pool_real()
    parte_b_compressao()

    p1 = porta_livre()
    procs.append(sobe('import time\n'
                      'app._LIVE_TTL_S = 1.0\napp._LIVE_MAX_SESSIONS = 3\n'
                      'app._live_historico["gate194"] = {\n'
                      '    "msgs": [{"role": "user", "content": "contexto velho"}],\n'
                      '    "visto": time.monotonic(), "geracao": 0}\n'
                      'app._settings["chat_backend"] = "openai"\n'
                      'app._settings["chat_backend_live"] = ""', p1))
    print(f"\n[C] #186 na ordem do fio (TTL=1s) em :{p1}")
    asyncio.run(cenario_186(p1))

    p3 = porta_livre()
    procs.append(sobe('import time\n'
                      'app._LIVE_MAX_SESSIONS = 1\n'
                      'app._live_historico["gate194"] = {\n'
                      '    "msgs": [{"role": "user", "content": "contexto velho"}],\n'
                      '    "visto": time.monotonic(), "geracao": 0}\n'
                      'app._settings["chat_backend"] = "openai"\n'
                      'app._settings["chat_backend_live"] = ""', p3))
    print(f"\n[C2] retomada não conta para o teto (teto=1) em :{p3}")
    asyncio.run(cenario_teto_retomada(p3))

    p2 = porta_livre()
    procs.append(sobe('app._LIVE_MAX_SESSIONS = 1\n'
                      'app._settings["chat_backend"] = "openai"\n'
                      'app._settings["chat_backend_live"] = ""', p2))
    print(f"\n[D] #187: setup inválido não consome slot (teto=1) em :{p2}")
    asyncio.run(cenario_187_slot(p2))
finally:
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), 15)
        except Exception:                                # noqa: BLE001
            p.terminate()

print("\n✖ FALHAS:" if falhas else "\n✔ re-derivação por fora OK (#185/#186/#187/#189)")
for f in falhas:
    print("  -", f)
sys.exit(1 if falhas else 0)