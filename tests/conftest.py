import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# #152: o worker TTS persistente do Live sobe um PROCESSO com modelo real. A suíte
# rápida não tem modelo carregado, então o default aqui é DESLIGADO — o smoke com
# modelo real (`TTS_TEST_WORKER=1`) e o `live_ws.sh` ligam por conta própria. O
# knobs é lido em `live_pipeline._LIVE_WORKER_LIGADO` (import-time) e por isso o
# `setdefault` tem de vir ANTES de qualquer import do app/live_pipeline no teste.
#
# CONSEQUÊNCIA (ressalva do gate #158): rodar só `pytest tests/ -q` NÃO exercita o
# caminho ligado com modelo real. A lógica dele está coberta por
# `tests/test_live_worker.py` (filho stub, força `_worker_habilitado`/o knob) e o
# acoplamento real pelo marcador **`worker_real`** (endereço óbvio e barato):
#
#     TTS_TEST_WORKER=1 ./.venv-mlx/bin/python -m pytest -m worker_real -q
#
# Sem o env os testes `worker_real` ficam SKIPPED (o marcador endereça, o env é a
# chave). O cabeçalho do pytest (abaixo) imprime isto em TODO run, para quem roda
# só o default não achar que cobriu. Evidência de latência à parte:
# `./.venv-mlx/bin/python smoke_worker_persist.py`.
# (o `live_ws.sh` usa o modelo do settings.json: `TTS_ROD_MODEL` só vale quando o
#  arquivo não tem "model" — com settings.json preenchido, família isolada ponta a
#  ponta exige trocar o `model` do settings ou passar pelo teste do pipeline.)
# `# noqa` é do flake8: o pyflakes do hook não o lê, então o E402 abaixo é
# decorativo — silêncio de verdade, aqui, só com `__all__` (ver o hook).
import os                       # noqa: E402

os.environ.setdefault("TTS_LIVE_WORKER", "0")

# `DeprecationWarning: builtin type SwigPyPacked/SwigPyObject has no __module__
# attribute`. EMISSOR pinçado: os bindings SWIG que entram junto com
# `import onnxruntime` (o primeiro a carregar é
# `tests/test_app.py::test_ort_telemetry_*`, do caminho ONNX do VAD). O warning sai
# de `<frozen importlib._bootstrap>:488`, então filtrar por MÓDULO não pega (o
# módulo do emissor é importlib congelado) — o filtro é por MENSAGEM, e só essa,
# para não esconder warning nosso. É do venv, não temos como consertar o emissor.
# Dois pontos de aplicação, de propósito: este (global, cobre o shutdown do
# processo) e o `pytest_configure` abaixo (o plugin de warnings do pytest recria o
# contexto por teste e reaplica os filtros da config, descartando o global).
warnings.filterwarnings(
    "ignore", message=r"builtin type Swig\w* has no __module__ attribute")

# `RuntimeWarning: invalid value encountered in divide` — MESMO tratamento e MESMO
# motivo (#108, complemento do #101): sai de `mlx_audio/codec/.../higgs_audio.py:41`
# no IMPORT da lib (via `test_cloudflare_sh`, que importa o app e a stack de áudio),
# é cosmético e do venv. Filtro por MENSAGEM porque o warning é emitido durante o
# import, então o `module` do emissor não é estável entre testes.
# NUANCE registrada: a mensagem é genérica de numpy, então um `divide` NOSSO com
# exatamente este texto também some — é cosmético nos dois casos; qualquer outra
# mensagem nossa continua aparecendo (tem teste de contraprova abaixo).
warnings.filterwarnings(
    "ignore", message=r"invalid value encountered in divide", category=RuntimeWarning)


def pytest_configure(config):
    """Mesmo filtro do venv na via suportada pelo pytest (por MENSAGEM, não módulo)."""
    config.addinivalue_line(
        "filterwarnings",
        r"ignore:builtin type Swig\w* has no __module__ attribute:DeprecationWarning")
    config.addinivalue_line(
        "filterwarnings",
        r"ignore:invalid value encountered in divide:RuntimeWarning")
    # #166: o caminho LIGADO do worker do Live custa modelo real, então fica fora do
    # default. O marcador é o endereço dele — sem isto o `-m worker_real` avisa
    # "unknown marker" e ninguém descobre o comando.
    config.addinivalue_line(
        "markers",
        "worker_real: exercita o worker TTS persistente com MODELO real "
        "(precisa de TTS_TEST_WORKER=1; sem ele, skipped) — #152/#166")


def pytest_report_header(config):
    """Diz na CARA de todo run que o caminho ligado não está no default (#166).

    Vale no run verboso; com `-q` o cabeçalho some, então o
    `pytest_terminal_summary` abaixo repete o aviso no FIM — é o run que a equipe de
    fato usa (`pytest tests/ -q`)."""
    ligado = os.environ.get("TTS_LIVE_WORKER", "0") != "0"
    return [
        f"worker TTS do Live neste run: {'LIGADO' if ligado else 'desligado'} "
        f"(TTS_LIVE_WORKER={os.environ.get('TTS_LIVE_WORKER', '0')})",
        "caminho LIGADO com modelo real (fora do default): "
        "TTS_TEST_WORKER=1 pytest -m worker_real",
    ]


def pytest_terminal_summary(terminalreporter):
    """Mesmo aviso no RESUMO — aparece também com `-q` (#166)."""
    if os.environ.get("TTS_LIVE_WORKER", "0") != "0":
        terminalreporter.write_line(
            "[worker] Live com worker LIGADO neste run (TTS_LIVE_WORKER != 0)")
        return
    if _rodou_worker_real(terminalreporter):
        terminalreporter.write_line(
            "[worker] caminho LIGADO do worker do Live correu neste run "
            "(marcador worker_real)")
        return
    terminalreporter.write_line(
        "[worker] caminho LIGADO do worker do Live NÃO correu neste run: "
        "TTS_TEST_WORKER=1 ./.venv-mlx/bin/python -m pytest -m worker_real -q")


def _rodou_worker_real(terminalreporter) -> bool:
    """Algum teste `worker_real` PASSOU neste run? (skipped não conta: é o default.)"""
    for chave in ("passed", "failed", "error"):
        for rep in terminalreporter.stats.get(chave, []):
            if "worker_real" in getattr(rep, "keywords", {}):
                return True
    return False


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