"""Roteador de mic do client — soft-limit e rampa do gate (#329)."""

import numpy as np

from client.mic_router import Router


def _bloco(n: int = 256, valor: float = 1.0) -> tuple:
    entrada = np.full((n, 1), valor, dtype=np.float32)
    saida = np.zeros((n, 2), dtype=np.float32)
    return entrada, saida


def test_ganho_alto_doce_em_vez_de_clipar():
    """`np.clip` em ±1 distorcia com ganho > 1: agora o joelho em 0,7 dobra a ponta."""
    r = Router()
    r.gain = 2.0
    entrada, saida = _bloco(valor=0.8)          # 0,8 × 2 = 1,6: o clip daria 1,0
    r._callback(entrada, saida, 256, None, None)
    pico = float(np.abs(saida[:, 0]).max())
    assert pico < 1.0, f"clipou no teto: {pico}"
    assert pico > 0.95, f"limitou demais: {pico}"
    # nada de degrau: amostras constantes geram saída constante
    assert len(set(np.round(saida[10:20, 0], 6))) == 1


def test_gate_do_passthrough_rampeia():
    """Trocar passthrough True/False em degrau estalava; agora fecha em rampa."""
    r = Router()
    níveis = []
    for _ in range(6):
        entrada, saida = _bloco(valor=0.5)
        r._callback(entrada, saida, 256, None, None)
        níveis.append(float(saida[:, 0].max()))
    # com o gate aberto, nível cheio
    assert níveis[-1] > 0.4
    # o app começa a falar: o gate fecha gradualmente
    r.passthrough = False
    baixando = []
    for _ in range(20):
        entrada, saida = _bloco(valor=0.5)
        r._callback(entrada, saida, 256, None, None)
        baixando.append(float(saida[:, 0].max()))
        if baixando[-1] < 0.01:
            break
    assert baixando[0] > 0.0, "fechou em degrau (primeiro bloco já mudo)"
    assert baixando[-1] < 0.01, baixando
    assert len(baixando) >= 3, f"fechou rápido demais: {baixando}"
    assert all(a >= b - 1e-9 for a, b in zip(baixando, baixando[1:])), baixando
