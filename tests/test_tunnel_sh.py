"""`tunnel.sh`: o destino do `-R` TEM de ser IP de LAN (nunca loopback/utun/#293).

POR QUE ESTE ARQUIVO EXISTE (achado P1 da auditoria #286): o script só exigia
destino "não vazio". Por `127.0.0.1` a API entra SEM chave (loopback é isento em
`_is_local`) — um túnel que caia em loopback abre a instância para a internet em
silêncio — e um que caia em `utun` (VPN na rota default; `100.89.3.11` medido
nesta máquina) aponta para IP que a VPS não alcança. Nenhum dos dois dava erro.

Os shims substituem `route`, `ipconfig`, `ssh` e `sleep`: o teste não abre túnel,
não espera retry e não depende da rede da máquina. O `ssh` de mentira grava o
argv — é assim que se prova QUAL destino foi para a linha de comando.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "tunnel.sh"

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="script de macOS (route/ipconfig/launchctl)")

SHIM_SSH = '#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$TUNNEL_ARGV"\nexit "${SSH_RC:-0}"\n'
# ipconfig: responde o IP da linha `<if>=<ip>` de $IP_MAPA. `IP_APOS=n` faz as n-1
# primeiras chamadas falharem (Wi-Fi ainda associando).
SHIM_IPCONFIG = """#!/bin/sh
n=$(( $(cat "$IP_CONTA" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$IP_CONTA"
[ "$1" = getifaddr ] || exit 1
[ -n "${IP_APOS:-}" ] && [ "$n" -lt "$IP_APOS" ] && exit 1
ip="$(sed -n "s/^$2=//p" "$IP_MAPA" 2>/dev/null | head -1)"
[ -n "$ip" ] && printf '%s\\n' "$ip"
[ -n "$ip" ]
"""


def _shim(dir_: Path, nome: str, corpo: str) -> None:
    p = dir_ / nome
    p.write_text(corpo)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def amb(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    # route: a interface da rota default vem de ROTA_IF (vazio = sem rota default)
    _shim(bin_, "route", '#!/bin/sh\n[ -n "${ROTA_IF:-}" ] && echo "  interface: $ROTA_IF"\nexit 0\n')
    _shim(bin_, "ipconfig", SHIM_IPCONFIG)
    _shim(bin_, "sleep", "#!/bin/sh\nexit 0\n")          # sem esperar retry de 10 s
    _shim(bin_, "ssh", SHIM_SSH)
    mapa = tmp_path / "ips"
    mapa.write_text("")
    return {
        "dir": tmp_path,
        "argv": tmp_path / "ssh.argv",
        "mapa": mapa,
        "env": {
            "PATH": f"{bin_}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "TTS_TUNNEL_KEY": str(tmp_path / "chave-inexistente"),
            "TUNNEL_ARGV": str(tmp_path / "ssh.argv"),
            "IP_MAPA": str(mapa),
            "IP_CONTA": str(tmp_path / "conta"),
            "SSH_RC": "0",
        },
    }


def _roda(amb, dest="eu@vps", ips=None, extra_env=None, porta="7860"):
    amb["mapa"].write_text("".join(f"{i}={ip}\n" for i, ip in (ips or {}).items()))
    env = {**amb["env"], **(extra_env or {})}
    return subprocess.run([str(SCRIPT), dest, porta], env=env,
                          capture_output=True, text=True, timeout=30)


def test_destino_lan_privado_vai_para_o_destino_do_tunel(amb):
    """Caminho feliz: en0 com IP privado → `-R 127.0.0.1:<porta>:<ip-de-lan>:7860`."""
    r = _roda(amb, ips={"en0": "192.168.15.48"}, extra_env={"ROTA_IF": "en0"})
    assert r.returncode == 0, r.stdout + r.stderr
    argv = amb["argv"].read_text().splitlines()
    assert "127.0.0.1:7860:192.168.15.48:7860" in argv, argv
    assert "-N" in argv
    assert "192.168.15.48:7860" in r.stdout and "LAN ok" in r.stdout


def test_faixas_privadas_172_e_10_tambem_passam(amb):
    for ip in ("172.16.0.7", "172.31.255.254", "10.1.2.3"):
        amb["argv"].unlink(missing_ok=True)
        r = _roda(amb, ips={"en1": ip}, extra_env={"ROTA_IF": "en1"})
        assert r.returncode == 0, f"{ip}: {r.stdout}{r.stderr}"
        assert f"127.0.0.1:7860:{ip}:7860" in amb["argv"].read_text()


def test_identities_only_e_batchmode_viajam_no_ssh(amb):
    """Agente com dezenas de chaves não pode ser oferecido, e prompt de senha não
    pode parar um agente de launchd."""
    _roda(amb, ips={"en0": "192.168.15.48"}, extra_env={"ROTA_IF": "en0"})
    argv = amb["argv"].read_text()
    assert "IdentitiesOnly=yes" in argv and "BatchMode=yes" in argv


def test_lo0_e_recusado_com_mensagem_e_sem_loop(amb):
    """`TTS_TUNNEL_IF=lo0`: o `ipconfig` não tem endereço em lo0. Escolha manual
    não muda sozinha — falha na hora, dizendo que o destino tem de ser LAN."""
    r = _roda(amb, extra_env={"TTS_TUNNEL_IF": "lo0"})
    assert r.returncode == 2, r.stdout
    assert "lo0 sem endereço de LAN" in r.stdout
    assert "SEM chave" in r.stdout, "a mensagem tem de citar o invariante"
    assert not amb["argv"].exists(), "não pode ter chamado ssh"


def test_vpn_utun_100x_e_recusada(amb):
    """O caso medido no achado: VPN na rota default → utun4/100.89.3.11. É IP
    válido, mas a VPS não chega nele (CGNAT, fora do RFC1918)."""
    r = _roda(amb, ips={"utun4": "100.89.3.11"}, extra_env={"TTS_TUNNEL_IF": "utun4"})
    assert r.returncode == 2, r.stdout
    assert "NÃO é IP de LAN" in r.stdout and "100.89.3.11" in r.stdout
    assert not amb["argv"].exists()


def test_loopback_explicito_e_recusado_com_dica_da_interface(amb):
    """Se o `ipconfig` devolvesse 127.0.0.1, a dica manda usar a interface da LAN."""
    r = _roda(amb, ips={"lo0": "127.0.0.1"}, extra_env={"TTS_TUNNEL_IF": "lo0"})
    assert r.returncode == 2
    assert "NÃO é IP de LAN" in r.stdout
    assert "nunca 127.0.0.1" in r.stdout


def test_ipv6_link_local_nao_passa_como_lan(amb):
    """Regressão de escopo: só IPv4 privado é aceito (`fe80::` não é roteável da
    VPS para cá sem escopo)."""
    r = _roda(amb, ips={"en0": "fe80::1c2d:3eff:fe4a:5b6c"},
              extra_env={"TTS_TUNNEL_IF": "en0"})
    assert r.returncode == 2
    assert "NÃO é IP de LAN" in r.stdout


def test_sem_rota_default_insiste_e_desiste_com_motivo(amb):
    """Wi-Fi desligado no boot (caso do LaunchAgent): insiste até o teto e então
    falha DIZENDO o motivo, em vez de girar para sempre."""
    r = _roda(amb, extra_env={"TTS_TUNNEL_TENTATIVAS": "3"})
    assert r.returncode == 2, r.stdout
    assert r.stdout.count("sem interface de saída") == 3, r.stdout
    assert "desistindo" in r.stdout


def test_wifi_associando_ainda_sobe_quando_o_ip_aparece(amb):
    """O retry não pode ser fatal: IP aparecendo na 3ª tentativa → o túnel sobe."""
    r = _roda(amb, ips={"en0": "192.168.15.48"},
              extra_env={"ROTA_IF": "en0", "IP_APOS": "3"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "127.0.0.1:7860:192.168.15.48:7860" in amb["argv"].read_text()


def test_a_porta_da_vps_vai_na_linha_de_comando(amb):
    """Contrato: o bind é na VPS (127.0.0.1:<RPORT>) e o destino é a LAN na 7860 do
    app — trocar os lados é o erro clássico."""
    r = _roda(amb, porta="9876", ips={"en0": "192.168.1.9"},
              extra_env={"ROTA_IF": "en0"})
    assert r.returncode == 0
    assert "127.0.0.1:9876:192.168.1.9:7860" in amb["argv"].read_text()
