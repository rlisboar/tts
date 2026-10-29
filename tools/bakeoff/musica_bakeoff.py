"""Import com MÚSICA: separar a voz antes de qualquer outra limpeza (#520, caso d).

Música não é ruído estacionário: o denoise tira a ventoinha e deixa o
acompanhamento. A hipótese é a ordem inversa da usual — separar (Demucs, stem de
voz) e SÓ DEPOIS denoisar o que sobrar. Aqui se mede se isso vale.

Candidatos, do mais simples ao mais caro:
- `cru`: nada;
- `dfnet3onnx`: só denoise (o que existe hoje);
- `demucs`: só separação;
- `demucs_dfnet3onnx`: separação e depois denoise.

Métricas: DNSMOS (OVRL), timbre (similaridade do locutor antes/depois) e, para a
música, quanto do acompanhamento sobra — medido pelo RMS residual de um trecho só
de música passado pelo mesmo caminho.

Demucs é MIT; roda na venv de bancada (torch), por subprocesso.

Uso (venv de bancada):
    ./venv312/bin/python tools/bakeoff/musica_bakeoff.py --limite 4 --snrs 5,10
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import dfn_onnx  # noqa: E402
import pt_ruido  # noqa: E402
import ruido_bakeoff  # noqa: E402

DEMUCS_SR = 44100
#: o script roda na `.venv-mlx` (precisa do `app` para transcrever); o Demucs vive
#: na venv de bancada, que tem torch — por isso o subprocesso aponta para lá
VENV_DF = "/tmp/tts-bakeoff/venv312/bin/python"


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def separa_voz(x: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    """Stem de voz pelo Demucs. Devolve (voz, segundos)."""
    from scipy.signal import resample_poly

    pasta = comum.RESULTADOS / "_musica"
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir(parents=True, exist_ok=True)
    entrada = pasta / "in.wav"
    sf.write(str(entrada), resample_poly(x, DEMUCS_SR, sr).astype(np.float32),
             DEMUCS_SR, subtype="PCM_16")
    t = time.perf_counter()
    r = subprocess.run([VENV_DF, "-m", "demucs", "--two-stems", "vocals",
                        "-n", "htdemucs", "-o", str(pasta), str(entrada)],
                       capture_output=True)
    dt = time.perf_counter() - t
    achado = sorted(pasta.rglob("vocals.wav"))
    if r.returncode != 0 or not achado:
        raise RuntimeError((r.stderr or b"").decode()[-300:])
    y, sr2 = sf.read(str(achado[0]), dtype="float32")
    y = y if y.ndim == 1 else y.mean(axis=1)
    return resample_poly(y, sr, sr2).astype(np.float32)[: x.size], dt


def main() -> int:
    sys.path.insert(0, str(comum.RAIZ_REPO))
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=4)
    ap.add_argument("--snrs", default="5,10")
    a = ap.parse_args()
    comum.garante()
    carga = [round(v, 2) for v in os.getloadavg()]
    print(f"carga da máquina: {carga}", flush=True)

    from resemblyzer import VoiceEncoder, preprocess_wav

    enc = VoiceEncoder()
    por = pt_ruido.monta(comum.BASE / "pt")
    falantes = sorted(por)[: a.limite]
    linha_base = {}

    # quanto de MÚSICA sobra: a mesma música sozinha passa pelos caminhos
    musica = sf.read(str(comum.RUIDO / "musica.wav"), dtype="float32")[0]
    musica = musica[: 4 * comum.SR]
    linha_base["rms_musica_cru"] = round(_rms(musica), 5)
    try:
        voz_m, _ = separa_voz(musica, comum.SR)
        linha_base["rms_musica_demucs"] = round(_rms(voz_m), 5)
    except Exception as erro:  # noqa: BLE001
        print("separação de controle falhou:", erro, flush=True)

    linhas = []
    for snr in (float(s) for s in a.snrs.split(",")):
        for nome in ("cru", "dfnet3onnx", "demucs", "demucs_dfnet3onnx"):
            ovrl, timbre, wers, rtfs = [], [], [], []
            for i, f in enumerate(falantes):
                limpo = sf.read(str(por[f][2]), dtype="float32")[0][: 4 * comum.SR]
                ruido = pt_ruido._ruido("musica", limpo.size, i)
                rx, rr = _rms(limpo), _rms(ruido) or 1e-9
                sujo = (limpo + ruido * (rx / rr / 10 ** (snr / 20))).astype(np.float32)
                t = time.perf_counter()
                try:
                    y = sujo
                    if nome.startswith("demucs"):
                        y, _ = separa_voz(y, comum.SR)
                    if nome.endswith("dfnet3onnx") or nome == "dfnet3onnx":
                        from scipy.signal import resample_poly

                        y48 = resample_poly(y, 48000, comum.SR).astype(np.float32)
                        y = resample_poly(dfn_onnx.limpa(y48), comum.SR, 48000)[: y.size]
                    y = ruido_bakeoff._iguala_nivel(np.asarray(y, np.float32), sujo)
                except Exception as erro:  # noqa: BLE001
                    print(f"  {snr} {nome} {f}: FALHOU {erro}", flush=True)
                    continue
                dt = time.perf_counter() - t
                rtfs.append(dt / (sujo.size / comum.SR))
                m = ruido_bakeoff._dnsmos(y, comum.SR) or {}
                if m.get("ovrl_mos"):
                    ovrl.append(float(m["ovrl_mos"]))
                e_ref = enc.embed_utterance(preprocess_wav(limpo, source_sr=comum.SR))
                e_y = enc.embed_utterance(preprocess_wav(y, source_sr=comum.SR))
                timbre.append(float(np.dot(e_ref, e_y)))
                wers.append(ruido_bakeoff._wer(pt_ruido.FRASES[2], y, comum.SR))
                print(f"snr{snr:.0f} {nome} {f}: ovrl "
                      f"{ovrl[-1] if ovrl else float('nan'):.2f} sim "
                      f"{timbre[-1]:.3f} wer {wers[-1]:.2f} rtf {rtfs[-1]:.2f}", flush=True)
            if ovrl:
                linhas.append({"snr": snr, "candidato": nome,
                               "ovrl": round(float(np.mean(ovrl)), 3),
                               "timbre": round(float(np.mean(timbre)), 3) if timbre else None,
                               "wer": round(float(np.mean(wers)), 3),
                               "rtf": round(float(np.mean(rtfs)), 2), "n": len(ovrl)})
    print(flush=True)
    for snr in (float(s) for s in a.snrs.split(",")):
        dobro = [l for l in linhas if l["snr"] == snr]
        print(f"snr {snr:.0f}: " + " | ".join(
            f"{l['candidato']} ovrl {l['ovrl']:.2f} wer {l['wer']:.2f}" for l in dobro), flush=True)
    (comum.RESULTADOS / "musica.json").write_text(json.dumps(
        {"carga": carga, "controle_musica": linha_base, "linhas": linhas}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
