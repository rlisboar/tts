"""Prazo das separações que seguram o `_gen_lock` (#924).

Filho mudo não pode prender a trava para sempre. O leitor do separador de voz
acorda no `select`; o worker de música morre no `timeout` do `subprocess.run`.
A trava continua em volta da chamada: os dois filhos usam MPS.
"""

import json
import os
import subprocess
import sys
import threading
import time
import types

import numpy as np
import pytest

import app
import separa_voz

PRAZO_S = 0.4
# O terminate espera no máximo 2 s. Os 120 s de produção ficam de fora.
TETO_S = PRAZO_S + 2.5


def _mudo() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def _mata(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=2)


def _em_ate(fn, teto: float, proc: subprocess.Popen):
    """Se a leitura voltar a bloquear no readline, a célula falha em vez de pendurar.

    Mesmo desenho de `test_marca_filho_mudo._em_ate`: a mordida corre noutra
    thread e o join tem teto. O pytest-timeout não desarma um readline preso.
    """
    caixa: dict = {}

    def corre() -> None:
        try:
            caixa["valor"] = fn()
        except BaseException as exc:  # noqa: BLE001 — a thread devolve o erro
            caixa["erro"] = exc

    fio = threading.Thread(target=corre, daemon=True)
    fio.start()
    fio.join(teto)
    if fio.is_alive():
        if proc.poll() is None:
            proc.kill()
        fio.join(2)
        pytest.fail(f"a leitura não voltou em {teto:g}s")
    if "erro" in caixa:
        raise caixa["erro"]
    return caixa.get("valor")


def test_separa_filho_mudo_morre_no_prazo(monkeypatch, capsys):
    monkeypatch.setattr(separa_voz, "TIMEOUT_S", PRAZO_S)
    proc = _mudo()
    separa_voz._proc = proc
    t0 = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="filho mudo"):
            _em_ate(lambda: separa_voz._le_ate_resposta(proc, 1), TETO_S, proc)
    finally:
        _mata(proc)
        separa_voz._proc = None
    assert time.monotonic() - t0 < TETO_S
    assert proc.poll() is not None
    assert "filho mudo" in capsys.readouterr().out


def test_separa_ignora_aquecimento_e_devolve_o_id():
    codigo = (
        "import sys; "
        "sys.stdout.write('{\"tipo\": \"pronto\"}\\n{\"id\": 3}\\n'); "
        "sys.stdout.flush(); "
        "sys.stdin.read()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", codigo],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        linha = separa_voz._le_ate_resposta(proc, 3, prazo=2.0)
    finally:
        _mata(proc)
    assert json.loads(linha)["id"] == 3


def test_separa_resposta_partida_em_duas_leituras(monkeypatch):
    """`_casa_id` acumula: o id pode chegar cortado no meio de dois `os.read`."""
    codigo = (
        "import sys; "
        "sys.stdout.write('{\"id\": 4}\\n'); "
        "sys.stdout.flush(); "
        "sys.stdin.read()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", codigo],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    real = os.read
    cortes = [6]  # '{"id":' numa leitura, ' 4}\n' na seguinte

    def read_partido(fd, n, cortes=cortes, real=real):
        if cortes and fd == proc.stdout.fileno():
            n = min(n, cortes.pop(0))
        return real(fd, n)

    monkeypatch.setattr(os, "read", read_partido)
    try:
        linha = _em_ate(
            lambda: separa_voz._le_ate_resposta(proc, 4, prazo=2.0), 3.0, proc)
    finally:
        _mata(proc)
    assert cortes == []
    assert json.loads(linha) == {"id": 4}


def test_musica_filho_mudo_solta_a_trava(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "demucs", types.ModuleType("demucs"))
    monkeypatch.setattr(app, "_DEMUCS_DIR", tmp_path)
    monkeypatch.setattr(app, "_MUSICA_TIMEOUT_S", 0.4)
    (tmp_path / "htdemucs.yaml").write_text("x\n", encoding="utf-8")
    real = subprocess.run
    viu: dict = {}

    def boom(_cmd, **kwargs):
        viu["durante"] = app._gen_lock._is_owned()
        viu["timeout"] = kwargs.get("timeout")
        return real(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            capture_output=True, timeout=kwargs["timeout"],
        )

    monkeypatch.setattr(subprocess, "run", boom)
    t0 = time.monotonic()
    saida = app._separa_musica(np.zeros(1600, dtype=np.float32), 16000)
    assert saida is None
    assert viu["durante"] is True
    assert viu["timeout"] == 0.4
    assert time.monotonic() - t0 < 3.0
    assert not app._gen_lock._is_owned()
    assert "filho mudo" in capsys.readouterr().out
    assert "filho mudo" in app._musica_motivo()
