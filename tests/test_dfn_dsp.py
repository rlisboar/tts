"""O DSP do DFN3 em numpy tem de ser o MESMO do caminho de referência (#523).

O app não depende do pacote `df` (torch + `libdf`): `tools/bakeoff/dfn_dsp.py`
reimplementa STFT/ISTFT, banco ERB, normalização, máscara e filtro profundo. Um
port desses erra calado — sai áudio com cara de áudio. Este teste trava isso
contra um par ENTRADA/REFERÊNCIA versionado (1 s, 48 kHz); a referência foi gerada
pelo caminho oficial (`df.enhance.enhance` com o mesmo grafo), uma vez, e não é
regenerada aqui.

Limiar: SI-SDR ≥ 8 dB contra a referência. O caminho é equivalente, não idêntico
(a normalização ERB é uma recorrência aproximada), e o valor medido na criação do
teste foi 10,3 dB — o piso de 8 dB pega quebra de verdade sem brigar com ruído
numérico.

Sem o modelo ONNX em disco o teste é pulado: ele é baixado na primeira execução
do bake-off, não fica no git.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "tools" / "bakeoff"))

import dfn_dsp  # noqa: E402

DADOS = Path(__file__).parent / "data"
SR = 48000


def _si_sdr(est: np.ndarray, ref: np.ndarray) -> float:
    n = min(est.size, ref.size)
    e = np.asarray(est[:n], np.float64)
    s = np.asarray(ref[:n], np.float64)
    alvo = e @ s / (s @ s) * s
    return float(10 * np.log10((np.sum(alvo ** 2) or 1e-12) / (np.sum((e - alvo) ** 2) or 1e-12)))


def test_stft_istft_e_inversa_exata():
    """Sem isso todo o resto é inútil: o par tem de devolver o sinal."""
    p = dfn_dsp.carrega_params()
    x = np.random.default_rng(0).normal(0, 0.1, SR).astype(np.float64)
    y = dfn_dsp.istft(dfn_dsp.stft(x, p["janela"]), p["janela"])
    assert y.size == x.size
    assert np.abs(y - x).max() / np.abs(x).max() < 1e-6


def test_erb_bate_com_a_definicao():
    p = dfn_dsp.carrega_params()
    mag2 = np.full((1, 481), 2.5)
    esperado = 10 * np.log10(np.maximum(mag2 @ p["erb_fb"], dfn_dsp.MIN_POT))
    assert np.allclose(dfn_dsp.erb_db(mag2, p["erb_fb"]), esperado)


def test_filtro_profundo_aplica_o_coeficiente_central():
    """Coeficiente 1 na posição central = passa o próprio quadro."""
    spec = (np.random.default_rng(1).normal(size=(9, 481))
            + 1j * np.random.default_rng(2).normal(size=(9, 481)))
    coefs = np.zeros((dfn_dsp.ORDEM, 9, dfn_dsp.NB_DF), complex)
    coefs[dfn_dsp.ORDEM - 1 - dfn_dsp.LOOKAHEAD] = 1.0
    saida = dfn_dsp.filtro_profundo(spec.copy(), coefs)
    assert np.allclose(saida[:, : dfn_dsp.NB_DF], spec[:, : dfn_dsp.NB_DF])


def _sem_modelo() -> bool:
    """Respeita `BAKEOFF_DIR` (o default é `/tmp/tts-bakeoff`, a bancada) em vez de
    cravar o caminho — o #243 põe `/tmp/tts-bakeoff` na lista de compartilhados por
    desenho, mas o teste não deve depender do literal."""
    import comum

    return not (comum.BASE / "modelos" / "dfn3-onnx" / "deepfilter.onnx").is_file()


@pytest.mark.skipif(_sem_modelo(), reason="modelo ONNX do DFN3 ausente (baixado pelo bake-off)")
def test_limpeza_bate_com_a_referencia():
    import dfn_onnx

    sujo, sr = sf.read(str(DADOS / "dfn_entrada.wav"), dtype="float32")
    assert sr == SR
    ref, _ = sf.read(str(DADOS / "dfn_referencia.wav"), dtype="float32")
    limpo = dfn_onnx.limpa(sujo)
    assert limpo.size == sujo.size, "o tamanho tem de ser preservado"
    valor = _si_sdr(limpo, ref)
    assert valor >= 8.0, f"SI-SDR {valor:.1f} dB contra a referência — port divergiu"


@pytest.mark.skipif(_sem_modelo(), reason="modelo ONNX do DFN3 ausente (baixado pelo bake-off)")
def test_caso_discrepante_fica_travado():
    """A amostra em que o DSP próprio MAIS se afasta do original em OVRL.

    Ela é a pior das 8 células medidas (OVRL 2,66 contra 3,24 do original) e fica
    versionada para a diferença não crescer sem ninguém notar. Registro honesto: em
    SI-SDR ela é BOA (20,69 dB na criação), então a discrepância é do que o DNSMOS
    percebe, não de fidelidade do sinal — mais um indício de que o resíduo é o grafo
    ONNX contra o modelo torch, não a normalização.

    Piso de 12 dB: abaixo do medido, para não brigar com ruído numérico.
    """
    import dfn_onnx

    sujo, sr = sf.read(str(DADOS / "dfn_discrepante_entrada.wav"), dtype="float32")
    assert sr == SR
    ref, _ = sf.read(str(DADOS / "dfn_discrepante_referencia.wav"), dtype="float32")
    limpo = dfn_onnx.limpa(sujo)
    valor = _si_sdr(limpo, ref)
    assert valor >= 12.0, f"SI-SDR {valor:.1f} dB no caso discrepante (medido 20,7)"
