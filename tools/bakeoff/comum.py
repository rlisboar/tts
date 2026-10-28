"""Base comum dos bake-offs de áudio (#520/#521): caminhos, hash e avisos.

Regras da fase: NADA de `voices/`, `outputs/` ou `.speaker-profiles.json` do dono —
tudo debaixo de um diretório temporário (`BAKEOFF_DIR`, padrão /tmp/tts-bakeoff).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[2]
BASE = Path(os.environ.get("BAKEOFF_DIR", "/tmp/tts-bakeoff"))
FALA = BASE / "fala"
RUIDO = BASE / "ruido"
MISTURA = BASE / "mistura"
CLONE = BASE / "clone"
RESULTADOS = BASE / "resultados"

SR = 16000

#: diretório do extrator ECAPA — mesmo contrato do `TTS_SPEAKER_MODELO_DIR` do app e
#: do `models/README.md`. O `savedir` é o que impede o `from_hparams` de resolver
#: pelo CACHE do HuggingFace: sem ele o load busca o Hub e quebra em máquina
#: offline, mesmo com o modelo em `models/` (#598).
MODELO_ECAPA = Path(os.environ.get("TTS_SPEAKER_MODELO_DIR")
                    or RAIZ_REPO / "models" / "ecapa-voxceleb")

#: diretório dos pesos do separador de locutores (MossFormer2_SS_16K). Mesmo
#: contrato: `TTS_SEPARA_MODELO_DIR`, default em `models/`, e o load NUNCA vai ao
#: Hub. O código da rede é vendorizado em `tools/separa_vend/` (#526-bis).
MODELO_SEPARA = Path(os.environ.get("TTS_SEPARA_MODELO_DIR")
                     or RAIZ_REPO / "models" / "mossformer2-ss-16k")
VENDIDO_SEPARA = RAIZ_REPO / "tools" / "separa_vend"


def garante() -> None:
    for d in (FALA, RUIDO, MISTURA, CLONE, RESULTADOS):
        d.mkdir(parents=True, exist_ok=True)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def manifesto(extra: dict | None = None) -> dict:
    """Manifesto do corpus: hash de cada arquivo, para a evidência ser auditável."""
    dados = {"base": str(BASE), "sr": SR,
             "arquivos": {str(p.relative_to(BASE)): sha256(p)
                          for p in sorted(BASE.rglob("*.wav"))}}
    if extra:
        dados.update(extra)
    destino = RESULTADOS / "corpus.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(dados, indent=1, ensure_ascii=False))
    return dados
