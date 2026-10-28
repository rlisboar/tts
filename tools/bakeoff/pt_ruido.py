"""Cenário em pt-BR, com e sem o ruído do R1 (#521, condição (b) do PM).

O corpus principal é leitura de audiolivro em inglês. O produto é pt-BR, com
microfone de Mac e ventoinha/ar. Um extrator que só funciona no corpus limpo em
inglês não serve, então este cenário existe para o veredito poder ser desmentido.

Fala: vozes pt-BR do próprio macOS (`say`), 8 timbres, 3 frases por voz. É voz
sintética — mais fácil que fala humana real, e isso vai declarado. Cadastro com 2
frases, teste com a 3ª (amostra que não entrou no cadastro).

Ruído: os do R1, em SNR 5 e 10 dB, misturados na mesma proporção em cadastro e
teste (como seria um microfone ruim o tempo todo).

Métricas: EER com IC 95 % por bootstrap, e a FAR/FRR no limiar operacional.

Uso:
    /tmp/tts-bakeoff/venv312/bin/python tools/bakeoff/pt_ruido.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import identidade  # noqa: E402
import metricas  # noqa: E402

VOZES = ["Luciana", "Flo", "Eddy", "Reed", "Shelley", "Sandy", "Grandma", "Rocko"]
FRASES = [
    "A reunião de amanhã foi adiada para a próxima semana, sem prejuízo do prazo.",
    "Preciso que você confira a planilha de custos antes de mandar para o cliente.",
    "O tempo mudou de repente e a rua ficou cheia de gente correndo para casa.",
]
SNRS = (None, 10, 5)


def monta(pasta: Path) -> dict[str, list[Path]]:
    """Gera as falas em pt (uma por frase e voz) e devolve por locutor."""
    pasta.mkdir(parents=True, exist_ok=True)
    por: dict[str, list[Path]] = {}
    for voz in VOZES:
        caminhos = []
        for i, frase in enumerate(FRASES):
            destino = pasta / f"{voz}_{i}.wav"
            if not destino.exists() or destino.stat().st_size < 1000:
                subprocess.run(["say", "-v", voz, "--data-format=LEI16@16000",
                                "-o", str(destino), frase], check=True)
            caminhos.append(destino)
        por[voz] = caminhos
    return por


def _ruido(nome: str, n: int, semente: int) -> np.ndarray:
    x, _ = sf.read(str(comum.RUIDO / f"{nome}.wav"), dtype="float32")
    x = x if x.ndim == 1 else x.mean(axis=1)
    if x.size < n:
        x = np.tile(x, int(np.ceil(n / x.size)))
    ini = int(np.random.default_rng(semente).integers(0, x.size - n))
    return x[ini: ini + n]


def _com_ruido(x: np.ndarray, nome: str, snr: float, semente: int) -> np.ndarray:
    r = _ruido(nome, x.size, semente)
    rx = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    rr = float(np.sqrt(np.mean(r.astype(np.float64) ** 2))) or 1e-9
    return (x + r * (rx / rr / 10 ** (snr / 20))).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corte", type=float, default=0.44,
                    help="limiar operacional (o mesmo do #532 corrigido)")
    a = ap.parse_args()
    comum.garante()
    por = monta(comum.BASE / "pt")
    falantes = sorted(por)

    def eca(caminhos):
        # reusa o extrator do bake-off de identidade, que já normaliza (L2)
        m = identidade.ADAPTADORES["ecapa"]()
        return [m.emb(sf.read(str(p), dtype="float32")[0]) for p in caminhos]

    cad = {f: eca(por[f][:2]) for f in falantes}
    tst = {f: eca([por[f][2]])[0] for f in falantes}

    def cent(vs):
        c = np.mean(vs, axis=0)
        return c / (np.linalg.norm(c) or 1e-9)

    cembs = {f: cent(cad[f]) for f in falantes}
    saida = []
    for snr in SNRS:
        rot = "limpo" if snr is None else f"snr{snr}"
        cen, te = {}, {}
        for f in falantes:
            if snr is None:
                cen[f], te[f] = cembs[f], tst[f]
                continue
            # com ruído: reextrai a partir dos arquivos já ruidosos, para o ruído
            # entrar em cadastro e teste como entraria num microfone ruim
            tmp = comum.RESULTADOS / "_pt" / f"{f}_{rot}"
            tmp.mkdir(parents=True, exist_ok=True)
            arqs = []
            for i, p in enumerate(por[f][:2]):
                x = _com_ruido(sf.read(str(p), dtype="float32")[0], "ventoinha", snr, i)
                q = tmp / f"c{i}.wav"
                sf.write(str(q), x, comum.SR, subtype="PCM_16")
                arqs.append(q)
            cen[f] = cent(eca(arqs))
            xt = _com_ruido(sf.read(str(por[f][2]), dtype="float32")[0], "ventoinha", snr, 9)
            qt = tmp / "t.wav"
            sf.write(str(qt), xt, comum.SR, subtype="PCM_16")
            te[f] = eca([qt])[0]
        pos = [float(cen[f] @ te[f]) for f in falantes]
        neg = [float(cen[x] @ te[y]) for x in falantes for y in falantes if x != y]
        e = metricas.eer(pos, neg)
        ic = metricas.eer_ci(pos, neg)
        far = float(np.mean([s >= a.corte for s in neg]))
        frr = float(np.mean([s < a.corte for s in pos]))
        linha = {"condicao": rot, "eer": round(e, 2), "eer_ic95": ic,
                 "far": round(far, 3), "frr": round(frr, 3),
                 "n_pos": len(pos), "n_neg": len(neg)}
        saida.append(linha)
        print(f"{rot}: EER {e:.1f}% (IC {ic[0]:.1f}–{ic[1]:.1f}) | FAR {far:.0%} | "
              f"FRR {frr:.0%} | {len(pos)} pos x {len(neg)} neg", flush=True)
        # o limiar cru NÃO atravessa corpus: a curva mostra onde ele teria de ficar
        # em pt para a mesma FAR alvo, em vez de herdar o número do inglês
        curva = []
        for c in (0.44, 0.50, 0.56, 0.62, 0.68, 0.74):
            curva.append({"corte": c,
                          "far": round(float(np.mean([v >= c for v in neg])), 3),
                          "frr": round(float(np.mean([v < c for v in pos])), 3)})
        linha["curva"] = curva
        print("  " + " | ".join(f"{c['corte']:.2f}: FAR {c['far']:.0%} FRR "
                                f"{c['frr']:.0%}" for c in curva), flush=True)

    (comum.RESULTADOS / "pt_ruido.json").write_text(json.dumps(
        {"vozes": VOZES, "corte": a.corte, "linhas": saida}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
