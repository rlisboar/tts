"""Regravação alto-falante→microfone: a marca sobrevive ao ar? (#522)

É o teste mais honesto da marca: o áudio sai pelo alto-falante, atravessa a sala,
volta pelo microfone. O caso real é o ECO do Live — o próprio TTS voltando pelo
mic do dono — que a identidade não separa quando o dono clonou a própria voz.

Protocolo: toca o áudio MARCADO e grava, a 0,5 m e a 2 m, com volume moderado.
Grava um trecho de silêncio antes, para medir o piso da sala (sem ele o número de
detecção não tem contra o que ser comparado). A gravação é APAGADA no fim se
`--apaga` for passado.

Dispositivos do macOS (não são fixos): o mic sai de `avfoundation -list_devices`;
o padrão aqui é o índice 2 (MacBook Pro Microphone). A saída usa o `afplay`, que
toca no dispositivo padrão do sistema.

Uso (venv de bancada):
    ./venv312/bin/python tools/bakeoff/marca_regrava.py --limite 4 --apaga
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alvo  # noqa: E402
import comum  # noqa: E402
import marca_robustez  # noqa: E402

FFMPEG = "/opt/homebrew/bin/ffmpeg"
MIC = "2"
DISTANCIAS = ("0.5m", "2m")
MARGEM = 1.0


def _grava(destino: Path, segundos: float) -> None:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "avfoundation",
                    "-i", f":{MIC}", "-ar", str(comum.SR), "-ac", "1",
                    "-t", f"{segundos:.2f}", str(destino)],
                   stdin=subprocess.DEVNULL, check=True)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=4)
    ap.add_argument("--volume", default="0.6")
    ap.add_argument("--apaga", action="store_true")
    a = ap.parse_args()
    comum.garante()
    pasta = comum.RESULTADOS / "_regrava"
    if pasta.exists():
        shutil.rmtree(pasta)
    pasta.mkdir(parents=True)
    marca, detecta = marca_robustez._marcador()

    # piso da sala ANTES de qualquer som
    _grava(pasta / "silencio.wav", 2.0)
    piso = _rms(sf.read(str(pasta / "silencio.wav"), dtype="float32")[0])
    print(f"piso da sala (rms): {piso:.5f}", flush=True)

    por = alvo._falantes()
    falantes = [f for f, ps in sorted(por.items()) if len(ps) >= 2][: a.limite]
    linhas = []
    for falante in falantes:
        x, _ = alvo.junta(por[falante], 4)
        marcado = marca(x, comum.SR)
        fonte = pasta / f"marcado_{falante}.wav"
        sf.write(str(fonte), marcado, comum.SR, subtype="PCM_16")
        for dist in DISTANCIAS:
            gravado = pasta / f"gravado_{dist}_{falante}.wav"
            dur = marcado.size / comum.SR
            gravador = subprocess.Popen(
                [FFMPEG, "-y", "-loglevel", "error", "-f", "avfoundation",
                 "-i", f":{MIC}", "-ar", str(comum.SR), "-ac", "1",
                 "-t", f"{dur + MARGEM:.2f}", str(gravado)],
                stdin=subprocess.DEVNULL)
            subprocess.run(["afplay", "-v", a.volume, str(fonte)], check=False)
            gravador.wait()
            y, _ = sf.read(str(gravado), dtype="float32")
            # descarta a folga das pontas e pontua o miolo
            ini = int(0.35 * comum.SR)
            meio = y[ini: ini + int(dur * comum.SR)]
            prob = detecta(meio, comum.SR) if meio.size else float("nan")
            rms = _rms(meio)
            linha = {"falante": falante, "distancia": dist, "prob": round(prob, 3),
                     "rms": round(rms, 5), "db_acima_do_piso": round(
                         20 * np.log10(max(rms, 1e-9) / max(piso, 1e-9)), 1)}
            linhas.append(linha)
            print(f"{falante} a {dist}: prob {prob:.2f} | sinal {linha['db_acima_do_piso']} dB "
                  f"acima do piso", flush=True)

    resumo = {}
    for dist in DISTANCIAS:
        vals = [l["prob"] for l in linhas if l["distancia"] == dist]
        if vals:
            resumo[dist] = {"mediana": round(float(np.median(vals)), 3),
                            "detectados": int(sum(1 for v in vals if v >= 0.5)),
                            "n": len(vals)}
            print(f"{dist}: {resumo[dist]['detectados']}/{len(vals)} detectados, "
                  f"mediana {resumo[dist]['mediana']:.2f}", flush=True)

    (comum.RESULTADOS / "regrava.json").write_text(json.dumps(
        {"mic": MIC, "volume": a.volume, "piso_rms": round(piso, 5),
         "linhas": linhas, "resumo": resumo, "apagado": bool(a.apaga)}, indent=1))
    if a.apaga:
        shutil.rmtree(pasta)
        print("gravação apagada (ficou só o json)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
