"""Métricas de verificação de locutor — EER, minDCF e FRR em FAR alvo.

Implementação própria e auditável (a literatura é explícita): os escores de
mesma-classe e de classes diferentes são ordenados juntos, e as curvas saem da
varredura do limiar. `minDCF` normaliza por uma razão de custo e priori dadas —
é o número que não depende de escolher o limiar no olho.
"""

from __future__ import annotations

import numpy as np

#: DET curve para desenhar a evidência (FAR, FRR)
GRADE = np.linspace(0.001, 0.999, 400)


def _far_frr(escores_pos: np.ndarray, escores_neg: np.ndarray, limiares: np.ndarray):
    far = np.array([(escores_neg >= t).mean() for t in limiares])
    frr = np.array([(escores_pos < t).mean() for t in limiares])
    return far, frr


def eer(escores_pos, escores_neg) -> float:
    """Taxa de erro igualada, em %."""
    pos, neg = np.asarray(escores_pos, float), np.asarray(escores_neg, float)
    if not pos.size or not neg.size:
        return float("nan")
    limiares = np.unique(np.concatenate([pos, neg]))
    far, frr = _far_frr(pos, neg, limiares)
    i = int(np.argmin(np.abs(far - frr)))
    return float(100 * (far[i] + frr[i]) / 2)


def frr_em_far(escores_pos, escores_neg, far_alvo: float) -> float:
    """FRR quando o limiar é calibrado para a FAR alvo (0.01, 0.001...)."""
    pos, neg = np.asarray(escores_pos, float), np.asarray(escores_neg, float)
    if not pos.size or not neg.size:
        return float("nan")
    limiar = float(np.quantile(neg, 1.0 - far_alvo))
    return float(100 * (pos < limiar).mean())


def limiar_para_far(escores_neg, far_alvo: float) -> float:
    neg = np.asarray(escores_neg, float)
    if not neg.size:
        return float("nan")
    return float(np.quantile(neg, 1.0 - far_alvo))


def min_dcf(escores_pos, escores_neg, p_alvo: float = 0.01, custo_fa: float = 1.0,
            custo_fr: float = 1.0) -> float:
    """DCF mínimo normalizado: `p*CFR*FRR + (1-p)*CFA*FAR` sobre a razão de custo."""
    pos, neg = np.asarray(escores_pos, float), np.asarray(neg := escores_neg, float)
    if not pos.size or not neg.size:
        return float("nan")
    limiares = np.unique(np.concatenate([pos, neg]))
    far, frr = _far_frr(pos, neg, limiares)
    dcf = p_alvo * custo_fr * frr + (1 - p_alvo) * custo_fa * far
    return float(dcf.min() / min(p_alvo * custo_fr, (1 - p_alvo) * custo_fa))


def curva(escores_pos, escores_neg):
    limiares = np.quantile(np.asarray(escores_neg, float), 1 - GRADE) if len(escores_neg) else []
    far, frr = _far_frr(np.asarray(escores_pos, float), np.asarray(escores_neg, float),
                        np.asarray(limiares, float))
    return far.tolist(), frr.tolist()


def resumo(escores_pos, escores_neg) -> dict:
    """Bloco padrão de números para a evidência."""
    far, frr = curva(escores_pos, escores_neg)
    return {"eer": round(eer(escores_pos, escores_neg), 3),
            "minDCF_p001": round(min_dcf(escores_pos, escores_neg), 4),
            "frr_far1": round(frr_em_far(escores_pos, escores_neg, 0.01), 3),
            "frr_far01": round(frr_em_far(escores_pos, escores_neg, 0.001), 3),
            "n_pos": int(len(escores_pos)), "n_neg": int(len(escores_neg)),
            "det": {"far": far, "frr": frr}}
