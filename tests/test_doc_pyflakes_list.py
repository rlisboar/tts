"""Doc e hook não podem divergir na lista do gate estático (#389).

`LIVE.md:851` ensinava a rodar o gate estático com uma lista COPIADA e chamava
aquilo de "mesma lista do `.githooks/pre-commit`". A cópia apodreceu: ficou sem
`live_rnnoise.py` (e sem `live_pipeline.py`, `dsh_client.py`, `remote/*.py`) —
quem copiava o doc rodava um pyflakes cego para o módulo novo. O README tinha a
mesma cópia, igualmente velha.

Agora a lista tem UMA fonte (o hook) e os dois docs apontam para a derivação
(`tests/_pyflakes_gate.py`). Este arquivo cobra as duas coisas:

1. nenhum doc carrega cópia DIVERGENTE (se voltar a copiar, a cópia tem de ser
   exatamente a do hook);
2. a derivação está de fato documentada — é o que o QA vai rodar.

Vale também de guarda-chuva: é o teste que o item de auditoria exigia para o
"acrescentar módulo ao hook sem tocar no doc ficar vermelho".
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "tests"))

from _pyflakes_gate import modulos  # noqa: E402

DOCS = ("LIVE.md", "README.md")
DERIVACAO = "_pyflakes_gate.py"

# bloco ```bash ... ``` que chame pyflakes
BLOCO = re.compile(r"```bash\n(.*?)```", re.S)


def _alvos_do_doc(texto: str) -> set[str] | None:
    """Módulos citados num bloco que chama `-m pyflakes`; None = não há bloco."""
    for bloco in BLOCO.findall(texto):
        if "-m pyflakes" not in bloco:
            continue
        linha = bloco.replace("\\\n", " ")
        linha = next(l for l in linha.splitlines() if "-m pyflakes" in l)
        depois = linha.split("-m pyflakes", 1)[1]
        nomes = {p for p in depois.split() if p.endswith(".py") or "*" in p}
        return nomes
    return None


@pytest.mark.parametrize("doc", DOCS)
def test_doc_sem_copia_divergente_do_hook(doc):
    texto = (BASE / doc).read_text()
    nomes = _alvos_do_doc(texto)
    if nomes is None:
        return                      # doc não ensina o comando: nada a comparar
    do_hook = {Path(a).relative_to(BASE).as_posix() for a in modulos()}
    assert nomes == do_hook, (
        f"{doc} tem lista própria e ela diverge do hook.\n"
        f"falta no doc: {sorted(do_hook - nomes)}\n"
        f"sobra no doc: {sorted(nomes - do_hook)}")


@pytest.mark.parametrize("doc", DOCS)
def test_doc_ensina_a_derivacao(doc):
    assert DERIVACAO in (BASE / doc).read_text(), (
        f"{doc} deveria mandar rodar `tests/{DERIVACAO}` (fonte única da lista)")


def test_hook_cobre_todo_modulo_do_repo():
    """Módulo novo não pode nascer fora do estágio estático.

    É o buraco por onde `live_rnnoise.py` e `smoke_eco_regra.py` passaram: o hook
    tem a regra escrita ("módulo novo do épico, linha nova aqui") e nada cobrava.
    Todo `.py` de raiz/`remote/`/`client/` entra; `tests/` já é glob.
    """
    no_hook = {Path(a).relative_to(BASE).as_posix() for a in modulos()}
    todos = {p.relative_to(BASE).as_posix()
             for padrao in ("*.py", "remote/*.py", "client/*.py")
             for p in BASE.glob(padrao)}
    fora = sorted(todos - no_hook)
    assert not fora, f"módulo fora do pyflakes do hook: {fora}"


# ── 16 alvos explícitos: o número é o que faz o aviso "módulo novo" doer ─────────
# A lista é única (o hook), então acrescentar módulo lá não mexe em doc nenhum. O
# número explícito devolve a mordida do aceite do #389: módulo novo no hook sem
# tocar no doc fica vermelho aqui. `tests/*.py` é glob e de propósito não conta —
# senão criar um arquivo de teste (frequente) pediria edição de doc.
EXPLICITOS_DOC = re.compile(r"(\d+) alvos explícitos")


def _explicitos_no_hook() -> int:
    return sum(1 for a in modulos() if "/tests/" not in a)


@pytest.mark.parametrize("doc", DOCS)
def test_numero_de_alvos_explicitos_bate_com_o_hook(doc):
    m = EXPLICITOS_DOC.search((BASE / doc).read_text())
    assert m, f"{doc} não diz o número de alvos explícitos do hook"
    assert int(m.group(1)) == _explicitos_no_hook(), (
        f"{doc} diz {m.group(1)} alvos explícitos; o hook tem {_explicitos_no_hook()}. "
        "Se você acrescentou/removeu módulo no hook, atualize o número (e o texto).")
