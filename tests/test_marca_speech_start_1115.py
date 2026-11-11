"""Live aberto e ocioso: o `speech_start` sobe/renova o filho da marca (#1115).

O `prewarm()` da marca rodava só na abertura do WS. Com o Live aberto e mais de
`TTL_S` (600 s) sem turno, o vigia fechava o filho e o próximo turno subia outro
DENTRO do caminho do 1º áudio (3–6 s de `marca` no S2). Agora todo `speech_start`
chama `_live_marca_prewarm()`: a carga corre enquanto fala + STT + LLM correm.

Filho de mentira (só dorme), sem AudioSeal; vigia acelerado.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import app  # noqa: E402
import marca_dagua  # noqa: E402


def _speech_start(barge=False):
    return types.SimpleNamespace(
        tipo="speech_start", t_ms=0, prob=0.9, barge_in=barge,
        to_json=lambda: {"type": "speech_start", "barge_in": barge})


def _sess():
    return {"id": "t1115", "turno": 0}


@pytest.fixture
def evento_mudo(monkeypatch):
    """Só o que o `speech_start` toca fora da marca vira no-op."""
    monkeypatch.setattr(app, "_live_envia_json", lambda sess, dados: None)
    monkeypatch.setattr(app, "_live_marca_prov", lambda sess, prob: None)
    monkeypatch.setattr(app, "_live_tenta_continuacao", lambda sess: None)
    monkeypatch.setattr(app, "_live_log_kv", lambda *a, **k: None)


@pytest.fixture
def marca_ligada(monkeypatch):
    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)


@pytest.mark.parametrize("barge", [False, True])
def test_speech_start_pede_prewarm_da_marca(evento_mudo, marca_ligada,
                                            monkeypatch, barge):
    chamadas: list = []
    monkeypatch.setattr(marca_dagua, "prewarm", lambda: chamadas.append(1))
    app._live_trata_eventos(_sess(), [_speech_start(barge=barge)])
    assert chamadas == [1]


def test_marca_desligada_nao_sobe_filho(evento_mudo, monkeypatch):
    monkeypatch.setitem(app._settings, "tts_marca_dagua", False)
    chamadas: list = []
    monkeypatch.setattr(marca_dagua, "prewarm", lambda: chamadas.append(1))
    app._live_trata_eventos(_sess(), [_speech_start()])
    assert chamadas == []


def test_prewarm_que_estoura_nao_derruba_o_evento(evento_mudo, marca_ligada,
                                                   monkeypatch):
    def estoura():
        raise RuntimeError("sem marca")
    seguiu: list = []
    monkeypatch.setattr(marca_dagua, "prewarm", estoura)
    monkeypatch.setattr(app, "_live_tenta_continuacao", lambda s: seguiu.append(1))
    app._live_trata_eventos(_sess(), [_speech_start()])
    assert seguiu == [1], "o resto do speech_start tem de rodar"


@pytest.fixture
def filho_de_mentira(tmp_path, monkeypatch):
    script = tmp_path / "filho_dorme.py"
    script.write_text("import time\ntime.sleep(3600)\n", encoding="utf-8")
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "WORKER", script)
    monkeypatch.setattr(marca_dagua, "INTERVALO_VIGIA_S", 0.05)
    monkeypatch.setattr(marca_dagua, "_proc", None)
    monkeypatch.setattr(marca_dagua, "_vigia_thread", None)
    procs: list = []
    real = marca_dagua._pega

    def espiar():
        proc = real()
        if proc not in procs:
            procs.append(proc)
        return proc

    monkeypatch.setattr(marca_dagua, "_pega", espiar)
    try:
        yield procs
    finally:
        marca_dagua._proc = None
        for p in procs:
            if p.poll() is None:
                p.kill()
                try:
                    p.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass


def _espera(cond, teto: float) -> bool:
    fim = time.monotonic() + teto
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_live_ocioso_alem_do_ttl_o_speech_start_sobe_filho_novo(
        evento_mudo, marca_ligada, filho_de_mentira, monkeypatch):
    """O caso do card: o vigia fechou o filho com o Live aberto; a fala seguinte
    sobe outro ANTES do 1º PCM da resposta (aqui: sem nenhum pedido de marca)."""
    monkeypatch.setattr(marca_dagua, "TTL_S", 0.2)
    app._live_marca_prewarm()                    # abertura do WS
    assert _espera(lambda: len(filho_de_mentira) == 1, 5.0)
    primeiro = filho_de_mentira[0]
    # Live aberto e ocioso: o vigia vence o TTL e solta o filho
    assert _espera(lambda: marca_dagua._proc is None, 5.0)
    monkeypatch.setattr(marca_dagua, "TTL_S", 600.0)
    app._live_trata_eventos(_sess(), [_speech_start()])
    assert _espera(lambda: len(filho_de_mentira) == 2, 5.0), \
        "o speech_start não subiu filho: o 1º PCM pagaria a carga"
    novo = filho_de_mentira[1]
    assert novo.pid != primeiro.pid
    assert marca_dagua._proc is novo and novo.poll() is None


def test_prewarm_nao_trava_o_laco_com_pedido_de_marca_em_curso(
        evento_mudo, marca_ligada, filho_de_mentira):
    """Um pedido em curso segura `_trava`; o `speech_start` não pode esperar por ela
    (o laço de eventos do Live pararia junto). O evento roda numa thread com teto:
    se ele esperar a trava, a célula FALHA em vez de pendurar a suíte."""
    fim = threading.Event()

    def evento():
        app._live_trata_eventos(_sess(), [_speech_start()])
        fim.set()

    with marca_dagua._trava:                 # o pedido de marca em curso
        fio = threading.Thread(target=evento, daemon=True)
        fio.start()
        voltou = fim.wait(0.5)
    fio.join(5)                              # trava solta: o mutante destrava aqui
    assert voltou, "speech_start esperou a trava da marca (laço do Live parado)"
    # a thread do prewarm segue sozinha depois que a trava solta
    assert _espera(lambda: len(filho_de_mentira) == 1, 5.0)
