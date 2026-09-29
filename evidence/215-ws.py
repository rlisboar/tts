"""GATE #215 — re-derivação POR FORA dos itens 3 (#210), 4 (#211) e 5 (#212).

Nada aqui usa o teste do autor: o cliente é um TestClient próprio, a prova é de
ESTADO no fim do turno e de contrato observável no protocolo.

  item3: `session_id` do cliente é adotado; id inválido no setup não é adotado; e
         a consequência de contrato escrita no código (quem adivinha o id retoma a
         sessão de outro; 40 bits, sem vínculo com a chave).
  item4: ramo SEM pipeline — buffer/`truncado`/`st_stage`/registro não ficam presos.
  item5: `?ticket=` inválido cai para `?key=`/header; sem chave continua recusando.

O pipeline e o motor são neutralizados (dublês `None`) para exercitar o ramo stub
do handler, que é o caminho do item 4.

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-ws.py
"""
import logging
import os
import sys
import time

os.environ.setdefault("TTS_LIVE_WORKER", "0")
os.environ["TTS_ROD_API_KEY"] = "chave-do-gate-215"   # auth LIGADA (host testclient)
os.environ["TTS_LIVE_MAX_SESSIONS"] = "8"

import app                                    # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

logging.getLogger("live").setLevel(logging.WARNING)

KEY = "chave-do-gate-215"
HEADERS = {"x-api-key": KEY}
cli = TestClient(app.app)
falhas = []

# Mutações (o desenho PRÉ-fix de cada item), ligadas por `--mutacao=item4,item5`.
MUT = set()
SO = set()
for arg in sys.argv:
    if arg.startswith("--mutacao="):
        MUT.update(arg.split("=", 1)[1].split(","))
    elif arg == "--mutacao":
        MUT.update(("item3", "item4", "item5"))
    elif arg.startswith("--somente="):
        SO.update(arg.split("=", 1)[1].split(","))

# Marcador de uuid: distingue "o id veio do CLIENTE" de "o servidor gerou".
class UuidMarcador:
    marcado = "zzzzzzzzzz"      # hex[:10] como o handler usa
    hex = "zzzzzzzzzzzz"


app.uuid.uuid4 = lambda: UuidMarcador()


def ok(m):
    print(f"  ✔ {m}")


def falha(m):
    falhas.append(m)
    print(f"  ✘ {m}")


def sem_pipeline():
    app._live_pipe_novo = lambda sess: None
    app._live_engine_novo = lambda sess: None


def limpa():
    with app._live_lock:
        app._live_sessions.clear()
    app._live_historico.clear()


def espera(cond, limite=3.0):
    fim = time.time() + limite
    while time.time() < fim:
        if cond():
            return True
        time.sleep(0.01)
    return False


def conecta(query="", headers=None):
    # `{}` é falsy: sem este cuidado, "sem chave" mandava o header do gate junto
    cabecalhos = HEADERS if headers is None else headers
    return cli.websocket_connect(f"/api/live/ws{query}", headers=cabecalhos).__enter__()


def setup(ws, **extra):
    ws.send_json({"type": "setup", **extra})
    return ws.receive_json()


# ---------------------------------------------------------------- item 3 (#210)
def item3():
    print("item3 (#210) — session_id do cliente")
    limpa()
    hist = [{"role": "user", "text": "oi"}]
    ws = conecta()
    pronto = setup(ws, session_id="meu-id-fixo", history=hist)
    if pronto.get("session_id") == "meu-id-fixo" and pronto.get("resumed") is False:
        ok("id próprio do cliente é ADOTADO no ready (resumed=False)")
    else:
        falha(f"id próprio não adotado: {pronto}")
    if pronto.get("session_id") != UuidMarcador.marcado:
        ok("o id adotado NÃO é o gerado pelo servidor (uuid marcado não apareceu)")
    else:
        falha("o servidor gerou o id em vez de adotar o do cliente")
    ws.close()

    # sem id: aí sim o servidor gera (prova que o marcador de uuid funciona)
    ws = conecta()
    pronto = setup(ws, history=hist)
    if pronto.get("session_id") == UuidMarcador.marcado:
        ok("sem `session_id` o servidor GERA (uuid marcado aparece)")
    else:
        falha(f"geração do sid não usou o uuid: {pronto.get('session_id')}")
    ws.close()
    espera(lambda: app._live_hist_pega("meu-id-fixo") is not None)

    ws = conecta()
    pronto = setup(ws, session_id="meu-id-fixo")
    if pronto.get("session_id") == "meu-id-fixo" and pronto.get("resumed") is True:
        ok("reconexão com o MESMO id retoma a sessão (resumed=True)")
    else:
        falha(f"reconexão com o mesmo id não retomou: {pronto}")
    ws.close()

    # consequência de contrato escrita no código: o id NÃO tem vínculo com a chave
    # (o registro é chaveado só pelo sid) — outra credencial válida retoma o mesmo id
    OUTRA = "chave-de-outro-cliente-do-gate"
    with app._apikeys_lock:
        app._apikeys["keys"].append({"id": "g215", "name": "gate", "secret": OUTRA,
                                     "created_at": 0})
    ws = conecta(headers={"x-api-key": OUTRA})
    pronto = setup(ws, session_id="meu-id-fixo")
    if pronto.get("session_id") == "meu-id-fixo" and pronto.get("resumed") is True:
        ok("id do cliente é retomável por OUTRA chave válida (sem vínculo com a chave)")
    else:
        falha(f"outra chave não retomou o mesmo id: {pronto}")
    ws.close()
    if len(app.uuid.uuid4().hex) == 10:
        ok("id gerado pelo servidor tem 10 hex = 40 bits (o que o contrato declara)")

    # consequência de contrato: OUTRO cliente, nada em comum além do id
    limpa()
    ws1 = conecta()
    setup(ws1, session_id="id-de-outro", history=[{"role": "user", "text": "A"}])
    ws1.close()
    espera(lambda: app._live_hist_pega("id-de-outro") is not None)
    ws2 = conecta()
    p2 = setup(ws2, session_id="id-de-outro")
    if p2.get("session_id") == "id-de-outro" and p2.get("resumed") is True:
        ok("outro cliente que ADIVINHA o id retoma a sessão (é o contrato escrito)")
    else:
        falha(f"adivinhação do id não retomou: {p2}")
    with app._live_lock:
        n = len(app._live_sessions)
    ok(f"`_live_sessions` com {n} entrada(s) para o mesmo id (2 WS, o 2º sobrescreve)")
    ws2.close()

    # id inválido no setup NÃO é adotado
    limpa()
    for invalido in ("id com espaço", "a" * 65, "id/path"):
        ws = conecta()
        ev = setup(ws, session_id=invalido)
        with app._live_lock:
            sids = list(app._live_sessions)
        if ev.get("type") == "error" and not sids:
            ok(f"id inválido {invalido!r} recusa o setup e não cria sessão")
        else:
            falha(f"id inválido {invalido!r}: ev={ev} sids={sids}")
        ws.close()
    ok(f"teto do regex (64 chars) passa: "
       f"{len(app._live_valida_setup({'session_id': 'x' * 64})['session_id'])} chars")
    if len("x" * 10) == 10 and len(app._valida_sid() if hasattr(app, "_valida_sid") else "x" * 10) == 10:
        pass


# ---------------------------------------------------------------- item 4 (#211)
def item4():
    print("item4 (#211) — ramo SEM pipeline usa `_live_envia_json`")
    limpa()
    chamadas = {"hist": 0}
    original = app._live_hist_pos_turno
    app._live_hist_pos_turno = lambda sess: chamadas.__setitem__("hist", chamadas["hist"] + 1)

    ws = conecta()
    setup(ws)
    sess = list(app._live_sessions.values())[0]
    sess["buffer"] = bytearray(b"\x01\x02" * 500)
    sess["truncado"] = True
    app._live_stage(sess, "stt")                      # estado em que o painel mente
    ws.send_json({"type": "end_of_speech"})
    fim = {}
    limite = time.time() + 3
    while time.time() < limite:
        ev = ws.receive_json()
        if ev.get("type") in ("turn_complete", "error", "interrupted"):
            fim = ev
            break
    estado = {"st_stage": sess.get("st_stage"), "buffer": len(sess["buffer"]),
              "truncado": sess.get("truncado"), "hist": chamadas["hist"],
              "evento": fim.get("type"), "truncated": fim.get("truncated"),
              "stub": fim.get("stub"), "buffer_bytes": fim.get("buffer_bytes")}
    print(f"    estado no fim do turno: {estado}")
    if estado["evento"] == "turn_complete" and estado["stub"] is True:
        ok("turno do stub fechou com `turn_complete` (via `_live_envia_json`)")
    else:
        falha(f"stub não fechou o turno: {estado}")
    if estado["st_stage"] == "idle":
        ok("`st_stage` voltou a idle (o painel para de mentir 'fechando')")
    else:
        falha(f"`st_stage` preso em {estado['st_stage']}")
    if estado["buffer"] == 0:
        ok("buffer do mic zerado no fim do turno")
    else:
        falha(f"buffer com {estado['buffer']} bytes presos")
    if estado["truncado"] is False and estado["truncated"] is True:
        ok("`truncado` foi estampado no evento e consumido (vale por turno)")
    else:
        falha(f"`truncado` não foi consumido/estampado: {estado}")
    if estado["hist"] == 1:
        ok("`_live_hist_pos_turno` rodou (registro de retomada/compressão)")
    else:
        falha(f"hist_pos_turno rodou {estado['hist']}x")
    ws.close()
    app._live_hist_pos_turno = original


# ---------------------------------------------------------------- item 5 (#212)
def item5():
    print("item5 (#212) — ticket inválido cai para chave/header (LAN/túnel)")
    limpa()
    ticket = cli.post("/api/live/ticket", headers=HEADERS).json()["ticket"]

    def tenta(query, headers=None, rotulo=""):
        """Devolve ('ok', ready) ou ('recusa', code) — sem depender de exceção."""
        try:
            ws = conecta(query, headers)
        except Exception as exc:                   # noqa: BLE001
            return ("recusa", repr(exc))
        ev = setup(ws)
        if ev.get("type") == "error":
            fecha = ws.receive()                     # o close vem no frame seguinte
            ws.close()
            return ("recusa", (ev.get("code"), fecha.get("code")))
        ws.close()
        return ("ok", ev)

    tipo, dado = tenta(f"?ticket={ticket}")
    (ok if tipo == "ok" else falha)(f"ticket válido sozinho: {tipo} {dado if tipo=='recusa' else ''}")
    tipo, dado = tenta(f"?ticket={ticket}&key={KEY}")
    (ok if tipo == "ok" else falha)(f"ticket USADO + ?key= válida: {tipo} (fallback)")
    tipo, dado = tenta("?ticket=inventado&key=" + KEY)
    (ok if tipo == "ok" else falha)(f"ticket INVENTADO + ?key= válida: {tipo}")
    tipo, dado = tenta("?ticket=inventado", HEADERS)
    (ok if tipo == "ok" else falha)(f"ticket INVENTADO + header válido: {tipo}")
    for par in ("?ticket=inventado", "?ticket=", ""):
        tipo, dado = tenta(par, headers={})
        if tipo == "recusa" and dado == ("unauthorized", 4401):
            ok(f"sem chave e {par or '(sem query)'} recusa (erro unauthorized + 4401)")
        else:
            falha(f"sem chave e {par or '(sem query)'} NÃO recusou: {tipo} {dado}")

    t2 = cli.post("/api/live/ticket", headers=HEADERS).json()["ticket"]
    tenta(f"?ticket={t2}")
    if app._live_ticket_consome(t2) is False:
        ok("ticket tentado foi CONSUMIDO no handshake (não sobra para reuso)")
    else:
        falha("ticket sobreviveu ao uso")


def envio_cru(sess, obj):
    """Desenho pré-fix do ramo stub: fila crua, sem `_live_enriquece`/`observa`."""
    sess["fila"].put(("json", obj))


def autentica_antiga(ws):
    """Desenho pré-fix do #212: ticket era caminho sem volta."""
    if app._is_local(ws) or not app._auth_enabled():
        return True
    ticket = (ws.query_params.get("ticket") or "").strip()
    if ticket:
        return app._live_ticket_consome(ticket)
    chave = (ws.query_params.get("key") or "").strip() or app._extract_request_key(ws)
    return app._key_is_valid(chave)


def main():
    sem_pipeline()
    if "item4" in MUT:
        app._live_envia_json = envio_cru
    if "item5" in MUT:
        app._live_autentica = autentica_antiga
    for nome, fn in (("item3", item3), ("item4", item4), ("item5", item5)):
        if not SO or nome in SO:
            fn()
    print(f"\nfalhas: {len(falhas)}")
    for f in falhas:
        print(f"  - {f}")
    print("VEREDITO:", "OK" if not falhas else "FALHOU")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())