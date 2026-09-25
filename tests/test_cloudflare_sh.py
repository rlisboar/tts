"""cloudflare.sh: provision/install sem eval, sem token no stdout e arquivo 0600.

Roda o zsh de verdade com shims de `cloudflared`, `curl`, `launchctl`, `brew` e
`chmod` no PATH — nada de rede, nada de Cloudflare real.

Os valores do cert.pem/tunnel são hostis de propósito: carregam o que o `eval`
antigo reinterpretava (`$ \\` ' " \\ ; * ?`) e o que quebra XML dentro do plist
(`& < >`).

Os shims ficam num diretório compartilhado da sessão (cada execução de um script
recém-criado é cara no macOS; o HOME é novo a cada teste e o binário não).
"""
from __future__ import annotations

import base64
import json
import os
import plistlib
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "cloudflare.sh"
ZSH = shutil.which("zsh")

TOKEN = "t0k$en-`b'c\"d & <e> ;f *g?h\\i"
ACCT = "acc$1"
NAME = "tts-teste"
LABEL = "com.local.cloudflared-tts"

pytestmark = [
    pytest.mark.skipif(ZSH is None, reason="precisa de zsh"),
    pytest.mark.skipif(sys.platform != "darwin", reason="plist/launchctl/stat do macOS"),
]


def _shim(path: Path, body: str) -> None:
    path.write_text("#!/bin/zsh\n" + body)
    path.chmod(0o755)


def _escreve_shims(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    _shim(
        bin_dir / "cloudflared",
        """case "${1:-}" in
  tunnel)
    case "${2:-}" in
      list) [ "${3:-}" = "--output" ] && print -r -- "[{\\"id\\":\\"tid-123\\",\\"name\\":\\"$T_NAME\\"}]" ;;
      token) print -rn -- "$T_TOKEN" ;;
    esac ;;
esac
exit 0
""",
    )
    _shim(bin_dir / "curl",
          'print -rl -- "$@" >> "$T_CURL_LOG"\n'
          '[ "${T_CURL_SUCCESS:-true}" = "false" ] && print -r -- \'{"success":false,"errors":[{"code":10000,"message":"Authentication error"}]}\' && exit 0\n'
          'print -r -- \'{"success":true}\'\n')
    _shim(
        bin_dir / "launchctl",
        """[ "${T_LAUNCHCTL_FALHA:-0}" = "1" ] && exit 1   # simula launchd recusando
case "${1:-}" in
  bootstrap) /usr/bin/stat -f %Lp "${3:-}" >> "$T_BOOT_LOG" 2>/dev/null ;;
  print) print -r -- "state = running"; print -r -- "arguments = ... --token-file $T_TOKEN ..." ;;
esac
exit 0
""",
    )
    _shim(
        bin_dir / "chmod",
        # registra o modo ANTES de aplicar: prova que o umask já criou 0600.
        '/usr/bin/stat -f %Lp "${2:-}" >> "$T_CHMOD_LOG" 2>/dev/null\nexec /bin/chmod "$@"\n',
    )
    _shim(bin_dir / "brew", "exit 0\n")


@pytest.fixture(scope="session")
def bin_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("shims")
    _escreve_shims(d)
    return d


@pytest.fixture
def amb(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    """HOME falso + cert.pem hostil + shims no PATH (nenhum binário real chamado)."""
    home = tmp_path / "home"
    (home / ".cloudflared").mkdir(parents=True)
    blob = base64.b64encode(
        json.dumps({"apiToken": TOKEN, "accountID": ACCT, "zoneID": "z1"}).encode()
    ).decode()
    (home / ".cloudflared" / "cert.pem").write_text(
        "-----BEGIN ARGO TUNNEL TOKEN-----\n"
        + "\n".join(textwrap.wrap(blob, 60))
        + "\n-----END ARGO TUNNEL TOKEN-----\n"
    )

    env = dict(os.environ)
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}:{env['PATH']}",
        T_NAME=NAME,
        T_TOKEN=TOKEN,
        T_CURL_LOG=str(tmp_path / "curl.log"),
        T_BOOT_LOG=str(tmp_path / "boot.log"),
        T_CHMOD_LOG=str(tmp_path / "chmod.log"),
    )
    env.pop("CLOUDFLARE_LABEL", None)
    return env


def _run(env: dict[str, str], *args: str, stdin: str | None = None,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ZSH, "-f", str(SCRIPT), *args],
        env=env, input=stdin, capture_output=True, text=True, cwd=str(cwd or REPO),
    )


def _provision(env: dict[str, str]) -> subprocess.CompletedProcess:
    return _run(env, "provision", NAME, "tts.exemplo.test", "192.168.15.34")


def _saida(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def _plist(env: dict[str, str], label: str = LABEL) -> Path:
    return Path(env["HOME"]) / "Library" / "LaunchAgents" / f"{label}.plist"


def _token_file(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".cloudflared" / f"{NAME}.token"


def _modo(p: Path) -> int:
    return p.stat().st_mode & 0o777


def _escritos(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# ---------------------------------------------------------------- provision

def test_provision_sem_eval_manda_o_token_e_a_conta_inteiros(amb):
    """Com `eval`, `$`, espaço, `\\` etc. eram reinterpretados e o valor mentia."""
    r = _provision(amb)
    assert r.returncode == 0, _saida(r)

    chamadas = _escritos(Path(amb["T_CURL_LOG"]))
    urls = [l for l in chamadas if l.startswith("https://")]
    assert urls, "o provision deveria ter chamado a API do Cloudflare"
    assert f"/accounts/{ACCT}/" in urls[0]
    assert f"Authorization: Bearer {TOKEN}" in chamadas


def test_provision_nao_imprime_o_token(amb):
    r = _provision(amb)
    assert r.returncode == 0, _saida(r)
    assert TOKEN not in _saida(r)


def test_provision_grava_o_token_em_arquivo_0600(amb):
    assert _provision(amb).returncode == 0

    tf = _token_file(amb)
    assert tf.read_text().rstrip("\n") == TOKEN
    assert _modo(tf) == 0o600
    # o arquivo já nasceu 0600 (umask 077), não virou 0600 só depois do chmod
    assert [l.split()[0] for l in _escritos(Path(amb["T_CHMOD_LOG"]))][0] == "600"


def test_provision_aborta_quando_a_api_nega(amb):
    """F2: `_api` terminava em `print` (rc 0) — erro da API passava batido e o
    provision ainda dizia "Pronto." com o ingress não configurado."""
    amb["T_CURL_SUCCESS"] = "false"
    r = _provision(amb)
    assert r.returncode == 1
    assert "erro: " in r.stderr and "Authentication error" in r.stderr
    assert "NÃO foi configurado" in r.stdout
    assert "Pronto." not in r.stdout
    assert not _token_file(amb).exists(), "abortou mas gravou o token"


def test_provision_imprime_install_por_arquivo(amb):
    r = _provision(amb)
    assert r.returncode == 0, _saida(r)
    assert "install --token-file ~/.cloudflared/tts-teste.token" in r.stdout


# ------------------------------------------------------------------ install

def _instala_por_arquivo(env: dict[str, str], *extra: str) -> subprocess.CompletedProcess:
    _provision(env)
    return _run(env, "install", "--token-file", str(_token_file(env)), *extra)


def test_install_por_arquivo_nao_imprime_token_e_plist_fica_0600(amb):
    r = _instala_por_arquivo(amb)
    assert r.returncode == 0, _saida(r)
    assert TOKEN not in _saida(r)

    plist = _plist(amb)
    assert _modo(plist) == 0o600
    # F3: o plist aponta para o arquivo (--token-file), sem o token literal
    args = plistlib.loads(plist.read_bytes())["ProgramArguments"]
    assert args[-2:] == ["--token-file", str(_token_file(amb))]
    assert TOKEN not in plist.read_text()
    # já estava 0600 antes do chmod e antes do launchctl carregar
    assert "600" in _escritos(Path(amb["T_CHMOD_LOG"]))[0].split()
    assert _escritos(Path(amb["T_BOOT_LOG"])) == ["600"]


@pytest.mark.parametrize("forma", ["cru", "stdin"])
def test_install_aceita_token_cru_e_por_stdin(amb, forma):
    if forma == "cru":
        r = _run(amb, "install", TOKEN)
    else:
        r = _run(amb, "install", "-", stdin=TOKEN)
    assert r.returncode == 0, _saida(r)
    # F3: mesmo no caminho cru/stdin o token vai para arquivo 0600 e o plist
    # aponta para ele — nada de token no `ps` nem no plist
    normalizado = Path(amb["HOME"]) / ".cloudflared" / f"{LABEL}.token"
    assert normalizado.read_text() == TOKEN and _modo(normalizado) == 0o600
    args = plistlib.loads(_plist(amb).read_bytes())["ProgramArguments"]
    assert args[-2:] == ["--token-file", str(normalizado)]
    assert TOKEN not in _plist(amb).read_text()


def test_install_com_token_file_relativo_grava_caminho_absoluto(amb, tmp_path):
    """P3 do gate: o launchd roda com cwd `/` — caminho relativo no plist não
    resolve e o agente simplesmente não sobe (sem erro no install)."""
    onde = tmp_path / "cwd"
    onde.mkdir()
    (onde / "meu.token").write_text(TOKEN)

    r = _run(amb, "install", "--token-file", "meu.token", cwd=onde)
    assert r.returncode == 0, _saida(r)

    caminho = plistlib.loads(_plist(amb).read_bytes())["ProgramArguments"][-1]
    assert os.path.isabs(caminho), f"plist com caminho relativo: {caminho}"
    assert Path(caminho).is_file() and Path(caminho).samefile(onde / "meu.token")
    assert Path(caminho).read_text() == TOKEN


def test_install_aperta_plist_frouxo_que_ja_existia(amb):
    plist = _plist(amb)
    plist.parent.mkdir(parents=True)
    plist.write_text("<velho/>")
    plist.chmod(0o644)

    assert _run(amb, "install", TOKEN).returncode == 0
    assert _modo(plist) == 0o600
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][-2] == "--token-file"


def test_install_respeita_label_posicional(amb):
    assert _provision(amb).returncode == 0
    r = _run(amb, "install", "--token-file", str(_token_file(amb)), "com.exemplo.outro")
    assert r.returncode == 0, _saida(r)
    assert _plist(amb, "com.exemplo.outro").is_file()
    assert not _plist(amb).exists()


def test_install_sem_token_reclama_e_nao_cria_plist(amb):
    r = _run(amb, "install")
    assert r.returncode == 1
    assert "install <token>" in _saida(r)
    assert not _plist(amb).exists()


def test_install_avisa_e_falha_quando_o_launchd_recusa(amb):
    """F1: bootstrap/load têm stderr suprimido — sem conferência o install dizia
    "instalado" e saía 0 com o agente recusado."""
    amb["T_LAUNCHCTL_FALHA"] = "1"
    r = _run(amb, "install", TOKEN)
    assert r.returncode == 1
    assert "AVISO" in r.stderr and "NÃO ficou carregado" in r.stderr
    assert "Conector instalado" not in r.stdout
    assert _plist(amb).exists()          # o plist é escrito; o problema é o launchd


# ------------------------------------------------------- token / status / uso

def test_token_e_o_unico_ponto_que_revela(amb):
    assert _provision(amb).returncode == 0
    r = _run(amb, "token")
    assert r.returncode == 0
    assert r.stdout.rstrip("\n") == TOKEN


def test_status_sai_nao_zero_quando_o_agente_nao_esta_carregado(amb):
    amb["T_LAUNCHCTL_FALHA"] = "1"
    r = _run(amb, "status")
    assert r.returncode == 1
    assert "não carregado" in r.stdout


def test_status_filtra_o_token_do_launchctl_print(amb):
    _instala_por_arquivo(amb)
    r = _run(amb, "status")
    assert r.returncode == 0
    assert "state = running" in r.stdout
    assert TOKEN not in r.stdout


def test_uso_quando_nao_tem_subcomando(amb):
    r = _run(amb)
    assert r.returncode == 1
    assert "install --token-file" in r.stdout