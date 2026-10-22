"""Alvos do estágio ESTÁTICO do hook — fonte única do teste e do CI (#293).

O nome é herdado do `pyflakes`, que era o linter até a #378: hoje quem roda é o
**ruff** (`check`, conjunto `F`, config no `pyproject.toml`). A lista continua sendo
derivada do hook, e a derivação é o que importa — o binário mudou, a fonte é a
mesma.

A lista NÃO é copiada em lugar nenhum: `.githooks/pre-commit` é a única fonte e
este módulo a deriva (junta as continuações `\\`, expande os globs, aponta tudo
para a raiz do repo). O teste `tests/test_hook_pyflakes.py` usa a derivação com
asserções e contraprova; o job `estatica` do CI chama o `__main__` daqui.

POR QUE UM MÓDULO E NÃO SÓ O TESTE: o CI não pode depender de `pytest` para o
estágio mais barato do gate, e rodar `pytest tests/test_hook_pyflakes.py` puxa o
`conftest.py`, que importa o `app` (fastapi, torch, MLX) só para isso — o job
deixava de ser "segundos". Aqui a dependência é o `ruff` e a stdlib.

Uso:
    python3 tests/_pyflakes_gate.py      # roda o estágio; exit code do ruff
"""

import glob
import shlex
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HOOK = BASE / ".githooks" / "pre-commit"


FERRAMENTA = "ruff"


def comando_do_hook() -> list[str]:
    """A linha `$PY -m <ferramenta> check <módulos...>` do hook, continuações juntas."""
    linhas = HOOK.read_text().splitlines()
    for i, linha in enumerate(linhas):
        if f"-m {FERRAMENTA}" not in linha or linha.lstrip().startswith("#"):
            continue
        pedaco = linha
        while pedaco.rstrip().endswith("\\"):
            i += 1
            pedaco = pedaco.rstrip()[:-1] + " " + linhas[i]
        return shlex.split(pedaco)
    raise AssertionError(f"não achei o comando do {FERRAMENTA} em {HOOK}")


def modulos() -> list[str]:
    """Os módulos do comando do hook, com os globs (`tests/*.py`) expandidos."""
    partes = comando_do_hook()
    if "||" in partes:                       # `... client/mic_router.py || exit 1`
        partes = partes[:partes.index("||")]
    corte = partes.index(FERRAMENTA) + 1
    while corte < len(partes) and partes[corte] in ("check", "--select", "F"):
        corte += 1
    alvos: list[str] = []
    for padrao in partes[corte:]:
        if any(c in padrao for c in "*?["):
            alvos += [str(BASE / p) for p in sorted(glob.glob(padrao, root_dir=BASE))]
        else:
            alvos.append(str(BASE / padrao))
    return alvos


def rodar(extras: list[str] | None = None, *, so_extras: bool = False) -> subprocess.CompletedProcess:
    """Roda o `ruff check` na lista do hook (mesmo intérprete do hook: o do venv).

    `so_extras` lint só os arquivos passados — é o que os testes de violação
    plantada usam, senão a árvore suja de terceiro contamina a contraprova.
    """
    alvos = list(extras or []) if so_extras else [*modulos(), *(extras or [])]
    return subprocess.run([sys.executable, "-m", FERRAMENTA, "check", *alvos],
                          capture_output=True, text=True, cwd=BASE)


def _conteudo_do_ref(ref: str, relativo: str) -> str | None:
    """Conteúdo do arquivo no `ref` (None = não existe lá)."""
    r = subprocess.run(["git", "show", f"{ref}:{relativo}"], capture_output=True, text=True, cwd=BASE)
    return r.stdout if r.returncode == 0 else None


def rodar_no_ref(ref: str) -> subprocess.CompletedProcess:
    """Roda o lint no CONTEÚDO DO REF (default daqui), não no working tree (#456).

    O gate é de terceiro: quem roda a suíte no meio do trabalho de outro agente não
    pode pagar o vermelho de um arquivo que a outra pessoa ainda está editando. A
    lista de alvos continua sendo a do hook, mas o TEXTO vem do `ref`; arquivo que
    não existe no ref (novo, ainda untracked) fica de fora — o lugar dele é o teste
    da árvore, que é opt-in.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="lint-ref-") as tmp:
        raiz = Path(tmp)
        # a config vem do próprio ref: o conjunto de regras tem de ser o do commit
        pyproject = _conteudo_do_ref(ref, "pyproject.toml")
        if pyproject is not None:
            (raiz / "pyproject.toml").write_text(pyproject)
        alvos: list[str] = []
        for absoluto in modulos():
            relativo = str(Path(absoluto).relative_to(BASE))
            conteudo = _conteudo_do_ref(ref, relativo)
            if conteudo is None:
                continue
            destino = raiz / relativo
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(conteudo)
            alvos.append(str(destino))
        return subprocess.run([sys.executable, "-m", FERRAMENTA, "check", *alvos],
                              capture_output=True, text=True, cwd=raiz)


def main() -> int:
    r = rodar()
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
