"""Gate QA da task_ab353d65 (#194): re-derivação dos 5 achados da auditoria do
backend (#185-#189) — o que os testes do autor NÃO cobriam.

Cada teste aqui existe porque a versão do autor deixava passar uma ordem de
eventos diferente (a morte do socket ANTIGO com o novo vivo, o cliente do pool
devolvido fora de ordem, o teto sob concorrência real).
"""

import contextlib
import threading
import time

import pytest

import app
from test_api import (  # noqa: F401 — fixtures, não estão no conftest
    _setup_ok, dsh_fake_bin, dsh_limpo, engine_fake, hist_limpo, live_limpo,
    pipeline_fake, ws_client)
from test_api import _isolamento_disco  # noqa: F401

__all__ = ["_isolamento_disco", "dsh_fake_bin", "dsh_limpo", "engine_fake",
           "hist_limpo", "live_limpo", "pipeline_fake", "ws_client"]


@pytest.fixture(autouse=True)
def backend_do_chat_pinado(monkeypatch):
    """O `settings.json` é COMPARTILHADO: outra suíte de tela pode estar no meio de
    um save (`chat_backend=dsh`, `chat_dsh_bin=/nao/existe/dsh`) e o `import app` do
    pytest lê o arquivo. Sem este pino, a sessão do Live nasce com um DshClient REAL
    no meio de um teste de registry e o frame `dsh_indisponivel` desalinha o
    `receive()`. Aqui o caminho é o stub: nenhum processo é spawnado."""
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "")


# ---------------------------------------------------------------- #186 (P2)
def test_socket_ANTIGO_morrendo_nao_apaga_a_sessao_retomada(ws_client, live_limpo,
                                                            pipeline_fake, engine_fake,
                                                            hist_limpo):
    """#186 pelo ângulo que faltava: quem fecha primeiro é o socket ANTIGO, com a
    sessão nova VIVA. O `pop` incondicional do finally apagava a entrada da nova —
    ela saía do registry, do TTL e da contagem do teto, e o `_live_hist_guarda` do
    zumbi regravava o contexto velho por cima."""
    with contextlib.ExitStack() as pilha:
        ws1 = pilha.enter_context(ws_client.websocket_connect("/api/live/ws"))
        _setup_ok(ws1)
        sess_velha = list(app._live_sessions.values())[0]
        sess_velha["pipe"].history[:] = [{"role": "user", "content": "contexto VELHO"}]
        app._live_hist_guarda(sess_velha)
        sid = sess_velha["id"]

        ws2 = pilha.enter_context(ws_client.websocket_connect("/api/live/ws"))
        pronto = _setup_ok(ws2, session_id=sid)
        assert pronto["type"] == "ready" and pronto["resumed"] is True
        sess_nova = app._live_sessions[sid]
        assert sess_nova is not sess_velha
        sess_nova["pipe"].history[:] = [{"role": "user", "content": "contexto NOVO"}]

        # a sessão nova JÁ rodou um turno (é o que o `_live_hist_pos_turno` faz):
        # a geração dela passa a mandar no registro do `sid`
        app._live_hist_guarda(sess_nova)
        assert [m["content"] for m in app._live_historico[sid]["msgs"]] == ["contexto NOVO"]

        ws1.close()                                  # o ANTIGO morre primeiro
        time.sleep(0.5)

        assert app._live_sessions.get(sid) is sess_nova, \
            "o finally do socket antigo apagou a sessão retomada"

        # o zumbi não pode regravar o contexto velho por cima do da retomada
        reg = app._live_historico.get(sid) or {}
        assert [m["content"] for m in reg.get("msgs") or []] == ["contexto NOVO"], reg

        # e a sessão nova continua VISÍVEL para o sweep (TTL) e para o teto
        sess_nova["visto"] = time.monotonic() - 9999
        assert app._live_sweep() == [sid], "a sessão nova ficou fora do sweep"

        # o `prewarm` do pipeline pode chegar antes: lê até o pong
        ws2.send_json({"type": "ping", "t": 3})
        for _ in range(10):
            m = ws2.receive_json()
            if m.get("type") == "pong":
                break
        assert m == {"type": "pong", "t": 3}, f"a sessão nova morreu junto ({m})"


def test_retomada_nao_rouba_a_entrada_da_sessao_antiga(ws_client, live_limpo,
                                                       pipeline_fake, engine_fake,
                                                       hist_limpo):
    """Ordem inversa (a do autor): o NOVO fecha primeiro. O antigo continua dono da
    própria entrada e volta a valer — nada de pop cruzado."""
    with contextlib.ExitStack() as pilha:
        ws1 = pilha.enter_context(ws_client.websocket_connect("/api/live/ws"))
        _setup_ok(ws1)
        sess_velha = list(app._live_sessions.values())[0]
        sess_velha["pipe"].history[:] = [{"role": "user", "content": "contexto VELHO"}]
        app._live_hist_guarda(sess_velha)          # sem histórico o guard nem registra
        sid = sess_velha["id"]
        ws2 = pilha.enter_context(ws_client.websocket_connect("/api/live/ws"))
        _setup_ok(ws2, session_id=sid)
        assert app._live_sessions[sid] is not sess_velha
        ws2.close()
        time.sleep(0.4)
        # CONTRATO (documentado no #186): a entrada do `sid` é da sessão NOVA; quando
        # ela morre, o registry esvazia mesmo com o socket ANTIGO vivo — o antigo
        # termina pelo próprio socket (a TTL vale para a registrada). O que NÃO pode
        # é o pop de uma derrubar a entrada da outra enquanto ela vive (teste acima).
        assert sid not in app._live_sessions, app._live_sessions
        ws1.send_json({"type": "ping", "t": 9})
        for _ in range(10):
            m = ws1.receive_json()
            if m.get("type") == "pong":
                break
        assert m == {"type": "pong", "t": 9}, "o socket antigo morreu junto (não devia)"


# ---------------------------------------------------------------- #185 (P2)
def test_cliente_do_pool_volta_pelo_modelo_DELE_e_o_antigo_e_fechado(dsh_limpo,
                                                                     monkeypatch):
    """#185 pela IDENTIDADE, não só pelo rótulo: o `devolve` de um cliente em voo,
    DEPOIS da troca de config, não pode carimbar a chave nova nele. E o cliente da
    config velha tem de ser FECHADO quando descartado, não largado no pool."""
    fechados = []
    monkeypatch.setattr(app, "_chat_dsh_livres", [])
    monkeypatch.setattr(app, "_chat_dsh_chave", None)

    def cliente_falso(cfg):
        cli = _Cli(cfg["model"])
        cli.fechados = fechados
        return cli

    class _Cli:
        def __init__(self, modelo):
            self.modelo = modelo
            self.alive = True
            self.efeito = None

        def close(self):
            self.alive = False
            self.fechados.append(self)

        def prewarm(self):
            return None

    monkeypatch.setattr(app, "_chat_dsh_novo", cliente_falso)
    app._settings["chat_dsh_model"] = '["rota","modelo-A"]'
    try:
        a = app._chat_dsh_cliente()                 # em voo
        app._settings["chat_dsh_model"] = '["rota","modelo-B"]'
        b = app._chat_dsh_cliente()                 # prewarm da config nova
        app._chat_dsh_devolve(b)
        app._chat_dsh_devolve(a)                    # devolução FORA de ordem
        entregue = app._chat_dsh_cliente()
        assert entregue.modelo == '["rota","modelo-B"]', \
            f"o pool entregou o cliente da config velha: {entregue.modelo}"
        assert entregue is b, "o pool tinha o cliente CERTO e preferiu outro"
        assert a.alive is False, "o cliente da config velha ficou vivo no pool"
    finally:
        app._settings["chat_dsh_model"] = app.dsh_client.DSH_DEFAULT_MODEL


# ---------------------------------------------------------------- #188 (P3)
def test_teto_de_sessoes_e_atomico_sob_concorrencia(ws_client, live_limpo, monkeypatch):
    """#188(b) com duas conexões SIMULTÂNEAS (o teste do autor é sequencial): a
    checagem do teto tem de ser atômica com a inserção, senão as duas passam."""
    monkeypatch.setattr(app, "_LIVE_MAX_SESSIONS", 1)
    prontos, recusados, erros = [], [], []

    def abre(i):
        try:
            c = app._ws_cliente() if hasattr(app, "_ws_cliente") else None
            from starlette.testclient import TestClient
            c = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
            with c.websocket_connect("/api/live/ws") as ws:
                ws.send_json({"type": "setup"})
                r = ws.receive_json()
                (prontos if r["type"] == "ready" else recusados).append(r)
                time.sleep(0.3)
        except Exception as exc:                     # noqa: BLE001
            erros.append(exc)

    ths = [threading.Thread(target=abre, args=(i,)) for i in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(10)
    assert not erros, erros
    assert len(prontos) == 1, f"o teto de 1 vazou: {len(prontos)} sessões prontas"
    assert len(recusados) == 1 and recusados[0]["code"] == "busy", recusados
    time.sleep(0.3)
    assert not app._live_sessions, "sessão presa no registry"

# ---------------------------------------------------------------- #189 (P3)
def test_resumo_do_live_usa_o_POOL_e_nao_o_cliente_da_sessao(ws_client, live_limpo,
                                                             pipeline_fake, hist_limpo,
                                                             monkeypatch):
    """#189 pelo que o teste do autor não prova: o resumo tem de sair pelo POOL (outro
    processo), senão ele disputa o slot de prompt da sessão com o turno em curso
    (-32602, lição do #162/#170). O cliente DA SESSÃO não pode ser chamado."""
    monkeypatch.setattr(app, "_live_resume_fn", None)
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
    do_pool, do_cliente_da_sessao = [], []

    class ClienteDaSessao:
        def collect(self, msgs):
            do_cliente_da_sessao.append(msgs)
            return "resumo-do-cliente"

    monkeypatch.setattr(app, "_chat_dsh_cliente", lambda: _Pool())
    monkeypatch.setattr(app.dsh_client, "DshClient", lambda **kw: _Pool())

    class _Pool:
        def __init__(self, **kw):
            pass

        def prewarm(self):
            return None

        def collect(self, msgs):
            do_pool.append(msgs)
            return "resumo-do-pool"

        def close(self):
            pass

    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        sess["dsh"] = ClienteDaSessao()
        sess["pipe"].history[:] = [{"role": "user", "content": "x" * 400}] * 40
        assert app._live_hist_comprime(sess) is True
        assert do_pool, "o resumo não passou pelo POOL do dsh"
        assert not do_cliente_da_sessao, "o resumo usou o cliente da SESSÃO (colide)"
        assert sess["pipe"].history[0]["content"].startswith("Resumo do que já foi dito:")


# ---------------------------------------------------------------- #188 (P3) —
def test_busy_chega_DEPOIS_do_setup_e_nao_no_connect(ws_client, live_limpo, monkeypatch):
    """Contrato de MOMENTO do #188: o teto é decidido no registro, então o `busy` só
    pode sair depois do `setup` — nada de erro na abertura do socket (que o cliente
    ainda não sabe interpretar). Medido na ordem do FIO."""
    monkeypatch.setattr(app, "_LIVE_MAX_SESSIONS", 1)
    monkeypatch.setattr(app, "_LIVE_SETUP_TIMEOUT_S", 0.3)
    with ws_client.websocket_connect("/api/live/ws") as ws1:
        _setup_ok(ws1)
        # (a) socket aberto com o teto CHEIO e SEM `setup`: o 1º frame tem de ser o
        # timeout do setup, não `busy` — é a prova de que a checagem saiu do connect.
        with ws_client.websocket_connect("/api/live/ws") as ws2:
            assert ws2.receive_json()["code"] == "setup_timeout", \
                "veio frame antes do setup: a checagem do teto voltou para o connect"
        # (b) mandando o `setup`: aí sim `busy`, depois do setup e com 1013
        monkeypatch.setattr(app, "_LIVE_SETUP_TIMEOUT_S", 30)
        with ws_client.websocket_connect("/api/live/ws") as ws3:
            ws3.send_json({"type": "setup"})
            assert ws3.receive_json()["code"] == "busy"


def test_pipeline_que_nao_nasce_fecha_o_dsh_orfao(ws_client, live_limpo, monkeypatch):
    """#188(a) pelo que o teste do autor não cobre: `sess["dsh"]` já foi criado
    quando o pipeline estoura, e sem pipe ninguém o fecharia — o processo dsh
    ficaria vivo até o fim do mundo. O finally tem de fechá-lo."""
    fechados = []

    class DshOrfao:
        def close(self):
            fechados.append(self)

    def explode(sess):
        sess["dsh"] = DshOrfao()          # como o `_live_pipe_novo` real faz
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "_live_pipe_novo", explode)
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        assert ws.receive_json()["code"] == "pipeline"
    time.sleep(0.3)
    assert fechados, "o dsh órfão da sessão sem pipeline não foi fechado"
    assert not app._live_sessions, "sessão presa no registry"


@pytest.mark.xfail(strict=True, reason="RESIDUAL do #186 (task nova): o `is sess` "
                   "está FORA do lock, então a retomada que nasce entre a checagem e "
                   "o pop ainda é apagada. Fix: checar dentro do `with _live_lock`.")
def test_retomada_que_nasce_DURANTE_o_fechamento_sobrevive(ws_client, live_limpo,
                                                           pipeline_fake, engine_fake,
                                                           hist_limpo):
    """A guarda tem de valer também quando a sessão nova nasce ENQUANTO a antiga
    fecha (não só nas ordens em que uma já morreu). Aqui o lock fica preso durante o
    fechamento e a retomada entra no meio — o pop da antiga não pode levar a nova.

    xfail(strict) enquanto o residual existir: quando o dono mover a checagem para
    dentro do lock, este teste passa e o `xfail` vira XPASS — aí é só tirar a marca."""
    with ws_client.websocket_connect("/api/live/ws") as ws:
        _setup_ok(ws)
        sess = list(app._live_sessions.values())[0]
        sid = sess["id"]
        app._live_lock.acquire()                 # o finally vai parar aqui dentro
        try:
            ws.close()
            time.sleep(0.4)
            nova = dict(sess)
            nova["geracao"] = sess["geracao"] + 1
            app._live_sessions[sid] = nova       # retomada nascendo agora
        finally:
            app._live_lock.release()
        time.sleep(0.5)
        assert app._live_sessions.get(sid) is nova, \
            "o fechamento da sessão antiga apagou a retomada que nasceu no meio"
