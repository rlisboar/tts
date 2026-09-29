"""Smoke test do worker isolado (subprocesso real, modelo leve já em cache).

Lento (~40–70s): roda só com TTS_TEST_WORKER=1 — fora do pre-commit.
Teria pegado o bug de aliases no import de common (NameError que só existia
em runtime do subprocesso).

Os três testes de modelo real levam o marcador **worker_real** (#166), que é o
endereço óbvio da suíte opcional: `.venv-mlx/bin/python -m pytest -m worker_real`
sem o env os coleta e SKIPPA; com `TTS_TEST_WORKER=1` roda. O caminho LIGADO do
worker do Live mora aqui porque depende de modelo — o resto (protocolo, lock,
fallback) é o `tests/test_live_worker.py`, rápido e no default.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import app


@pytest.mark.worker_real
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


# ---------------------------------------------------------------------------
# modo PERSISTENTE `--serve` (#152) — filho real, kokoro, 2 pedidos
# ---------------------------------------------------------------------------

@pytest.mark.worker_real
@pytest.mark.skipif(not os.environ.get("TTS_TEST_WORKER"),
                    reason="lento: carrega modelo real — rode com TTS_TEST_WORKER=1")
def test_worker_persistente_serve_smoke(tmp_path, monkeypatch):
    """O mesmo filho atende N pedidos REUSANDO o modelo (é o ponto da #152).

    O Live tem orçamento de 1,5 s por turno: o worker por job pagava ~7,5 s de
    recarga a cada turno. Aqui o 2º pedido (o custo real de um turno depois do
    pre-warm) tem de caber no orçamento, e o áudio não pode sair mudo."""
    monkeypatch.setitem(app._settings, "model", "kokoro")   # o mais leve do catálogo
    import time as _t

    import live_pipeline as lp

    w = lp._LiveWorker(voice_id="", timeout_s=180)
    t0 = _t.time()
    w.start()
    carga = _t.time() - t0                  # fora do turno (pre-warm da sessão)
    try:
        t1 = _t.time()
        a = w.gerar("Teste do worker persistente.", {"num_steps": 8})
        d1 = _t.time() - t1
        t2 = _t.time()
        b = w.gerar("Segundo pedido, quente.", {"num_steps": 8})
        d2 = _t.time() - t2
    finally:
        proc = w._proc
        w.fecha()

    assert a.size > 1000 and b.size > 1000
    assert float(np.sqrt((a * a).mean())) > 0.01, "1º pedido saiu mudo"
    assert float(np.sqrt((b * b).mean())) > 0.01, "2º pedido saiu mudo"
    assert proc.poll() is not None, "o filho tem de morrer no close"
    print(f"\n[#152] carga (fora do turno) {carga:.1f}s | "
          f"1º synth {d1 * 1000:.0f} ms | 2º synth {d2 * 1000:.0f} ms")
    assert d2 < 1.5, f"pedido quente fora do orçamento do Live: {d2:.2f}s"


@pytest.mark.worker_real
@pytest.mark.skipif(not os.environ.get("TTS_TEST_WORKER"),
                    reason="lento: carrega modelo real — rode com TTS_TEST_WORKER=1")
def test_pipeline_usa_o_worker_e_fecha_com_a_sessao(tmp_path, monkeypatch):
    """O CAMINHO LIGADO ponta a ponta, com modelo real (ressalva (a) do gate #158).

    A suíte default é `TTS_LIVE_WORKER=0` (para não carregar modelo) — logo ela não
    prova que o Live REALMENTE usa o worker. Aqui: família isolada + knob ligado, o
    turno tem de sair do filho (pid diferente) e o filho tem de morrer com a
    sessão."""
    import live_pipeline as lp_mod

    monkeypatch.setitem(app._settings, "model", "kokoro")
    monkeypatch.setattr(lp_mod, "_LIVE_WORKER_LIGADO", True)

    pipe = lp_mod.LivePipeline(lambda o: None, lambda b: None, voice_id=None)
    try:
        pipe.start()                     # pre-warm: sobe o worker fora do turno
        assert pipe._worker is not None and pipe._worker.ativo
        assert pipe._worker._proc.pid != os.getpid(), "tem de ser outro processo"
        perfil = lp_mod._perfil_live(primeiro_chunk=True, max_steps=12)
        audio = pipe._tts("Primeiro turno real.", perfil)
        proc = pipe._worker._proc
    finally:
        pipe.close()

    assert audio.size > 1000
    assert float(np.sqrt((audio * audio).mean())) > 0.005, "turno saiu mudo"
    assert proc.poll() is not None, "o filho tem de morrer com a sessão"


# ---------------------------------------------------------------------------
# _write_status: rápidos, sem MLX (regressão do #17 — mesma classe do #12)
# ---------------------------------------------------------------------------

def test_write_status_tmp_unico_sob_escritores_concorrentes(tmp_path):
    """tinha tmp de nome FIXO próprio (`status.tmp`) e nenhum try/except: dois
    escritores no mesmo status estouravam FileNotFoundError e derrubavam a
    atualização. Hoje o caminho é por job, mas o worker é multi-processo por
    design — o padrão convidava ao erro."""
    import threading

    from tts_worker import _write_status

    dst = tmp_path / "status.json"
    erros = []

    def batida(tag):
        for i in range(200):
            try:
                _write_status(dst, {"fase": tag, "i": i})
            except Exception as e:  # noqa: BLE001 — queremos ver QUAL exceção
                erros.append(f"{tag}/{i}: {type(e).__name__}: {e}")

    ths = [threading.Thread(target=batida, args=(f"w{k}",)) for k in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    assert erros == []
    assert set(json.loads(dst.read_text())) == {"fase", "i"}   # íntegro, não truncado
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_write_status_limpa_tmp_quando_replace_falha(tmp_path, monkeypatch):
    """Erro no meio não pode deixar `.tmp` órfão (o pai faz poll do arquivo)."""
    import common
    from tts_worker import _write_status

    dst = tmp_path / "status.json"

    def boom(src, dst_):
        raise OSError("disco cheio")

    monkeypatch.setattr(common.os, "replace", boom)
    with pytest.raises(OSError):
        _write_status(dst, {"status": "running"})

    assert list(tmp_path.iterdir()) == []   # sem status.tmp / status.json.*.tmp órfão
