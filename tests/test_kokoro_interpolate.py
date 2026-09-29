"""Patch do `interpolate` do Kokoro (#152) — rápido, SEM modelo.

O `mlx_audio` calcula `size = ceil(n * scale)` e o float erra para cima em
comprimentos múltiplos de 300 (ex.: `34200 * (1/300)` = 114.00000000000001 → 115).
O `_f02sine` comprime e reexpande, então o comprimento final vira 34500 enquanto o
`uv` continua 34200 e o kokoro estoura com
`Shapes (1,34200,1) and (1,34500,9) cannot be broadcast` — em ~1/4 dos textos
curtos, justamente os primeiros chunks do Live.

Aqui fixa-se o CONSERTO (fórmula exata, idempotência, alcance do patch e o fato de
não estragar se a lib corrigir antes); o áudio real é o
`test_worker.py::test_worker_persistente_serve_smoke` (TTS_TEST_WORKER=1).
"""

from __future__ import annotations

import math

import pytest

import backends

mx = pytest.importorskip("mlx.core")

AFETADOS = (17400, 33600, 34200, 37800)      # medidos na investigação
NAO_AFETADOS = (33000, 31800, 27000, 29400, 42000)


def _tamanho_do_autor(entrada: int, escala: float) -> int:
    """A fórmula do terceiro, como está no venv (com o erro de float)."""
    return max(1, int(math.ceil(float(entrada) * float(escala))))


def _tamanho_do_patch(entrada: int, escala: float) -> int:
    return backends._interpolate_kokoro_seguro(
        mx.zeros((1, 9, entrada)), scale_factor=escala).shape[-1]


def test_a_escala_do_problema_esta_reproduzida_aqui():
    """Contraprova: a fórmula do terceiro erra PARA CIMA nos comprimentos afetados.

    `_f02sine` comprime com `1/300` e reexpande com `300`: com o ceil errado o
    comprimento final não volta ao original (o `uv` volta) e o kokoro estoura."""
    for n in AFETADOS:
        assert _tamanho_do_autor(n, 1 / 300) == n // 300 + 1, f"{n} não reproduz o bug"
    for n in NAO_AFETADOS:
        assert _tamanho_do_autor(n, 1 / 300) == n // 300


@pytest.mark.parametrize("n", AFETADOS + NAO_AFETADOS)
def test_patch_comprime_e_reexpande_de_volta_ao_mesmo_tamanho(n):
    """O invariante que o `uv` cobra: comprimir e expandir volta ao original."""
    comprimido = _tamanho_do_patch(n, 1 / 300)
    assert comprimido == n // 300, "o patch tem de dar o valor EXATO"
    assert _tamanho_do_patch(comprimido, 300) == n, "ida-e-volta não fechou"


@pytest.mark.parametrize("n,escala", [(1000, 1 / 300), (999, 2.0), (7, 1 / 3),
                                      (1, 1 / 300), (51950, 1 / 300)])
def test_patch_nao_muda_o_arredondamento_normal(n, escala):
    """Fora do erro de float, o `ceil` do patch é o mesmo do terceiro."""
    assert _tamanho_do_patch(n, escala) == _tamanho_do_autor(n, escala)


def test_patch_e_idempotente_e_nao_vaza_para_outras_familias():
    from mlx_audio.tts.models import interpolate as base
    from mlx_audio.tts.models.kokoro import istftnet

    original = base.interpolate            # o módulo compartilhado do mlx_audio
    assert backends._patch_kokoro_interpolate() is True
    primeira = istftnet.interpolate
    assert primeira is backends._interpolate_kokoro_seguro
    assert getattr(istftnet, "_rod_interp_seguro", False) is True

    assert backends._patch_kokoro_interpolate() is True     # 2ª vez: não reempilha
    assert istftnet.interpolate is primeira
    assert base.interpolate is original, "o patch não pode vazar p/ outras famílias"


def test_nao_estraga_se_a_lib_ja_vier_corrigida():
    """Com a lib corrigida o `size` é o mesmo — o patch só vira um atalho inerte."""
    for n in AFETADOS + NAO_AFETADOS:
        corrigido = int(round(n * (1 / 300), 9))          # o que a lib corrigida daria
        assert _tamanho_do_patch(n, 1 / 300) == corrigido == n // 300


def test_tamanho_explicito_tambem_e_aceito():
    """A lib usa `interpolate` com `size` em outros pontos — não pode quebrar."""
    out = backends._interpolate_kokoro_seguro(mx.zeros((1, 3, 10)), size=25)
    assert out.shape == (1, 3, 25)
    out2 = backends._interpolate_kokoro_seguro(mx.zeros((1, 3, 10)), size=(25,))
    assert out2.shape == (1, 3, 25)