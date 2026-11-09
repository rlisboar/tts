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
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
