# (a poda por NOME que existia aqui saiu no #435: com `APIKEYS_PATH`,
# `LEGACY_APIKEY_PATH` e `SPEAKER_PATH` isolados na SESSÃO, a suíte não escreve mais
# no arquivo do dono — podar deixou de ser necessário. Quem cobra é
# `tests/test_chaves_isoladas.py`.)


# Espião de teardown (`tests/_espiao.py`): imprime o que ficou vivo em
# `_live_sessions`/`_chat_dsh_livres`/`_jobs` ao fim de cada teste. Sem este
# registro ele seria arquivo inerte — nenhum runner carrega `_*.py` sozinho.
pytest_plugins = ("_espiao",)

import os
import pathlib
import shutil
import subprocess
import tempfile
import sys
import time
import warnings
from pathlib import Path

import pytest


# ── #992: `rm` do PATH que não apaga ────────────────────────────────────────────
# O harness de agentes põe um guard (`.../rm-guard/<pid>/rm`, junto de `find` e
# `unlink`) à frente no PATH. Ele morre no parse de `-f` (`ERR_PARSE_ARGS_UNKNOWN_OPTION`)
# e sai **0 sem apagar**: toda suíte que limpa temporário com `rm` fica vermelha só
# nesta máquina — e o `rm -f` silencioso faz o teste mentir sobre o estado real.
# Se o `rm` de um diretório do PATH não apaga de fato, o diretório sai do PATH da
# sessão (os subprocessos herdam). O produto usa `/bin/rm` absoluto onde importa.
def _rm_do_dir_apaga(diretorio: str) -> bool:
    exe = Path(diretorio) / "rm"
    if not exe.exists():
        return True
    with tempfile.TemporaryDirectory() as tmp:
        alvo = Path(tmp) / "x"
        alvo.write_text("x")
        try:
            subprocess.run([str(exe), "-f", str(alvo)], capture_output=True, timeout=30)
        except OSError:
            return True
        return not alvo.exists()


def _path_sem_rm_quebrado() -> None:
    partes = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    quebrados = [d for d in partes if not _rm_do_dir_apaga(d)]
    if quebrados:
        os.environ["PATH"] = os.pathsep.join(d for d in partes if d not in quebrados)


_path_sem_rm_quebrado()

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

os.environ.setdefault("TTS_LIVE_WORKER", "0")
# #813: o CORS copia a lista de extensões na CRIAÇÃO do middleware, então a célula
# não consegue monkeypatchá-la — o id de teste entra por ambiente, antes do import.
os.environ["TTS_ORIGENS_PERMITIDAS"] = "chrome-extension://abcdefghijklmnop"

# ── #721: o estado do dono vive FORA do repositório ─────────────────────────────
# `~/Library/Application Support/tts-studio` por padrão. Sem exportar um destino
# aqui, o `import app` da suíte copiaria (migração) e gravaria no Application
# Support REAL — exatamente o que se quer evitar. Trocado antes de qualquer
# `import app`; a guarda `_estado_do_dono_intocado` cobra que nada escape.
def _retrato_app_support() -> dict:
    """(nome → sha256, mtime) do estado REAL do dono. Só leitura."""
    import hashlib

    real = pathlib.Path.home() / "Library" / "Application Support" / "tts-studio"
    if not real.is_dir():
        return {}
    volta = {}
    for p in sorted(real.iterdir()):
        if not p.is_file():
            continue
        try:
            volta[p.name] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                             p.stat().st_mtime)
        except OSError:
            volta[p.name] = ("ilegivel", 0)
    return volta


# o retrato tem de ser ANTES do primeiro `import app` (é ele que migra/grava)
_RETRATO_APP_SUPPORT = _retrato_app_support()

# ── #1018: os CAMINHOS de estado do dono também não decidem ─────────────────────
# `TTS_*_PATH`/`TTS_*_DIR` de estado vencem o `TTS_STATE_DIR` (por desenho, #461).
# Com um `export TTS_SETTINGS_PATH=…` no shell do dono, a fixture `_estado_isolado`
# copiava o arquivo REAL para o basetemp: o estado deixava de ser sintético e o
# segredo voltava para o /tmp — o #206 não pegava porque só neutralizava
# `TTS_CHAT_*`/`TTS_LIVE_*`/`TTS_TEST_*`/`TTS_ROD_*`. Tirados ANTES do primeiro
# `import app`; quem precisa de outro caminho usa `monkeypatch` no próprio teste.
# (`TTS_TEST_WORKER`/`TTS_LIVE_WORKER` seguem de fora: são interruptores.)
_ENV_ESTADO = ("TTS_STATE_DIR", "TTS_SETTINGS_PATH", "TTS_APIKEYS_PATH",
               "TTS_LEGACY_APIKEY_PATH", "TTS_SPEAKER_PATH",
               "TTS_SPEAKER_AUDIO_DIR", "TTS_OUTPUTS_DIR", "TTS_VOICES_DIR",
               "TTS_JOBS_HIST_PATH", "TTS_METRICS_DIR")

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
                if (k.startswith(_ENV_NEUTRO) or k in _ENV_ESTADO)
                and k not in _ENV_INTERRUPTOR}

# DEPOIS da retirada: o estado sintético da sessão é o DESTE diretório. Se a
# atribuição viesse antes, o próprio `_ENV_ESTADO` levaria o `TTS_STATE_DIR` embora
# e o app cairia no default da árvore (`.state-local/`) — só aparece quando algo
# recarrega o módulo, por isso a suíte inteira pegou e o arquivo isolado não.
_TMP_ESTADO = tempfile.mkdtemp(prefix="tts-estado-")
os.environ["TTS_STATE_DIR"] = _TMP_ESTADO

# #278: o RNNoise do Live sobe um ffmpeg por sessão. Default do app é ligado;
# a suíte não paga isso — o filtro real está em tests/test_live_rnnoise.py.
# Fica DEPOIS da neutralização: `TTS_LIVE_*` do dono já foi retirado, e este
# valor é o da sessão de teste, não o ambiente dele.
os.environ["TTS_LIVE_RNNOISE"] = "0"

# #1050: o watchdog do /health (#915) fica DESLIGADO na sessão (`<= 0`). O prazo C
# (`faulthandler.dump_traceback_later`, que saiu do app no task_ee97f0f6)
# despejava de uma thread C SEM a GIL; quando o laço que rearma só atrasou (CPU
# disputada, duas suítes) e o principal segue rodando Python, o despejo lê frame que acabou de ser liberado e falha em
# `dump_frame` — SIGSEGV num processo limpo, e sob pytest a thread do faulthandler
# gira a 100 % de CPU no mesmo endereço. O rearme seguinte chama
# `cancel_dump_traceback_later()`, que espera essa thread SEGURANDO a GIL: o
# processo inteiro congela e o SIGALRM do `pytest-timeout` nunca roda. A pilha
# truncada em `_mean` / `  File ` no log era o despejo morrendo, não o teste.
# Quem cobra o watchdog (`tests/test_health_watchdog.py`) arma por `monkeypatch`.
# O log vai para o tmp da sessão: o default do app é o arquivo do servidor do
# dono, e era escrito por toda suíte que rodasse ao mesmo tempo.
os.environ["TTS_HEALTH_TRAVADO_S"] = "0"
os.environ["TTS_WATCHDOG_LOG"] = os.path.join(_TMP_ESTADO, "tts-watchdog.log")

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


# --- #1131: suíte cheia só pela trava de máquina (`tools/fila_suite.sh`) -----------------
# Duas suítes cheias ao mesmo tempo flakam e se matam (#1111/#1130). O hook e o `integra.sh`
# já passam pela trava, que exporta `TTS_FILA_SEGURA=1`. Quem rodar `pytest tests/` à mão, sem
# ela, é barrado AQUI com rc 75 — antes de a primeira célula rodar. "Suíte cheia" = a COLETA
# cobre (quase) todos os `tests/test_*.py`, e não um número mágico de testes que envelhece.
# Rodada curta (um arquivo, `-k` estreito, o `pytest` aninhado de uma célula) não é afetada.
# Escapes: `TTS_FILA=0` (explícito) e `CI=true` (o GitHub Actions; no runner self-hosted do mini
# a proteção é o porteiro `tools/ci_mini_ocioso.sh`, não esta trava).
from _guarda_trava import suite_cheia_fora_da_trava as _suite_cheia_fora_da_trava  # noqa: E402


def pytest_collection_finish(session):
    aqui = pathlib.Path(__file__).resolve().parent
    todos = {p.name for p in aqui.glob("test_*.py")}
    coletados = {pathlib.Path(str(i.fspath)).name for i in session.items}
    if _suite_cheia_fora_da_trava(coletados, todos, os.environ):
        pytest.exit(
            "suíte cheia fora da trava de máquina (#1131): rode\n"
            "    tools/fila_suite.sh roda -- ./.venv-mlx/bin/python -m pytest tests/ -q\n"
            "(dentro de `screen`). Rodada curta (um arquivo) não precisa da trava; "
            "escape explícito: TTS_FILA=0.", returncode=75)


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
    # `state/metrics` do dono (#610): `metrics_hist.DIR` é constante lida no IMPORT,
    # então a env tem de valer antes da coleta — não cabe na fixture de estado (que
    # roda depois). Sem isto, a rodada engorda o histórico REAL de métricas.
    if not os.environ.get("TTS_METRICS_DIR"):
        metricas = pathlib.Path(config.option.basetemp) / "estado" / "metrics"
        metricas.mkdir(parents=True, exist_ok=True)
        os.environ["TTS_METRICS_DIR"] = str(metricas)


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


# ── quem escreveu no arquivo do DONO? (#535) ────────────────────────────────
# A sessão isola settings/chaves em tmp; se algo AINDA toca o arquivo real, o dono
# vê o estado mexido e ninguém sabe de onde veio (foi o que aconteceu no gate de
# 01:24). Aqui cada teste é cercado por uma impressão digital dos arquivos do dono:
# mudou, o teste é NOMEADO. `TTS_EXIGE_DONO_INTOCADO=1` transforma o relatório em
# vermelho (a prova do card usa isso); sem a variável fica só o aviso, porque o app
# do dono pode estar rodando e gravando ao lado, o que não é regressão nossa.
_ARQUIVOS_DO_DONO = ("settings.json", ".apikeys.json", ".apikey", ".speaker-profiles.json")
_escritas_no_dono: list[str] = []
#: quem criou arquivo NOVO em `state/metrics` (nome do teste) — o guard é de sessão
#: e sem atribuição o culpado fica invisível, atribuído ao último teste da fila
_metricas_por_teste: list[str] = []


def _raiz_do_dono() -> Path | None:
    """Raiz cujo estado é o do DONO — a principal, vista de QUALQUER worktree.

    `TTS_DONO_DIR` vence (é o contrato que o runner já usa). Sem ele, a raiz sai do
    `--git-common-dir`: num worktree isso é `<principal>/.git`, então o pai é a
    raiz principal; num repo comum coincide com a própria raiz. None quando não dá
    para resolver (cópia sem `.git`, git antigo) — aí a guarda fica muda."""
    import subprocess

    env = os.environ.get("TTS_DONO_DIR")
    if env:
        return Path(env)
    raiz_repo = Path(__file__).resolve().parent.parent
    try:
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=raiz_repo,
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — guarda não pode quebrar a suíte
        return None
    comum = (r.stdout or "").strip()
    if r.returncode != 0 or not comum:
        return None
    caminho = Path(comum)
    if not caminho.is_absolute():
        caminho = raiz_repo / caminho
    return caminho.parent


def _impressao_do_dono() -> dict[str, tuple[str, int]]:
    """Hash+mtime dos arquivos REAIS do dono (a raiz principal, não o tmp).

    A sessão aponta os `TTS_*_PATH` para tmp para isolar — mas era o tmp que a
    guarda fotografava, então TODO teste que grava settings (como deve) virava
    "escrita no dono": ~37 linhas de ruído e `TTS_EXIGE_DONO_INTOCADO=1` vermelho
    falso (#685). O alvo é o arquivo da raiz principal; num worktree sem estado ele
    não existe e a entrada fica vazia — silêncio, não erro (#659)."""
    import hashlib

    import app

    raiz = _raiz_do_dono()
    caminhos = {"settings.json": app.SETTINGS_PATH,
                ".apikeys.json": app.APIKEYS_PATH,
                ".apikey": app.LEGACY_APIKEY_PATH,
                ".speaker-profiles.json": app.SPEAKER_PATH}
    saida: dict[str, tuple[str, int]] = {}
    for nome in _ARQUIVOS_DO_DONO:
        alvo = (raiz / nome) if raiz is not None else caminhos[nome]
        try:
            st = alvo.stat()
            saida[nome] = (hashlib.sha256(alvo.read_bytes()).hexdigest()[:12], st.st_mtime_ns)
        except OSError:
            saida[nome] = ("", 0)
    return saida


def _diferencas(antes: dict, depois: dict) -> list[str]:
    return sorted(n for n in _ARQUIVOS_DO_DONO if antes.get(n) != depois.get(n))


#: testes que tocam o arquivo REAL de propósito (provam a guarda e o worktree sem
#: estado): a escrita deles é esperada, não ruído — nem vermelho sob o knob
_DONO_ESPERADO = ("tests/test_chaves_isoladas.py::test_guarda_nomeia",
                  "tests/test_worktree_sem_estado.py::test_suite_passa_em_worktree")


def _atribuir_escritas(antes: dict, quem: str) -> None:
    if quem.startswith(_DONO_ESPERADO):
        return
    for nome in _diferencas(antes, _impressao_do_dono()):
        _escritas_no_dono.append(f"{quem} escreveu {nome}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Cerca o teste inteiro (setup+call+teardown) e nomeia quem sujou o dono."""
    try:
        antes = _impressao_do_dono()
    except Exception:  # noqa: BLE001 — guarda não pode quebrar a suíte
        yield
        return
    import app as _app

    try:
        _antes_metricas = {p.name for p in (_app.BASE / "state" / "metrics").glob("*.jsonl")}
    except Exception:  # noqa: BLE001
        _antes_metricas = set()
    yield
    try:
        _atribuir_escritas(antes, f"{item.nodeid}")
        _novos_m = ({p.name for p in (_app.BASE / "state" / "metrics").glob("*.jsonl")}
                    - _antes_metricas)
        if _novos_m:
            _metricas_por_teste.append(f"{item.nodeid} → {sorted(_novos_m)}")
    except Exception:  # noqa: BLE001
        pass


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
    if _escritas_no_dono:
        terminalreporter.write_line(
            f"[dono] {len(_escritas_no_dono)} escrita(s) no estado REAL do dono:")
        for linha in _escritas_no_dono[:20]:
            terminalreporter.write_line(f"[dono]   {linha}")
        if len(_escritas_no_dono) > 20:
            terminalreporter.write_line(f"[dono]   … +{len(_escritas_no_dono) - 20}")


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
    _sem_arquivo_na_arvore = not app.SETTINGS_PATH.exists()
    original = app.SETTINGS_PATH
    app.SETTINGS_PATH = destino
    if _sem_arquivo_na_arvore:
        # sem arquivo na árvore (worktree, #659): grava os DEFAULTS no destino, para
        # a célula que mexe no arquivo encontrar o mesmo conjunto de chaves de sempre
        app._save_settings()
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
    apikeys_destino.chmod(0o600)     # mesmo modo do arquivo real
    apikeys_original = app.APIKEYS_PATH
    app.APIKEYS_PATH = apikeys_destino
    # O `.apikey` (arquivo legado que o `run.sh` lê) TAMBÉM: `_save_apikeys` chama
    # `_sync_legacy_apikey_file()`, então sem isto a suíte reescrevia o do dono a
    # cada gravação de chave mesmo com o `.apikeys.json` isolado (#435).
    legado_destino = pasta / ".apikey"
    legado_destino.write_text(chave_teste + "\n")
    legado_destino.chmod(0o600)
    legado_original = app.LEGACY_APIKEY_PATH
    app.LEGACY_APIKEY_PATH = legado_destino
    # perfis de locutor: mesmo critério (o app grava em `_speaker_salva`)
    perfis_destino = pasta / ".speaker-profiles.json"
    if app.SPEAKER_PATH.exists():
        perfis_destino.write_bytes(app.SPEAKER_PATH.read_bytes())
    perfis_original = app.SPEAKER_PATH
    app.SPEAKER_PATH = perfis_destino
    # áudio de cadastro (#658): em tmp pela RAM E pelo ambiente — o diretório é
    # derivado no import, então subprocesso/cópia de boot recalculava e enchia
    # `state/speaker-audio/` do dono (15 pastas medidas).
    audio_destino = pasta / "speaker-audio"
    audio_destino.mkdir()
    audio_original = app.SPEAKER_AUDIO_DIR
    app.SPEAKER_AUDIO_DIR = audio_destino
    env_audio_original = os.environ.get("TTS_SPEAKER_AUDIO_DIR")
    os.environ["TTS_SPEAKER_AUDIO_DIR"] = str(audio_destino)
    # `outputs/` em tmp (#458): a suíte grava wav de verdade e o diretório do dono
    # acumulava milhares deles (medido: 3.509, 34 MB) sem poda possível — o teto é
    # do #430. O job herda o caminho da requisição, então isto vale também para os
    # testes de job.
    saida_destino = pasta / "outputs"
    saida_destino.mkdir()
    saida_original = app.OUTPUTS_DIR
    app.OUTPUTS_DIR = saida_destino
    # e por AMBIENTE também (#542): `app.OUTPUTS_DIR` só vale neste processo —
    # worker de job, cópia de boot e qualquer subprocesso recalculavam o caminho
    # e caíam no `outputs/` do dono (3.510 `t<hash>.wav` acumulados assim).
    env_saida_original = os.environ.get("TTS_OUTPUTS_DIR")
    os.environ["TTS_OUTPUTS_DIR"] = str(saida_destino)
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
    app.SPEAKER_AUDIO_DIR = audio_original
    app.SETTINGS_PATH = original
    app.OUTPUTS_DIR = saida_original
    app._ENV_API_KEY, app._ADMIN_API_KEY = env_api_original, admin_api_original
    if env_audio_original is None:
        os.environ.pop("TTS_SPEAKER_AUDIO_DIR", None)
    else:
        os.environ["TTS_SPEAKER_AUDIO_DIR"] = env_audio_original
    if env_saida_original is None:
        os.environ.pop("TTS_OUTPUTS_DIR", None)
    else:
        os.environ["TTS_OUTPUTS_DIR"] = env_saida_original
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


import signal as _signal

# Referências presas no import: um teste que troque `signal.getsignal` por
# monkeypatch/atribuição não pode cegar a restauração abaixo.
_GETSIGNAL = _signal.getsignal
_SIGNAL = _signal.signal


@pytest.fixture(autouse=True)
def _sinais_do_processo_intactos():
    """SIGTERM/SIGINT do processo do pytest voltam ao que eram depois de cada teste (#1071).

    `test_handler_encaminha_para_o_handler_anterior` chama o
    `_instala_rastro_de_sinal()` de verdade com um "anterior" falso que só anota:
    o handler ficava instalado no processo e engolia SIGTERM e SIGINT pelo resto
    da suíte. `kill`, Ctrl-C e o cancelamento do integrador não paravam nada (o
    hook do #1051 commitou depois de um kill). O mesmo vale para qualquer
    lifespan real que instale o rastro."""
    antes = {s: _GETSIGNAL(s) for s in (_signal.SIGTERM, _signal.SIGINT)}
    yield
    for s, h in antes.items():
        if _GETSIGNAL(s) is not h:
            try:
                _SIGNAL(s, h)
            except (ValueError, OSError):    # fora da thread principal: nada a fazer
                pass


@pytest.fixture(autouse=True)
def _apikeys_publicadas():
    """Republica o snapshot de chaves antes de cada teste (#799).

    `_exige_chave` lê o snapshot imutável sem lock; teste que mexe em `_apikeys`
    na mão (sem passar por `_save_apikeys`) deixaria o vizinho vendo o estado
    anterior."""
    import app

    app._apikeys_publica()
    yield


@pytest.fixture(autouse=True)
def _settings_restaurados_entre_testes():
    """O `_settings` global volta ao que era no início de cada teste (#1143).

    `_settings` é um dict de processo e vários testes chamam `app._apply_settings`
    direto, sem `monkeypatch`. O caso medido: `test_live_turns.py` deixava
    `live_barge_filtros.modelo.on = True` para o resto da sessão, e todo
    `/api/live/ws` seguinte esperava o aquecimento do classificador antes do
    `ready` — o `test_api` estourava o prazo de 20 s do `_setup_ok` conforme a
    ordem dos arquivos. A restauração é por chave: só volta o que mudou, com
    cópia profunda do original (quem guardou a referência do dict aninhado antes
    do teste continua apontando para o valor de antes, não para uma cópia nova
    de uma chave que ninguém mexeu). Chave criada pelo teste é removida.

    O snapshot é tirado no SETUP do teste, depois das fixtures de escopo maior:
    o que um fixture de módulo/sessão configurou faz parte do "antes"."""
    import copy

    import app

    antes = copy.deepcopy(app._settings)
    yield
    for chave in list(app._settings):
        if chave not in antes:
            app._settings.pop(chave, None)
    for chave, valor in antes.items():
        if app._settings.get(chave, _AUSENTE_1143) != valor:
            app._settings[chave] = copy.deepcopy(valor)


_AUSENTE_1143 = object()


@pytest.fixture(autouse=True)
def _registro_limpo_entre_testes():
    """Zera `_jobs` e o pool do dsh entre testes — o espiao de teardown é sinal.

    Antes, o job de um teste sobrava para o próximo e o espiao (`tests/_espiao.py`)
    falava em ~metade da suíte (667 linhas em 1332 testes, medido no #505): com
    ruído constante, vazamento NOVO não se distinguia de acúmulo conhecido.

    O espiao classifica desde o #582: `live`/`dsh` são ALARME (registry), `jobs` é
    só ESTADO e só sai quando o número muda. O pre-warm do dsh em voo é aterrissado
    ANTES do relatório (`pytest_runtest_makereport` no espiao), senão o cliente cai
    no pool depois do teste que o pediu e a conta vai para o vizinho.

    Este fixture zera no SETUP; a limpeza final é do espiao, que reporta antes de
    zerar. Quem mover isso para cá (fixture teardown) cega o espiao para
    `jobs`/`dsh` (#575). O que um teste precisa herdar de outro passa por
    `monkeypatch`/fixture própria.
    """
    import app

    with app._jobs_lock:
        app._jobs.clear()
    app._chat_dsh_livres.clear()
    # Freio de força bruta (#785): o contador é GLOBAL por IP e a suíte inteira usa
    # o mesmo `testclient`. Passadas ~20 falhas de auth o IP fica travado por 60 s
    # e o resto da suíte vira 429 — dependendo de a rodada caber na janela. Cada
    # teste começa com o freio zerado.
    with app._auth_falhas_lock:
        app._auth_falhas.clear()
    yield
    # a limpeza FINAL é do espiao (`tests/_espiao.py`): ele reporta antes de
    # zerar. Deixar aqui também cegava o espiao (#575).


def _retrato_outputs(pasta) -> set:
    """Nomes do diretório real, sem recursão (a suíte só cria arquivo solto)."""
    try:
        return {p.name for p in pasta.iterdir()}
    except OSError:
        return set()


@pytest.fixture(scope="session", autouse=True)
def _outputs_do_dono_intocado():
    """A suíte não pode escrever no `outputs/` REAL do dono (#542).

    O caminho é redirecionado para tmp desde o #458, mas o isolamento era só de
    RAM: worker de job e subprocesso recalculavam o caminho e deixavam
    `t<hash>.wav` (0,1 s) no diretório do dono — 3.510 deles foram medidos lá.
    Agora o caminho também vai por `TTS_OUTPUTS_DIR` (fixture de estado) e esta
    guarda reprova a rodada se um artefato NOSSO reaparecer no diretório real.

    O que é do dono e não nosso (o app vivo gerando áudio, a poda periódica dele)
    entra como aviso, não como falha. O AVISO é `print` no teardown: ele só
    aparece com `-s` (o `pytest.fail` do artefato nosso aparece sempre, então o que
    reprova não depende de flag). Reprovam só as DUAS assinaturas do próprio
    código sob teste: `t<hash10>.wav` (o job falso do `speech_queue_bypass`) e
    `.live-<thread>-<ns>.wav` (temporário antigo do STT do Live: desde o #571 ele
    mora no TMPDIR do sistema, e a assinatura fica para as rodadas com código
    anterior e para quem voltar a gravar ali).
    """
    import re

    import app

    alvo = app.BASE / "outputs"
    antes = _retrato_outputs(alvo)
    yield
    novos = _retrato_outputs(alvo) - antes
    sumidos = antes - _retrato_outputs(alvo)
    nossos = sorted(n for n in novos
                    if re.fullmatch(r"(t[0-9a-f]{10}|\.live-\d+-\d+)\.wav", n))
    outros = sorted(novos - set(nossos))
    if sumidos or outros:
        print(f"\n[outputs] o diretório real mudou por fora da suíte: "
              f"+{len(outros)}/-{len(sumidos)} arquivo(s) "
              f"(app do dono rodando? se não, investigue)")
    if nossos:
        pytest.fail(
            "a suíte escreveu no `outputs/` REAL do dono (#542): "
            + ", ".join(nossos[:10])
            + (f" (+{len(nossos) - 10})" if len(nossos) > 10 else "")
            + " — algum caminho não está seguindo `TTS_OUTPUTS_DIR`",
            pytrace=False)


@pytest.fixture(scope="session", autouse=True)
def _estado_do_dono_intocado():
    """O Application Support REAL não pode ser tocado pela suíte (#721).

    O retrato é do IMPORT do conftest (antes de qualquer `import app`, que é quem
    migra/grava); comparar aqui pega escrita, remoção e arquivo novo — o mesmo dano
    que apagou o settings 3 vezes, agora num diretório que a suíte não deveria nem
    enxergar."""
    yield
    depois = _retrato_app_support()
    if depois != _RETRATO_APP_SUPPORT:
        novos = sorted(set(depois) - set(_RETRATO_APP_SUPPORT))
        mudados = sorted(k for k in _RETRATO_APP_SUPPORT
                         if depois.get(k) != _RETRATO_APP_SUPPORT[k])
        pytest.fail(
            "a suíte tocou o estado REAL do dono em Application Support (#721): "
            + "; ".join(filter(None, [
                "novos: " + ", ".join(novos[:5]) if novos else "",
                "alterados: " + ", ".join(mudados[:5]) if mudados else ""]))
            + " — falta `TTS_STATE_DIR` em algum caminho",
            pytrace=False)
    if _TMP_ESTADO.startswith(tempfile.gettempdir()):   # não apagar o real
        shutil.rmtree(_TMP_ESTADO, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _audio_cadastro_do_dono_intocado():
    """A suíte não pode gravar em `state/speaker-audio` REAL do dono (#658).

    `enroll` copia a amostra aceita para lá; sem `TTS_SPEAKER_AUDIO_DIR` cada
    teste que cadastra deixa pasta (15 medidas). Pasta NOVA reprova."""
    import app

    alvo = app.BASE / "state" / "speaker-audio"
    antes = {p.name for p in alvo.iterdir()} if alvo.is_dir() else set()
    yield
    novos = sorted(({p.name for p in alvo.iterdir()} if alvo.is_dir() else set()) - antes)
    if novos:
        pytest.fail(
            "a suíte criou pasta no `state/speaker-audio` REAL do dono (#658): "
            + ", ".join(novos[:10])
            + " — algum caminho não está seguindo `TTS_SPEAKER_AUDIO_DIR`",
            pytrace=False)


@pytest.fixture(scope="session", autouse=True)
def _metricas_do_dono_intocadas():
    """A suíte não pode gravar no `state/metrics` REAL do dono (#610).

    O `metrics_hist.DIR` é constante de módulo, lida no import: quem não isolar por
    `TTS_METRICS_DIR` (exportada no `pytest_configure`) engorda o histórico de
    operação do dono com rota de teste. Arquivo NOVO lá é nosso e reprova.
    CRESCIMENTO de arquivo do dia é só aviso: o app do dono vivo escreve no mesmo
    arquivo, então não dá para separar pelo tamanho."""
    import app

    alvo = app.BASE / "state" / "metrics"
    antes = {p.name: p.stat().st_size for p in alvo.glob("*.jsonl")}
    yield
    depois = {p.name: p.stat().st_size for p in alvo.glob("*.jsonl")}
    novos = sorted(set(depois) - set(antes))
    if novos:
        pytest.fail(
            "a suíte escreveu no `state/metrics` REAL do dono (#610): "
            + ", ".join(novos[:10])
            + ("\n  por teste: " + "; ".join(_metricas_por_teste[-5:])
               if _metricas_por_teste else "\n  (sem atribuição por teste)")
            + " — algum caminho não está seguindo `TTS_METRICS_DIR`",
            pytrace=False)
    cresceram = sorted(n for n, tam in depois.items() if tam > antes.get(n, 0))
    if cresceram:
        print(f"\n[metricas] o histórico real cresceu por fora da suíte: "
              f"{', '.join(cresceram[:5])} (app do dono rodando? se não, investigue)")
