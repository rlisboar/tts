"""Gate/revisão do #439: o aviso de nível precisa pegar REGRESSÃO de fonte.

Com a cadeia normal ligada, o RMS final volta ao alvo e o loudness fecha em
−18 LUFS — então `rms` baixo só pega NaN/zeros. Quem denuncia fonte fraca é o
GANHO que a normalização aplicou. E arquivo ilegível não é "mudo".
"""

import json

import app
import common
import tts_worker
from test_app import _TEXTO_1_TRECHO, _bom, _job_de_render

__all__ = ["app", "common", "tts_worker"]


def _roda(monkeypatch, tmp_path, gerador):
    jid = _job_de_render(monkeypatch, tmp_path, gerador, texto=_TEXTO_1_TRECHO)
    app._run_tts_job(jid, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    return app._jobs.pop(jid)


def test_fonte_dez_vezes_mais_baixa_avisa_pelo_GANHO(monkeypatch, tmp_path):
    """Cadeia normal ligada (sem anular normalize nem loudness)."""
    job = _roda(monkeypatch, tmp_path, lambda *a, **k: _bom() * 0.1)
    meta = job["output"]
    assert meta["ganho_normalizacao_db"] >= common.GANHO_AVISO_DB, meta["ganho_normalizacao_db"]
    assert "fonte muito baixa" in meta["warning"]
    assert "fonte muito baixa" in (job.get("warning") or "")
    # e o rms final NÃO denuncia sozinho: ele volta ao alvo
    assert meta["rms"] > common.RMS_AVISO


def test_audio_normal_nao_avisa(monkeypatch, tmp_path):
    job = _roda(monkeypatch, tmp_path, lambda *a, **k: _bom())
    meta = job["output"]
    assert meta["ganho_normalizacao_db"] < common.GANHO_AVISO_DB
    assert meta["warning"] == "" and meta["ilegivel"] is False


def test_arquivo_ilegivel_nao_e_chamado_de_nivel_baixo(monkeypatch, tmp_path):
    destino = tmp_path / "quebrado.wav"
    destino.write_bytes(b"nao sou wav")
    medido = app._metricas_audio(destino)
    assert medido["ilegivel"] is True and medido["rms"] == 0.0


def test_worker_tem_os_mesmos_campos(tmp_path, monkeypatch):
    chamadas = []

    def _sintetiza(est, texto, omni=None, fx=False):
        chamadas.append(texto)
        return _bom(4.0)

    cfg = {
        "status_path": str(tmp_path / "s.json"), "piece_dir": str(tmp_path / "p"),
        "outputs_dir": str(tmp_path / "o"),
        "text": "Hoje a gente vai falar de tres coisas na ordem e depois os custos",
        "voice_id": "v", "voices_dir": str(tmp_path / "voices"),
        "voice_path": str(tmp_path / "voices" / "v.wav"), "model": "omnivoice",
        "settings": {"chunk_max_chars": 140}, "omni": {"num_steps": 4},
        "base_dir": str(tmp_path),
    }
    (tmp_path / "c.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(tts_worker, "_carrega", lambda c: {
        "sr": 24000, "family": "omnivoice", "omni": {"num_steps": 4},
        "settings": {"chunk_max_chars": 140}})
    monkeypatch.setattr(tts_worker, "_sintetiza", _sintetiza)
    monkeypatch.setattr(tts_worker, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(tts_worker, "normaliza_loudness", lambda *a, **k: 0.0)
    monkeypatch.setattr(__import__("sys"), "argv", ["tts_worker.py", str(tmp_path / "c.json")])
    assert tts_worker.main() == 0
    meta = json.loads(next((tmp_path / "o").glob("*.json")).read_text())
    for campo in ("ganho_normalizacao_db", "ilegivel", "warning"):
        assert campo in meta, campo
