"""Marca d'água nas famílias ISOLADAS (#797).

O caminho isolado grava o trecho no worker e o pai marcava só no caminho
in-process — a família isolada saía sem marca e o anti-clone furava em silêncio.
A régua é a do gate do #657: 100 % de detecção no positivo, 0 % no negativo,
cobrindo TODAS as famílias do catálogo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import app  # noqa: E402
import marca_dagua  # noqa: E402
import tts_worker  # noqa: E402


def _fala() -> np.ndarray:
    t = np.arange(16000) / 16000
    x = np.sin(2 * np.pi * 190 * t) + 0.4 * np.sin(2 * np.pi * 520 * t)
    return (x / np.abs(x).max() * 0.25).astype(np.float32)


def _roda_worker(tmp_path, monkeypatch, familia: str) -> Path:
    """Roda o worker de VERDADE com a geração dublada: prova o laço, não o helper."""
    pdir, outdir = tmp_path / "pecas", tmp_path / "saida"
    pdir.mkdir(), outdir.mkdir()
    cfg = {
        "job_id": f"j-{familia}", "text": "uma frase curta", "voice_id": "",
        "voice_path": "", "language": "pt", "omni": {},
        "model": "kokoro", "marca": True,
        "settings": {"chunk_max_chars": 140},
        "piece_dir": str(pdir), "outputs_dir": str(outdir),
        "voices_dir": str(tmp_path), "base_dir": str(RAIZ),
        "status_path": str(pdir / "status.json"),
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))

    def _gera(*a, **k):
        return _fala()

    monkeypatch.setattr(tts_worker, "generate_with_backend", _gera)
    class _Estado(dict):
        """Estado do modelo dublado: o que o laço pedir e não existir vem vazio."""

        def __missing__(self, chave):
            return ""

    estado = _Estado({"sr": 16000, "family": familia, "model": None, "conds": None,
                      "omni": {}, "voice_id": "", "be_meta": {},
                      "settings": {}})
    monkeypatch.setattr(tts_worker, "_carrega", lambda cfg: estado)
    monkeypatch.setattr(tts_worker, "resolve_backend",
                        lambda *a, **k: {"id": familia, "family": familia, "path": "x",
                                         "is_shortcut": False, "meta": {}})
    monkeypatch.setattr(tts_worker, "normaliza_loudness", lambda *a, **k: None)
    monkeypatch.setattr(tts_worker, "apply_audio_fx", lambda a, *x, **k: a)
    monkeypatch.setattr(tts_worker, "trim_tail_silence", lambda a, *x, **k: a)
    monkeypatch.setattr(tts_worker, "fade_edges", lambda a, *x, **k: a)
    monkeypatch.setattr(tts_worker, "resolve_omni_source", lambda *a, **k: ("", ""))
    monkeypatch.setattr(tts_worker, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["tts_worker.py", str(cfg_path)])
    tts_worker.main()
    return sorted(pdir.glob("*.wav"))[0]


@pytest.mark.parametrize("familia", sorted(app._ISOLATED_FAMILIES))
def test_toda_familia_isolada_leva_marca(familia, tmp_path, monkeypatch):
    """Se a família entra no laço isolado, o trecho gravado TEM de sair marcado."""
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    peca = _roda_worker(tmp_path, monkeypatch, familia)

    prob = marca_dagua.detecta_prob(peca)
    assert prob is not None and prob > 0.9, f"{familia}: trecho sem marca (prob {prob})"


def test_trecho_cru_nao_acusa_marca(tmp_path):
    """O negativo: sem a marca, o detector não pode achar nada."""
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    peca = tmp_path / "cru.wav"
    sf.write(str(peca), _fala(), 16000, subtype="PCM_16")
    prob = marca_dagua.detecta_prob(peca)
    assert prob is not None and prob < 0.5, f"cru acusou marca ({prob})"


def test_desligado_nao_marca(tmp_path):
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    peca = tmp_path / "desligado.wav"
    sf.write(str(peca), _fala(), 16000, subtype="PCM_16")
    tts_worker._marca_peca(peca, False)
    prob = marca_dagua.detecta_prob(peca)
    assert prob is not None and prob < 0.5


def test_o_job_isolado_recebe_a_ordem_de_marcar(monkeypatch, tmp_path):
    """A ligação: a setting viaja no config do worker, senão a marca não acontece."""
    capturado = {}

    class _Proc:
        pid = 4242
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    def _popen(argv, **kw):
        cfg = json.loads(Path(argv[2]).read_text())   # [python, worker, config]
        capturado.update(cfg)
        return _Proc()

    monkeypatch.setattr(app.subprocess, "Popen", _popen)
    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)
    monkeypatch.setattr(app, "_unload_local_tts", lambda *a, **k: None)
    monkeypatch.setattr(app, "_jobs", {})
    app._jobs["j1"] = {"status": "queued"}
    monkeypatch.setattr(app, "_piece_dir", lambda job_id: tmp_path / job_id)
    monkeypatch.setattr(app, "_schedule_job_cleanup", lambda *a, **k: None)

    app._run_tts_job_isolated("j1", "oi", "voz", tmp_path / "voz.wav", "pt", {},
                              {"id": "kokoro", "family": "kokoro", "meta": {}})
    assert capturado.get("marca") is True, "o config do worker não pediu a marca"
