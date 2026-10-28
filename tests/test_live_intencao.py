"""Contrato do classificador de intenção (#507) — SEM o modelo.

O arquivo roda com um **worker de mentira** (mesmo protocolo NDJSON, respostas
canhão): o Qwen custa ~2 GB e segundos de boot, e era ele que deixava o teste
intermitente (`task_6fd904ce`). A prova de ACERTO com o modelo real é o
`tools/bakeoff/../mede_intencao.py` (fora do pytest); aqui se prova o contrato:
prazo, `None`, isolamento de crash, worker único, TTL, `stats` e egress.

Espera-se por CONDIÇÃO com prazo — nunca `sleep` fixo.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import live_intencao  # noqa: E402

#: worker falso: mesmo protocolo, respostas canhão. As variantes são injetadas
#: no próprio script (o spawn é `python <arquivo>`, sem argumentos).
STUB = """
import json, sys
MAPA = {{"mas e o preço?": 0.9, "uhum, ok": 0.05, "sim, sim, continua": 0.1}}
{antes}
print(json.dumps({{"tipo": "pronto"}}), flush=True)
for linha in sys.stdin:
{mudo}
    p = json.loads(linha)
    if p.get("tipo") == "close":
        break
    fala = p.get("fala") or ""
    print(json.dumps({{"id": p.get("id"), "p_cortar": MAPA.get(fala, 0.5),
                      "decisao": "cortar", "ms": 3}}), flush=True)
"""


def _stub(tmp_path: Path, mudo: bool = False, quebra: bool = False) -> Path:
    p = tmp_path / "stub_worker.py"
    p.write_text(STUB.format(antes="raise SystemExit(3)" if quebra else "",
                             mudo="    continue" if mudo else "    pass"))
    return p


@pytest.fixture(autouse=True)
def _limpa():
    live_intencao.fecha_tudo()
    yield
    live_intencao.fecha_tudo()


def _espera(cond, teto: float = 10.0, passo: float = 0.02) -> bool:
    limite = time.monotonic() + teto
    while time.monotonic() < limite:
        if cond():
            return True
        time.sleep(passo)
    return bool(cond())


def _inc(tmp_path, mudo=False, quebra=False, timeout_ms=5000):
    """Prazo folgado por padrão: o stub responde em ms e o 400 ms do produto é
    cobrado à parte (no teste de prazo e no `mede_intencao.py`). Sob suíte
    carregada o prazo de produção vencia aqui e o arquivo ficava intermitente."""
    inc = live_intencao.Intencao(python=sys.executable, timeout_ms=timeout_ms)
    inc._script = _stub(tmp_path, mudo, quebra)

    return inc


def _aquece(inc) -> bool:
    """Sobe o stub apontando o módulo para ele (o worker real é o do repo)."""
    live_intencao.WORKER = inc._script
    return inc.aquece(espera_s=10)


def test_worker_que_nao_sobe_da_None(tmp_path):
    inc = live_intencao.Intencao(python="/nao/existe/python")
    assert inc.aquece() is False
    assert inc.decide("x", "y") is None
    assert inc.ativo is False


def test_worker_que_morre_no_boot_da_None(tmp_path):
    inc = _inc(tmp_path, quebra=True)
    assert _aquece(inc) is False          # sem `pronto`: não sobe
    assert inc.decide("x", "y") is None


def test_decide_respeita_o_limiar(tmp_path, monkeypatch):
    inc = _inc(tmp_path)
    assert _aquece(inc)
    r = inc.decide("o prazo é sexta", "mas e o preço?", "pt")
    assert r and r["decisao"] == "cortar" and r["p_cortar"] >= 0.5
    monkeypatch.setattr(live_intencao, "LIMIAR", 0.99)
    assert inc.decide("o prazo é sexta", "mas e o preço?", "pt")["decisao"] == "continuar"


def test_decisao_deterministica(tmp_path):
    inc = _inc(tmp_path)
    assert _aquece(inc)
    a = inc.decide("x", "uhum, ok")
    b = inc.decide("x", "uhum, ok")
    assert a and b and a["decisao"] == b["decisao"] == "continuar"
    assert a["p_cortar"] == b["p_cortar"]


def test_prazo_estourado_da_None_e_nao_pendura(tmp_path):
    inc = _inc(tmp_path, mudo=True, timeout_ms=150)
    assert _aquece(inc)
    t = time.perf_counter()
    assert inc.decide("x", "qualquer") is None
    assert (time.perf_counter() - t) < 0.6
    assert inc.ativo is True               # mudo ≠ morto: segue utilizável


def test_crash_do_filho_nao_derruba_o_pai(tmp_path):
    inc = _inc(tmp_path)
    assert _aquece(inc)
    inc._proc.kill()
    assert _espera(lambda: not inc.ativo)
    assert inc.decide("x", "y") is None


def test_fala_vazia_da_None_sem_chamar(tmp_path):
    inc = _inc(tmp_path)
    assert _aquece(inc)
    assert inc.decide("x", "   ") is None


def test_duas_decisoes_simultaneas_nao_trocam_resposta(tmp_path):
    inc = _inc(tmp_path)
    assert _aquece(inc)
    saida = {}

    def _roda(nome, fala):
        saida[nome] = inc.decide("x", fala)

    ts = [threading.Thread(target=_roda, args=("a", "mas e o preço?")),
          threading.Thread(target=_roda, args=("b", "uhum, ok"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert saida["a"]["decisao"] == "cortar"
    assert saida["b"]["decisao"] == "continuar"


# -- worker único, TTL e stats ------------------------------------------------

def test_duas_sessoes_compartilham_um_unico_worker(tmp_path):
    live_intencao.WORKER = _stub(tmp_path)
    a = live_intencao.pega()
    b = live_intencao.pega()
    assert a is b and a.ativo
    assert live_intencao.estado()["refs"] == 2
    live_intencao.solta()
    live_intencao.solta()


def test_ttl_sem_uso_fecha_o_worker(tmp_path, monkeypatch):
    live_intencao.WORKER = _stub(tmp_path)
    monkeypatch.setattr(live_intencao, "TTL_S", 0.1)
    monkeypatch.setattr(live_intencao, "INTERVALO_VIGIA_S", 0.02)
    inc = live_intencao.pega()
    assert _espera(lambda: inc.ativo)
    vistos = []
    inc.fecha = lambda: vistos.append(1) or inc.__class__.fecha(inc)
    live_intencao.solta()
    assert _espera(lambda: vistos), "o TTL venceu e o `fecha()` não foi chamado"


def test_stats_publica_vivo_e_rss(tmp_path):
    live_intencao.WORKER = _stub(tmp_path)
    assert live_intencao.estado()["vivo"] is False
    live_intencao.pega()
    e = live_intencao.estado()
    assert e["vivo"] is True and e["rss_mb"] > 0 and e["ttl_s"] > 0
    live_intencao.solta()


def test_modo_local_nao_chama_nada_de_fora(tmp_path, monkeypatch):
    """EGRESS: no modo local, nem com o worker morto, nada externo é consultado."""
    import app

    chamadas = []
    monkeypatch.setattr(app, "_use_remote_tts", lambda: chamadas.append("tts") or False)
    monkeypatch.setattr(app, "_use_remote_translate", lambda: chamadas.append("tr") or False)
    monkeypatch.setattr(__import__("dsh_client"), "DshClient",
                        lambda *a, **k: chamadas.append("dsh"))
    inc = _inc(tmp_path)
    assert _aquece(inc)
    inc.decide("x", "mas e o preço?")
    inc._proc.kill()
    assert _espera(lambda: not inc.ativo)
    assert inc.decide("x", "mas e o preço?") is None
    assert chamadas == [], chamadas


def test_default_dos_filtros_vem_completo_e_modelo_desligado():
    import app

    padrao = app._SETTINGS_DEFAULTS["live_barge_filtros"]
    assert set(padrao) == set(app._LIVE_BARGE_FILTROS)
    assert app._normaliza_filtro_barge("modelo", padrao["modelo"]) == {
        "on": False, "provedor": "local", "limiar": 0.5, "timeout_ms": 400}
