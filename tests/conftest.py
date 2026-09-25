import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session", autouse=True)
def _estado_isolado(tmp_path_factory):
    """settings.json do repo -> cópia em tmp, uma por PROCESSO de teste.

    O arquivo do repo é estado compartilhado: com o time rodando pytest em
    paralelo (e o servidor vivo do usuário ao lado), dois testes que comparam
    "antes vs depois" no disco viram falso vermelho — medido antes desta fixture:
    1 de 3 suítes em paralelo falhou em
    `test_settings_400_nao_deixa_ram_divergindo_do_disco` (stt_beam 4 x 9, escrito
    pela suíte vizinha).

    Os testes leem `app.SETTINGS_PATH`, então redirecionar o atributo basta; a
    cópia começa idêntica ao arquivo real, para o processo herdar a config atual
    (defaults + overrides) em vez de defaults secos. Quem quiser o arquivo do
    repo usa `app.BASE / "settings.json"`.
    """
    import app

    destino = tmp_path_factory.mktemp("estado") / "settings.json"
    if app.SETTINGS_PATH.exists():
        destino.write_bytes(app.SETTINGS_PATH.read_bytes())
    original = app.SETTINGS_PATH
    app.SETTINGS_PATH = destino
    yield destino
    app.SETTINGS_PATH = original