"""Watchdog do /health (#915).

O probe é síncrono e morre na fila do anyio quando o pool está preso. O relógio
tem de viver no ASGI, antes da rota: um timer dentro de health() não arma se a
rota nem começa, e um /health async esconderia o travamento do monitor.
"""

import asyncio
import threading
import time

from fastapi.testclient import TestClient

import app


def _zera_voo():
    with app._saude_voo._trava:
        app._saude_voo._em_voo.clear()
        app._saude_voo._ultimo_despejo = 0.0


def _arma(monkeypatch, arq, teto=0.3):
    _zera_voo()
    monkeypatch.setattr(app, "_SAUDE_WATCHDOG_ARQ", str(arq))
    monkeypatch.setattr(app, "_SAUDE_TRAVADO_S", teto)
    monkeypatch.setattr(app, "_SAUDE_DESPEJO_INTERVALO_S", 30.0)
    monkeypatch.setattr(app, "_SAUDE_GANCHO", None)


def _sem_despejo(arq) -> None:
    """Armar o prazo C cria o arquivo vazio. Despejo é cabeçalho ou `Timeout (`."""
    if not arq.exists():
        return
    texto = arq.read_text(encoding="utf-8", errors="replace")
    assert "[watchdog]" not in texto, texto[:500]
    assert "Timeout (" not in texto, texto[:500]


def _espera(arq, trecho, prazo=5.0) -> str:
    limite = time.monotonic() + prazo
    texto = ""
    while time.monotonic() < limite:
        if arq.is_file():
            texto = arq.read_text(encoding="utf-8", errors="replace")
            if trecho in texto:
                return texto
        time.sleep(0.05)
    raise AssertionError(f"despejo sem {trecho!r}: {texto[:800]!r}")


def _scope_health():
    return {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }


async def _receive_vazio():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_watchdog_esta_registrado_por_fora_do_teto():
    """O relógio está no app, por fora do teto de corpo.

    A célula do despejo monta o middleware à mão. Sem este registro, produção
    não anota o /health e o 421/413 do teto deixa de ser a camada de dentro.
    """
    classes = [m.cls for m in app.app.user_middleware]
    assert app._SaudeWatchdog in classes
    assert classes.index(app._SaudeWatchdog) < classes.index(app._TetoDeCorpo)


def test_path_do_health_e_exato():
    assert app._saude_e_health({"type": "http", "path": "/health"})
    assert not app._saude_e_health({"type": "http", "path": "/healthz"})
    assert not app._saude_e_health({"type": "http", "path": "/health/"})
    assert not app._saude_e_health({"type": "http", "path": "/api/health"})
    assert not app._saude_e_health({"type": "websocket", "path": "/health"})
    assert not app._saude_e_health({"type": "http", "path": "/api/tts"})
    if app._BASE_PATH:
        assert app._saude_e_health(
            {"type": "http", "path": f"{app._BASE_PATH}/health"})
        assert not app._saude_e_health(
            {"type": "http", "path": f"{app._BASE_PATH}/api/health"})


def test_asgi_despeja_com_a_rota_ainda_presa(tmp_path, monkeypatch):
    """O relógio arma antes de o inner app voltar.

    Se o `entra` sair de dentro do middleware (ou for para depois do await),
    o pedido preso na fila do anyio não despeja e esta célula morre.
    """
    arq = tmp_path / "watchdog.log"
    _arma(monkeypatch, arq)
    solta = asyncio.Event()
    entrou = asyncio.Event()

    async def preso(scope, receive, send):
        entrou.set()
        await solta.wait()

    async def cenario():
        mw = app._SaudeWatchdog(preso)
        tarefa = asyncio.create_task(mw(
            {"type": "http", "path": "/health"}, _receive_vazio, _send_mudo))
        await asyncio.wait_for(entrou.wait(), 2)
        texto = _espera(arq, "[watchdog]")
        assert "/health sem resposta" in texto
        assert "pid=" in texto
        assert arq.stat().st_mode & 0o777 == 0o600
        solta.set()
        await asyncio.wait_for(tarefa, 2)

    try:
        asyncio.run(cenario())
    finally:
        _zera_voo()


async def _send_mudo(_msg):
    return None


def test_outra_rota_presa_nao_despeja(tmp_path, monkeypatch):
    arq = tmp_path / "watchdog.log"
    _arma(monkeypatch, arq)

    async def preso(scope, receive, send):
        await asyncio.sleep(0.8)

    async def cenario():
        mw = app._SaudeWatchdog(preso)
        await mw({"type": "http", "path": "/api/tts"}, _receive_vazio, _send_mudo)

    try:
        asyncio.run(cenario())
    finally:
        _zera_voo()
    time.sleep(0.4)
    _sem_despejo(arq)


def test_health_preso_nomeia_o_gancho(tmp_path, monkeypatch):
    """A pilha nomeia quem segurou a rota. Sem o gancho o health volta na hora
    e o arquivo não nasce com `in gancho`."""
    arq = tmp_path / "watchdog.log"
    _arma(monkeypatch, arq)
    solta = threading.Event()
    entrou = threading.Event()
    caixa = []
    erro = []

    def gancho():
        entrou.set()
        solta.wait(8)

    monkeypatch.setattr(app, "_SAUDE_GANCHO", gancho)

    async def send(msg):
        caixa.append(msg)

    def chama():
        try:
            asyncio.run(app.app(_scope_health(), _receive_vazio, send))
            erro.append(None)
        except Exception as exc:  # noqa: BLE001 — a célula quer o erro, não o traceback
            erro.append(exc)

    t = threading.Thread(target=chama, daemon=True)
    t.start()
    try:
        assert entrou.wait(5), "health() não chamou o gancho"
        texto = _espera(arq, "in gancho")
        assert "in health" in texto
        assert "[watchdog]" in texto
    finally:
        solta.set()
        t.join(10)
        _zera_voo()
    assert erro == [None], erro
    status = [m.get("status") for m in caixa if m.get("type") == "http.response.start"]
    assert status == [200], status


def test_health_rapido_nao_despeja(tmp_path, monkeypatch):
    arq = tmp_path / "watchdog.log"
    # o primeiro GET paga o lifespan; com o teto de produção isso não despeja
    _arma(monkeypatch, arq, teto=30.0)
    cliente = TestClient(app.app, raise_server_exceptions=False)
    assert cliente.get("/health").status_code == 200
    _zera_voo()
    monkeypatch.setattr(app, "_SAUDE_TRAVADO_S", 0.3)
    if arq.exists():
        arq.unlink()
    t0 = time.monotonic()
    r = cliente.get("/health")
    levou = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert set(r.json()) == {"ok", "boot_ts", "rev"}
    assert levou < 0.3, f"health rápido levou {levou:.2f}s"
    time.sleep(0.6)
    _sem_despejo(arq)


def test_watchdog_c_despeja_com_a_gil_presa(tmp_path, monkeypatch):
    """A thread C do faulthandler escreve com a GIL presa numa chamada C.

    `time.sleep` e um ctypes comum soltam a GIL: o laço rearmaria e o prazo
    não estouraria. `PyDLL` não solta. Um `nanosleep` só (não um laço de
    `usleep`, que devolve a GIL entre as voltas) segura o prazo inteiro.
    O arquivo nasce com `Timeout (`.
    """
    import ctypes
    import faulthandler

    arq = tmp_path / "watchdog.log"
    _arma(monkeypatch, arq, teto=0.4)
    app._saude_rearma()
    assert arq.is_file(), "o rearme não criou o log"
    assert arq.stat().st_mode & 0o777 == 0o600
    antes = arq.read_text(encoding="utf-8", errors="replace")
    assert "Timeout (" not in antes

    class _Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    libc = ctypes.PyDLL(None, use_errno=True)
    libc.nanosleep.argtypes = [
        ctypes.POINTER(_Timespec), ctypes.POINTER(_Timespec)]
    libc.nanosleep.restype = ctypes.c_int
    # 1,2 s cobre o prazo de 0,4 s inteiro dentro da chamada que segura a GIL.
    req = _Timespec(1, 200_000_000)
    rem = _Timespec(0, 0)
    try:
        rc = libc.nanosleep(ctypes.byref(req), ctypes.byref(rem))
        texto = arq.read_text(encoding="utf-8", errors="replace")
    finally:
        faulthandler.cancel_dump_traceback_later()
    assert rc == 0, f"nanosleep rc={rc} errno={ctypes.get_errno()}"
    assert "Timeout (" in texto
    assert "test_watchdog_c_despeja_com_a_gil_presa" in texto
    assert "[watchdog]" not in texto
    assert len(texto) > len(antes)
    assert arq.stat().st_mode & 0o777 == 0o600
