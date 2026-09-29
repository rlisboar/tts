"""Simula o cenário REAL da árvore compartilhada: um agente edita um módulo do
servidor DEPOIS do boot do processo de teste (é o que produz os 2 falsos vermelhos
de `test_build_*` quando 6 agentes rodam pytest ao mesmo tempo).

Uso: PYTHONPATH=.  ./.venv-mlx/bin/python -m pytest tests/test_api.py -k build -q -p midrun_edit
"""
import pathlib
import sys


def pytest_configure(config):
    sys.path.insert(0, str(pathlib.Path.cwd()))
    import app                                     # congela o BOOT aqui
    assert app._BUILD_CODIGO, "boot tem de existir antes da edição"


def pytest_collection_modifyitems(config, items):
    p = pathlib.Path("live_pipeline.py")
    texto = p.read_text()
    if "# midrun" not in texto:
        p.write_text(texto + "# midrun\n")


def pytest_runtest_setup(item):
    if "build" not in item.name:
        return
    import app
    # pré-condição do experimento: a árvore JÁ divergiu do boot do processo
    assert app._build_hash() != app._BUILD_CODIGO, \
        "o experimento precisa de árvore != boot (cenário do terceiro editando no meio)"