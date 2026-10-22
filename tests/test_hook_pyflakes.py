"""Guard do estágio ESTÁTICO do hook (`.githooks/pre-commit`): o lint do repo não
pode ficar vermelho.

POR QUE ESTE ARQUIVO EXISTE (task_2851555b / #183): um F401 num arquivo NOVO deixa
`pytest tests/ -q` VERDE e só aparece quando alguém tenta commitar — e quem paga o
vermelho é o TERCEIRO (o gate é o mesmo para todo mundo, e o commit dele aborta por
um defeito que não é dele). Aqui o MESMO comando do hook roda dentro do pytest.

A LISTA É DERIVADA DO HOOK, não copiada — a derivação mora em
`tests/_pyflakes_gate.py` (mesma fonte que o job `estatica` do CI usa, #293): se um
módulo novo entra na lista de lá (é o combinado do repo: "módulo novo do épico,
linha nova aqui"), este teste o pega sem ninguém lembrar de duplicar a linha.

O LINTER é o `ruff check` com o conjunto `F` (#378) — o mesmo conjunto do pyflakes,
agora com config no `pyproject.toml` e com `# noqa` de verdade. O nome do
módulo/arquivo é herdado do pyflakes de propósito: renomear mexeria no hook, no CI e
na documentação sem ganho.
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "tests"))

from _pyflakes_gate import FERRAMENTA, comando_do_hook, modulos, rodar  # noqa: E402


def test_hook_usa_o_ruff_com_a_lista_dele():
    """A derivação não pode virar no-op e o hook tem de chamar a ferramenta certa."""
    assert FERRAMENTA == "ruff"
    assert "ruff" in comando_do_hook(), "o hook deixou de chamar o ruff"
    alvos = modulos()
    assert str(BASE / "app.py") in alvos, "o comando do hook mudou de forma — revisar"
    assert sum(1 for a in alvos if a.startswith(str(BASE / "tests"))) >= 5, \
        "o glob `tests/*.py` não expandiu — o guard estaria cego para a suíte"
    assert len(alvos) == len(set(alvos)), "módulo repetido na lista do hook"


def test_lint_do_hook_sai_limpo():
    """O estágio estático do hook, byte a byte igual (mesma lista, mesmo intérprete)."""
    alvos = modulos()
    faltando = [a for a in alvos if not Path(a).exists()]
    assert not faltando, f"o hook aponta para módulo que não existe: {faltando}"
    r = rodar()
    assert r.returncode == 0, f"lint vermelho (o commit de terceiro trava):\n{r.stdout}{r.stderr}"


def test_guard_se_morde_com_violacao_plantada(tmp_path):
    """Contraprova AUTOMATIZADA: sem ela, se o `ruff` parasse de acusar (versão
    nova, `-m` quebrado, lista virando no-op), o teste acima passaria em falso.

    A violação é plantada FORA de `tests/` de propósito: dentro, ela entraria no
    glob `tests/*.py` e quem estivesse rodando a suíte em paralelo veria o vermelho
    dela — que é justamente o efeito que este guard quer medir, mas não de graça.
    """
    alvo = tmp_path / "plantado.py"
    alvo.write_text("import os\n\n\ndef test_z():\n    assert True\n")
    r = rodar([str(alvo)])
    assert r.returncode == 1, f"o lint engoliu a violação plantada:\n{r.stdout}{r.stderr}"
    assert str(alvo) in r.stdout and "F401" in r.stdout, \
        f"acusa, mas não cita o arquivo/código plantado:\n{r.stdout}"


def test_noqa_cala_o_f401(tmp_path):
    """Aceite do #378: `# noqa` PASSA a valer (o pyflakes o ignorava e o silêncio
    exigido era `__all__`). Se este teste ficar vermelho, os imports de fixture
    espalhados pelo repo viram ruído de novo."""
    alvo = tmp_path / "com_noqa.py"
    alvo.write_text("import os  # noqa: F401\n\n\ndef test_z():\n    assert True\n")
    r = rodar([str(alvo)])
    assert r.returncode == 0, f"o `# noqa` foi ignorado:\n{r.stdout}{r.stderr}"


def test_a_ferramenta_roda_no_interprete_do_hook():
    """Sanidade: `-m ruff check` funciona com o mesmo intérprete do hook (o do venv)."""
    r = subprocess.run([sys.executable, "-m", "ruff", "--version"],
                       capture_output=True, text=True, cwd=BASE)
    assert r.returncode == 0, r.stderr
