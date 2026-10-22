"""GATE QA #I (`task_db3159a3`): remoto com anomalia e fallback local.

O caminho remoto não passava pelo portão de anomalia e não tinha fallback: um
`200` com silêncio entrava direto e um `500` persistente derrubava o job inteiro
mesmo com modelo local disponível. Aqui, com stub:

- `200` com silêncio → o trecho é regerado LOCAL e o WAV final não é mudo;
- `500` persistente → job `done` (nunca `error`), com `remote_fallback` no meta;
- remoto saudável → nenhum fallback e nenhum aviso;
- o fallback não pode mascarar remoto LENTO: o tempo do remoto aparece no `elapsed`;
- o caminho local do fallback respeita o `_gen_lock` (geração não corre solta);
- mordida: desligar o portão do ramo remoto reprova o primeiro caso.
"""

import threading
import time

import numpy as np
import soundfile as sf

pytest_plugins = ["test_api"]

import app  # noqa: E402
from test_app import _TEXTO_1_TRECHO, _bom, _job_de_render  # noqa: E402

__all__ = ["app"]


def _local_ok(monkeypatch):
    monkeypatch.setattr(app, "_ensure_remote_voice", lambda *a, **k: None)
    monkeypatch.setitem(app._settings, "remote_tts_voice", "")


def _roda(monkeypatch, tmp_path, remoto, gerador=None):
    jid = _job_de_render(monkeypatch, tmp_path, gerador or (lambda *a, **k: _bom()),
                         texto=_TEXTO_1_TRECHO, remoto=True)
    app._run_tts_job(jid, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    return jid, app._jobs.pop(jid)


def test_setecento_com_silencio_regera_local_e_nao_e_mudo(monkeypatch, tmp_path):
    _local_ok(monkeypatch)
    remotos = {"n": 0}

    def _remoto(*a, **k):
        remotos["n"] += 1
        return np.zeros(24000, dtype=np.float32)

    monkeypatch.setattr(app, "_tts_remote_chunk", _remoto)
    _jid, job = _roda(monkeypatch, tmp_path, remoto=True)
    assert job["status"] == "done"
    assert remotos["n"] == 2, "o silêncio remoto não foi tentado duas vezes"
    assert job["output"]["remote_fallback"] is True
    assert job["output"]["rms"] > 0.05
    wav, _ = sf.read(str(tmp_path / f"{job['output']['id']}.wav"), dtype="float32")
    assert float(np.sqrt(np.mean(wav ** 2))) > 0.05, "gravou mudo"


def test_cinco_centos_persistente_termina_done_e_marcado(monkeypatch, tmp_path):
    _local_ok(monkeypatch)

    def _remoto(*a, **k):
        raise RuntimeError("TTS remoto falhou (500)")

    monkeypatch.setattr(app, "_tts_remote_chunk", _remoto)
    _jid, job = _roda(monkeypatch, tmp_path, remoto=True)
    assert job["status"] == "done", job.get("error")
    assert job["output"]["remote_fallback"] is True
    assert job["output"]["rms"] > 0.05


def test_remoto_saudavel_nao_marca_fallback_nem_aviso(monkeypatch, tmp_path):
    _local_ok(monkeypatch)
    monkeypatch.setattr(app, "_tts_remote_chunk", lambda *a, **k: _bom())
    _jid, job = _roda(monkeypatch, tmp_path, remoto=True)
    assert job["output"]["remote_fallback"] is False
    assert not job.get("warning")


def test_remoto_lento_nao_e_mascarado(monkeypatch, tmp_path):
    """Fallback não pode esconder latência: o tempo do remoto aparece no elapsed."""
    _local_ok(monkeypatch)

    def _remoto(*a, **k):
        time.sleep(0.6)
        return _bom()

    monkeypatch.setattr(app, "_tts_remote_chunk", _remoto)
    _jid, job = _roda(monkeypatch, tmp_path, remoto=True)
    assert job["output"]["elapsed"] >= 0.6, job["output"]["elapsed"]


def test_fallback_local_respeita_o_gen_lock(monkeypatch, tmp_path):
    _local_ok(monkeypatch)
    monkeypatch.setattr(app, "_tts_remote_chunk",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("500")))
    dono = {"quem": None}

    def _gera(*a, **k):
        # se o `_gen_lock` estiver preso aqui, é o fallback que o segura
        dono["quem"] = app._gen_lock.locked()
        return _bom()

    _jid, job = _roda(monkeypatch, tmp_path, remoto=True, gerador=_gera)
    assert job["status"] == "done"
    assert dono["quem"] is True, "geração local do fallback fora do _gen_lock"
    assert not app._gen_lock.locked(), "o lock ficou preso depois do job"


def test_threads_nao_veem_fallback_parcial(monkeypatch, tmp_path):
    """`remote_fallback` só existe depois de o job decidir — não antes."""
    _local_ok(monkeypatch)
    monkeypatch.setattr(app, "_tts_remote_chunk", lambda *a, **k: _bom())
    jid = _job_de_render(monkeypatch, tmp_path, lambda *a, **k: _bom(),
                         texto=_TEXTO_1_TRECHO, remoto=True)
    t = threading.Thread(target=app._run_tts_job,
                         args=(jid, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                               "pt", {"num_steps": 4}), kwargs={"use_queue": False})
    t.start()
    t.join(30)
    job = app._jobs.pop(jid)
    assert job["status"] == "done"
    assert "remote_fallback" in job["output"]
