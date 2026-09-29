"""Guard do estágio ESTÁTICO do hook (`.githooks/pre-commit`): o `pyflakes` do repo
não pode ficar vermelho.

POR QUE ESTE ARQUIVO EXISTE (task_2851555b / #183): um F401/F811 num arquivo NOVO
deixa `pytest tests/ -q` VERDE e só aparece quando alguém tenta commitar — e quem
paga o vermelho é o TERCEIRO (o gate é o mesmo para todo mundo, e o commit dele
aborta por um defeito que não é dele). Foi o que aconteceu com os imports de
fixture de `tests/test_qa_gate176.py`/`tests/_probe_audit.py`. Aqui o MESMO comando
do hook roda dentro do pytest, então o vermelho aparece em quem roda a suíte.

A LISTA É DERIVADA DO HOOK, não copiada: se um módulo novo entra na lista de lá (é
o combinado do repo: "módulo novo do épico, linha nova aqui"), este teste o pega
sem ninguém lembrar de duplicar a linha.

NUANCE do `# noqa` (medida, pyflakes 3.4.0): o pyflakes PURO não honra `noqa` (é
recurso do flake8). Import que só serve de FIXTURE — uso que o pyflakes não
enxerga — se cala com `__all__ = [...]` no módulo, que marca o import como usado e
derruba o F401 e o F811 derivado (o pyflakes só acusa 'redefinition of UNUSED').
"""

import glob
import shlex
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HOOK = BASE / ".githooks" / "pre-commit"


def _comando_do_hook() -> list[str]:
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


def _modulos() -> list[str]:
    """Os módulos do comando do hook, com os globs (`tests/*.py`) expandidos."""
    partes = _comando_do_hook()
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


def test_hook_roda_pyflakes_na_lista_dele():
    """A derivação não pode virar no-op: o alvo tem de conter o app e os testes."""
    alvos = _modulos()
    assert str(BASE / "app.py") in alvos, "o comando do hook mudou de forma — revisar"
    assert sum(1 for a in alvos if a.startswith(str(BASE / "tests"))) >= 5, \
        "o glob `tests/*.py` não expandiu — o guard estaria cego para a suíte"
    assert len(alvos) == len(set(alvos)), "módulo repetido na lista do hook"


def test_pyflakes_do_hook_sai_limpo():
    """O estágio estático do hook, byte a byte igual (mesma lista, mesmo intérprete)."""
    alvos = _modulos()
    faltando = [a for a in alvos if not Path(a).exists()]
    assert not faltando, f"o hook aponta para módulo que não existe: {faltando}"
    r = subprocess.run([sys.executable, "-m", "pyflakes", *alvos],
                       capture_output=True, text=True, cwd=BASE)
    assert r.returncode == 0, \
        f"pyflakes vermelho (o commit de terceiro trava):\n{r.stdout}{r.stderr}"