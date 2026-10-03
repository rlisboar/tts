"""Simula o cenário REAL da árvore compartilhada: um agente edita um módulo do
servidor DEPOIS do boot do processo de teste (é o que produz os 2 falsos vermelhos
de `test_build_*` quando 6 agentes rodam pytest ao mesmo tempo).

Uso: PYTHONPATH=evidence ./.venv-mlx/bin/python -m pytest tests/test_api.py -k build -q -p 214-midrun-edit
(o arquivo mora em `evidence/`, então o módulo é `214-midrun-edit` e o PYTHONPATH é
`evidence` — `PYTHONPATH=. -p midrun_edit` NÃO resolve: não existe esse módulo.)

ATENÇÃO: ele ACRESCENTA `# midrun` no fim de `live_pipeline.py`. O `pytest_unconfigure`
devolve o arquivo no fim da sessão (#227): deixá-lo sujo não é inofensivo — com a
árvore permanentemente divergente do boot, o ramo EXATO do par do /api/build nunca
mais roda e quem mede a tolerância depois mede outro cenário.
"""
import pathlib
import sys

_ADICIONOU = False


def pytest_configure(config):
    sys.path.insert(0, str(pathlib.Path.cwd()))
    import app                                     # congela o BOOT aqui
    assert app._BUILD_CODIGO, "boot tem de existir antes da edição"


def pytest_collection_modifyitems(config, items):
    global _ADICIONOU
    p = pathlib.Path("live_pipeline.py")
    texto = p.read_text()
    if "# midrun" not in texto:
        p.write_text(texto + "# midrun\n")
        _ADICIONOU = True


def pytest_unconfigure(config):
    """Devolve a árvore: só remove a linha que ESTE plugin acrescentou."""
    if not _ADICIONOU:
        return
    p = pathlib.Path("live_pipeline.py")
    if p.exists():
        p.write_text(p.read_text().replace("# midrun\n", "", 1))


def pytest_runtest_setup(item):
    if "build" not in item.name:
        return
    import app
    # pré-condição do experimento: a árvore JÁ divergiu do boot do processo
    assert app._build_hash() != app._BUILD_CODIGO, \
        "o experimento precisa de árvore != boot (cenário do terceiro editando no meio)"