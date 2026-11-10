"""#300: sender morto não pode deixar sessão ZUMBÁ, e a fila de envio tem teto.

A fila da sessão era `queue.Queue()` sem `maxsize` e a ÚNICA consumidora é a task
de envio: quando `ws.send_*` levanta, o `except` largava a sessão viva no registry,
com `visto` renovado a cada frame (o TTL nunca vence), segurando 1 de
`_LIVE_MAX_SESSIONS` e acumulando tudo o que a IA gerasse. Estes testes fixam os
dois lados: o SLOT sai na hora e a fila para de crescer no teto (derrubando o
áudio mais antigo, nunca o JSON).

Sem modelo e sem WS real — dublês.
"""

from __future__ import annotations

import asyncio
import pathlib

import app

TETO = 100_000                      # 100 kB: ~2 s de áudio, cabe num teste
CHUNK = b"\x00\x01" * 12_000        # 24 kB por chunk (PCM16)


def _fila(teto: int = TETO) -> app._LiveFila:
    return app._LiveFila(teto)


def _sess(fila=None) -> dict:
    sess = {"id": "s-fila", "fila": fila if fila is not None else _fila(),
            "turno": 1, "visto": 1.0, "st_proximo": 0.0, "t0": 1.0,
            "criada": 0.0, "vencida": False, "substituida": False, "fechar": False,
            "cancelado": app.threading.Event(),
            "audio_pendente": 0, "falando": False, "st_chunks": 0,
            "st_audio_bytes": 0, "st_stage": "idle", "engine": None}
    return sess


class _PipeFake:
    """Só o que o `_live_solta_slot` chama: a geração tem de parar."""

    def __init__(self) -> None:
        self.cancelado = 0

    def cancel(self) -> None:
        self.cancelado += 1


class _WsFalso:
    """WS de dublê: registra o que saiu; pode morrer ou travar no envio."""

    def __init__(self, falhar: bool = False, travar: bool = False,
                 travar_json: int = 0) -> None:
        self.falhar = falhar
        self.travar = travar
        # quantos `send_json` seguem pendurados (o ramo JSON precisa da mesma
        # célula do áudio — achado 2 do gate #1088). Contador, e não booleano,
        # para o `error`/`close` do fim ainda poder sair.
        self.travar_json = travar_json
        self.enviados: list = []
        self.fechado: int | None = None
        self._solta = asyncio.Event()

    async def send_json(self, obj) -> None:
        if self.falhar:
            raise RuntimeError("socket morto")
        if self.travar_json > 0:
            self.travar_json -= 1
            await self._solta.wait()     # cliente que não lê: idem, no ramo JSON
        self.enviados.append(obj)

    async def send_bytes(self, payload: bytes) -> None:
        if self.falhar:
            raise RuntimeError("socket morto")
        if self.travar:
            await self._solta.wait()     # cliente que não lê: sender fica preso
        self.enviados.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.fechado = code


# -- teto da fila ----------------------------------------------------------
def test_teto_conta_bytes_e_derruba_o_audio_mais_antigo():
    fila = _fila()
    for i in range(6):
        fila.put(("audio", bytes([i]) * (len(CHUNK))))
    assert fila.bytes <= TETO                 # parou no teto
    assert fila.descartados == 6 - TETO // len(CHUNK)
    # o que sobra é o MAIS NOVO (0 já saiu; 5 está lá)
    restantes = [item[1][0] for item in list(fila._q.queue)]
    assert restantes and restantes[-1] == 5 and 0 not in restantes


def test_json_nunca_e_derrubado():
    fila = _fila()
    fila.put(("json", {"type": "turn_complete"}))
    for _ in range(6):
        derrubados = fila.put(("audio", CHUNK))
    assert derrubados == 1                    # só o áudio atrás do teto caiu
    tipos = [item[0] for item in list(fila._q.queue)]
    assert "json" in tipos and fila.qsize() <= TETO // len(CHUNK) + 1


def test_put_devolve_quantos_derrubou_e_get_desconta():
    fila = _fila()
    fila.put(("audio", CHUNK))
    antes = fila.bytes
    fila.get()                                # consome o único item
    assert fila.bytes == 0 and antes == len(CHUNK)
    derrubados = 0
    for _ in range(5):
        derrubados += fila.put(("audio", CHUNK))
    assert derrubados > 0 and fila.bytes <= TETO
    assert fila.descartados == derrubados


def test_item_unico_maior_que_o_teto_fica_na_fila():
    fila = _fila()
    assert fila.put(("audio", CHUNK * 10)) == 0   # nada antigo para derrubar
    assert fila.qsize() == 1 and fila.bytes == 10 * len(CHUNK)


def test_teto_zero_volta_a_ser_ilimitado():
    fila = _fila(0)
    for _ in range(10):
        assert fila.put(("audio", CHUNK)) == 0
    assert fila.bytes == 10 * len(CHUNK) and fila.descartados == 0


def test_sem_audio_no_teto_a_fila_passa_do_teto_em_json():
    """Só JSON acima do teto: não há o que derrubar e não pode entrar em laço."""
    fila = _fila()
    for _ in range(4):
        fila.put(("json", {"type": "stats"}))
    assert fila.qsize() == 4 and fila.bytes == 0


# -- ajuste do `audio_pendente` -------------------------------------------
def test_audio_derrubado_sai_da_conta_de_em_voo():
    sess = _sess()
    for _ in range(6):
        app._live_envia_audio(sess, CHUNK)
    assert sess["fila"].bytes <= TETO
    assert sess["audio_descartados"] > 0
    # não pode ficar somando chunks que nunca vão ao ar
    assert sess["audio_pendente"] <= sess["fila"].qsize()


def test_envia_audio_funciona_com_queue_crua_de_duble():
    """`queue.Queue` puro devolve None no `put`: o call site não pode quebrar."""
    sess = _sess(fila=app.queue.Queue())
    app._live_envia_audio(sess, CHUNK)
    assert sess["audio_pendente"] == 1 and sess["fila"].qsize() == 1
    assert "audio_descartados" not in sess


# -- morte do sender -------------------------------------------------------
def test_sender_morto_solta_o_slot_na_hora():
    """O aceite do #300: a sessão sai de `_live_sessions` e o slot volta."""
    sess = _sess()
    pipe = _PipeFake()
    sess["pipe"] = pipe
    app._live_sessions["s-fila"] = sess
    try:
        assert len(app._live_sessions) == 1
        app._live_envia_audio(sess, CHUNK)
        ws = _WsFalso(falhar=True)
        asyncio.run(app._live_sender(sess, ws))
        assert "s-fila" not in app._live_sessions     # slot liberado
        assert len(app._live_sessions) == 0
        assert sess["fechar"] is True
        assert sess["cancelado"].is_set()
        assert ws.fechado == 1011
        assert pipe.cancelado == 1        # ninguém vai ouvir: para a geração
    finally:
        app._live_sessions.pop("s-fila", None)


def test_sender_nao_derruba_sessao_de_outra_conexao():
    """`sid` repetido: o pop do sender velho não pode levar a sessão NOVA."""
    sess = _sess()
    outra = _sess()
    outra["id"] = "s-fila"
    app._live_sessions["s-fila"] = outra
    try:
        app._live_envia_audio(sess, CHUNK)
        asyncio.run(app._live_sender(sess, _WsFalso(falhar=True)))
        assert app._live_sessions["s-fila"] is outra
    finally:
        app._live_sessions.pop("s-fila", None)


def test_todo_send_no_ws_do_live_tem_teto():
    """Guarda estática: NENHUM `ws.send_*` do app fica sem `wait_for` (#1088).

    O conserto anterior cobria 3 de 12 e o gate reprovou — a célula dinâmica
    cobre o caminho do sender, esta cobre os envios de handshake (`ready`,
    `busy`, `unauthorized`, erros de `setup`), que são de outro escopo e onde o
    `ready` é o pior caso: ele sai ANTES de a task de envio existir, então
    pendurar ali nasce uma sessão muda sem ninguém para socorrer.
    """
    linhas = pathlib.Path(app.__file__).read_text().splitlines()
    soltos = []
    for n, linha in enumerate(linhas):
        if "ws.send_json(" not in linha and "ws.send_bytes(" not in linha:
            continue
        if not _tem_teto(linhas, n):
            soltos.append(f"{n + 1}: {linha.strip()}")
    assert not soltos, "send sem teto no WS do Live:\n  " + "\n  ".join(soltos)


def _tem_teto(linhas: list[str], i: int) -> bool:
    """O `send_*` em `i` está dentro de um `wait_for` que PASSA o teto?

    Exigir as duas coisas é o ponto: só `wait_for(` aceita a chamada sem o
    argumento, que estoura `TypeError` em runtime e passa verde na guarda —
    o `TypeError` é engolido pelo `except` de quem chama. O teto é o segundo
    argumento, então ele pode estar muitas linhas abaixo (o `ready` monta um
    dict de 15 linhas): por isso a varredura vai até fechar o `wait_for`.
    """
    if "wait_for(" in linhas[i]:
        profundidade = 0
    elif "wait_for(" in (linhas[i - 1] if i else ""):
        profundidade = 1          # o `wait_for(` abriu na linha de cima
    else:
        return False
    for j in range(i, min(len(linhas), i + 40)):
        if "_LIVE_SEND_TIMEOUT_S" in linhas[j]:
            return True
        profundidade += linhas[j].count("(") - linhas[j].count(")")
        if profundidade <= 0:     # o `wait_for` fechou sem o teto
            return False
    return False


def test_send_json_pendurado_tambem_derruba_a_sessao(monkeypatch):
    """Mesmo teto no ramo JSON (achado 2 do gate #1088).

    Só o `send_bytes` estava coberto: removendo o `wait_for` do `send_json` a
    suíte ficava verde. Aqui o `turn_complete` pendura e a sessão tem de cair
    igual — sem isso, mensagem de controle (inclusive o `ready`) fica sem teto.
    """
    monkeypatch.setattr(app, "_LIVE_SEND_TIMEOUT_S", 0.05)
    # sem `stats` periódico: ele também é `send_json` e mascararia o ramo do item
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 0)
    sess = _sess()
    sess["pipe"] = _PipeFake()
    ws = _WsFalso(travar_json=1)
    app._live_sessions["s-fila"] = sess
    try:
        sess["fila"].put(("json", {"type": "turn_complete"}))

        async def fluxo() -> None:
            await asyncio.wait_for(app._live_sender(sess, ws), 2.0)

        asyncio.run(fluxo())
        assert ws.fechado == 1011
        assert sess["pipe"].cancelado == 1
    finally:
        app._live_sessions.pop("s-fila", None)


def test_ready_pendurado_solta_o_slot(monkeypatch):
    """`ready` sem leitor devolve o slot (#1088).

    A sessão entra no registry ANTES do `ready`, e a task de envio ainda não
    existe. O sweeper só marca `vencida` — não fecha nem tira do registry. Se o
    `wait_for` ficar fora do `finally`, o estouro vaza o slot para sempre; se
    não houver `wait_for`, este teste estoura os 2 s.
    """
    monkeypatch.setattr(app, "_LIVE_SEND_TIMEOUT_S", 0.05)
    # o sweeper real só marca `vencida` e dorme 15 s — não pode nascer neste teste
    monkeypatch.setattr(app, "_live_sweeper_iniciado", True)
    monkeypatch.setattr(app, "_live_autentica", lambda ws: True)
    monkeypatch.setattr(app, "_live_engine_novo", lambda sess: None)
    monkeypatch.setattr(app, "_live_valida_setup", lambda msg: {
        "voice_id": "voz-teste", "voice_id_pedido": "", "session_id": "",
        "system": "", "history": [], "history_descartados": 0,
        "vad": {"silence_ms": 600, "prefix_ms": 100, "barge_ms": 400},
    })
    filtros = dict(app._settings.get("live_barge_filtros") or {})
    filtros["modelo"] = {"on": False}
    monkeypatch.setitem(app._settings, "live_barge_filtros", filtros)

    def _proibido(*_a, **_k):
        raise AssertionError("ready travado seguiu depois do teto")

    monkeypatch.setattr(app, "_live_rnnoise_novo", _proibido)
    monkeypatch.setattr(app, "_live_pipe_novo", _proibido)

    class _Ws:
        def __init__(self) -> None:
            self.headers: dict = {}
            self.fechado: int | None = None

        async def accept(self, subprotocol=None) -> None:
            return None

        async def receive(self):
            return {"type": "websocket.receive", "text": "{\"type\": \"setup\"}"}

        async def send_json(self, obj) -> None:
            await asyncio.Event().wait()

        async def send_bytes(self, payload: bytes) -> None:
            await asyncio.Event().wait()

        async def close(self, code: int = 1000) -> None:
            self.fechado = code

    ws = _Ws()
    antes = dict(app._live_sessions)
    app._live_sessions.clear()
    try:
        async def fluxo() -> None:
            await asyncio.wait_for(app.live_ws(ws), 2.0)

        asyncio.run(fluxo())
        assert app._live_sessions == {}
        assert ws.fechado == 1011
    finally:
        app._live_sessions.clear()
        app._live_sessions.update(antes)


def test_cliente_que_nao_le_para_de_crescer_no_teto():
    """Sender vivo e preso no envio: a fila estabiliza no teto, não vaza RAM."""
    sess = _sess()
    ws = _WsFalso(travar=True)

    async def fluxo() -> int:
        tarefa = asyncio.create_task(app._live_sender(sess, ws))
        for _ in range(10):
            app._live_envia_audio(sess, CHUNK)
        await asyncio.sleep(0.3)
        pico = max(sess["fila"].bytes, 0)
        tarefa.cancel()
        try:
            await tarefa
        except asyncio.CancelledError:
            pass
        return pico

    pico = asyncio.run(fluxo())
    assert pico <= TETO                       # não passou do teto
    assert sess["fila"].descartados > 0       # derrubou justamente por isso


# -- sessão normal ---------------------------------------------------------
def test_sessao_vencida_ainda_avisa_e_fecha_limpa():
    sess = _sess()
    sess["vencida"] = True
    app._live_sessions["s-fila"] = sess
    try:
        ws = _WsFalso()
        asyncio.run(app._live_sender(sess, ws))
        assert ws.enviados[-1]["code"] == "session_ttl"
        assert ws.fechado == 1000
        assert sess["fechar"] is False         # quem fecha é o endpoint, não aqui
    finally:
        app._live_sessions.pop("s-fila", None)


def test_stats_publica_o_backlog_da_fila():
    sess = _sess()
    app._live_envia_audio(sess, CHUNK)
    s = app._live_stats(sess)
    assert s["playback"]["fila_bytes"] == len(CHUNK)
    assert s["playback"]["fila_itens"] == 1 and s["playback"]["fila_descartados"] == 0


def test_send_pendurado_derruba_a_sessao_em_vez_de_emudecer(monkeypatch):
    """#1022 (P1): `send` sem teto trava a task de envio PARA SEMPRE.

    Sintoma medido no mini: 8 min de silêncio com o app saudável — o laço de
    recepção segue renovando `visto`, o TTL nunca vence, nenhum `error` sai e o
    Live fica mudo até o cliente desistir. O par parado de ler não pode ser
    invisível: estourou o teto, solta o SLOT, avisa no log e fecha.
    """
    monkeypatch.setattr(app, "_LIVE_SEND_TIMEOUT_S", 0.05)
    sess = _sess()
    sess["pipe"] = _PipeFake()
    ws = _WsFalso(travar=True)
    app._live_sessions["s-fila"] = sess
    try:
        app._live_envia_audio(sess, CHUNK)

        async def fluxo() -> None:
            await asyncio.wait_for(app._live_sender(sess, ws), 2.0)

        asyncio.run(fluxo())
        assert ws.fechado == 1011              # fechou em vez de ficar mudo
        assert sess["pipe"].cancelado == 1     # a geração parou
    finally:
        app._live_sessions.pop("s-fila", None)
