# (a poda por NOME que existia aqui saiu no #435: com `APIKEYS_PATH`,
# `LEGACY_APIKEY_PATH` e `SPEAKER_PATH` isolados na SESSÃO, a suíte não escreve mais
# no arquivo do dono — podar deixou de ser necessário. Quem cobra é
# `tests/test_chaves_isoladas.py`.)


# Espião de teardown (`tests/_espiao.py`): imprime o que ficou vivo em
# `_live_sessions`/`_chat_dsh_livres`/`_jobs` ao fim de cada teste. Sem este
# registro ele seria arquivo inerte — nenhum runner carrega `_*.py` sozinho.
pytest_plugins = ("_espiao",)

import pathlib
import shutil
import sys
import time
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
#
# #388: aviso não é exigência. `tests/test_worker_real_exigido.py` fica VERMELHO
# quando o diff toca `tts_worker.py`/`live_pipeline.py` e o env não está setado — o
# hook seta sozinho quando a área é commitada e o CI compara com a base do PR.
# (o `live_ws.sh` usa o modelo do settings.json: `TTS_ROD_MODEL` só vale quando o
#  arquivo não tem "model" — com settings.json preenchido, família isolada ponta a
#  ponta exige trocar o `model` do settings ou passar pelo teste do pipeline.)
# O `noqa` era do flake8 e o pyflakes do hook não o lia; desde a #378 o lint é o
# ruff e ele VALE — o E402 abaixo só existe por causa do setup de ambiente.
# decorativo — silêncio de verdade, aqui, só com `__all__` (ver o hook).
import os                       # noqa: E402

os.environ.setdefault("TTS_LIVE_WORKER", "0")

# ── #206: o AMBIENTE do dono não decide o veredito ───────────────────────────────
# O env TEM PRECEDÊNCIA sobre o settings (por desenho, #112): um
# `export TTS_CHAT_BACKEND_LIVE=openai` no shell do dono, ou um `TTS_LIVE_*` de
# tuning esquecido, mudava o caminho medido por testes que não pinam o campo — e o
# veredito virava refém do estado dele. Seis casos da mesma família já custaram
# rodada (#143/#145, #191, #195, #199, #203 e o resíduo do #182).
#
# A SESSÃO nasce com esses knobs NEUTRALIZADOS; quem precisa de um valor usa
# `monkeypatch` no próprio teste (padrão já usado no repo). As redes pontuais
# (`dsh_limpo`, `delenv` locais) FICAM: são a regressão de quem editar este arquivo.
#
# `TTS_ROD_*` entrou junto (#384): é o prefixo do APP, e dois deles decidem o
# modelo de papel — `TTS_ROD_ADMIN_KEY` e `TTS_ROD_API_KEY` são lidos no import
# (app.py:521-522). Medido: com `TTS_ROD_ADMIN_KEY` exportada (é o que o README
# manda usar) a suíte verde virava 11 failed + 6 errors, porque toda chave deixa
# de ser admin e com ela some o `is_admin` que os testes cobram. Ficou de fora do
# #206 por descuido de prefixo, não por decisão.
#
# DUAS EXCEÇÕES, de propósito — são INTERRUPTORES do dono, não comportamento a
# neutralizar: `TTS_TEST_WORKER` (liga o caminho lento com modelo real, ver o topo)
# e `TTS_LIVE_WORKER` (o default que este arquivo acabou de definir). Nenhum
# `TTS_ROD_*` é interruptor: todos são configuração, então todos caem.
_ENV_NEUTRO = ("TTS_CHAT_", "TTS_LIVE_", "TTS_TEST_", "TTS_ROD_")
_ENV_INTERRUPTOR = ("TTS_TEST_WORKER", "TTS_LIVE_WORKER")
_ENV_DO_DONO = {k: os.environ.pop(k) for k in sorted(os.environ)
                if k.startswith(_ENV_NEUTRO) and k not in _ENV_INTERRUPTOR}

# #278: o RNNoise do Live sobe um ffmpeg por sessão. Default do app é ligado;
# a suíte não paga isso — o filtro real está em tests/test_live_rnnoise.py.
# Fica DEPOIS da neutralização: `TTS_LIVE_*` do dono já foi retirado, e este
# valor é o da sessão de teste, não o ambiente dele.
os.environ["TTS_LIVE_RNNOISE"] = "0"

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


# `/api/shutdown` termina em `os._exit(0)` numa THREAD que dorme 0,4 s antes e
# roda o teardown. Neutralizado só no módulo (`test_qa_gate362.py`) o `_exit` real
# voltava no restore da fixture e a thread matava o pytest NO ARQUIVO SEGUINTE:
# suíte completa morria a 82% sem resumo nem traceback (3× reproduzido). A suíte
# nunca quer um exit de verdade, então a neutralização é da SESSÃO; teste que
# ASSERE o `_exit` continua patchando localmente por cima.
@pytest.fixture(autouse=True, scope="session")
def _sem_exit_de_verdade():
    import app
    original = app.os._exit
    app.os._exit = lambda *a, **k: None
    yield
    app.os._exit = original


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
    # #6cd62cf4: dois `pytest tests/` simultâneos na mesma máquina dividiam o MESMO
    # basetemp (`$TMPDIR/pytest-of-<user>`): cada rodada reescreve o symlink
    # `pytest-current` e o teardown de uma apagava o que a outra ainda ia remover —
    # `FileNotFoundError` no meio da rodada e falso vermelho (medido no gate #384).
    # Raiz por PROCESSO resolve na origem. `--basetemp` explícito continua mandando
    # (os controles do #224 dependem disso).
    if not config.option.basetemp:
        import getpass
        import tempfile
        raiz = (pathlib.Path(tempfile.gettempdir())
                / f"pytest-suite-{getpass.getuser()}" / f"{config.rootpath.name}-{os.getpid()}")
        raiz.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(raiz)
        _limpa_raizes_velhas(raiz.parent)      # lixo de rodadas verdes antigas


# #6cd62cf4: raiz por processo não pode virar lixo em TMPDIR. O que sobra de sessão
# VERDE é raiz VAZIA (o pytest apaga a sessão, não a raiz) e o `sessionfinish` roda
# cedo demais para checar vazio; a varredura abaixo roda no configure seguinte e
# remove as vazias com mais de 1 h — raiz de rodada viva é recente ou tem arquivo,
# então não é tocada. Vermelha tem arquivo e fica, que é o que o #224 quer.
_VARRIDURA_S = 3600


def _limpa_raizes_velhas(pai: Path) -> None:
    try:
        agora = time.time()
        for velho in pai.iterdir():
            if not velho.is_dir() or agora - velho.stat().st_mtime < _VARRIDURA_S:
                continue
            if any(velho.rglob("*")):
                continue
            shutil.rmtree(velho, ignore_errors=True)
    except OSError:
        pass


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
    ] + ([f"ambiente do dono neutralizado na sessão (#206): "
          f"{', '.join(_ENV_DO_DONO)}"] if _ENV_DO_DONO else [])


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
def _ambiente_neutro():
    """#206: devolve ao processo o ambiente do dono tirado na neutralização acima.

    O `setUp` é no import do conftest (precisa valer ANTES de qualquer `import app`);
    aqui só o desfazimento, para um pytest in-process (IDE, plugin, xdist) não deixar
    o shell do dono alterado.
    """
    yield _ENV_DO_DONO
    os.environ.update(_ENV_DO_DONO)


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

    #224: a cópia é CREDENCIAL — o `settings.json` do dono costuma trazer o
    `chat_api_key` em texto claro, e o basetemp fica em TMPDIR. Por isso o
    teardown abaixo apaga SÓ o que esta fixture criou (o resto do basetemp fica
    em paz, servindo de depuração); a outra ponta, a sessão que NÃO se retém, é
    o `tmp_path_retention_policy = failed` do `pytest.ini`. As duas são cobradas
    por `tests/test_tmpdir_sem_credencial.py`.
    """
    import json
    import secrets

    import app

    pasta = tmp_path_factory.mktemp("estado")
    destino = pasta / "settings.json"
    if app.SETTINGS_PATH.exists():
        destino.write_bytes(app.SETTINGS_PATH.read_bytes())
    original = app.SETTINGS_PATH
    app.SETTINGS_PATH = destino
    # CHAVES SINTÉTICAS, não cópia das do dono (#435): copiar o arquivo real
    # espalhava o SECRET dele por todo basetemp retido (medido: 48 sobras com o
    # segredo real). A sessão nasce com uma chave de teste — papel legado
    # (`role: null`, que é o que a suíte exercita como "chave antiga administra") —
    # e o arquivo do dono nem é lido para dentro da RAM.
    # Três chaves SINTÉTICAS, ids FIXOS (a suíte não depende de `via-zip`/`padrão`):
    # a 1ª é LEGADA de propósito (`role: null`) — é ela que o fixture `auth` usa e o
    # que os testes de "chave antiga administra" exercitam; as outras duas existem
    # para quem precisa de admin ou de `use` sem criá-las.
    apikeys_destino = pasta / ".apikeys.json"
    chave_teste = "sk-teste-" + secrets.token_hex(12)
    apikeys_destino.write_text(json.dumps({
        "enabled": True,
        "keys": [
            {"id": "sessao-legado", "name": "pytest-legado", "secret": chave_teste,
             "created_at": "2026-01-01 00:00:00", "role": None},
            {"id": "sessao-admin", "name": "pytest-admin",
             "secret": "sk-teste-" + secrets.token_hex(12),
             "created_at": "2026-01-01 00:00:00", "role": "admin"},
            {"id": "sessao-uso", "name": "pytest-uso",
             "secret": "sk-teste-" + secrets.token_hex(12),
             "created_at": "2026-01-01 00:00:00", "role": "use"},
        ],
    }, indent=2))
    apikeys_original = app.APIKEYS_PATH
    app.APIKEYS_PATH = apikeys_destino
    # O `.apikey` (arquivo legado que o `run.sh` lê) TAMBÉM: `_save_apikeys` chama
    # `_sync_legacy_apikey_file()`, então sem isto a suíte reescrevia o do dono a
    # cada gravação de chave mesmo com o `.apikeys.json` isolado (#435).
    legado_destino = pasta / ".apikey"
    legado_destino.write_text(chave_teste + "\n")
    legado_original = app.LEGACY_APIKEY_PATH
    app.LEGACY_APIKEY_PATH = legado_destino
    # perfis de locutor: mesmo critério (o app grava em `_speaker_salva`)
    perfis_destino = pasta / ".speaker-profiles.json"
    if app.SPEAKER_PATH.exists():
        perfis_destino.write_bytes(app.SPEAKER_PATH.read_bytes())
    perfis_original = app.SPEAKER_PATH
    app.SPEAKER_PATH = perfis_destino
    # env do dono fora da RAM: `TTS_ROD_API_KEY`/`TTS_ROD_ADMIN_KEY` exportados
    # decidiriam papel (e poriam secret real na memória). Restaurados no teardown.
    env_api_original, admin_api_original = app._ENV_API_KEY, app._ADMIN_API_KEY
    app._ENV_API_KEY = app._ADMIN_API_KEY = ""
    # RAM com as chaves sintéticas: o `import app` já tinha carregado as do dono
    app._load_apikeys()
    yield destino
    app.APIKEYS_PATH = apikeys_original
    app.LEGACY_APIKEY_PATH = legado_original
    app.SPEAKER_PATH = perfis_original
    app.SETTINGS_PATH = original
    app._ENV_API_KEY, app._ADMIN_API_KEY = env_api_original, admin_api_original
    # `ignore_errors`: o pytest pode ter varrido o basetemp antes (sessão verde
    # com a política `failed`), e num run vermelho é justamente aqui que a cópia
    # sai do diretório que o pytest vai RETER. Não roda se o processo levar
    # SIGKILL — aí sobra uma cópia por sessão interrompida, até o
    # `make_numbered_dir` do pytest varrer (é o resíduo aceito e documentado).
    shutil.rmtree(pasta, ignore_errors=True)


# (a poda por NOME que existia aqui saiu no #435: com `APIKEYS_PATH`,
# `LEGACY_APIKEY_PATH` e `SPEAKER_PATH` isolados na SESSÃO, a suíte não escreve mais
# no arquivo do dono, então podar deixou de ser necessário. Quem cobra é
# `tests/test_chaves_isoladas.py`.)
