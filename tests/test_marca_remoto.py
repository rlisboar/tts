"""Marca d'água no caminho REMOTO (#812).

O servidor CUDA (removido do repo no #1058) não marca — o contrato é o CLIENTE marcar
o que volta, no mesmo ponto em que o caminho isolado marca. Estas células seguram
esse contrato, inclusive o caso `remote_fallback` (#114), em que o trecho cai no
local e não pode ser marcado duas vezes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import app  # noqa: E402
import marca_dagua  # noqa: E402


def _tom(n: int = 24000) -> np.ndarray:
    t = np.arange(n) / 24000
    x = np.sin(2 * np.pi * 210 * t) + 0.4 * np.sin(2 * np.pi * 610 * t)
    return (x / np.abs(x).max() * 0.3).astype(np.float32)


def _roda(monkeypatch, tmp_path, *, remoto_ok: bool, marca: bool = True):
    """Roda o job pelo caminho remoto; devolve as peças gravadas e as marcações."""
    marcadas: list[Path] = []
    original = marca_dagua.marca_wav

    def _espia(caminho):
        marcadas.append(Path(caminho))
        return original(caminho)

    monkeypatch.setattr(marca_dagua, "marca_wav", _espia)
    monkeypatch.setitem(app._settings, "tts_marca_dagua", marca)
    monkeypatch.setattr(app, "_use_remote_tts", lambda: True)
    monkeypatch.setattr(app, "_schedule_job_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(app, "_jobs_hist_salva", lambda *a, **k: None)
    monkeypatch.setattr(app, "_unload_local_tts", lambda *a, **k: None)
    monkeypatch.setattr(app, "_piece_dir", lambda job_id: tmp_path / job_id)

    if remoto_ok:
        monkeypatch.setattr(app, "_tts_remote_chunk", lambda *a, **k: _tom())
    else:
        def _morre(*a, **k):
            raise RuntimeError("servidor fora")

        monkeypatch.setattr(app, "_tts_remote_chunk", _morre)

        class _Modelo:
            sample_rate = 24000

        estado = app._model_state
        estado["model"] = _Modelo()
        monkeypatch.setattr(app, "_get_model", lambda *a, **k: _Modelo())
        monkeypatch.setattr(app, "_generate_chunk", lambda *a, **k: _tom())

    app._jobs["jr"] = {"status": "queued"}
    app._run_tts_job("jr", "uma frase curta", "voz", tmp_path / "voz.wav", "pt", {})
    return sorted((tmp_path / "jr").glob("*.wav")), marcadas


def test_retorno_remoto_sai_marcado(monkeypatch, tmp_path):
    """O servidor não marca; se o cliente não marcar, o anti-clone fura por aqui."""
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    pecas, marcadas = _roda(monkeypatch, tmp_path, remoto_ok=True)

    assert pecas, "o job remoto não gravou peça"
    assert marcadas, "o trecho remoto passou sem marca"
    for peca in pecas:
        prob = marca_dagua.detecta_prob(peca)
        assert prob is not None and prob > 0.9, f"peça remota sem marca ({prob})"


def test_fallback_para_local_marca_uma_vez_so(monkeypatch, tmp_path):
    """`remote_fallback` cai no local: uma marca por peça, não duas (#114).

    A contagem de `marca_wav` é o que salva esta célula de passar por acidente:
    marca dupla continua dando detecção 1,0, então só olhar a probabilidade
    aprovava o defeito. O que se conta é a CHAMADA, por peça."""
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    pecas, marcadas = _roda(monkeypatch, tmp_path, remoto_ok=False)

    assert app._jobs["jr"].get("remote_fallback") is True, "não caiu no fallback"
    assert len(marcadas) == len(pecas), f"marcou {len(marcadas)} para {len(pecas)} peças"


def test_remoto_com_a_marca_desligada_nao_marca(monkeypatch, tmp_path):
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    pecas, marcadas = _roda(monkeypatch, tmp_path, remoto_ok=True, marca=False)

    assert pecas and not marcadas
    for peca in pecas:
        prob = marca_dagua.detecta_prob(peca)
        assert prob is not None and prob < 0.5
