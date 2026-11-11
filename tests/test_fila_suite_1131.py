"""#1131 — seguimento da trava (`tools/fila_suite.sh`): guarda do conftest, `status` com
quem espera, fallback sem job control e o `.gitattributes` do MORDIDAS."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FILA = REPO / "tools" / "fila_suite.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="precisa de bash")

_VIVOS: list[subprocess.Popen] = []


@pytest.fixture(autouse=True)
def _limpa():
    yield
    for p in _VIVOS:
        for sinal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sinal)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.2)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    _VIVOS.clear()


def _env(tmp_path, **extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("TTS_FILA") and k != "CI"}
    e.update(TTS_FILA_DIR=str(tmp_path / "locks"), HOME=str(tmp_path))
    e.update(extra)
    return e


def _roda(tmp_path, *cmd, agente="ag", **extra):
    p = subprocess.Popen([BASH, str(FILA), "roda", "--", *cmd],
                         env=_env(tmp_path, TTS_AGENTE=agente, **extra),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    _VIVOS.append(p)
    return p


def _status(tmp_path):
    return subprocess.run([BASH, str(FILA), "status"], env=_env(tmp_path), capture_output=True,
                          text=True).stdout


def _espera(cond, n=100):
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.1)
    return False


# --- (a) guarda do conftest ----------------------------------------------------------

def test_logica_do_guarda_cobertura_e_escapes():
    from _guarda_trava import suite_cheia_fora_da_trava as f
    todos = {f"test_{i}.py" for i in range(100)}
    cheia = {f"test_{i}.py" for i in range(95)}
    assert f(cheia, todos, {}) is True                      # suíte cheia sem a trava → barra
    assert f({"test_1.py"}, todos, {}) is False             # rodada curta nunca
    assert f(set(list(todos)[:50]), todos, {}) is False     # metade (-k estreito) não é cheia
    assert f(cheia, todos, {"TTS_FILA_SEGURA": "1"}) is False   # dentro da trava
    assert f(cheia, todos, {"TTS_FILA": "0"}) is False          # escape explícito
    assert f(cheia, todos, {"CI": "true"}) is False             # runner do CI
    assert f(set(), set(), {}) is False


def _coleta(tmp_path, *args, **extra):
    env = _env(tmp_path, **extra)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", *args, "--collect-only", "-q",
                           "-p", "no:cacheprovider"], cwd=str(REPO), env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=300)


def test_suite_cheia_sem_a_trava_aborta_com_rc_75_e_ensina_o_caminho(tmp_path):
    r = _coleta(tmp_path, "tests/")
    assert r.returncode == 75, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert "tools/fila_suite.sh roda --" in r.stdout + r.stderr


def test_suite_cheia_dentro_da_trava_e_com_escape_passa(tmp_path):
    assert _coleta(tmp_path, "tests/", TTS_FILA_SEGURA="1").returncode == 0
    assert _coleta(tmp_path, "tests/", TTS_FILA="0").returncode == 0


def test_rodada_curta_nunca_e_barrada(tmp_path):
    r = _coleta(tmp_path, "tests/test_fila_suite_1131.py")
    assert r.returncode == 0, (r.stdout[-300:], r.stderr[-300:])


# --- (b) status lista quem espera ------------------------------------------------------

def test_status_lista_quem_espera_e_limpa_depois(tmp_path):
    dono = _roda(tmp_path, "sleep", "6", agente="dono")
    assert _espera(lambda: (tmp_path / "locks" / "suite" / "cmd").exists())
    esp = _roda(tmp_path, "echo", "vez", agente="esperador")
    assert _espera(lambda: (tmp_path / "locks" / "espera").exists()
                   and list((tmp_path / "locks" / "espera").iterdir()))
    out = _status(tmp_path)
    assert "agente=dono" in out and "espera #1" in out and "agente=esperador" in out, out
    assert "echo vez" in out, out
    dono.wait(timeout=30)
    esp.communicate(timeout=30)
    assert esp.returncode == 0
    out2 = _status(tmp_path)
    assert "espera #" not in out2, out2          # registro saiu junto com a espera
    assert "livre" in out2, out2


def test_espera_de_pid_morto_e_varrida_pelo_status(tmp_path):
    """SIGKILL no esperador não roda trap: o registro fica. O `status` não pode mentir."""
    dono = _roda(tmp_path, "sleep", "20", agente="dono")
    assert _espera(lambda: (tmp_path / "locks" / "suite" / "cmd").exists())
    esp = _roda(tmp_path, "echo", "x", agente="fantasma")
    assert _espera(lambda: (tmp_path / "locks" / "espera").exists()
                   and list((tmp_path / "locks" / "espera").iterdir()))
    os.killpg(esp.pid, signal.SIGKILL)
    esp.wait(timeout=10)
    assert len(list((tmp_path / "locks" / "espera").iterdir())) == 1       # sobrou o arquivo
    out = _status(tmp_path)
    assert "fantasma" not in out and "espera #" not in out, out            # varrido
    assert not list((tmp_path / "locks" / "espera").iterdir())
    assert dono.poll() is None


def test_espera_que_recebe_sinal_sai_do_registro(tmp_path):
    dono = _roda(tmp_path, "sleep", "20", agente="dono")
    assert _espera(lambda: (tmp_path / "locks" / "suite" / "cmd").exists())
    esp = _roda(tmp_path, "echo", "x", agente="cancelada")
    assert _espera(lambda: (tmp_path / "locks" / "espera").exists()
                   and list((tmp_path / "locks" / "espera").iterdir()))
    esp.send_signal(signal.SIGTERM)
    esp.wait(timeout=10)
    assert not list((tmp_path / "locks" / "espera").iterdir())
    assert dono.poll() is None


# --- (c2) fallback sem job control -----------------------------------------------------

def test_sem_job_control_o_sinal_ainda_alcanca_os_filhos_diretos_e_avisa(tmp_path):
    filho_f, neto_f = tmp_path / "filho.pid", tmp_path / "neto.pid"
    p = _roda(tmp_path, "sh", "-c", f"sleep 60 & echo $! > {neto_f}; echo $$ > {filho_f}; wait",
              TTS_FILA_SEM_JOBCTRL="1")
    assert _espera(lambda: neto_f.exists() and filho_f.exists() and filho_f.read_text().strip())
    filho, neto = int(filho_f.read_text()), int(neto_f.read_text())
    p.send_signal(signal.SIGTERM)
    out = p.communicate(timeout=30)[0]
    assert "sem job control" in out, out
    for pid in (filho, neto):
        with pytest.raises(ProcessLookupError):
            for _ in range(30):
                os.kill(pid, 0)
                time.sleep(0.1)
            os.kill(pid, 0)
    assert not (tmp_path / "locks" / "suite").exists()


# --- .gitattributes ----------------------------------------------------------------------

def test_gitattributes_junta_o_mordidas_sem_conflito(tmp_path):
    attrs = (REPO / ".gitattributes").read_text()
    assert "tests/MORDIDAS.md merge=union" in attrs
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / ".gitattributes").write_text(attrs)

    def g(*a):
        r = subprocess.run(["git", "-c", "user.email=x@y", "-c", "user.name=x", *a],
                           cwd=str(repo), capture_output=True, text=True)
        return r

    g("init", "-q", "-b", "main")
    (repo / "tests" / "MORDIDAS.md").write_text("base\n")
    g("add", "-A")
    g("commit", "-qm", "base")
    g("checkout", "-qb", "a")
    (repo / "tests" / "MORDIDAS.md").write_text("base\ncoisa do A\n")
    g("commit", "-qam", "a")
    g("checkout", "-q", "main")
    (repo / "tests" / "MORDIDAS.md").write_text("base\ncoisa do MAIN\n")
    g("commit", "-qam", "main")
    r = g("rebase", "main", "a")
    assert r.returncode == 0, r.stdout + r.stderr
    txt = (repo / "tests" / "MORDIDAS.md").read_text()
    assert "coisa do A" in txt and "coisa do MAIN" in txt and "<<<<<<<" not in txt


def test_nenhum_teste_recarrega_o_conftest():
    """Regressão do commit do #1131 (falhou duas vezes): uma célula que carregava o
    `conftest.py` por `importlib` REEXECUTAVA o módulo e reinicializava o ambiente isolado
    (`TTS_OUTPUTS_DIR`/`TTS_STATE_DIR`), quebrando testes seguintes do mesmo processo. A lógica
    pura mora em `tests/_guarda_trava.py` e é importada normalmente."""
    achados = []
    for arq in (REPO / "tests").glob("*.py"):
        if arq.name == "test_fila_suite_1131.py":
            continue
        txt = arq.read_text()
        # `spec_from_file_location(..., <algo com conftest.py>)`: o carregamento do arquivo
        if re.search(r"spec_from_file_location\([^)]*conftest\.py", txt):
            achados.append(arq.name)
    assert not achados, achados
