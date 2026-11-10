"""#1071: a suíte não pode engolir SIGTERM/SIGINT.

`test_handler_encaminha_para_o_handler_anterior` (test_ciclo_vida) instala o
rastro de sinal do app de verdade, com um "anterior" falso que só anota. Sem
restauração, o handler ficava no processo e o resto da suíte ignorava `kill` e
Ctrl-C. A célula roda, num pytest FILHO, esse teste e depois um teste que dorme;
manda o sinal quando o segundo começa e cobra que o filho morra em ≤5 s.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VAZOU = "tests/test_ciclo_vida.py::test_handler_encaminha_para_o_handler_anterior"


def _sinais_padrao() -> None:
    """No filho: `SIGINT`/`SIGTERM` de volta ao padrão, antes do exec (#1096).

    `Popen` já restaura `SIGPIPE`/`SIGXFSZ`, mas NÃO estes dois. Sem isto o filho
    herda a disposição do pai — e um pai sem job control (`nohup`, `&`, runner de
    CI) nasce com `SIGINT=SIG_IGN`, de modo que o `kill -INT` não mata ninguém e
    a célula reprova por herança, não por defeito.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


@pytest.mark.timeout(240)
@pytest.mark.parametrize("sinal", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_suite_morre_no_sinal_depois_do_teste_que_instala_o_rastro(tmp_path, sinal):
    marca = tmp_path / "dormindo"
    dorme = tmp_path / "test_dorme_1071.py"
    dorme.write_text(
        "import os, pathlib, time\n"
        "def test_dorme():\n"
        f"    pathlib.Path({str(marca)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(120)\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    filho = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
         f"--rootdir={REPO}", "-c", str(REPO / "pytest.ini"), VAZOU, str(dorme)],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        preexec_fn=_sinais_padrao)
    try:
        t0 = time.monotonic()
        while not marca.exists():
            assert filho.poll() is None, f"o filho saiu antes do teste que dorme:\n{filho.stdout.read()}"
            assert time.monotonic() - t0 < 180, "o teste que dorme não começou em 180 s"
            time.sleep(0.2)
        time.sleep(0.5)                       # já dentro do sleep(120)
        filho.send_signal(sinal)
        try:
            filho.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{sinal.name} engolido: o pytest filho seguiu vivo 5 s depois "
                        "(handler do rastro de sinal vazou do teste anterior)")
        assert filho.returncode != 0, f"{sinal.name} não interrompeu (rc=0)"
    finally:
        if filho.poll() is None:
            filho.kill()
            filho.wait()


def test_restauracao_nao_depende_dos_atributos_do_modulo_signal():
    """Os pinos `_GETSIGNAL`/`_SIGNAL` são presos no import (#1096, não-bloqueante).

    Trocar `signal.getsignal`/`signal.signal` no MÓDULO (em vez de `monkeypatch`)
    não pode cegar a restauração do fixture: ela usa os pinos, não o atributo.

    O arquivo precisa nascer DENTRO de `tests/` — conftest é carregado por
    ancestralidade de diretório, e um `tmp_path` não tem o nosso. O filho roda com
    `-p no:timeout`: o próprio `pytest-timeout` chama `signal.signal` no teardown
    DELE, depois do nosso, e estouraria sozinho.
    """
    temp = REPO / "tests" / f"_tmp_pino_1071_{os.getpid()}.py"
    temp.write_text(
        "import signal\n"
        "def test_troca_os_atributos_crus():\n"
        "    signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "    signal.getsignal = None\n"
        "    signal.signal = None\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
             "-p", "no:timeout", f"--rootdir={REPO}", "-c", str(REPO / "pytest.ini"),
             str(temp)],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180)
    finally:
        temp.unlink(missing_ok=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
