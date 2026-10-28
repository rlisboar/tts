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
import json, sys, threading, time
MAPA = {{"mas e o preço?": 0.9, "uhum, ok": 0.05, "sim, sim, continua": 0.1}}
{antes}


def responde(pid, fala, atraso):
    if atraso:
        time.sleep(atraso)
    print(json.dumps({{"id": pid, "p_cortar": MAPA.get(fala, 0.5),
                      "decisao": "cortar", "ms": 3}}), flush=True)


print(json.dumps({{"tipo": "pronto"}}), flush=True)
for linha in sys.stdin:
{mudo}
    p = json.loads(linha)
    if p.get("tipo") == "close":
        break
    fala = p.get("fala") or ""
    if {paralelo}:
        # fora de ordem de propósito: a fala de "cortar" demora e a de "continuar"
        # volta primeiro. Sem isso os ids saem na ordem de chegada e um cliente que
        # case "a próxima linha é minha" passa mesmo trocando as respostas.
        threading.Thread(target=responde, args=(p.get("id"), fala, {atraso}),
                         daemon=True).start()
    else:
        responde(p.get("id"), fala, 0)
"""

#: atraso da fala de "cortar" quando o stub responde fora de ordem
ATRASO_CORTAR = 0.35


def _stub(tmp_path: Path, mudo: bool = False, quebra: bool = False,
          fora_de_ordem: bool = False, demora_pronto: float = 0.0) -> Path:
    p = tmp_path / "stub_worker.py"
    antes = ("raise SystemExit(3)" if quebra else
             (f"import time; time.sleep({demora_pronto})" if demora_pronto else ""))
    p.write_text(STUB.format(antes=antes,
                             mudo="    continue" if mudo else "    pass",
                             paralelo=fora_de_ordem,
                             atraso=ATRASO_CORTAR if fora_de_ordem else 0))
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


def _inc(tmp_path, mudo=False, quebra=False, timeout_ms=5000,
         fora_de_ordem=False):
    """Prazo folgado por padrão: o stub responde em ms e o 400 ms do produto é
    cobrado à parte (no teste de prazo e no `mede_intencao.py`). Sob suíte
    carregada o prazo de produção vencia aqui e o arquivo ficava intermitente."""
    inc = live_intencao.Intencao(python=sys.executable, timeout_ms=timeout_ms)
    inc._script = _stub(tmp_path, mudo, quebra, fora_de_ordem)

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
    """Stub responde FORA DE ORDEM: o id é o que casa, não a posição na fila."""
    inc = _inc(tmp_path, fora_de_ordem=True)
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


# --------------------------------------------------------------------------
# #595: o filtro não pode morrer de vez num aquecimento estourado
# --------------------------------------------------------------------------
def test_aquecimento_estourado_rearma_e_volta_a_decidir(tmp_path, monkeypatch):
    """Achado F1 do gate: `_morto` nunca limpava e o filtro morria na sessão.

    Com o prazo de aquecimento curto, a 1ª tentativa estoura (o stub demora o
    `pronto`). Depois do cooldown a 2ª tentativa tem de SUBIR e o `decide` voltar a
    decidir — com o código antigo o 2º `aquece` devolvia False para sempre e o
    `decide` era None até o fim da sessão."""
    monkeypatch.setattr(live_intencao, "FALHA_ESPERA_S", 0.05)
    inc = live_intencao.Intencao(python=sys.executable, timeout_ms=5000)
    live_intencao.WORKER = _stub(tmp_path, demora_pronto=0.6)
    assert inc.aquece(espera_s=0.05) is False      # estourou
    assert inc.ativo is False
    assert inc.decide("x", "mas e o preço?") is None
    # `tenta_em_s` é arredondado (mostra no `stats`): espera o relógio real
    assert _espera(lambda: time.monotonic() >= inc._tenta_em + 0.02), \
        "o cooldown não pode ser eterno"
    live_intencao.WORKER = _stub(tmp_path)         # agora sobe na hora
    assert inc.aquece(espera_s=10) is True, "o 2º aquecimento tinha de re-armar"
    assert inc.ativo is True
    r = inc.decide("o prazo é sexta", "mas e o preço?")
    assert r and r["decisao"] == "cortar", "o filtro tem de decidir de novo"


def test_falha_arma_cooldown_e_nao_sobe_a_cada_turno(tmp_path, monkeypatch):
    monkeypatch.setattr(live_intencao, "FALHA_ESPERA_S", 5.0)
    inc = live_intencao.Intencao(python="/nao/existe/python")
    assert inc.aquece() is False
    assert inc.tenta_em_s > 1.0, "falhou e não armou espera"
    # dentro do cooldown não tenta de novo (marca o spawn pela 2ª chamada)
    monkeypatch.setattr(live_intencao.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawn")))
    assert inc.aquece() is False
    assert inc.aquece(forcar=True) is False        # `forcar` fura o cooldown


def test_ativo_acompanha_o_processo_e_nao_a_marca_de_falha(tmp_path):
    inc = _inc(tmp_path)
    assert _aquece(inc)
    assert inc.ativo is True
    inc._proc.kill()
    inc._proc.wait(5)
    assert _espera(lambda: inc.ativo is False)
    assert inc.morto is True or inc.ativo is False


def test_worker_unico_conta_referencias_e_fecha_tudo(tmp_path):
    live_intencao.WORKER = _stub(tmp_path)
    a = live_intencao.pega()
    b = live_intencao.pega()
    assert a is b, "duas sessões têm de compartilhar o MESMO worker"
    assert live_intencao.estado()["refs"] == 2
    live_intencao.solta()
    assert live_intencao.estado()["refs"] == 1
    live_intencao.solta()
    live_intencao.fecha_tudo()
    assert live_intencao.estado()["vivo"] is False
