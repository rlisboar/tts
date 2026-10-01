#!/usr/bin/env python3
"""Evidência da #152: 1º áudio do turno do Live com uma família ISOLADA (kokoro).

Mede, no app real (mesmo `_tts_app`/`_generate_chunk` do turno, mesma voz), o
tempo de "pedido do chunk → áudio pronto", sempre com o pre-warm JÁ FEITO (o
pre-warm é fora do turno, então ele é reportado à parte):

  A) ANTES — turno in-process (o caminho de hoje);
  B) DEPOIS — turno pelo worker PERSISTENTE da sessão (#152);
  C) baseline do problema — worker POR JOB (subprocesso por pedido);
  D) worker do Live gerando com um JOB EM LOTE concorrente (mesmo `_gen_lock`):
     prova que não há atropelo de Metal e que os dois saem com áudio;
  E) geração in-process DEPOIS de uma sessão com worker: prova que liberar o
     modelo do pai não deixa estado sujo e o que se paga ao voltar ao in-process.

Rodar:  ./.venv-mlx/bin/python smoke_worker_persist.py     (Metal: ~2-5 min)
Não toca o settings.json (mexe só em `app._settings`, na memória).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import numpy as np                      # noqa: E402

import app                              # noqa: E402
import live_pipeline as lp              # noqa: E402
import os

MODELO = "kokoro"
TEXTO = "Claro, o dia está bonito hoje."
app._settings["model"] = MODELO
app._settings["default_voice"] = None


def rms(a) -> float:
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    return float(np.sqrt(float((a * a).mean()))) if a.size else 0.0


def _perfil():
    return lp._perfil_live(primeiro_chunk=True, max_steps=lp._LIVE_MAX_STEPS)


def _cronometra(rotulo: str, fn):
    """Tempo do PEDIDO do chunk (o que o turno paga) — não do pre-warm."""
    t = time.perf_counter()
    audio = fn()
    ms = (time.perf_counter() - t) * 1000
    dur = np.asarray(audio).size / 24000
    print(f"{rotulo:42s} {ms:8.0f} ms  dur {dur:5.2f}s  rms {rms(audio):.4f}",
          flush=True)
    return ms, audio


def cenario_in_process(resultados: dict):
    """A) caminho de HOJE: modelo no processo do servidor."""
    t0 = time.perf_counter()
    lp._prewarm_app(voice_id=None, tts_in_process=True)
    print(f"{'[A] pre-warm in-process (fora do turno)':42s} "
          f"{time.perf_counter() - t0:8.1f} s", flush=True)
    ms, audio = _cronometra("[A] ANTES  1º áudio in-process",
                            lambda: lp._tts_app(TEXTO, _perfil()))
    _cronometra("[A] ANTES  2º turno (mesmo texto)",
                lambda: lp._tts_app(TEXTO, _perfil()))
    resultados["A"] = ms
    return audio


def cenario_worker(resultados: dict):
    """B) worker persistente: sobe no pre-warm, gera o turno, morre na sessão."""
    lp._LIVE_WORKER_LIGADO = True
    pipe = lp.LivePipeline(lambda o: None, lambda b: None, voice_id=None)
    t0 = time.perf_counter()
    pipe.start()                       # sobe+preeaquece o worker FORA do turno
    print(f"{'[B] worker sobe no pre-warm (fora do turno)':42s} "
          f"{time.perf_counter() - t0:8.1f} s", flush=True)
    try:
        ms, audio = _cronometra("[B] DEPOIS 1º áudio worker persistente",
                                lambda: pipe._tts(TEXTO, _perfil()))
        _cronometra("[B] DEPOIS 2º turno (mesmo texto)",
                    lambda: pipe._tts(TEXTO, _perfil()))
        proc = pipe._worker._proc if pipe._worker else None
    finally:
        pipe.close()
    print(f"{'[B] worker morre com a sessão':42s} "
          f"returncode {proc.poll() if proc else 'None'}", flush=True)
    resultados["B"] = ms
    return audio


def cenario_pos_worker(resultados: dict):
    """E) geração IN-PROCESS depois de uma sessão que usou o worker.

    Prova que liberar o modelo do pai (`_unload_local_tts` no pre-warm com
    `tts_in_process=False`) não deixa estado sujo: o `_get_model` recarrega do
    zero e o 1º chunk sai com áudio. O que ele paga a mais (reload + compilação da
    primeira forma) é o preço de VOLTAR ao in-process, e não uma regressão."""
    _cronometra("[E] 1º in-process depois da sessão com worker (recarrega)",
                lambda: lp._tts_app(TEXTO, _perfil()))
    ms, audio = _cronometra("[E] 2º in-process (quente)",
                            lambda: lp._tts_app(TEXTO, _perfil()))
    resultados["E"] = ms
    return audio


def cenario_worker_por_job(tmp: Path) -> float:
    """C) baseline: UM processo por pedido (modo antigo do tts_worker)."""
    for sub in ("pieces", "out"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    cfg = {
        "job_id": "ev152", "text": TEXTO, "voice_id": "", "voice_path": "",
        "language": "auto", "omni": {"num_steps": lp._LIVE_MAX_STEPS, "speed": 1.0},
        "model": MODELO,
        "settings": {"chunk_max_chars": 140, "omni_ref_max_s": 10.0,
                     "omni_precision": "bf16", "audio_gain_db": 0.0},
        "piece_dir": str(tmp / "pieces"), "outputs_dir": str(tmp / "out"),
        "voices_dir": str(app.VOICES_DIR), "base_dir": str(BASE),
        "status_path": str(tmp / "status.json"),
    }
    (tmp / "cfg.json").write_text(json.dumps(cfg))
    t = time.perf_counter()
    p = subprocess.run([str(BASE / ".venv-mlx" / "bin" / "python"),
                        str(BASE / "tts_worker.py"), str(tmp / "cfg.json")],
                       capture_output=True, timeout=900)
    ms = (time.perf_counter() - t) * 1000
    assert p.returncode == 0, (p.stdout + p.stderr).decode()[-800:]
    return ms


def cenario_paralelo(resultados: dict):
    """D) worker do Live × job em lote no mesmo `_gen_lock` — sem atropelo."""
    lp._LIVE_WORKER_LIGADO = True
    pipe = lp.LivePipeline(lambda o: None, lambda b: None, voice_id=None)
    pipe.start()
    casa = Path(f"/tmp/ev152-lote-{os.getpid()}")
    dur_lote, erros = {}, []

    def lote():
        try:
            with app._gen_lock:           # exatamente o que o job da UI faz
                dur_lote["ms"] = cenario_worker_por_job(casa)
        except Exception as exc:          # noqa: BLE001
            erros.append(f"lote: {type(exc).__name__}: {exc}")

    try:
        th = threading.Thread(target=lote)
        th.start()
        time.sleep(0.2)                   # garante a disputa do lock
        ms, audio = _cronometra("[D] worker com job de lote concorrente",
                                lambda: pipe._tts(TEXTO, _perfil()))
        th.join(900)
    finally:
        pipe.close()
    assert not erros, erros
    assert rms(audio) > 0.005, "worker saiu mudo na disputa"
    print(f"{'[D] job de lote concorrente (vai até o fim)':42s} "
          f"{dur_lote['ms']:8.0f} ms (serializado pelo _gen_lock)", flush=True)
    resultados["D"] = ms
    return audio


def main() -> int:
    fam = app._current_backend()["family"]
    print(f"modelo={MODELO}  família={fam}  isolada={fam in app._ISOLATED_FAMILIES}\n",
          flush=True)
    resultados: dict = {}
    cenario_in_process(resultados)
    cenario_worker(resultados)
    cenario_pos_worker(resultados)      # E: in-process DEPOIS de usar o worker

    lote = cenario_worker_por_job(Path(f"/tmp/ev152-baseline-{os.getpid()}"))
    print(f"{'[C] worker POR JOB (baseline do problema)':42s} {lote:8.0f} ms",
          flush=True)

    cenario_paralelo(resultados)

    print("\nRESUMO (1º áudio do turno, pre-warm fora da conta):")
    print(f"  in-process (antes) ........... {resultados['A']:.0f} ms")
    print(f"  worker persistente (depois) .. {resultados['B']:.0f} ms")
    print(f"  worker por job (baseline) .... {lote:.0f} ms")
    print(f"  worker × job de lote ......... {resultados['D']:.0f} ms")
    print(f"  in-process pós-worker (quente) {resultados['E']:.0f} ms")
    assert resultados["B"] < lote / 2, "o worker persistente tinha de ser bem mais rápido"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())