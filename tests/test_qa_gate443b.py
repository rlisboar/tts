"""GATE QA #F (`task_290e2c8d`): regra de eco não pode engolir fala humana.

O custo dos dois erros é assimétrico: falso-descarte mata a fala do humano em
silêncio (sem LLM, sem TTS, sem retry); falso-eco faz o modelo responder à
própria voz. O aceite exige **zero** falso-descarte e eco puro ≥92 % (o piso do
instrumento hoje é 95 %).

A matriz vive versionada em `smoke_eco_regra.py` — é o instrumento do gate.
"""

import importlib.util
from pathlib import Path

import live_pipeline as lp

RAIZ = Path(__file__).resolve().parent.parent


def _smoke():
    spec = importlib.util.spec_from_file_location("smoke_eco_regra", RAIZ / "smoke_eco_regra.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SMOKE = _smoke()


def _mede():
    falsos = [t for ref, t in SMOKE.HUMANO if SMOKE.classifica(ref, t)]
    perdidos = [(ref, t) for ref, t in SMOKE.ECO if not SMOKE.classifica(ref, t)]
    return falsos, len(SMOKE.ECO) - len(perdidos), len(SMOKE.ECO)


def test_corpus_tem_tamanho_de_gate():
    assert len(SMOKE.HUMANO) >= 20, len(SMOKE.HUMANO)
    assert len(SMOKE.ECO) >= 31, len(SMOKE.ECO)


def test_zero_falso_descarte_nas_falas_humanas():
    falsos, _acertos, _total = _mede()
    assert falsos == [], f"falas humanas descartadas: {falsos}"


def test_os_tres_casos_medidos_na_auditoria_passam():
    """Os três que a regra antiga engolia, citados na auditoria."""
    casos = [
        ("longa", "e a tarde?"),
        ("longa", "Não, o prazo é outro"),
        ("longa", "tá bonito hoje e a gente pode"),
    ]
    for ref, texto in casos:
        assert SMOKE.classifica(ref, texto) is False, texto


def test_eco_puro_continua_descartado():
    _falsos, acertos, total = _mede()
    taxa = acertos / total
    assert taxa >= 0.92, f"eco descartado caiu para {taxa:.1%}"
    assert taxa >= 0.95, f"abaixo do piso atual do instrumento: {taxa:.1%}"


def test_outras_decisoes_de_turno_seguem_intactas():
    """Silêncio, prefixo e barge não podem mudar por tabela com a regra de eco."""
    import live_turns as lt

    cfg = lt.Config(prefix_ms=100, silence_ms=600)
    assert cfg.silence_ms == 600 and cfg.prefix_ms == 100
    eng = lt.TurnEngine(config=cfg)
    est = eng.estatisticas()
    for chave in ("turnos", "turnos_curtos", "barge_in", "barge_in_falso", "cancelados"):
        assert chave in est, chave
    assert est["turnos"] == 0
    # regra de eco e motor são módulos distintos: a assinatura não pode voltar a
    # decidir por posição crua sem o vocabulário
    import inspect

    fonte = inspect.getsource(lp._parece_eco)
    assert "minimo" in inspect.signature(lp._parece_eco).parameters
    assert "ref" in fonte and "transcript" in fonte
