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
            "eer_ic95": eer_ci(escores_pos, escores_neg),
            "minDCF_p001": round(min_dcf(escores_pos, escores_neg), 4),
            "frr_far1": round(frr_em_far(escores_pos, escores_neg, 0.01), 3),
            "frr_far01": round(frr_em_far(escores_pos, escores_neg, 0.001), 3),
            "n_pos": int(len(escores_pos)), "n_neg": int(len(escores_neg)),
            "det": {"far": far, "frr": frr}}


def _boot(escores_pos, escores_neg, fn, n: int = 400, semente: int = 7) -> tuple[float, float]:
    """Intervalo de confiança 95 % por bootstrap do ENSAIO (reamostra as duas
    listas). Com 20 locutores o EER tem cauda larga e o número cru enganava —
    `task_a870518d`."""
    rng = np.random.default_rng(semente)
    pos, neg = np.asarray(escores_pos, float), np.asarray(escores_neg, float)
    vals = []
    for _ in range(n):
        p = pos[rng.integers(0, pos.size, pos.size)]
        q = neg[rng.integers(0, neg.size, neg.size)]
        v = fn(p, q)
        if v == v:
            vals.append(v)
    if not vals:
        return (float("nan"), float("nan"))
    return (round(float(np.percentile(vals, 2.5)), 3), round(float(np.percentile(vals, 97.5)), 3))


def eer_ci(escores_pos, escores_neg) -> list[float]:
    return list(_boot(escores_pos, escores_neg, eer))


def frr_ci(escores_pos, escores_neg, far_alvo: float) -> list[float]:
    return list(_boot(escores_pos, escores_neg, lambda p, q: frr_em_far(p, q, far_alvo)))


# ---------------------------------------------------------------- separação (#532)

def si_sdr(est, ref) -> float:
    """SI-SDR em dB: projeta a estimativa na referência e mede o que sobra fora da
    projeção. Escala invariante, então ganho arbitrário do separador não conta."""
    e = np.asarray(est, np.float64)
    s = np.asarray(ref, np.float64)
    n = min(e.size, s.size)
    e, s = e[:n], s[:n]
    if not n or not np.any(s):
        return float("nan")
    alvo = e @ s / (s @ s) * s
    ruido = e - alvo
    num, den = np.sum(alvo ** 2), np.sum(ruido ** 2)
    return float(10 * np.log10((num or 1e-12) / (den or 1e-12)))


def si_sdri(est, ref, mistura) -> float:
    """Melhoria sobre a mistura, em dB. É o número que o card pede: > 0 significa
    que o separador aproximou do alvo mais do que a própria mistura estava."""
    return si_sdr(est, ref) - si_sdr(mistura, ref)


def alinhamento(hip: list[str], ref: list[str]) -> dict:
    """Edição mínima com contagem separada de substituição/inserção/deleção.

    WER sozinho esconde o que interessa aqui: palavra de TERCEIRO aparece como
    substituição ou como inserção, e as duas se cancelam contra uma deleção do
    alvo. A taxa de inserção isola o vazamento."""
    m, n = len(hip), len(ref)
    d = [[0] * (n + 1) for _ in range(m + 1)]
    op = [[""] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        d[i][0], op[i][0] = i, "ins"   # palavra da hipótese sem par na referência
    for j in range(1, n + 1):
        d[0][j], op[0][j] = j, "del"   # palavra da referência que sumiu
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            custos = {"c": d[i - 1][j - 1] + (hip[i - 1] != ref[j - 1]),
                      "ins": d[i - 1][j] + 1, "del": d[i][j - 1] + 1}
            melhor = min(custos, key=lambda k: custos[k])
            d[i][j], op[i][j] = custos[melhor], melhor
    sub = ins = dele = 0
    i, j = m, n
    while i or j:
        o = op[i][j]
        if o == "c":
            sub += hip[i - 1] != ref[j - 1]
            i, j = i - 1, j - 1
        elif o == "ins":
            ins += 1
            i -= 1
        else:
            dele += 1
            j -= 1
    n_ref = max(n, 1)
    return {"wer": (sub + ins + dele) / n_ref, "sub": sub, "ins": ins, "del": dele,
            "n_ref": n}


def vazamento(hip: list[str], ref: list[str]) -> float:
    """Fração das palavras da referência que foram INVADIDAS por palavra alheia.

    Diferente da taxa de inserção pura: normaliza pelo tamanho do enunciado para
    que 1 palavra de terceiro num enunciado de 100 palavras não pese igual a 1 em
    10. É a métrica de vazamento do card."""
    a = alinhamento(hip, ref)
    return (a["ins"] + 0.5 * a["sub"]) / max(a["n_ref"], 1)
