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
  *"/ttsproxy/ttsproxy/"*) codigo="${T_PREFIXO_DUPLO:-401}"; corpo='' ;;
  *"/health")
      if [ -n "${T_HOST_CODE:-}" ]; then
        case "$*" in *"Host: "*) codigo="$T_HOST_CODE"; corpo='' ;; *) codigo="${T_PUB:-200}"; corpo='{"ok":true,"boot_ts":111,"rev":"abc1234"}' ;; esac
      else
        codigo="${T_PUB:-200}"; corpo='{"ok":true,"boot_ts":111,"rev":"abc1234"}'
      fi ;;
  *"/api/voices") codigo="${T_AUTH:-401}"; corpo='' ;;
  http://127.0.0.1:*/ready) codigo="${T_CONECTOR:-200}"; corpo='{"status":"ok"}' ;;
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
    assert "ok conector" in log, "não checou o /ready do conector"
    assert "boot_ts=111" in log and "rev=abc1234" in log, "o histórico tem de guardar boot_ts/rev"
    assert _contador(amb) == 0
    assert not (amb["logs"] / "tts-health-ALERTA.txt").exists()


# --- #499: queda PARCIAL. O app de pé na LAN + conector do Cloudflare caído deixa
# o público 200 por um tempo (cache/DNS) e a LAN não vê o conector; só o /ready
# local dele acusa. Sem esta checagem a queda parcial passava verde.
def test_conector_sem_ready_e_falha_mesmo_com_lan_e_publico_200(amb):
    r = _run(amb, "check", env={"T_CONECTOR": "000"})
    assert r.returncode == 1, "conector caído passou verde com LAN e público 200"
    assert "ok lan" in _log(amb) and "ok publico" in _log(amb)
    assert "falha conector(/ready http=000" in _log(amb)
    assert _contador(amb) == 1


def test_conector_respondendo_nao_e_200_tambem_conta(amb):
    """503/404 também é conector fora da borda — só 200 é registrado."""
    assert _run(amb, "check", env={"T_CONECTOR": "503"}).returncode == 1
    assert "falha conector(/ready http=503)" in _log(amb)


def test_conector_cai_na_porta_local_20242(amb):
    _run(amb, "check")
    assert any("http://127.0.0.1:20242/ready" in l for l in Path(amb["env"]["T_CURL_LOG"]).read_text().splitlines()), \
        "a porta de métricas do cloudflared mudou sem ninguém notar"


def test_knob_desliga_a_checagem_do_conector(amb):
    r = _run(amb, "check", env={"T_CONECTOR": "000", "TTS_PROBE_CONECTOR": "0"})
    assert r.returncode == 0
    assert "conector" not in _log(amb)


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


def test_host_publico_fora_da_allowlist_vira_alerta(amb):
    """421 com `Host:` público = `TTS_ALLOWED_HOSTS` faltando no serviço (#837)."""
    r = _run(amb, "check", env={"T_HOST_CODE": "421"})
    assert r.returncode == 1
    assert "fora de TTS_ALLOWED_HOSTS" in _log(amb)


def test_prefixo_duplo_com_200_acusa_bypass(amb):
    """`/ttsproxy/ttsproxy/...` sem chave devolvendo 2xx é bypass de auth (#778).

    O Starlette tira o `root_path` de novo e a rota protegida casa sem passar pela
    chave — pela internet isso entrega settings, vozes e síntese com a voz do dono."""
    r = _run(amb, "check", env={"T_PREFIXO_DUPLO": "200"})
    assert r.returncode == 1
    assert "prefixo-duplo(http=200 sem chave — bypass de auth)" in _log(amb)


def test_prefixo_duplo_401_passa_na_rev_corrigida(amb):
    """401 é a resposta da rev CORRIGIDA (negação por padrão antes do roteamento).

    A célula existe para o default do shim ser o contrato certo: sem ela, um shim
    que só devolvesse 404 deixaria a suíte verde e o primeiro deploy vermelho."""
    assert _run(amb, "check", env={"T_PREFIXO_DUPLO": "401"}).returncode == 0
    # a sonda tem de ter PEDIDO o path duplicado (sem isto a célula passa sem ela)
    assert any("/ttsproxy/ttsproxy/" in l for l in
               Path(amb["env"]["T_CURL_LOG"]).read_text().splitlines()), \
        "a sonda não requisitou o path duplicado"


def test_prefixo_duplo_404_e_ALERTA_em_rev_antiga(amb):
    """404 é a rev ANTERIOR à correção — e na sonda CONTÍNUA isso é alerta (#806).

    Aceitar 404 como ok deixaria a sonda verde na produção de hoje, que é
    exatamente o estado sem a correção do #777. Some-se a isso que 404 na rev
    nova também é o que um deploy quebrado devolveria: o verde mentiria nos dois
    casos."""
    r = _run(amb, "check", env={"T_PREFIXO_DUPLO": "404"})
    assert r.returncode == 1
    assert "bypass do #777 não coberto" in _log(amb)


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
