"""Smoke test do worker isolado (subprocesso real, modelo leve já em cache).

Lento (~40–70s): roda só com TTS_TEST_WORKER=1 — fora do pre-commit.
Teria pegado o bug de aliases no import de common (NameError que só existia
em runtime do subprocesso).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import app


@pytest.mark.skipif(not os.environ.get("TTS_TEST_WORKER"),
                    reason="lento: carrega modelo real — rode com TTS_TEST_WORKER=1")
def test_worker_isolado_smoke(tmp_path):
    base = Path(app.BASE)
    piece_dir = tmp_path / "pieces"
    outputs_dir = tmp_path / "outputs"
    piece_dir.mkdir()
    outputs_dir.mkdir()

    cfg = {
        "job_id": "testworker",
        "text": "Teste do worker isolado.",
        "voice_id": "",
        "voice_path": "",
        "language": "auto",
        "omni": {"speed": 1.0, "num_steps": 8},
        # kokoro: 82M, cache do HF, sem clone — o mais leve do catálogo
        "model": "kokoro",
        "settings": {"chunk_max_chars": 140, "omni_ref_max_s": 10.0,
                     "omni_precision": "bf16", "audio_gain_db": 0.0},
        "piece_dir": str(piece_dir),
        "outputs_dir": str(outputs_dir),
        "voices_dir": str(app.VOICES_DIR),
        "base_dir": str(base),
        "status_path": str(tmp_path / "status.json"),
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))

    proc = subprocess.run(
        [sys.executable, str(base / "tts_worker.py"), str(cfg_path)],
        capture_output=True, timeout=300,
    )
    tail = (proc.stdout + proc.stderr).decode(errors="replace")[-400:]
    assert proc.returncode == 0, f"worker falhou: {tail}"

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["status"] == "done", status.get("error")
    assert status["pieces"] >= 1

    wavs = list(outputs_dir.glob("*.wav"))
    assert wavs, "worker não gerou WAV final"
    import soundfile as sf

    d, sr = sf.read(str(wavs[0]))
    assert len(d) > sr // 2, "áudio curto demais"
    assert float(np.sqrt(np.mean(d ** 2))) > 0.005, "áudio (quase) mudo"
