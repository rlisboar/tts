"""Plugin do filho do controle #206 (lado ESTADO): o `settings.json` do DONO.

O `conftest` COPIA o arquivo do dono para um tmp — ou seja, o CONTEÚDO dele entra
na suíte (só o caminho é isolado). Este plugin, carregado por `-p` no filho, aponta
`app.SETTINGS_PATH` para uma cópia HOSTIL antes de o conftest copiá-la, para que o
campo do dono (ex.: `chat_backend_live`) chegue nos testes.

Não é código de produção: existe só para o controle executável do #206. Carregado
por `-p _estado_hostil` com `tests/` no PYTHONPATH.
"""
import json
import os
import pathlib

import app


def pytest_configure(config):
    if "QA206_SETTINGS" not in os.environ:
        return          # filho que não é do controle do ARQUIVO: não opina
    caminho = pathlib.Path(os.environ["QA206_SETTINGS"])
    dados = json.loads(caminho.read_text())
    app.SETTINGS_PATH = caminho      # o conftest copia daqui
    app._settings.update(dados)      # o app já leu o arquivo real no import