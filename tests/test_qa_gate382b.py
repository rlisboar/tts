"""GATE QA #H (`task_3510ca2d`): não-finito não pode passar como áudio válido.

Regressão medida no #284: `nan < 0.01` é False, então NaN/Inf atravessava o
portão de anomalia, o `normalize` propagava, o PCM16 gravava ZEROS e o job
terminava `done` com arquivo mudo e sem retry.

Este gate prova o portão nas duas pontas (julgamento e DSP), o render de ponta a
ponta e — o que separa medir de acreditar — que áudio BOM não dispara retry.
Mordida: arrancar a checagem de finitude reprova.
"""

import numpy as np
import soundfile as sf

pytest_plugins = ["test_api"]

import app  # noqa: E402
from common import motivo_anomalia, normalize  # noqa: E402
from test_app import _TEXTO_1_TRECHO, _bom, _job_de_render  # noqa: E402

__all__ = ["app"]


def test_anomalo_reprova_nan_e_inf():
    nan = np.full(24000, np.nan, dtype=np.float32)
    inf = np.full(24000, np.inf, dtype=np.float32)
    assert app._anomalo(nan, 24000, "x" * 100) is True
    assert app._anomalo(inf, 24000, "x" * 100) is True
    assert "finito" in app._motivo_anomalia(nan, 24000, "x" * 100)


def test_anomalo_com_um_nan_no_meio_tambem_reprova():
    x = _bom()
    x[1000] = np.nan
    assert app._anomalo(x, 24000, _TEXTO_1_TRECHO) is True


def test_normalize_de_vetor_com_nan_devolve_finito():
    x = _bom()
    x[1000] = np.nan
    x[2000] = np.inf
    y = normalize(x)
    assert np.isfinite(y).all()
    assert float(np.abs(y).max()) <= 1.0


def test_audio_bom_nao_dispara_retry_falso():
    """Contraprova: o portão não pode ser sensível demais."""
    assert app._anomalo(_bom(), 24000, _TEXTO_1_TRECHO) is False
    assert motivo_anomalia(_bom(), 24000, _TEXTO_1_TRECHO) == ""


def test_render_com_nan_nao_termina_em_arquivo_mudo(monkeypatch, tmp_path):
    chamadas = []

    def _gerador(model, texto, *a, **k):
        chamadas.append(texto)
        if len(chamadas) == 1:
            return np.full(24000, np.nan, dtype=np.float32)
        return _bom()

    jid = _job_de_render(monkeypatch, tmp_path, _gerador, texto=_TEXTO_1_TRECHO)
    app._run_tts_job(jid, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs.pop(jid)
    try:
        assert job["status"] == "done"
        assert len(chamadas) == 2 and job.get("retries") == 1
        meta = job["output"]
        assert meta["anomalias"] == 1 and "finito" in meta["anomalias_motivos"][0]
        assert meta["rms"] > 0.05
        wav, _ = sf.read(str(tmp_path / f"{meta['id']}.wav"), dtype="float32")
        assert np.isfinite(wav).all() and float(np.sqrt(np.mean(wav ** 2))) > 0.05
    finally:
        pass
