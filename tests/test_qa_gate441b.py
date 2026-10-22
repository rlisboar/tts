"""GATE cruzado de #441 (`task_5a34c69a`): F3, F4 e F5 do follow-up do #281.

F3 — `_caiu` tem de RECOLHER o processo: antes só marcava `_ok=False`, o `_proc`
seguia preenchido e o app pagava `to_thread` + lock + `poll()` a cada frame para
sempre. `morto` é o sinal de remoção, e tem de ser distinto de `ativo` (que é
False durante o `abrir()` — derrubar por ele mataria o handshake).

F4 — `fechar()` bloqueia até ~0,3 s (espera do filtro + `wait`) e é chamado do
event loop: tem de ir por `asyncio.to_thread`. O outro call site é síncrono de
propósito porque já roda em `to_thread`.

F5 — a evidência em `evidence/375-F5-ns-rnnoise.md` é conferida aqui: métrica e
baseline honestos, ressalvas escritas.
"""

import asyncio
import inspect
import subprocess
import threading
import time

import live_rnnoise as lr

__all__ = ["lr"]


def _ns():
    ns = lr.RnnoiseAoVivo()
    assert ns.abrir() is True
    return ns


def _falso_proc():
    from types import SimpleNamespace

    return SimpleNamespace(pid=1, stdin=None, wait=lambda timeout=None: None,
                           kill=lambda: None)


def _estado(pid: int) -> str:
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def test_f3_caiu_recolhe_o_processo_e_devolve_cru():
    ns = _ns()
    try:
        # segura a referência do Popen: sem ela o `__del__` do CPython reapa o
        # filho na coleta e o gate passaria mesmo sem o recolhimento explícito
        proc = ns._proc
        pid = proc.pid
        proc.kill()
        time.sleep(0.1)
        cru = b"\x01\x00" * 160
        assert ns.processa(cru) == cru, "não devolveu o cru"
        assert ns.morto is True
        assert ns._proc is None, "não recolheu o processo"
        # reaped: sem entrada no ps (ou ao menos sem Z)
        # o `poll()` do `processa` já reapa o filho morto; o que só o `_mata`
        # garante é fechar o pipe de entrada (e matar de fato o que não sai)
        assert proc.stdin is not None and proc.stdin.closed, "não fechou o stdin do filtro"
        assert _estado(pid) in ("",) or "Z" not in _estado(pid), _estado(pid)
    finally:
        ns.fechar()


def test_f3_morto_e_distinto_de_ativo_durante_o_abrir():
    """`ativo` é False no handshake: derrubar por ele mataria a sessão nova."""
    ns = lr.RnnoiseAoVivo()
    t = threading.Thread(target=ns.abrir)
    t.start()
    t.join(20)
    assert ns.ativo is True and ns.morto is False
    ns.fechar()
    assert ns.ativo is False


def test_f3_frame_seguinte_tira_o_filtro_da_sessao():
    import app

    ns = _ns()
    try:
        sess = {"rnnoise": ns, "id": "s"}
        ns._proc.kill()
        time.sleep(0.1)
        cru = b"\x02\x00" * 160
        assert app._live_pcm_limpo(sess, cru) == cru
        assert "rnnoise" not in sess, "o filtro ficou preso na sessão"
    finally:
        ns.fechar()


def test_f4_handler_usa_to_thread():
    import app

    fonte = inspect.getsource(app._live_handler_fecha) if hasattr(
        app, "_live_handler_fecha") else inspect.getsource(app)
    assert "await asyncio.to_thread(ns.fechar)" in fonte


def _com_mata_lenta(ns):
    original = ns._mata

    def _lenta(proc):
        time.sleep(0.2)
        return original(proc)

    ns._mata = _lenta
    ns._proc = _falso_proc()
    return ns


async def _tiques_durante(fechar_em_thread: bool) -> int:
    ns = _com_mata_lenta(lr.RnnoiseAoVivo())
    tiques = 0
    parar = asyncio.Event()

    async def _ticker():
        nonlocal tiques
        while not parar.is_set():
            tiques += 1
            await asyncio.sleep(0.01)

    tarefa = asyncio.create_task(_ticker())
    await asyncio.sleep(0.03)
    base = tiques
    if fechar_em_thread:
        await asyncio.to_thread(ns.fechar)
    else:
        ns.fechar()
    marcas = tiques - base
    parar.set()
    await tarefa
    return marcas


def test_f4_fechar_fora_do_loop_nao_zera_o_ticker():
    """Em thread o loop segue tiquetando durante o fechamento; síncrono, não."""
    em_thread = asyncio.run(_tiques_durante(True))
    assert em_thread >= 8, f"loop travou mesmo em thread: {em_thread} tiques"
    travado = asyncio.run(_tiques_durante(False))
    assert travado <= 3, f"fechar síncrono não travou o loop: {travado} dentes"


def test_f5_evidencia_tem_metrica_baseline_e_ressalvas():
    from pathlib import Path

    texto = (Path(__file__).resolve().parent.parent
             / "evidence" / "375-F5-ns-rnnoise.md").read_text()
    assert "erro de palavra contra o limpo" in texto          # métrica
    assert "baseline" in texto.lower()
    assert "Ressalvas" in texto and "NÃO diz" in texto
    assert "noiseSuppression" in texto                        # a metade não medida
    assert "vento" in texto.lower() or "ventoinha" in texto
