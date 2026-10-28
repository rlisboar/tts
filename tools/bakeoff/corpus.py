"""Corpus COMPARTILHADO dos bake-offs de identidade (#521) e de ruído (#520).

Duas fontes de fala, nesta ordem de preferência:

1. **pública** — LibriSpeech `dev-clean` (CC BY 4.0), baixada uma vez em
   `/tmp/tts-bakeoff/baixa` e reduzida a um subconjunto limitado de locutores e
   falas. É a fonte que dá EER honesto: locutores reais, muitos deles com poucas
   falas curtas.
2. **sintética** — TTS do próprio app (pt), com locutores distintos por preset.
   Serve de reserva quando não há rede e como caso "clone do próprio app".

Nunca toca `voices/`, `outputs/` nem `.speaker-profiles.json`.

Uso:
    ./.venv-mlx/bin/python tools/bakeoff/corpus.py publico
    ./.venv-mlx/bin/python tools/bakeoff/corpus.py sintetico
    ./.venv-mlx/bin/python tools/bakeoff/corpus.py monta
"""

from __future__ import annotations

import os
import random
import sys
import tarfile
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import ruido  # noqa: E402

SEMENTE = 20261028
MAX_LOCUTORES = 12
MIN_FALAS, MAX_FALAS = 6, 10          # por locutor
DURO_MIN_S, DURO_MAX_S = 1.0, 8.0     # as durações do aceite
SNRS = (0, 5, 10, 20)
URL = "https://www.openslr.org/resources/12/dev-clean.tar.gz"
BAIXA = comum.BASE / "baixa"


def _le_16k(p: Path) -> np.ndarray:
    x, sr = sf.read(str(p), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != comum.SR:
        import scipy.signal as ss

        x = ss.resample_poly(x, comum.SR, sr).astype(np.float32)
    return x


def _grava(p: Path, x: np.ndarray) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), np.clip(x, -1, 1), comum.SR, subtype="PCM_16")


def publico() -> int:
    """LibriSpeech dev-clean → subconjunto limitado, cortado nas durações do aceite."""
    comum.garante()
    if not (BAIXA / "LibriSpeech").exists():
        BAIXA.mkdir(parents=True, exist_ok=True)
        tar = BAIXA / "dev-clean.tar.gz"
        if not tar.exists():
            print("baixando dev-clean…", flush=True)
            urllib.request.urlretrieve(URL, tar)
        with tarfile.open(tar) as t:
            t.extractall(BAIXA, filter="data")
    rng = random.Random(SEMENTE)
    locutores = sorted(p for p in (BAIXA / "LibriSpeech" / "dev-clean").iterdir() if p.is_dir())
    rng.shuffle(locutores)
    feitos = 0
    for loc in locutores[:MAX_LOCUTORES]:
        falas = sorted(loc.rglob("*.flac"))
        if len(falas) < MIN_FALAS:
            continue
        alvo = rng.randint(MIN_FALAS, MAX_FALAS)
        escolhidas = rng.sample(falas, min(alvo, len(falas)))
        for i, arq in enumerate(escolhidas):
            x = _le_16k(arq)
            dur = rng.uniform(DURO_MIN_S, min(DURO_MAX_S, x.size / comum.SR))
            ini = rng.randint(0, max(0, x.size - int(dur * comum.SR)))
            x = x[ini: ini + int(dur * comum.SR)]
            _grava(comum.FALA / f"pub_{loc.name}_{i:02d}.wav", x)
            feitos += 1
    print(f"falas públicas: {feitos} de {MAX_LOCUTORES} locutores")
    return feitos


def sintetico(n_por_preset: int = 6) -> int:
    """Fala sintetizada pelo próprio app — reserva offline e caso de clone."""
    comum.garante()
    sys.path.insert(0, str(comum.RAIZ_REPO))
    os.environ.setdefault("TTS_LIVE_WORKER", "0")
    import app  # noqa: E402

    app._settings["chunk_max_chars"] = 400
    textos = [
        "Bom dia, vamos começar pela agenda de hoje e depois pelos números do mês.",
        "O relatório ficou pronto ontem e já está na pasta compartilhada com todos.",
        "Precisamos revisar o contrato antes de sexta, principalmente a cláusula oito.",
        "A reunião com o fornecedor rendeu três propostas e nenhuma delas serve.",
        "Me passa o resumo do que ficou decidido na última conversa com o cliente.",
        "Amanhã eu volto com a versão final do texto para você aprovar sem pressa.",
    ]
    modelo = app._get_model()
    feitos = 0
    for preset in ("vd-narrador", "vd-locutora", "vd-formal"):
        for i in range(n_por_preset):
            fala = textos[i % len(textos)]
            audio = app._generate_chunk(modelo, fala, "pt", None, None,
                                        {"num_steps": 8}, sr=24000)
            import scipy.signal as ss

            x = ss.resample_poly(np.asarray(audio, dtype=np.float32), comum.SR, 24000)
            x = app._pos_processa(x, comum.SR)
            dur = min(DURO_MAX_S, max(DURO_MIN_S, x.size / comum.SR))
            _grava(comum.FALA / f"sin_{preset}_{i:02d}.wav",
                   x[: int(dur * comum.SR)])
            feitos += 1
    print(f"falas sintéticas: {feitos}")
    return feitos


def monta() -> int:
    """Misturas ruidosas em SNRs fixos, com sementes reprodutíveis."""
    comum.garante()
    rng = np.random.default_rng(SEMENTE)
    falas = sorted(comum.FALA.glob("*.wav"))
    if not falas:
        print("sem falas: rode `publico` ou `sintetico` antes")
        return 0
    for camada in comum.RUIDO.glob("*.wav"):
        camada.unlink()
    feitos = 0
    for fam, fn in ruido.FAMILIAS.items():
        sujo = fn(20.0, int(rng.integers(1, 10_000)))
        _grava(comum.RUIDO / f"{fam}.wav", ruido.normaliza_rms(sujo, -26))
    for fala in falas:
        x = _le_16k(fala)
        for fam, snr in ((f, s) for f in ruido.FAMILIAS for s in SNRS):
            camada = ruido.normaliza_rms(_le_16k(comum.RUIDO / f"{fam}.wav")[: x.size], -26)
            pot = np.mean(x ** 2) or 1e-9
            camada = camada * np.sqrt(pot / (np.mean(camada ** 2) or 1e-9) / 10 ** (snr / 10))
            _grava(comum.MISTURA / f"{fala.stem}__{fam}_snr{snr}.wav", x + camada)
            feitos += 1
    print(f"misturas: {feitos}")
    return feitos


if __name__ == "__main__":
    comum.garante()
    acao = sys.argv[1] if len(sys.argv) > 1 else "ajuda"
    tabela = {"publico": publico, "sintetico": sintetico, "monta": monta}
    if acao not in tabela:
        print(__doc__)
        raise SystemExit(2)
    tabela[acao]()
    comum.manifesto({"semente": SEMENTE, "snrs": list(SNRS)})
    print("manifesto:", comum.RESULTADOS / "corpus.json")
