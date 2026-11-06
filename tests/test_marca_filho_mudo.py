"""Filho da marca mudo não segura a geração (#915).

O job local chama `marca_wav` dentro do `_gen_lock`. O `readline` antigo não
voltava. Aqui o filho não escreve: a geração solta o áudio cru dentro do
prazo, o pid morre e o pedido seguinte sobe outro.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import marca_dagua  # noqa: E402

PRAZO_S = 0.4
# O terminate espera no máximo 2 s. Os 60 s de produção ficam de fora.
TETO_S = PRAZO_S + 2.5


def _vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _em_ate(fn, teto: float):
    """Se a leitura voltar a bloquear, a célula falha em vez de pendurar."""
    caixa: dict = {}

    def corre() -> None:
        try:
            caixa["valor"] = fn()
        except BaseException as exc:  # noqa: BLE001
            caixa["erro"] = exc

    fio = threading.Thread(target=corre, daemon=True)
    fio.start()
    fio.join(teto)
    if fio.is_alive():
        proc = marca_dagua._proc
        if proc is not None and proc.poll() is None:
            proc.kill()
        fio.join(2)
        pytest.fail(f"a geração não voltou em {teto:g}s")
    if "erro" in caixa:
        raise caixa["erro"]
    return caixa["valor"]


def _gera(wav: Path) -> tuple[str, float]:
    """O passo do job que segura o `_gen_lock`: marca, ou solta o áudio cru."""
    t0 = time.monotonic()
    try:
        if marca_dagua.disponivel():
            marca_dagua.marca_wav(wav)
        estado = "com marca"
    except Exception as exc:  # noqa: BLE001 — o job também engole e segue
        if "filho mudo" not in str(exc):
            raise
        estado = "sem marca"
    return estado, time.monotonic() - t0


@pytest.fixture
def isola_marca(tmp_path, monkeypatch):
    script = tmp_path / "filho_mudo.py"
    script.write_text("import time\ntime.sleep(3600)\n", encoding="utf-8")
    anterior = marca_dagua._proc
    marca_dagua._proc = None
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "TIMEOUT_S", PRAZO_S)
    monkeypatch.setattr(marca_dagua, "WORKER", script)
    monkeypatch.setattr(marca_dagua, "_vigia", lambda: None)
    procs: list = []
    real = marca_dagua._pega

    def espiar():
        proc = real()
        procs.append(proc)
        return proc

    monkeypatch.setattr(marca_dagua, "_pega", espiar)
    try:
        yield procs
    finally:
        marca_dagua._proc = anterior
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass


def test_filho_mudo_solta_a_geracao_dentro_do_prazo(tmp_path, isola_marca, capsys):
    """Filho que não escreve: volta sem marca, o pid morre, o próximo sobe outro."""
    wav = tmp_path / "fala.wav"
    cru = b"RIFF" + b"\x00" * 32
    wav.write_bytes(cru)

    estado, dt = _em_ate(lambda: _gera(wav), TETO_S + 1)
    assert estado == "sem marca"
    assert wav.read_bytes() == cru
    assert PRAZO_S - 0.05 <= dt < TETO_S
    assert len(isola_marca) == 1
    assert not _vivo(isola_marca[0].pid)
    assert marca_dagua._proc is None

    estado2, dt2 = _em_ate(lambda: _gera(wav), TETO_S + 1)
    assert estado2 == "sem marca"
    assert len(isola_marca) == 2
    assert isola_marca[1].pid != isola_marca[0].pid
    assert not _vivo(isola_marca[1].pid)
    assert marca_dagua._proc is None
    assert PRAZO_S - 0.05 <= dt2 < TETO_S
    assert wav.read_bytes() == cru

    aviso = capsys.readouterr().out
    assert aviso.count("filho mudo") == 2
    assert aviso.count("sem marca") == 2
    assert f"pid={isola_marca[0].pid}" in aviso
    assert f"pid={isola_marca[1].pid}" in aviso


def test_resposta_com_id_volta_antes_do_prazo(tmp_path, monkeypatch, isola_marca):
    """Linha com o id volta. O `pronto` sem id, que o worker manda ao subir, não conta."""
    script = tmp_path / "filho_ok.py"
    script.write_text(
        "import json, sys, time\n"
        "sys.stdout.write(json.dumps({'tipo': 'pronto'}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "pedido = json.loads(sys.stdin.readline())\n"
        "sys.stdout.write(json.dumps({'id': pedido['id'], 'ok': 1}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(3600)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(marca_dagua, "WORKER", script)
    t0 = time.monotonic()
    dados = marca_dagua._pede({"op": "marca", "wav": "x", "saida": "y"})
    dt = time.monotonic() - t0
    assert dados["ok"] == 1
    assert isinstance(dados["id"], int)
    assert dt < PRAZO_S
    assert _vivo(isola_marca[-1].pid)


def test_live_pcm_mudo_sai_cru_no_prazo_curto(tmp_path, monkeypatch, capsys):
    """O `_marca_pcm` do Live não espera os 60 s do job. O trecho sai cru."""
    import numpy  # noqa: F401 — paga o import fora do cronômetro
    import soundfile  # noqa: F401
    import app

    script = tmp_path / "filho_mudo.py"
    script.write_text("import time\ntime.sleep(3600)\n", encoding="utf-8")
    anterior = marca_dagua._proc
    marca_dagua._proc = None
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "TIMEOUT_S", 60.0)
    monkeypatch.setattr(marca_dagua, "WORKER", script)
    monkeypatch.setattr(marca_dagua, "_vigia", lambda: None)
    monkeypatch.setattr(app, "_LIVE_MARCA_PRAZO_S", PRAZO_S)
    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)
    pids: list[int] = []
    real = marca_dagua._pega

    def espiar():
        proc = real()
        pids.append(proc.pid)
        return proc

    monkeypatch.setattr(marca_dagua, "_pega", espiar)
    pcm = b"\x00\x00" * 240

    def chama():
        t0 = time.monotonic()
        out = app._marca_pcm(pcm, app._LIVE_AUDIO_SR)
        return out, time.monotonic() - t0

    try:
        out, dt = _em_ate(chama, TETO_S + 1)
        assert out == pcm
        assert PRAZO_S - 0.05 <= dt < TETO_S
        assert pids and not _vivo(pids[0])
        assert marca_dagua._proc is None
        aviso = capsys.readouterr().out
        assert "filho mudo" in aviso
        assert "sem marca" in aviso
        assert f"em {PRAZO_S:g}s" in aviso
        assert "em 60" not in aviso
    finally:
        proc = marca_dagua._proc
        marca_dagua._proc = anterior
        if proc is not None and proc is not anterior and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
