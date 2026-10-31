#!/usr/bin/env python3
"""Smoke da SEPARAÇÃO no turno do Live (#533, item 2).

O `smoke_so_minha_voz.py` mede o PORTÃO por janela; este mede o que a separação
faz com o TURNO: pega elocuções do dono, soma um terceiro a 5 dB e uma ventoinha
(20 dB), e compara o transcript do áudio CRU com o do áudio SEPARADO — vazamento
(fração de palavra de terceiro) e WER, como na evidência do #534.

Também mede o CUSTO no caminho do Live: `_separacao_do_turno` roda atrás do turno
já aberto e o aceite é ≤1,2 s a mais.

    ./.venv-mlx/bin/python smoke_separa_turno.py
    N=6 ./.venv-mlx/bin/python smoke_separa_turno.py     # menos turnos

Piso: latência p50 da separação ≤1200 ms. Vazamento/WER são INFORMATIVOS (o piso
deles é o do #534) — o que este smoke guarda é a WIRING do Live: o áudio separado
tem de ser o transcrito.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import wave

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

import numpy as np                                   # noqa: E402
import soundfile as sf                               # noqa: E402

sys.path.insert(0, str(RAIZ / "tests"))
from _estado_tmp import isola                        # noqa: E402

BASE = pathlib.Path(os.environ.get("BAKEOFF_DIR", "/tmp/tts-bakeoff"))
FALA = BASE / "fala"
RUIDO = BASE / "ruido"
N = int(os.environ.get("N", "6"))
SEGUNDOS = float(os.environ.get("SEGUNDOS", "6"))
SNR_DB = float(os.environ.get("SNR", "5"))
PISO_LAT_MS = float(os.environ.get("PISO_LAT_MS", "1200"))


def _le(p: pathlib.Path) -> np.ndarray:
    x, sr = sf.read(str(p), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != 16000:
        import scipy.signal as ss
        x = ss.resample_poly(x, 16000, sr).astype(np.float32)
    return x


def _locutores() -> tuple[str, str]:
    por: dict[str, list[pathlib.Path]] = {}
    for w in sorted(FALA.glob("pub_*.wav")):
        por.setdefault(w.stem.split("_")[1], []).append(w)
    ordem = sorted(por.items(), key=lambda kv: -len(kv[1]))
    return ordem[0][0], ordem[1][0]


def _soma(alvo: np.ndarray, camada: np.ndarray, snr_db: float) -> np.ndarray:
    n = min(alvo.size, camada.size)
    alvo, camada = alvo[:n], camada[:n]
    p_a = float(np.mean(alvo ** 2) or 1e-9)
    p_c = float(np.mean(camada ** 2) or 1e-9)
    return alvo + camada * np.sqrt(p_a / p_c / 10 ** (snr_db / 10))


def _grava(pcm: np.ndarray, destino: pathlib.Path) -> pathlib.Path:
    with wave.open(str(destino), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    return destino


def main() -> int:
    if not FALA.exists():
        raise SystemExit(f"sem corpus em {FALA}: rode `tools/bakeoff/corpus.py publico`")
    _isola_estado()
    import app
    import separa_voz

    if not separa_voz.disponivel():
        raise SystemExit(f"separador indisponível: {separa_voz.motivo()}")
    dono, terceiro = _locutores()
    clips_d, clips_t = sorted(FALA.glob(f"pub_{dono}_*.wav")), sorted(FALA.glob(f"pub_{terceiro}_*.wav"))
    vecs = [v for v in (app._speaker_embed(p) for p in clips_d) if v]
    app._speaker_load_estado = lambda: ({"dono": {"vecs": vecs,
                                                  "modelo": app.SPEAKER_MODELO_ATUAL}}, "")
    app._settings["live_so_minha_voz"] = {"on": True, "perfil": "dono",
                                          "rigor_far": app._VOZ_RIGOR_PADRAO,
                                          "limiar": 0.0, "separacao": True}
    print(f"corpus: dono={dono} · terceiro={terceiro} · {N} turnos de {SEGUNDOS:g}s "
          f"a SIR {SNR_DB:g} dB + ventoinha")
    # PREWARM como na sessão real (`_live_pipe_start` chama `_separa_prewarm`):
    # sem ele o 1º turno paga a carga do modelo e o número mede a máquina fria
    import threading
    _t0 = time.monotonic()
    app._separa_prewarm()
    for t in threading.enumerate():
        if t.name == "separa-prewarm":
            t.join(120)
    print(f"prewarm do separador: {int((time.monotonic() - _t0) * 1000)} ms")

    vent = _le(RUIDO / "ventoinha.wav") if (RUIDO / "ventoinha.wav").exists() else None
    lat, vaz, vaz_sep, casaram = [], [], [], 0
    with tempfile.TemporaryDirectory(prefix="separa-smoke-") as td:
        td = pathlib.Path(td)
        for i in range(N):
            alvo = _le(clips_d[i % len(clips_d)])[: int(16000 * SEGUNDOS)]
            outro = _le(clips_t[i % len(clips_t)])[: alvo.size]
            if outro.size < alvo.size:
                outro = np.pad(outro, (0, alvo.size - outro.size))
            mix = _soma(alvo, outro, SNR_DB)
            if vent is not None and vent.size:
                mix = _soma(mix, np.resize(vent, mix.size), 20.0)
            cru = _grava(mix, td / f"cru{i}.wav")
            seps, campos = app._separacao_do_turno(cru)
            if campos.get("separacao") != "ok":
                print(f"  ✘ turno {i}: separação {campos.get('separacao')} "
                      f"({campos.get('separacao_motivo')})")
                continue
            lat.append(campos.get("separacao_ms") or 0)
            t_cru = (app._transcribe(cru, language="en").get("text") or "").strip()
            t_sep = (app._transcribe(seps, language="en").get("text") or "").strip()
            ref = (clips_d[i % len(clips_d)].with_suffix(".txt").read_text()).strip()
            try:
                from tools.bakeoff.metricas import vazamento
                vaz.append(vazamento(t_cru, ref))
                vaz_sep.append(vazamento(t_sep, ref))
            except Exception:                # noqa: BLE001 — métrica é informativa
                pass
            if seps != cru:
                casaram += 1
                seps.unlink(missing_ok=True)
            print(f"  turno {i}: {campos.get('separacao_ms')} ms · "
                  f"descartados={campos.get('segmentos_descartados')} · "
                  f"cru={t_cru[:60]!r} · sep={t_sep[:60]!r}")
    if not lat:
        print("✘ nenhum turno separado")
        return 1
    lat.sort()
    p50 = lat[len(lat) // 2]
    print(f"\nlatência da separação: p50 {p50} ms · p95 {lat[max(0, int(len(lat)*0.95)-1)]} ms "
          f"(piso p50 ≤{PISO_LAT_MS:g}) · {casaram}/{len(lat)} turnos separados")
    if vaz:
        print(f"vazamento médio: cru {100*sum(vaz)/len(vaz):.1f}% · "
              f"separado {100*sum(vaz_sep)/len(vaz_sep):.1f}%")
    ok = p50 <= PISO_LAT_MS
    print("VEREDITO:", "OK" if ok else "REPROVADO")
    return 0 if ok else 1


def _isola_estado() -> None:
    """Estado do app em tmp (#711): sem isto o `_apply_settings` daqui grava
    `settings.json` no worktree."""
    isola()


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
