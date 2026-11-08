"""speaker_alvo: piso absoluto de presença e escala do limiar com FAR (#976).

Tudo sintético (#728): ruído gerado, embeddings inventados, sem modelo na 7890
e sem gravação de pessoa. O cosseno 0,26 é o dono medido em janela de 1,5 s
(#663); 0,15 é o intruso exclusivo que o piso relativo (0,55 × melhor) deixava
passar. Com FAR alvo o limiar cru do gate (0,6) NÃO decide: a nota AS-norm é
que tem de bater 2,434.
"""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app
import separa_voz

SR = 16000
MODELO = app.SPEAKER_MODELO_ATUAL


def _vetor(cosseno: float) -> list[float]:
    """Vetor 2-D com esse cosseno contra o centróide [1, 0]."""
    c = float(np.clip(cosseno, -1.0, 1.0))
    return [c, float(np.sqrt(max(0.0, 1.0 - c * c)))]


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.square(np.asarray(x, dtype=np.float64)))))


def _grava(caminho: Path, amostras) -> None:
    sf.write(str(caminho), np.asarray(amostras, dtype=np.float32), SR, subtype="PCM_16")


def _separa_em(dir_saida, *fluxos):
    dir_s = Path(dir_saida)
    dir_s.mkdir(exist_ok=True)
    saidas = []
    for i, fluxo in enumerate(fluxos):
        caminho = dir_s / f"fluxo{i}.wav"
        _grava(caminho, fluxo)
        saidas.append(caminho)
    return saidas


def _le(wav: Path):
    return sf.read(str(wav), dtype="float32")[0]


def test_so_terceiro_e_vetado_por_inteiro(monkeypatch, tmp_path):
    """Escore 0,15 em tudo: o relativo vira 0,0825 e aprovaria. O piso absoluto veta."""
    intruso = _vetor(0.15)
    assert max(intruso[0], 0.0) < separa_voz.PISO_PRESENCA_CRU

    monkeypatch.setattr(app, "_speaker_embed", lambda p, modelo=None: list(intruso))
    rng = np.random.default_rng(7)
    mix = (rng.standard_normal(2 * SR) * 0.2).astype(np.float32)
    wav = tmp_path / "intruso.wav"
    _grava(wav, mix)
    monkeypatch.setattr(separa_voz, "separa",
                        lambda _w, destino: _separa_em(destino, mix, mix))

    final, info = separa_voz.separa_alvo(wav, [[1.0, 0.0]], MODELO)
    saida = _le(final)

    assert info["dono_presente"] is False
    assert info["descartados"]["trechos"] >= 1
    # atenuação forte (0,03), não silêncio digital e não a fala intacta
    assert _rms(saida) < _rms(mix) * 0.05
    assert _rms(saida) > _rms(mix) * 0.01
    # o relativo continuaria aprovando: 0,15 >= 0,15 × 0,55
    assert info["escores_janelas"]
    assert all(janela[1] >= info["piso"] for janela in info["escores_janelas"])


def test_dono_alternado_com_intruso_preserva_so_o_dono(monkeypatch, tmp_path):
    """Dono a 0,26 (#663, janela de 1,5 s) fica; intruso abaixo do relativo sai."""
    assert 0.26 > separa_voz.PISO_PRESENCA_CRU
    dono_v = _vetor(0.26)
    intr_v = _vetor(0.10)

    def _emb(p, modelo=None):
        amostras = sf.read(str(p), dtype="float32")[0]
        return list(dono_v if float(np.mean(amostras)) > 0 else intr_v)

    monkeypatch.setattr(app, "_speaker_embed", _emb)
    rng = np.random.default_rng(3)
    dono = rng.standard_normal(SR).astype(np.float32) * 0.05 + 0.15
    intr = rng.standard_normal(SR).astype(np.float32) * 0.05 - 0.15
    gap = np.zeros(SR // 2, dtype=np.float32)
    mix = np.concatenate([dono, gap, intr])
    wav = tmp_path / "alternado.wav"
    _grava(wav, mix)
    monkeypatch.setattr(separa_voz, "separa",
                        lambda _w, destino: _separa_em(destino, mix, mix))

    final, info = separa_voz.separa_alvo(wav, [[1.0, 0.0]], MODELO)
    saida = _le(final)
    miolo = slice(SR // 5, SR * 4 // 5)
    fim_intr = SR + SR // 2

    assert info["dono_presente"] is True
    assert _rms(saida[miolo]) > _rms(mix[miolo]) * 0.9
    assert _rms(saida[fim_intr + SR // 5: fim_intr + SR * 4 // 5]) < (
        _rms(mix[fim_intr + SR // 5: fim_intr + SR * 4 // 5]) * 0.05)


def _coorte_do(vetor: list[float]) -> list[list[float]]:
    """Dois impostores iguais no oposto do vetor: AS-norm dispara (sd → 1e-6)."""
    oposto = [-c for c in vetor]
    return [oposto, oposto]


def _far_ligado(monkeypatch, vetor_dono: list[float]) -> None:
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1")
    monkeypatch.setattr(app, "_speaker_cohorte", lambda modelo: _coorte_do(vetor_dono))


def test_far_alvo_separa_na_escala_asnorm_e_nao_no_cru(monkeypatch, tmp_path):
    """ECAPA com FAR alvo "1" decide no cru 0,39 (#982), não no AS-norm 2,434.

    Cosseno 0,40 passa de 0,39 e perde do 0,6. Os dois fluxos não são a mesma
    voz e a correlação com o original é nula — `separou` dispara. Voltar o
    limiar para 2,434, ou ignorar o FAR e usar 0,6, deixa `sobrepostas` em zero.
    """
    dono_v = _vetor(0.40)
    intr_v = _vetor(-1.0)
    assert dono_v[0] < 0.6

    def _emb(p, modelo=None):
        amostras = sf.read(str(p), dtype="float32")[0]
        pico = float(np.max(np.abs(amostras)))
        media = float(np.mean(amostras))
        if pico < 1e-4 or abs(media) < 0.02:
            return list(dono_v)
        return list(intr_v)

    _far_ligado(monkeypatch, dono_v)
    monkeypatch.setattr(app, "_speaker_embed", _emb)
    mix = np.zeros(2 * SR, dtype=np.float32)
    rng = np.random.default_rng(11)
    fluxo_dono = (rng.standard_normal(2 * SR) * 0.2).astype(np.float32)
    fluxo_intr = np.full(2 * SR, 0.05, dtype=np.float32)
    wav = tmp_path / "far.wav"
    _grava(wav, mix)
    monkeypatch.setattr(
        separa_voz, "separa",
        lambda _w, destino: _separa_em(destino, fluxo_dono, fluxo_intr))

    final, info = separa_voz.separa_alvo(wav, [[1.0, 0.0]], MODELO)
    saida = _le(final)

    assert info["limiar"] == pytest.approx(0.39)
    assert info["dono_presente"] is True
    assert info["sobrepostas"] >= 1
    assert _rms(saida) > 0.05


def test_far_alvo_um_falante_nao_separa_voz_unica(monkeypatch, tmp_path):
    """Os dois fluxos casam e estão perto no cru: FAR ligado fica com o original."""
    dono_v = _vetor(0.40)

    def _emb(p, modelo=None):
        return list(dono_v)

    _far_ligado(monkeypatch, dono_v)
    monkeypatch.setattr(app, "_speaker_embed", _emb)
    mix = np.zeros(2 * SR, dtype=np.float32)
    rng = np.random.default_rng(13)
    outro = (rng.standard_normal(2 * SR) * 0.2).astype(np.float32)
    wav = tmp_path / "um.wav"
    _grava(wav, mix)
    monkeypatch.setattr(
        separa_voz, "separa",
        lambda _w, destino: _separa_em(destino, outro, -outro))

    final, info = separa_voz.separa_alvo(wav, [[1.0, 0.0]], MODELO)
    saida = _le(final)

    assert info["limiar"] == pytest.approx(0.39)
    assert info["sobrepostas"] == 0
    assert _rms(saida) < 1e-3
