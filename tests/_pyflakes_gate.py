"""Alvos do estágio ESTÁTICO do hook — fonte única do teste e do CI (#293).

A lista NÃO é copiada em lugar nenhum: `.githooks/pre-commit` é a única fonte e
este módulo a deriva (junta as continuações `\\`, expande os globs, aponta tudo
para a raiz do repo). O teste `tests/test_hook_pyflakes.py` usa a derivação com
asserções e contraprova; o job `estatica` do CI chama o `__main__` daqui.

POR QUE UM MÓDULO E NÃO SÓ O TESTE: o CI não pode depender de `pytest` para o
estágio mais barato do gate, e rodar `pytest tests/test_hook_pyflakes.py` puxa o
`conftest.py`, que importa o `app` (fastapi, torch, MLX) só para isso — o job
deixava de ser "segundos". Aqui a dependência é o `pyflakes` e a stdlib.

Uso:
    python3 tests/_pyflakes_gate.py      # roda o estágio; exit code do pyflakes
"""

import glob
import shlex
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HOOK = BASE / ".githooks" / "pre-commit"


def comando_do_hook() -> list[str]:
    """A linha `$PY -m pyflakes <módulos...>` do hook, com as continuações `\\` juntas."""
    linhas = HOOK.read_text().splitlines()
    for i, linha in enumerate(linhas):
        if "-m pyflakes" not in linha or linha.lstrip().startswith("#"):
            continue
        pedaco = linha
        while pedaco.rstrip().endswith("\\"):
            i += 1
            pedaco = pedaco.rstrip()[:-1] + " " + linhas[i]
        return shlex.split(pedaco)
    raise AssertionError(f"não achei o comando do pyflakes em {HOOK}")


def modulos() -> list[str]:
    """Os módulos do comando do hook, com os globs (`tests/*.py`) expandidos."""
    partes = comando_do_hook()
    if "||" in partes:                       # `... client/mic_router.py || exit 1`
        partes = partes[:partes.index("||")]
    corte = partes.index("pyflakes") + 1
    alvos: list[str] = []
    for padrao in partes[corte:]:
        if any(c in padrao for c in "*?["):
            alvos += [str(BASE / p) for p in sorted(glob.glob(padrao, root_dir=BASE))]
        else:
            alvos.append(str(BASE / padrao))
    return alvos


def rodar(extras: list[str] | None = None) -> subprocess.CompletedProcess:
    """Roda o pyflakes na lista do hook (mesmo intérprete do hook: o do venv)."""
    return subprocess.run([sys.executable, "-m", "pyflakes", *modulos(), *(extras or [])],
                          capture_output=True, text=True, cwd=BASE)


def main() -> int:
    r = rodar()
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
