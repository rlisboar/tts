"""Prazo das separações que seguram o `_gen_lock` (#924).

Filho mudo não pode prender a trava para sempre. O leitor do separador de voz
acorda no `select`; o worker de música morre no `timeout` do `subprocess.run`.
A trava continua em volta da chamada: os dois filhos usam MPS.
"""

import json
import subprocess
import sys
import time
import types

import numpy as np
import pytest

import app
import separa_voz


def _mudo() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def _mata(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=2)


def test_separa_filho_mudo_morre_no_prazo(monkeypatch, capsys):
    monkeypatch.setattr(separa_voz, "TIMEOUT_S", 0.4)
    proc = _mudo()
    separa_voz._proc = proc
    t0 = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="filho mudo"):
            separa_voz._le_ate_resposta(proc, 1)
    finally:
        _mata(proc)
        separa_voz._proc = None
    assert time.monotonic() - t0 < 3.0
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
