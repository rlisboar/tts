"""O grafo do DFN3 carrega OFFLINE, a partir de `models/dfn3-onnx` (#622).

Mesmo contrato dos outros pesos (#598/#526-bis): artefato fora do git, sha256 no
manifesto, `--check` pelo script e carga do arquivo LOCAL — o app usa
`onnxruntime`, então nada de Hub, de cache do HF nem de `df`/torch.

Sem o grafo a limpeza cai no gating e o relatório diz `motor: gating`: não quebra,
mas o vencedor do R1 não roda. Aqui se cobra o artefato e a carga; o DSP e a
equivalência de áudio são do `tests/test_dfn_dsp.py`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
#: o REPO é fixo (estrutura, manifesto, git); a CARGA segue o contrato do app
PASTA = REPO / "models" / "dfn3-onnx"
PASTA_RUNTIME = Path(os.environ.get("TTS_DFN3_DIR") or PASTA)
GRAFO_REPO = PASTA / "deepfilter.onnx"
GRAFO = PASTA_RUNTIME / "deepfilter.onnx"   # só para o skip e a carga

# #783: no runner de CI os pesos não existem (são gitignored) — a célula
# vira skip COM MOTIVO em vez de vermelho de ambiente.
if not (GRAFO.is_file() or GRAFO.is_symlink()):
    pytest.skip("sem peso em models/ (gitignored) — rode tools/baixa_modelos.sh", allow_module_level=True)

_OFFLINE = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "TTS_MODELOS_URL": "http://127.0.0.1:9"}

CARREGA = """
import os, sys
import onnxruntime as ort
caminho = os.environ["TTS_DFN3_DIR"] + "/deepfilter.onnx"
sess = ort.InferenceSession(caminho, providers=["CPUExecutionProvider"])
entradas = [i.name for i in sess.get_inputs()]
saidas = [o.name for o in sess.get_outputs()]
assert entradas and saidas, (entradas, saidas)
print("ok", len(entradas), len(saidas))
"""

_peso = pytest.mark.skipif(not GRAFO.is_file(),
                           reason="grafo do DFN3 fora do disco — rode tools/baixa_modelos.sh")


def _roda(tmp_path: Path, pasta: Path = PASTA_RUNTIME) -> subprocess.CompletedProcess:
    env = {**os.environ, **_OFFLINE, "HF_HOME": str(tmp_path / "hf"),
           "TTS_DFN3_DIR": str(pasta)}
    return subprocess.run([sys.executable, "-c", CARREGA], capture_output=True,
                          text=True, timeout=180, cwd=str(tmp_path), env=env)


@_peso
def test_grafo_carrega_offline_com_hfhome_vazio(tmp_path):
    r = _roda(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok " in r.stdout, r.stdout
    assert not list((tmp_path / "hf").rglob("*.onnx")), \
        "o load copiou o grafo para o HF_HOME: o caminho local não está valendo"


def test_grafo_e_regular_e_o_config_esta_ao_lado():
    """Arquivo regular — ou symlink que RESOLVE (worktree liga os pesos da raiz de
    propósito): o que não vale é symlink quebrado ou para o cache do HF. O
    `config.json` é lido junto (parâmetros do filtro) e fica no mesmo diretório."""
    assert GRAFO_REPO.is_file() and GRAFO_REPO.resolve().is_file()
    assert (PASTA / "config.json").is_file()
    assert (PASTA / "NOTICE").is_file() and (PASTA / "FONTE.txt").is_file()


def test_artefato_fica_fora_do_git():
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "models/dfn3-onnx"],
                       capture_output=True, text=True)
    rastreados = set(r.stdout.split())
    # NOTICE/FONTE podem estar só no disco (rodada antes do `git add`): o que NÃO
    # pode é peso no índice
    assert rastreados <= {"models/dfn3-onnx/NOTICE", "models/dfn3-onnx/FONTE.txt"}, \
        f"peso no índice: {sorted(rastreados)}"
    assert (PASTA / "NOTICE").is_file() and (PASTA / "FONTE.txt").is_file()
    ign = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q",
                          str(GRAFO_REPO)], capture_output=True)
    assert ign.returncode == 0, "o grafo não está no .gitignore"
