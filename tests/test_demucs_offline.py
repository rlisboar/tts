"""O `htdemucs` carrega OFFLINE, a partir de `models/demucs` (#678).

Mesmo contrato dos outros pesos: artefato fora do git, sha256 no manifesto, carga
do arquivo LOCAL — sem Hub e sem cache do HF. O worker passa o diretório como
`repo` do `get_model`; sem ele a etapa falha em vez de baixar em silêncio.

Licença: MIT (código e pesos do Demucs, Meta). O texto vai no `NOTICE`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PASTA = REPO / "models" / "demucs"
PESO = PASTA / "955717e8.th"

# #783: no runner de CI os pesos não existem (são gitignored) — a célula
# vira skip COM MOTIVO em vez de vermelho de ambiente.
if not (PESO.is_file() or PESO.is_symlink()):
    pytest.skip("sem peso em models/ (gitignored) — rode tools/baixa_modelos.sh", allow_module_level=True)

_OFFLINE = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "TTS_MODELOS_URL": "http://127.0.0.1:9"}

#: carga + uma passada curta: stem `vocals` tem de sair não vazio (o modelo
#: realmente rodou, não só abriu o arquivo)
CARREGA = """
import os
from pathlib import Path

import numpy as np
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model

repo = Path(os.environ["TTS_DEMUCS_DIR"])
modelo = get_model("htdemucs", repo=repo)
modelo.eval()
x = np.zeros((2, 24000), dtype=np.float32)
x[:, 1000:20000] = 0.1
with torch.no_grad():
    saida = apply_model(modelo, torch.from_numpy(x)[None], split=False)[0]
voz = saida[modelo.sources.index("vocals")].mean(axis=0).numpy()
assert voz.size == 24000, voz.size
assert float(np.abs(voz).max()) > 1e-4, "stem vocals saiu mudo"
print("ok", round(float(np.abs(voz).max()), 5))
"""

_peso = pytest.mark.skipif(not PESO.is_file(),
                           reason="htdemucs fora do disco — rode tools/baixa_modelos.sh")


def _roda(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, **_OFFLINE, "HF_HOME": str(tmp_path / "hf"),
           "TTS_DEMUCS_DIR": str(PASTA)}
    return subprocess.run([sys.executable, "-c", CARREGA], capture_output=True,
                          text=True, timeout=600, cwd=str(tmp_path), env=env)


@_peso
def test_htdemucs_roda_offline_com_hfhome_vazio(tmp_path):
    r = _roda(tmp_path)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert r.stdout.startswith("ok "), r.stdout
    assert not list((tmp_path / "hf").rglob("*.th")), \
        "o load copiou modelo para o HF_HOME: o caminho local não está valendo"


def test_peso_resolve_e_a_licenca_esta_ao_lado():
    """MIT: o NOTICE tem de estar no diretório, com o texto, e o peso RESOLVE
    (regular, ou symlink para a raiz num worktree)."""
    assert PESO.is_file() and PESO.resolve().is_file()
    notice = (PASTA / "NOTICE").read_text()
    assert "MIT" in notice and "Demucs" in notice
    assert "dl.fbaipublicfiles.com" in (PASTA / "FONTE.txt").read_text()
    # o bag yaml aponta o único modelo do pacote
    assert "955717e8" in (PASTA / "htdemucs.yaml").read_text()


def test_artefato_fica_fora_do_git():
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "models/demucs"],
                       capture_output=True, text=True)
    rastreados = set(r.stdout.split())
    assert rastreados <= {"models/demucs/NOTICE", "models/demucs/FONTE.txt",
                          "models/demucs/htdemucs.yaml"}, \
        f"peso no índice: {sorted(rastreados)}"
    ign = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", str(PESO)],
                         capture_output=True)
    assert ign.returncode == 0, "o .th não está no .gitignore"
