"""Base comum dos bake-offs de áudio (#520/#521): caminhos, hash e avisos.

Regras da fase: NADA de `voices/`, `outputs/` ou `.speaker-profiles.json` do dono —
tudo debaixo de um diretório temporário (`BAKEOFF_DIR`, padrão /tmp/tts-bakeoff).
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
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


#: Caminhos de estado que o app lê no IMPORT (`app._caminho_estado_env`).
#: São os mesmos do `_ENV_ESTADO` do `tests/conftest.py`. `VOICES_DIR`,
#: `OUTPUTS_DIR` e `SPEAKER_AUDIO_DIR` caem na árvore do repo (`BASE`), não no
#: `TTS_STATE_DIR`, por isso cada um é fixado à parte.
ESTADO_ENV = {
    "TTS_SETTINGS_PATH": "settings.json",
    "TTS_APIKEYS_PATH": ".apikeys.json",
    "TTS_LEGACY_APIKEY_PATH": ".apikey",
    "TTS_SPEAKER_PATH": ".speaker-profiles.json",
    "TTS_SPEAKER_AUDIO_DIR": "speaker-audio",
    "TTS_OUTPUTS_DIR": "outputs",
    "TTS_VOICES_DIR": "voices",
    "TTS_JOBS_HIST_PATH": "jobs-hist.json",
    "TTS_METRICS_DIR": "metrics",
}
_ESTADO: Path | None = None


def isola_estado() -> Path:
    """Aponta TODO o estado do app para um diretório temporário (#1127).

    Tem de vir ANTES do `import app`: os caminhos são resolvidos no import. Sem
    isso o bake-off lia o `settings.json`, as chaves e os perfis do dono, e o
    `voices/`/`outputs/` da árvore. Sobrescreve até um `TTS_*_PATH` exportado no
    shell (por desenho do #461 ele venceria o `TTS_STATE_DIR`). Idempotente; o
    diretório é apagado na saída do processo.
    """
    global _ESTADO
    if _ESTADO is not None:
        return _ESTADO
    if "app" in sys.modules:
        raise RuntimeError("isola_estado() depois do `import app`: o app já leu o "
                           "estado do dono — chame antes de importar")
    raiz = Path(tempfile.mkdtemp(prefix="bakeoff-estado-"))
    atexit.register(shutil.rmtree, raiz, True)
    os.environ["TTS_STATE_DIR"] = str(raiz)
    for nome, rel in ESTADO_ENV.items():
        os.environ[nome] = str(raiz / rel)
    for rel in ("voices", "outputs", "speaker-audio", "metrics"):
        (raiz / rel).mkdir()
    _ESTADO = raiz
    return raiz


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
