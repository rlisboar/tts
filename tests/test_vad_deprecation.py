"""O VAD não pode voltar a emitir DeprecationWarning (backport importlib_resources).

Sem `importlib_resources` no ambiente, o `silero_vad/model.py` cai no
`importlib.resources.path()` — deprecado desde o py3.11, 1 warning por load.
Espelha a aceite da task #35: no servidor remoto o mesmo comando roda no venv de
lá (`remote/deploy.sh recon` imprime as linhas `dep:` e `silero DeprecationWarning:`).

Atenção ao filtro: `DeprecationWarning` de biblioteca é *ignorado* por padrão em
script solto, e sob `-W error` o próprio `silero_vad` captura a exceção e cai no
`files()` — então o comando discriminante é `-W always` (o de `-W error` só prova
que não estoura).
"""
from __future__ import annotations

import subprocess
import sys

import pytest

CARGA = "import silero_vad; silero_vad.load_silero_vad(onnx=True)"

pytestmark = pytest.mark.skipif(sys.version_info < (3, 11),
                                reason="importlib.resources.path só é deprecado a partir do 3.11")


def _vad(*warns: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *warns, "-c", CARGA], capture_output=True, text=True)


def test_carregar_o_vad_onnx_nenhum_deprecationwarning():
    r = _vad("-W", "always::DeprecationWarning")
    assert r.returncode == 0, r.stderr
    assert "DeprecationWarning" not in r.stderr, r.stderr


def test_aceite_da_task_35_sai_zero():
    """O comando do aceite (`-W error::DeprecationWarning`) tem de sair 0."""
    r = _vad("-W", "error::DeprecationWarning")
    assert r.returncode == 0, r.stderr
