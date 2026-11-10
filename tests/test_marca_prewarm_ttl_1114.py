"""Filho do `prewarm` da marca sobrevive ao 1º ciclo do vigia (#1114).

`_ultima` nascia em 0.0 e o `prewarm()` -> `_pega()` não o tocava. O vigia do
TTL, no 1º ciclo, via `monotonic() - 0.0` (o uptime da máquina: no mini, ~2,3 M s)
maior que `TTL_S` e fechava o filho ~5 s depois de ele nascer. O 1º turno do Live
subia outro filho e pagava 3–6 s de `marca` no S2.

As células não carregam AudioSeal: o filho é um script de mentira que só dorme.
O relógio do módulo é trocado por um com uptime de dias, para a célula não
depender do uptime de quem roda a suíte (máquina recém-ligada esconderia o bug).
"""

from __future__ import annotations

import sys
import time as _time
import types
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import marca_dagua  # noqa: E402

#: uptime de dias, como o do mini (2 323 959 s em 2026-11-10)
UPTIME_S = 2_400_000.0
CICLO_S = 0.05


def _espera(cond, teto: float) -> bool:
    fim = _time.monotonic() + teto
    while _time.monotonic() < fim:
        if cond():
            return True
        _time.sleep(0.02)
    return cond()


@pytest.fixture
def filho_de_mentira(tmp_path, monkeypatch):
    """Filho que só dorme; vigia acelerado; relógio do módulo com uptime de dias."""
    script = tmp_path / "filho_dorme.py"
    script.write_text("import time\ntime.sleep(3600)\n", encoding="utf-8")
    relogio = types.SimpleNamespace(
        monotonic=lambda: _time.monotonic() + UPTIME_S, sleep=_time.sleep)
    monkeypatch.setattr(marca_dagua, "time", relogio)
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "WORKER", script)
    monkeypatch.setattr(marca_dagua, "INTERVALO_VIGIA_S", CICLO_S)
    monkeypatch.setattr(marca_dagua, "_proc", None)
    monkeypatch.setattr(marca_dagua, "_vigia_thread", None)
    # o estado de nascença do módulo, que é o que o bug pega
    monkeypatch.setattr(marca_dagua, "_ultima", 0.0)
    procs: list = []
    try:
        yield procs
    finally:
        proc = marca_dagua._proc
        marca_dagua._proc = None            # o vigia sai no próximo ciclo
        for p in [proc, *procs]:
            if p is not None and p.poll() is None:
                p.kill()
                try:
                    p.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
        vigia = marca_dagua._vigia_thread
        if vigia is not None:
            vigia.join(1)


def test_filho_do_prewarm_continua_vivo_depois_de_varios_ciclos_do_vigia(
        filho_de_mentira):
    """Com uptime de dias, o filho recém-subido NÃO é dado como vencido."""
    proc = marca_dagua._pega()             # o que o `prewarm()` faz na thread
    filho_de_mentira.append(proc)
    vigia = marca_dagua._vigia_thread
    assert vigia is not None and vigia.is_alive()
    # 10 ciclos do vigia: com o `0.0` o 1º já fechava (close + terminate)
    _time.sleep(CICLO_S * 10)
    assert proc.poll() is None, "o vigia matou o filho do prewarm logo ao nascer"
    assert marca_dagua._proc is proc


def test_filho_ocioso_alem_do_ttl_ainda_morre(filho_de_mentira, monkeypatch):
    """O conserto não desliga o TTL: filho parado de verdade continua saindo."""
    monkeypatch.setattr(marca_dagua, "TTL_S", 0.2)
    proc = marca_dagua._pega()
    filho_de_mentira.append(proc)
    assert _espera(lambda: marca_dagua._proc is None, 3.0), "TTL deixou de valer"
    assert _espera(lambda: proc.poll() is not None, 8.0)


def test_prewarm_de_filho_vivo_renova_o_ttl(filho_de_mentira, monkeypatch):
    """A abertura do Live (`prewarm`) sobre filho perto do TTL o mantém vivo."""
    monkeypatch.setattr(marca_dagua, "TTL_S", 0.6)
    proc = marca_dagua._pega()
    filho_de_mentira.append(proc)
    for _ in range(4):                     # 4 × 0,3 s = 1,2 s > TTL de 0,6 s
        _time.sleep(0.3)
        assert marca_dagua._pega() is proc
    assert proc.poll() is None
    assert marca_dagua._proc is proc
