"""#1130 — trava atômica de máquina para a suíte cheia (`tools/fila_suite.sh`).

Antes, cada espera checava "máquina vazia" por `pgrep` e largava depois: checar e largar
não são atômicos, então duas esperas passavam juntas. Aqui quem larga é quem TEM a trava.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FILA = REPO / "tools" / "fila_suite.sh"
BASH = shutil.which("bash")


def _env(tmp_path, **extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("TTS_FILA")}
    e.update(TTS_FILA_DIR=str(tmp_path / "locks"), TTS_AGENTE="teste", HOME=str(tmp_path))
    e.update(extra)
    return e


_VIVOS: list[subprocess.Popen] = []


def _derruba_vivos():
    """Derruba o GRUPO de cada processo que as células abriram (start_new_session: pgid == pid)."""
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


@pytest.fixture(autouse=True)
def _nao_deixa_orfao():
    """#1130 (gate do qa): célula que falha NÃO pode deixar `fila_suite.sh` órfão (já deixou,
    e o órfão segura a trava dos outros). `try/finally` de verdade: derruba o GRUPO de cada
    processo que a célula abriu, passe ou falhe."""
    yield
    _derruba_vivos()


def _roda(tmp_path, *cmd, **extra):
    p = subprocess.Popen([BASH, str(FILA), "roda", "--", *cmd], env=_env(tmp_path, **extra),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    _VIVOS.append(p)
    return p


def _marca(tmp_path, nome, seg):
    """Comando que registra INICIO/FIM com timestamp fino num arquivo comum."""
    log = tmp_path / "ordem.log"
    return ["sh", "-c",
            f'echo "{nome} inicio $(python3 -c \'import time;print(time.time())\')" >> {log}; '
            f'sleep {seg}; '
            f'echo "{nome} fim $(python3 -c \'import time;print(time.time())\')" >> {log}']


def _ordem(tmp_path):
    out = []
    for l in (tmp_path / "ordem.log").read_text().splitlines():
        n, ev, t = l.split()
        out.append((n, ev, float(t)))
    return out


pytestmark = pytest.mark.skipif(BASH is None, reason="precisa de bash")


def test_duas_largadas_simultaneas_so_uma_roda_por_vez(tmp_path):
    ps = [_roda(tmp_path, *_marca(tmp_path, n, 1.5)) for n in ("a", "b", "c")]
    for p in ps:
        assert p.wait(timeout=60) == 0, p.stdout.read()
    ev = _ordem(tmp_path)
    assert len(ev) == 6
    # nunca dois "inicio" sem um "fim" no meio: execução estritamente serial
    ativos = 0
    for _, e, _ in sorted(ev, key=lambda x: x[2]):
        ativos += 1 if e == "inicio" else -1
        assert 0 <= ativos <= 1, ev


def test_sem_a_trava_as_duas_rodam_juntas(tmp_path):
    """Mordida: TTS_FILA=0 desliga a trava e a sobreposição aparece (a célula acima tem dente)."""
    ps = [_roda(tmp_path, *_marca(tmp_path, n, 1.5), TTS_FILA="0") for n in ("a", "b")]
    for p in ps:
        assert p.wait(timeout=60) == 0
    ev = sorted(_ordem(tmp_path), key=lambda x: x[2])
    assert [e for _, e, _ in ev][:2] == ["inicio", "inicio"], ev


def test_status_mostra_o_dono_e_depois_livre(tmp_path):
    p = _roda(tmp_path, "sleep", "4")
    for _ in range(50):
        if (tmp_path / "locks" / "suite" / "cmd").exists():
            break
        time.sleep(0.1)
    r = subprocess.run([BASH, str(FILA), "status"], env=_env(tmp_path), capture_output=True, text=True)
    assert "OCUPADA" in r.stdout and "agente=teste" in r.stdout and "sleep 4" in r.stdout, r.stdout
    p.wait(timeout=30)
    r2 = subprocess.run([BASH, str(FILA), "status"], env=_env(tmp_path), capture_output=True, text=True)
    assert "livre" in r2.stdout, r2.stdout


def test_trava_orfa_de_pid_morto_e_recuperada(tmp_path):
    t = tmp_path / "locks" / "suite"
    t.mkdir(parents=True)
    morto = subprocess.Popen(["true"]); morto.wait()
    (t / "pid").write_text(f"{morto.pid}\n")
    (t / "agente").write_text("fantasma\n")
    p = _roda(tmp_path, "echo", "rodou")
    out = p.communicate(timeout=30)[0]
    assert p.returncode == 0, out
    assert "recuperada" in out and "rodou" in out, out
    assert not (tmp_path / "locks" / "suite").exists()


def test_recuperadores_simultaneos_de_orfa_nao_quebram_a_exclusao(tmp_path):
    t = tmp_path / "locks" / "suite"
    t.mkdir(parents=True)
    morto = subprocess.Popen(["true"]); morto.wait()
    (t / "pid").write_text(f"{morto.pid}\n")
    ps = [_roda(tmp_path, *_marca(tmp_path, n, 1.0)) for n in ("a", "b", "c", "d")]
    for p in ps:
        assert p.wait(timeout=60) == 0, p.stdout.read()
    ev = sorted(_ordem(tmp_path), key=lambda x: x[2])
    ativos = 0
    for _, e, _ in ev:
        ativos += 1 if e == "inicio" else -1
        assert 0 <= ativos <= 1, ev


@pytest.mark.parametrize("sinal", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_sinal_derruba_a_arvore_inteira_e_solta_a_trava(tmp_path, sinal):
    """Gate do qa: `kill` só do pid deixava o NETO vivo. Aqui a árvore é real:
    `sh -c 'sleep 60 & echo $! > neto.pid; echo $$ > filho.pid; wait'` — o `sleep` é neto
    da fila. Depois do sinal, filho E neto têm de estar mortos, e a trava solta."""
    filho_f, neto_f = tmp_path / "filho.pid", tmp_path / "neto.pid"
    p = _roda(tmp_path, "sh", "-c",
              f"sleep 60 & echo $! > {neto_f}; echo $$ > {filho_f}; wait")
    for _ in range(80):
        if neto_f.exists() and filho_f.exists() and filho_f.read_text().strip():
            break
        time.sleep(0.1)
    filho, neto = int(filho_f.read_text()), int(neto_f.read_text())
    p.send_signal(sinal)
    p.wait(timeout=30)
    assert p.returncode in (128 + sinal, -sinal), p.returncode
    for nome, pid in (("filho", filho), ("neto", neto)):
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"{nome} (pid {pid}) sobreviveu ao sinal {sinal.name}")
    assert not (tmp_path / "locks" / "suite").exists()      # trava solta


def test_o_fixture_de_limpeza_mata_de_verdade_o_grupo_que_a_celula_abandonou(tmp_path):
    """Nit do qa (#1131): a célula anterior só abandonava o processo e o teardown rodava DEPOIS
    da asserção — não provava que o fixture mata. Aqui chamamos a mesma função de limpeza e
    exigimos a morte do filho E do neto; sem o `killpg` na limpeza esta célula falha."""
    filho_f, neto_f = tmp_path / "filho.pid", tmp_path / "neto.pid"
    p = _roda(tmp_path, "sh", "-c", f"sleep 60 & echo $! > {neto_f}; echo $$ > {filho_f}; wait")
    for _ in range(80):
        if neto_f.exists() and filho_f.exists() and filho_f.read_text().strip():
            break
        time.sleep(0.1)
    filho, neto = int(filho_f.read_text()), int(neto_f.read_text())
    assert p.poll() is None
    _derruba_vivos()                          # exatamente o que o teardown do autouse faz
    for pid in (p.pid, filho, neto):
        with pytest.raises(ProcessLookupError):
            for _ in range(30):
                os.kill(pid, 0)
                time.sleep(0.1)
            os.kill(pid, 0)


def test_reentrante_hook_dentro_de_suite_travada_so_executa(tmp_path):
    interno = f'{BASH} {FILA} roda -- echo aninhado'
    p = _roda(tmp_path, "sh", "-c", interno)
    out = p.communicate(timeout=30)[0]
    assert p.returncode == 0, out
    assert "aninhado" in out and out.count("com a trava") == 1, out


def test_espera_esgotada_sai_75(tmp_path):
    seg = _roda(tmp_path, "sleep", "6")
    for _ in range(50):
        if (tmp_path / "locks" / "suite" / "cmd").exists():
            break
        time.sleep(0.1)
    r = subprocess.run([BASH, str(FILA), "roda", "--", "echo", "x"],
                       env=_env(tmp_path, TTS_FILA_ESPERA="1"), capture_output=True, text=True)
    assert r.returncode == 75, (r.returncode, r.stdout, r.stderr)
    seg.wait(timeout=30)


def test_propaga_o_codigo_de_saida_e_solta(tmp_path):
    p = _roda(tmp_path, "sh", "-c", "exit 7")
    p.communicate(timeout=30)
    assert p.returncode == 7
    assert not (tmp_path / "locks" / "suite").exists()


def test_hook_roda_a_suite_cheia_pela_trava():
    codigo = [l for l in (REPO / ".githooks" / "pre-commit").read_text().splitlines()
              if not l.lstrip().startswith("#")]
    assert any("tools/fila_suite.sh roda --" in l and "pytest tests/ -q" in l for l in codigo), codigo

def test_mutex_de_recuperacao_orfao_expira_e_nao_trava_ninguem(tmp_path):
    """Gate do PM: se o recuperador morrer (SIGKILL) DENTRO do mutex `.recupera`, ele fica no
    disco. Não pode travar todo mundo para sempre: com mais de 10 s de idade o próximo
    recuperador o apaga. Aqui a trava dona está morta E o `.recupera` órfão é antigo."""
    raiz = tmp_path / "locks"
    (raiz / "suite").mkdir(parents=True)
    morto = subprocess.Popen(["true"]); morto.wait()
    (raiz / "suite" / "pid").write_text(f"{morto.pid}\n")
    rec = raiz / ".recupera"
    rec.mkdir()
    antigo = time.time() - 60
    os.utime(rec, (antigo, antigo))
    p = _roda(tmp_path, "echo", "rodou", TTS_FILA_ESPERA="20")
    out = p.communicate(timeout=40)[0]
    assert p.returncode == 0, out
    assert "rodou" in out and "recuperada" in out, out
    assert not rec.exists()


def test_mutex_de_recuperacao_recente_nao_e_roubado(tmp_path):
    """O outro lado do teto: um `.recupera` RECENTE é de um recuperador vivo e é respeitado
    (a fila espera, e sai 75 pelo teto de espera em vez de roubar o mutex)."""
    raiz = tmp_path / "locks"
    (raiz / "suite").mkdir(parents=True)
    morto = subprocess.Popen(["true"]); morto.wait()
    (raiz / "suite" / "pid").write_text(f"{morto.pid}\n")
    (raiz / ".recupera").mkdir()                       # mtime agora
    r = subprocess.run([BASH, str(FILA), "roda", "--", "echo", "x"],
                       env=_env(tmp_path, TTS_FILA_ESPERA="2"), capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    assert r.returncode == 75, (r.returncode, r.stdout, r.stderr)
    assert (raiz / ".recupera").exists()
