"""Métricas do bake-off (#521): EER, minDCF, FRR por FAR e intervalo por bootstrap.

Puro numpy: roda em milissegundos, serve para a janela quieta. O que se cobra é o
comportamento da CONTA, não o resultado do ensaio.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "bakeoff"))
import metricas  # noqa: E402


def _par(sep: float, n=400, semente=1):
    r = np.random.default_rng(semente)
    return r.normal(sep + 0.5, 0.08, n), r.normal(0.5, 0.12, n * 8)


def test_eer_cai_quando_as_classes_separam():
    assert metricas.eer(*_par(0.5)) < metricas.eer(*_par(0.1))


def test_eer_igual_em_sobreposicao_total():
    r = np.random.default_rng(3)
    a, b = r.normal(0.5, 0.1, 500), r.normal(0.5, 0.1, 500)
    assert 35 <= metricas.eer(a, b) <= 65


def test_frr_em_far_alvo_sobe_quando_o_far_aperta():
    pos, neg = _par(0.35)
    assert metricas.frr_em_far(pos, neg, 0.001) > metricas.frr_em_far(pos, neg, 0.01)


def test_limiar_para_far_respeita_a_taxa():
    _, neg = _par(0.4)
    lim = metricas.limiar_para_far(neg, 0.01)
    assert 0.005 <= float((neg >= lim).mean()) <= 0.02


def test_min_dcf_penaliza_o_erro():
    pos, neg = _par(0.5)
    assert metricas.min_dcf(pos, neg) < metricas.min_dcf(*_par(0.1))


def test_intervalo_de_confianca_contem_o_ponto_e_aperta_com_n():
    pos, neg = _par(0.4, n=60)
    lo, hi = metricas.eer_ci(pos, neg)
    assert lo <= metricas.eer(pos, neg) <= hi
    largo = hi - lo
    pos2, neg2 = _par(0.4, n=600)
    lo2, hi2 = metricas.eer_ci(pos2, neg2)
    assert (hi2 - lo2) < largo


def test_resumo_traz_os_campos_do_aceite():
    pos, neg = _par(0.5)
    r = metricas.resumo(pos, neg)
    for campo in ("eer", "minDCF_p001", "frr_far1", "frr_far01", "eer_ic95", "det"):
        assert campo in r
