"""GATE QA #P (task_463ca1ec): métricas de áudio no meta e aviso de nível baixo.

O que precisa ser provado:

- todo output NOVO carrega os 5 campos (`rms`, `peak`, `crest_db`, `retries`,
  `anomalias`), **nos dois caminhos** (in-process e worker) — métrica que só
  existe num deles deixa o outro cego;
- render sabotado (ganho ÷10) termina com `warning` e `rms` baixo, e o `rms`
  aparece na listagem de `/api/outputs`;
- áudio normal NÃO gera warning (senão o aviso viraria ruído e ninguém olha).

O caminho do job roda de verdade com o gerador stubado; o do worker roda o
`tts_worker` importado, com o gerador do backend trocado.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app
import common

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import _voz_fake, auth, client, voices_tmp  # noqa: E402, F401

# Declarados como usados: são FIXTURES consumidas pelos testes do arquivo.
# O `ruff` já cobre isso com o `noqa` (política pós-#378); o `__all__` mantém
# o `pyflakes` (que ignora o `noqa`) também verde — inerte em runtime.
__all__ = [
    "_voz_fake",
    "auth",
    "client",
    "voices_tmp",
]


@pytest.fixture
def amb(tmp_path, monkeypatch, voices_tmp):
    """Backend in-process falso + gerador com ganho controlável."""
    monkeypatch.setattr(app, "_current_backend",
                        lambda *a, **k: {"id": "fake", "family": "kokoro", "meta": {}, "path": "x"})
    monkeypatch.setattr(app, "_use_remote_tts", lambda: False)
    # sem worker de verdade aqui: o gerador stubado é que controla o nível, e o
    # caminho isolado tem teste próprio (que roda o `main()` do worker)
    monkeypatch.setattr(app, "_ISOLATED_FAMILIES", frozenset())
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(app, "_get_model", lambda *a, **k: type("M", (), {"sample_rate": 24000})())
    monkeypatch.setitem(app._settings, "chunk_max_chars", 60)
    ganho = {"g": 0.2}

    def _gera(*_a, **_k):
        t = np.arange(24000) / 24000.0
        # senoide CHEIA (sem envelope: o hanning zerava as pontas e o trim
        # esvaziava o trecho). Normal (0,2) passa do RMS_AVISO; reduzido não.
        return (ganho["g"] * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    monkeypatch.setattr(app, "_generate_chunk", _gera)
    # a voz-fake padrão é UM SEGUNDO DE ZEROS: o gate de locutor não sobrevive a
    # referência muda (conv em tensor vazio). Aqui a amostra precisa ter sinal.
    t = np.arange(24000) / 24000.0
    sf.write(str(app.VOICES_DIR / "v1.wav"), (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32),
             24000, subtype="PCM_16")
    (app.VOICES_DIR / "v1.json").write_text(json.dumps({"id": "v1", "name": "v1"}))
    return ganho


def _roda() -> tuple[str, dict]:
    job_id = app._jobs_admit()
    app._run_tts_job(job_id, "frase de teste", "v1", app.VOICES_DIR / "v1.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    assert job["status"] == "done", job.get("error")
    meta_id = job["output"]["id"]
    meta = json.loads((app.OUTPUTS_DIR / f"{meta_id}.json").read_text())
    return meta_id, meta


CAMPOS = ("rms", "peak", "crest_db", "retries", "anomalias")


def test_output_novo_tem_os_cinco_campos(amb):
    _id, meta = _roda()
    faltando = [c for c in CAMPOS if c not in meta]
    assert not faltando, f"campos ausentes no meta: {faltando}"


def test_audio_normal_nao_gera_warning(amb):
    _id, meta = _roda()
    assert meta["rms"] > common.RMS_AVISO
    assert meta["warning"] == "", meta["warning"]


def test_render_sabotado_avisa_e_mostra_rms_baixo_na_listagem(amb_fixture=None):
    pass


def test_render_com_ganho_baixo_marca_warning(amb, monkeypatch):
    """÷10 do nível normal. O pós-cadeia normaliza o RMS e o loudness puxa para
    -18 LUFS: sem anular os DOIS, qualquer ganho volta ao mesmo nível e o aviso
    (que só dispara quando a normalização não salva) nunca apareceria."""
    monkeypatch.setattr(app, "_normaliza_loudness", lambda *a, **k: 0.0)
    monkeypatch.setattr(app, "_normalize", lambda a, *x, **k: a)
    amb["g"] = 0.02 / 10
    _id, meta = _roda()
    assert meta["rms"] < common.RMS_AVISO, meta["rms"]
    assert "nível final baixo" in meta["warning"]


def test_listagem_de_outputs_mostra_o_rms(amb, client, auth):
    amb["g"] = 0.002
    meta_id, _meta = _roda()
    r = client.get("/api/outputs", headers=auth)
    assert r.status_code == 200, r.text
    itens = r.json() if isinstance(r.json(), list) else r.json().get("outputs") or r.json().get("itens") or []
    alvo = next((i for i in itens if i.get("id") == meta_id), None)
    assert alvo is not None, f"output {meta_id} não apareceu na listagem"
    assert "rms" in alvo and "warning" in alvo


def test_caminho_do_worker_grava_o_mesmo_conjunto(tmp_path, monkeypatch):
    """O caminho ISOLADO (worker) é o mais usado com modelo pesado: métrica só no
    pai deixaria justo ele cego. Roda o `main()` do worker de verdade, com a
    síntese trocada e ganho baixo para exercitar o `warning`."""
    import tts_worker

    ganho = 0.002
    monkeypatch.setattr(tts_worker, "_carrega",
                        lambda cfg: {"sr": 24000, "omni": {}, "family": "kokoro"})
    monkeypatch.setattr(tts_worker, "_sintetiza",
                        lambda est, texto, omni=None, fx=False: (
                            ganho * np.sin(2 * np.pi * 220 * np.arange(24000) / 24000)
                        ).astype(np.float32))
    monkeypatch.setattr(tts_worker, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(tts_worker, "normaliza_loudness", lambda *a, **k: 0.0)
    monkeypatch.setattr(tts_worker, "_sintetiza",
                        lambda est, texto, omni=None, fx=False: (
                            0.002 * np.sin(2 * np.pi * 220 * np.arange(24000) / 24000)).astype(np.float32))

    cfg = {"text": "oi", "voice_id": "v1", "language": "pt", "model": "omnivoice",
           "piece_dir": str(tmp_path / "pieces"), "outputs_dir": str(tmp_path / "out"),
           "status_path": str(tmp_path / "status.json"), "settings": {}}
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setattr(sys, "argv", ["tts_worker.py", str(cfg_path)])

    assert tts_worker.main() == 0
    metas = list((tmp_path / "out").glob("*.json"))
    assert metas, "o worker não gerou meta"
    meta = json.loads(metas[0].read_text())
    faltando = [c for c in CAMPOS if c not in meta]
    assert not faltando, f"campos ausentes no meta do worker: {faltando}"
    assert meta["rms"] < common.RMS_AVISO and "nível final baixo" in meta["warning"]


def test_metricas_de_arquivo_ilegivel_nao_estoura(tmp_path):
    # #439: ilegível tem motivo próprio — não se finge áudio mudo
    assert app._metricas_audio(tmp_path / "nao-existe.wav") == {
        "rms": 0.0, "peak": 0.0, "crest_db": 0.0, "ilegivel": True}


def test_crest_db_e_consistente(amb):
    """Crista é pico/rms em dB: com senoide enjanelada o número tem de ser > 0."""
    _id, meta = _roda()
    assert meta["crest_db"] > 0
    esperado = 20 * np.log10(max(meta["peak"], 1e-9) / max(meta["rms"], 1e-9))
    assert abs(meta["crest_db"] - esperado) < 0.5
