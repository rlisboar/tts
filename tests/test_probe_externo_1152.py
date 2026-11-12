"""#1152 — tools/probe_externo.sh: sonda externa (só /health) que alarma após 2 falhas seguidas.

`curl` é um shim no PATH; nada de rede. O que se cobra: o contador por alvo (LAN/público
independentes), o alarme na falha N (uma vez, com o gancho TTS_PROBE_ALARME_CMD), o
RECUPERADO, o 200 sem corpo "ok" valendo falha e, sobretudo, que NENHUMA chave entra na URL, no
argv do curl, no log ou no estado (a sonda não lê nem grava chave)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SONDA = REPO / "tools" / "probe_externo.sh"
SEGREDO = "SEGREDO-NAO-VAZA-1152"


@pytest.fixture
def amb(tmp_path: Path) -> dict:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text(f"""#!/bin/bash
# curl falso: lan/publico decididos por env; grava o argv inteiro
echo "$@" >> "{tmp_path}/curl.argv"
url=""; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w|-m) shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  *the-dudes*) cod="${{T_PUB:-200}}" ;;
  *) cod="${{T_LAN:-200}}" ;;
esac
corpo='{{"ok":true}}'; [ "${{T_SEM_OK:-0}}" = 1 ] && corpo='<html>captive portal</html>'
[ -n "$out" ] && printf '%s' "$corpo" > "$out"
printf '%s' "$cod"
""")
    (bin_ / "curl").chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "TTS_PROBE_ESTADO": str(tmp_path / "estado"),
        "TTS_PROBE_LOG": str(tmp_path / "probe.log"),
        "TTS_PROBE_ALARME_CMD": f'echo "$PROBE_ALVO $PROBE_FALHAS $PROBE_MSG" >> {tmp_path}/alarmes',
        "TTS_API_KEY": SEGREDO, "TTS_ROD_API_KEY": SEGREDO,
        "_T": str(tmp_path),
    }


def _uma(amb, **extra) -> str:
    r = subprocess.run(["bash", str(SONDA), "uma"], env={**amb, **extra}, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return Path(amb["TTS_PROBE_LOG"]).read_text()


def _alarmes(amb) -> list[str]:
    p = Path(amb["_T"]) / "alarmes"
    return p.read_text().splitlines() if p.exists() else []


def test_tudo_ok_nao_alarma(amb):
    log = _uma(amb)
    assert "ok lan" in log and "ok publico" in log and "ALARME" not in log
    assert _alarmes(amb) == []


def test_uma_falha_nao_alarma_duas_seguidas_sim_uma_vez_so(amb):
    assert "ALARME" not in _uma(amb, T_LAN="000")
    assert _alarmes(amb) == []
    log = _uma(amb, T_LAN="000")                           # 2ª seguida → alarma
    assert "ALARME lan falhas_seguidas=2" in log
    assert len(_alarmes(amb)) == 1 and _alarmes(amb)[0].startswith("lan 2 ")
    assert "lan" in (Path(amb["TTS_PROBE_ESTADO"]) / "ALARME").read_text()
    _uma(amb, T_LAN="000")                                 # 3ª: não repete o alarme
    assert len(_alarmes(amb)) == 1


def test_lan_e_publico_sao_contados_separados(amb):
    _uma(amb, T_PUB="502")
    log = _uma(amb, T_PUB="502")
    assert "ALARME publico" in log and "ALARME lan" not in log
    assert [a.split()[0] for a in _alarmes(amb)] == ["publico"]


def test_falha_intercalada_zera_o_contador(amb):
    _uma(amb, T_LAN="000")
    _uma(amb)                                              # voltou: zera
    assert "ALARME" not in _uma(amb, T_LAN="000")          # 1ª de novo, não 2ª


def test_recuperado_loga_e_limpa_o_alarme(amb):
    _uma(amb, T_LAN="000"); _uma(amb, T_LAN="000")
    log = _uma(amb)
    assert "RECUPERADO lan após 2 falha(s)" in log
    assert not (Path(amb["TTS_PROBE_ESTADO"]) / "ALARME").exists()


def test_200_sem_corpo_ok_vale_falha(amb):
    """portal cativo / página de erro do proxy com 200: não é o app"""
    _uma(amb, T_SEM_OK="1")
    assert "ALARME lan" in _uma(amb, T_SEM_OK="1")


def test_nenhuma_chave_na_url_no_argv_no_log_nem_no_estado(amb):
    _uma(amb); _uma(amb, T_LAN="000"); _uma(amb, T_LAN="000")
    tudo = (Path(amb["_T"]) / "curl.argv").read_text() + Path(amb["TTS_PROBE_LOG"]).read_text()
    for f in Path(amb["TTS_PROBE_ESTADO"]).iterdir():
        tudo += f.read_text()
    assert SEGREDO not in tudo
    argv = (Path(amb["_T"]) / "curl.argv").read_text()
    assert "X-API-Key" not in argv and "key=" not in argv and "-H" not in argv.split()
    # só /health
    for ln in argv.splitlines():
        assert ln.split()[-1].endswith("/health"), ln


def test_log_e_estado_nascem_0600(amb):
    _uma(amb, T_LAN="000"); _uma(amb, T_LAN="000")
    assert oct(Path(amb["TTS_PROBE_LOG"]).stat().st_mode & 0o777) == "0o600"
    assert oct((Path(amb["TTS_PROBE_ESTADO"]) / "ALARME").stat().st_mode & 0o777) == "0o600"
