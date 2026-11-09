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
        threading.Thread(target=responde,
                         args=(p.get("id"), fala,
                               {atraso} if MAPA.get(fala, 0) > 0.5 else 0),
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


def _stub_real():
    """O worker de VERDADE (modelo) — só a célula de acerto usa."""
    return RAIZ / "intencao_worker.py"


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


def test_acerto_minimo_e_melhor_que_as_regras():
    """O aceite do #507: acerto >=85% no corpus e ACIMA das regras no mesmo corpus.

    É a célula que separa "o modelo decide" de "um rótulo fixo passa": fixando a
    probabilidade o acerto cai para 50% (o corpus é 35/35) e reprova aqui. A mordida
    é na `p_cortar`, não no campo `decisao` — o pai RECALCULA o rótulo por
    `p >= LIMIAR`, então fixar só o `decisao` do worker passa despercebido. Prazo
    folgado de propósito — na suíte cheia o Metal é disputado e o `None` do prazo
    de produção (400 ms) apareceria por mérito da carga, não do modelo; o número de
    produção é cobrado no `mede_intencao.py`."""
    import json

    corpus = [json.loads(l) for l in
              (Path(__file__).parent / "fixtures" / "intencao_corpus.jsonl").read_text().splitlines()
              if l.strip()]
    assert len(corpus) >= 60, f"corpus pequeno demais: {len(corpus)}"

    from mede_intencao import regra_decisao      # baseline do mesmo corpus

    regras = sum(1 for c in corpus if regra_decisao(c["fala"]) == c["esperado"])
    inc = live_intencao.Intencao(python=sys.executable, timeout_ms=1500)
    live_intencao.WORKER = _stub_real()
    if not inc.aquece(espera_s=120):
        pytest.skip(f"modelo não subiu: {inc.erro}")

    acertos, sem_resposta = 0, 0
    for c in corpus:
        r = inc.decide(c["ref"], c["fala"], c["idioma"])
        if r is None:
            sem_resposta += 1
            continue
        acertos += r["decisao"] == c["esperado"]
    total = len(corpus)
    print(f"\nmodelo {acertos}/{total} = {100 * acertos / total:.1f}% "
          f"| regras {regras}/{total} = {100 * regras / total:.1f}% "
          f"| sem resposta {sem_resposta}")
    assert acertos >= 0.85 * total, f"acerto {acertos}/{total} abaixo de 85%"
    assert acertos > regras, f"modelo ({acertos}) não ganhou das regras ({regras})"


def test_o_pai_decide_so_pela_probabilidade(tmp_path):
    """O limiar é do PAI: a resposta do worker não traz rótulo (#507).

    O worker já respondeu um campo `decisao` que o pai ignorava (ele recalcula por
    `p >= LIMIAR`). Campo que ninguém lê é armadilha: bastava os dois divergirem
    para ninguém ver. Aqui a resposta vem SEM o campo e a decisão tem de sair da
    probabilidade — inclusive contra um rótulo contraditório, se ele voltar."""
    inc = live_intencao.Intencao(python=sys.executable, timeout_ms=5000)
    inc._script = tmp_path / "stub_sem_rotulo.py"
    inc._script.write_text("""
import json, sys
print(json.dumps({"tipo": "pronto"}), flush=True)
for linha in sys.stdin:
    p = json.loads(linha)
    if p.get("tipo") == "close":
        break
    alta = "preço" in (p.get("fala") or "")
    # rótulo CONTRADITÓRIO de propósito: quem manda é a probabilidade
    print(json.dumps({"id": p.get("id"), "p_cortar": 0.93 if alta else 0.05,
                      "decisao": "continuar" if alta else "cortar", "ms": 1}), flush=True)
""")
    live_intencao.WORKER = inc._script
    assert inc.aquece(espera_s=10)
    assert inc.decide("x", "mas e o preço?")["decisao"] == "cortar"
    assert inc.decide("x", "uhum, ok")["decisao"] == "continuar"

    # e o worker de verdade não manda mais o campo
    import intencao_worker as iw

    fonte = Path(iw.__file__).read_text()
    assert '"decisao"' not in fonte.split("Resposta:")[1], "o worker voltou a rotular"


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
    import requests

    chamadas = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: chamadas.append("http"))
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
    # ESPERA POR CONDIÇÃO, não por relógio. O cadáver do filho que estourou é
    # derrubado pelo leitor em outra thread e, ao cair, RE-ARMA o cooldown — então
    # comparar `monotonic()` com `_tenta_em` dispara antes de o estado assentar e
    # falha na suíte cheia (passa isolado porque não há contenção). A condição é o
    # estado real: cadáver derrubado E cooldown livre.
    def _livre() -> bool:
        return inc._proc is None and inc.tenta_em_s == 0.0

    live_intencao.WORKER = _stub(tmp_path)         # agora sobe na hora
    assert _espera(_livre), "o cooldown não pode ser eterno"
    ok = False
    for _ in range(5):                             # tolera cooldown re-armado
        if inc.aquece(espera_s=10):
            ok = True
            break
        assert _espera(_livre), "o cooldown re-armou e não liberou"
    assert ok, "o 2º aquecimento tinha de re-armar"
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


# -- liberação da sessão (#890) ----------------------------------------------
#
# O fechamento LIMPO do WS não passava pelo `_live_solta_slot` (que só roda na
# morte da task de envio): `intencao_ref` ficava True, `solta()` nunca era
# chamado e o worker de 3,8 GB só saía pelo TTL. Estes testes fixam a devolução.

def _sess_890(sid: str = "s-890") -> dict:
    import app

    return {"id": sid, "intencao_ref": False, "fila": app._LiveFila(1000),
            "cancelado": threading.Event(), "visto": 1.0}


def test_libera_sessao_devolve_referencia_e_e_idempotente(tmp_path):
    import app

    live_intencao.WORKER = _stub(tmp_path)
    sess = _sess_890()
    live_intencao.pega()
    sess["intencao_ref"] = True
    with app._live_lock:
        app._live_sessions[sess["id"]] = sess

    app._live_libera_sessao(sess, "teste")
    assert live_intencao.estado()["refs"] == 0, "a referência tem de voltar"
    assert sess["id"] not in app._live_sessions, "a sessão tem de sair do registry"

    app._live_libera_sessao(sess, "teste")      # o `finally` e o sender podem os dois
    assert live_intencao.estado()["refs"] == 0, "devolver duas vezes não pode ir a -1"


def test_finally_do_ws_libera_a_referencia():
    """MORDIDA: sem a chamada no `finally`, o cliente que fecha limpo vaza a ref."""
    import app

    fonte = Path(app.__file__).read_text()
    i = fonte.index("async def live_ws(")
    fim = fonte.find("\nasync def ", i + 1)
    corpo = fonte[i:fim if fim != -1 else len(fonte)]
    assert "_live_libera_sessao(sess, " in corpo, (
        "o `finally` do WS tem de devolver a referência do classificador (#890)")


def test_aquecimento_preso_nao_segura_o_ttl(tmp_path, monkeypatch):
    """`_aquecendo` que não volta não pode deixar o worker residente para sempre."""
    live_intencao.WORKER = _stub(tmp_path)
    monkeypatch.setattr(live_intencao, "TTL_S", 0.1)
    monkeypatch.setattr(live_intencao, "INTERVALO_VIGIA_S", 0.02)
    monkeypatch.setattr(live_intencao, "AQUECIMENTO_MAX_S", 0.05)
    inc = live_intencao.pega()
    assert _espera(lambda: inc.ativo)
    vistos = []
    inc.fecha = lambda: vistos.append(1) or inc.__class__.fecha(inc)
    monkeypatch.setattr(live_intencao, "_aquecendo", True)   # marca presa
    live_intencao.solta()
    assert _espera(lambda: vistos), (
        "com o aquecimento preso, o TTL tem de vencer mesmo assim")
