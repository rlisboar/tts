"""`remote/health_probe.sh`: probe de disponibilidade, contador de falhas e alerta.

POR QUE ESTE ARQUIVO EXISTE (#344, achado P1 da auditoria #286): a produção não
tinha vigia nenhuma — produção caída era descoberta pelo uso. O contrato que
importa aqui é o ALARME: falha só vira alerta depois de N consecutivas (senão o
piscar de um deploy vira ruído), o alerta é um arquivo-flag e o contador zera na
primeira recuperação.

`curl`, `osascript` e `launchctl` são shims: nada de rede, nada de notificação
real, nada de launchd. O histórico e o alerta ficam num `$HOME` de tmp.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "remote" / "health_probe.sh"
ZSH = "zsh"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="probe é de macOS")

SHIM_CURL = r"""#!/bin/zsh
# curl falso: escolhe o status pela URL e pelos knobs T_*
url=""; so_codigo=0
for ((i = 1; i <= $#; i++)); do
  case "${@[i]}" in
    -w) so_codigo=1 ;;
    http*) url="${@[i]}" ;;
  esac
done
print -r -- "$url" >> "$T_CURL_LOG"
case "$url" in
  *"/api/voices") codigo="${T_AUTH:-401}"; corpo='' ;;
  https://*)      codigo="${T_PUB:-200}"; corpo='{"ok":true,"boot_ts":111,"rev":"abc1234"}' ;;
  http://*)       codigo="${T_LAN:-200}"; corpo='{"ok":true,"boot_ts":111,"rev":"abc1234"}' ;;
  *)              codigo=000; corpo='' ;;
esac
[ "$codigo" = 000 ] && { [ $so_codigo = 1 ] && print -rn -- 000; exit 7; }
if [ $so_codigo = 1 ]; then print -rn -- "$codigo"; else print -rn -- "$corpo"; fi
"""


def _shim(dir_: Path, nome: str, corpo: str) -> None:
    p = dir_ / nome
    p.write_text(corpo)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def amb(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _shim(bin_, "curl", SHIM_CURL)
    _shim(bin_, "osascript", '#!/bin/zsh\nprint -r -- "$*" >> "$T_OSA_LOG"\nexit 0\n')
    _shim(bin_, "launchctl", '#!/bin/zsh\nprint -r -- "$*" >> "$T_LAUNCH_LOG"\ncase "$1" in print) exit 0;; esac\nexit 0\n')
    home = tmp_path / "home"
    (home / "Library" / "Logs").mkdir(parents=True)
    return {
        "home": home,
        "logs": home / "Library" / "Logs",
        "env": {
            "PATH": f"{bin_}:{os.environ.get('PATH', '')}",
            "HOME": str(home),
            "T_CURL_LOG": str(tmp_path / "curl.log"),
            "T_OSA_LOG": str(tmp_path / "osa.log"),
            "T_LAUNCH_LOG": str(tmp_path / "launch.log"),
        },
    }


def _run(amb, *args, env=None):
    return subprocess.run([ZSH, str(SCRIPT), *args],
                          env={**amb["env"], **(env or {})},
                          capture_output=True, text=True, timeout=60)


def _log(amb) -> str:
    p = amb["logs"] / "tts-health-probe.log"
    return p.read_text() if p.exists() else ""


def _contador(amb) -> int:
    p = amb["logs"] / "tts-health-falhas"
    return int(p.read_text().strip()) if p.exists() else 0


def test_tudo_ok_nao_alerta_e_zera_contador(amb):
    r = _run(amb, "check")
    assert r.returncode == 0, r.stdout + r.stderr
    log = _log(amb)
    assert "ok lan" in log and "ok publico" in log and "ok auth" in log
    assert "boot_ts=111" in log and "rev=abc1234" in log, "o histórico tem de guardar boot_ts/rev"
    assert _contador(amb) == 0
    assert not (amb["logs"] / "tts-health-ALERTA.txt").exists()


def test_alerta_so_na_terceira_falha_consecutiva(amb):
    falha = {"T_LAN": "000", "T_PUB": "000", "T_AUTH": "000"}
    assert _run(amb, "check", env=falha).returncode == 1
    assert _contador(amb) == 1
    assert not (amb["logs"] / "tts-health-ALERTA.txt").exists(), "alertou cedo demais"
    assert _run(amb, "check", env=falha).returncode == 1
    assert not (amb["logs"] / "tts-health-ALERTA.txt").exists()

    assert _run(amb, "check", env=falha).returncode == 1
    flag = amb["logs"] / "tts-health-ALERTA.txt"
    assert flag.exists(), "3 falhas consecutivas não geraram alerta"
    assert "3 verificações consecutivas" in flag.read_text()
    assert flag.stat().st_mode & 0o777 == 0o600
    assert (amb["logs"] / "tts-health-probe.log").stat().st_mode & 0o777 == 0o600
    assert Path(amb["env"]["T_OSA_LOG"]).exists(), "não tentou a notificação do macOS"


def test_recuperacao_zera_contador_e_limpa_a_flag(amb):
    falha = {"T_LAN": "502", "T_PUB": "502", "T_AUTH": "000"}
    for _ in range(3):
        _run(amb, "check", env=falha)
    assert (amb["logs"] / "tts-health-ALERTA.txt").exists()

    assert _run(amb, "check").returncode == 0
    assert _contador(amb) == 0
    assert not (amb["logs"] / "tts-health-ALERTA.txt").exists(), "a flag ficou aberta"
    assert "recuperado (havia 3 falhas)" in _log(amb)


def test_sem_chave_dando_200_acusa_tunel_em_loopback(amb):
    """200 numa rota autenticada = destino do ingress é loopback e a auth é no-op:
    é o invariante do projeto, não um detalhe."""
    r = _run(amb, "check", env={"T_AUTH": "200"})
    assert r.returncode == 1
    assert "destino do túnel é LOOPBACK" in _log(amb)


def test_erro_diferente_de_401_tambem_conta(amb):
    assert _run(amb, "check", env={"T_AUTH": "403"}).returncode == 1
    assert "falha auth(http=403)" in _log(amb)


def test_health_200_sem_ok_no_corpo_e_falha(amb):
    """Conector devolvendo 200 com HTML (página de erro do Cloudflare) não é app."""
    (amb["home"] / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
    sh = amb["env"]["PATH"].split(":")[0] + "/curl"
    Path(sh).write_text(SHIM_CURL.replace('corpo=\'{"ok":true,"boot_ts":111,"rev":"abc1234"}\'',
                                          'corpo="<html>erro</html>"'))
    assert _run(amb, "check").returncode == 1


def test_status_mostra_o_alerta_aberto(amb):
    for _ in range(3):
        _run(amb, "check", env={"T_LAN": "000", "T_PUB": "000", "T_AUTH": "000"})
    r = _run(amb, "status")
    assert "EM ALERTA" in r.stdout and "ALERTA: produção sem resposta" in r.stdout
    assert "falhas consecutivas: 3" in r.stdout


def test_install_cria_agente_com_intervalo_e_roda_uma_vez(amb):
    r = _run(amb, "install")
    assert r.returncode == 0, r.stdout + r.stderr
    plist = (amb["home"] / "Library" / "LaunchAgents" / "com.local.tts-health-probe.plist").read_text()
    assert "<key>StartInterval</key><integer>120</integer>" in plist
    assert "health_probe.sh</string>" in plist and "<string>check</string>" in plist
    assert "ProcessType" in plist and "Nice" in plist     # não disputa o :7860
    launches = Path(amb["env"]["T_LAUNCH_LOG"]).read_text()
    assert "bootstrap" in launches
    assert "ok lan" in _log(amb), "o install já faz uma rodada"


def test_uninstall_tira_o_agente(amb):
    _run(amb, "install")
    r = _run(amb, "uninstall")
    assert r.returncode == 0
    assert not (amb["home"] / "Library" / "LaunchAgents" / "com.local.tts-health-probe.plist").exists()
    assert "bootout" in Path(amb["env"]["T_LAUNCH_LOG"]).read_text()
