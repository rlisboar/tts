"""remote/deploy.sh: deploy reproduzível/verificável sem tocar em máquina remota.

`ssh`/`scp` são falsos: o "servidor" é um diretório em tmp e o comando remoto roda
no zsh local (é assim que o quoting e os caminhos do script são exercitados de
verdade). `systemctl`, `sha256sum`, `nvidia-smi` e `curl` também são shims.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "remote" / "deploy.sh"
ZSH = shutil.which("zsh")

pytestmark = [
    pytest.mark.skipif(ZSH is None, reason="precisa de zsh"),
    pytest.mark.skipif(os.uname().sysname != "Darwin", reason="shims são de macOS"),
]

CHAVE_SEGREDA = "segredo"          # valor que não pode aparecer no `recon`
UNIT_VOXTRAL = "voxtral.service"
UNIT_OMNI = "omnivoice-tts.service"


def _shim(path: Path, body: str) -> None:
    path.write_text("#!/bin/zsh\n" + body)
    path.chmod(0o755)


@pytest.fixture(scope="session")
def shims(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("shims-deploy")
    _shim(d / "ssh", """
# ssh falso: ignora opções, pula o host, roda o comando no zsh local
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
host="${args[1]}"; cmd="${(j: :)args[2,-1]}"
print -r -- "$cmd" >> "$T_SSH_LOG"
exec /bin/zsh -c "$cmd"
""")
    _shim(d / "scp", """
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
origem="${args[1]}"; destino="${args[2]}"; caminho="${destino#*:}"
mkdir -p "${caminho:h}"
cp "$origem" "$caminho"
print -r -- "$origem -> $caminho" >> "$T_SCP_LOG"
""")
    _shim(d / "systemctl", """
case "${1:-}" in
  restart) print -r -- "${2:-}" >> "$T_RESTART_LOG" ;;
  is-active) print -r -- active ;;
  show) print -r -- "FragmentPath=/etc/systemd/system/${@[-1]}.d/override.conf"
        print -r -- "Environment=OMNI_API_KEY=$T_SEGREDO OMNI_ALLOW_NO_AUTH=0 VOXTRAL_API_KEY=x" ;;
  list-units) print -r -- "omnivoice-tts.service loaded active running OmniVoice" ;;
esac
exit 0
""")
    _shim(d / "sha256sum", 'exec /usr/bin/shasum -a 256 "$@"\n')
    _shim(d / "nvidia-smi", 'print -r -- "NVIDIA GeForce RTX 4090, 24564 MiB, 10 MiB"\n')
    _shim(d / "curl", """
metodo=GET; url=""
args=("$@")
for ((i = 1; i <= $#; i++)); do
  case "${@[i]}" in
    -X) metodo="${@[i+1]}" ;;
    http*) url="${@[i]}" ;;
  esac
done
print -r -- "$metodo $url" >> "$T_CURL_LOG"
if [ "$metodo" = POST ]; then
  case " $* " in *" Authorization: Bearer "*) print -rn -- "${T_CURL_AUTH_CODE:-404}" ;;
                 *) print -rn -- "${T_CURL_POST_CODE:-401}" ;;
  esac
elif [[ " $* " == *" -w "* ]]; then
  print -rn -- "${T_CURL_CODE:-200}"
else
  corpo="${T_CURL_BODY:-}"
  [ -z "$corpo" ] && corpo='{"ok":true,"auth":"required"}'
  print -rn -- "$corpo"
fi
""")
    return d


@pytest.fixture
def amb(tmp_path: Path, shims: Path) -> dict[str, str]:
    """Servidor falso: dirs com server.py/auth_policy.py + ambiente do script."""
    raiz = tmp_path / "servidor"
    for d, servidor in (("omnivoice", "omni_server.py"), ("voxtral", "voxtral_server.py")):
        (raiz / d).mkdir(parents=True)
        (raiz / d / "server.py").write_text((REPO / "remote" / servidor).read_text())
        (raiz / d / "auth_policy.py").write_text((REPO / "remote" / "auth_policy.py").read_text())

    env = dict(os.environ)
    env.update(
        PATH=f"{shims}:{env['PATH']}",
        TTS_REMOTE_HOST="rtx-fake",
        TTS_REMOTE_ESPERA="0",
        OMNI_REMOTE_DIR=str(raiz / "omnivoice"),
        VOXTRAL_REMOTE_DIR=str(raiz / "voxtral"),
        OMNI_REMOTE_UNIT=UNIT_OMNI,
        VOXTRAL_REMOTE_UNIT=UNIT_VOXTRAL,
        T_SEGREDO=CHAVE_SEGREDA,
        T_SSH_LOG=str(tmp_path / "ssh.log"),
        T_SCP_LOG=str(tmp_path / "scp.log"),
        T_RESTART_LOG=str(tmp_path / "restart.log"),
        T_CURL_LOG=str(tmp_path / "curl.log"),
    )
    for var in ("OMNI_API_KEY", "VOXTRAL_API_KEY"):
        env.pop(var, None)
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([ZSH, "-f", str(SCRIPT), *args], env=env, capture_output=True,
                          text=True, cwd=str(REPO))


def _saida(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def _servidor(env: dict[str, str], servico: str = "voxtral") -> Path:
    return Path(env["OMNI_REMOTE_DIR" if servico == "omni" else "VOXTRAL_REMOTE_DIR"])


def _log(env: dict[str, str], nome: str) -> list[str]:
    p = Path(env[f"T_{nome.upper()}_LOG"])
    return p.read_text().splitlines() if p.exists() else []


# ------------------------------------------------------------------- compare

def test_compare_igual_quando_o_repo_e_o_ar_batem(amb):
    r = _run(amb, "compare")
    assert r.returncode == 0, _saida(r)
    assert "[IGUAL]" in r.stdout and "voxtral_server.py" in r.stdout
    assert CHAVE_SEGREDA not in r.stdout      # só hashes


def test_compare_acusa_arquivo_divergente_e_ausente(amb):
    (_servidor(amb) / "server.py").write_text("# versao velha no servidor\n")
    (_servidor(amb, "omni") / "auth_policy.py").unlink()

    r = _run(amb, "compare")
    assert r.returncode == 1
    assert "[DIFERENTE]" in r.stdout
    assert "não existe no servidor" in r.stdout


# --------------------------------------------------------------------- recon

def test_recon_mostra_fatos_e_redige_a_chave(amb):
    r = _run(amb, "recon")
    assert r.returncode == 0, _saida(r)
    assert UNIT_VOXTRAL in r.stdout and UNIT_OMNI in r.stdout
    assert "sha256 no ar" in r.stdout and "RTX 4090" in r.stdout
    assert "FragmentPath" in r.stdout
    assert "dep: importlib_resources" in r.stdout          # deps do VAD (task #35)
    assert "silero DeprecationWarning:" in r.stdout
    assert CHAVE_SEGREDA not in r.stdout, "chave vazou no recon"
    assert "OMNI_API_KEY=<len 7>" in r.stdout, "esperava o valor redigido"


@pytest.mark.parametrize("valor", ["abc def", "abc\\ def", '"abc def"'])
def test_recon_nao_vaza_pedaco_de_segredo_com_espaco(amb, valor):
    """P3 do gate da #27: a redação quebrava o stdin em TOKENS por espaço — um
    valor com espaço virava dois pedaços, só o primeiro era redigido e o resto
    saía em claro (e o `len` mentia). Vale para o escape `\\ ` do systemd e aspas."""
    amb["T_SEGREDO"] = valor

    r = _run(amb, "recon")
    assert r.returncode == 0, _saida(r)
    assert f"OMNI_API_KEY=<len {len(valor)}>" in r.stdout, r.stdout
    assert valor not in r.stdout      # nada do valor em claro
    assert "def" not in r.stdout      # o fragmento que vazava antes


# --------------------------------------------------------------------- smoke

def test_smoke_ok_quando_health_responde_e_sem_chave_da_401(amb):
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert r.stdout.count("[ok] /health 200 com campo auth") == 2
    assert "[ok] sem chave → 401" in r.stdout


def test_smoke_falha_com_health_fora_do_ar(amb):
    amb["T_CURL_CODE"] = "502"
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "[FALHA] /health devolveu 502" in r.stdout


def test_smoke_avisa_quando_o_servidor_esta_aberto(amb):
    amb["T_CURL_BODY"] = '{"ok":true,"auth":"open"}'
    amb["T_CURL_POST_CODE"] = "404"
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert "[aviso] modo ABERTO" in r.stdout


def test_smoke_usa_a_chave_do_ambiente_quando_existe(amb):
    amb["VOXTRAL_API_KEY"] = "k1"
    amb["OMNI_API_KEY"] = "k1"
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert r.stdout.count("[ok] com a chave do ambiente") == 2

    amb["T_CURL_AUTH_CODE"] = "401"     # servidor recusando a chave do ambiente
    assert _run(amb, "smoke").returncode == 1
    assert "[FALHA] a chave de voxtral no ambiente foi recusada" in _run(amb, "smoke").stdout


# -------------------------------------------------------------------- deploy

def test_deploy_dry_run_nao_toca_no_servidor(amb):
    (_servidor(amb) / "server.py").write_text("# velho\n")
    r = _run(amb, "deploy")
    assert r.returncode == 0, _saida(r)
    assert "(dry-run" in r.stdout
    assert _log(amb, "scp") == [] and _log(amb, "restart") == []
    assert (_servidor(amb) / "server.py").read_text() == "# velho\n"


def test_deploy_apply_faz_backup_copia_restarta_e_smoke(amb):
    (_servidor(amb) / "server.py").write_text("# velho\n")

    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "backup:" in r.stdout and "sintaxe-ok" in r.stdout

    bak = list(_servidor(amb).glob("server.py.bak-*"))
    assert len(bak) == 1 and bak[0].read_text() == "# velho\n"
    assert (_servidor(amb) / "server.py").read_text() == (REPO / "remote" / "voxtral_server.py").read_text()
    assert _log(amb, "restart") == [UNIT_VOXTRAL]
    assert any("server.py" in linha for linha in _log(amb, "scp"))
    assert "[ok] /health 200" in r.stdout


def test_deploy_apply_sem_diff_nao_reinicia(amb):
    assert _run(amb, "deploy", "--apply").returncode == 0
    assert _log(amb, "restart") == []          # já estava igual: nada subiu
    assert "nada a fazer" in _run(amb, "deploy", "--apply").stdout


def test_service_limita_o_alvo_e_rollback_restaura(amb):
    (_servidor(amb, "omni") / "server.py").write_text("# omni velho\n")
    r = _run(amb, "deploy", "--apply", "--service", "omni")
    assert r.returncode == 0, _saida(r)
    assert _log(amb, "restart") == [UNIT_OMNI]
    bak = sorted(_servidor(amb, "omni").glob("server.py.bak-*"))
    assert bak and bak[-1].read_text() == "# omni velho\n"

    assert _run(amb, "rollback", "--apply", "--service", "omni").returncode == 0
    assert (_servidor(amb, "omni") / "server.py").read_text() == "# omni velho\n"
    assert _log(amb, "restart") == [UNIT_OMNI, UNIT_OMNI]


def test_rollback_dry_run_e_sem_backup(amb):
    r = _run(amb, "rollback", "--service", "voxtral")
    assert r.returncode == 0
    assert "[aviso] nenhum server.py.bak-*" in r.stdout

    (_servidor(amb) / "server.py").write_text("# ar velho\n")
    assert _run(amb, "deploy", "--apply", "--service", "voxtral").returncode == 0
    r = _run(amb, "rollback", "--service", "voxtral")
    assert "(dry-run" in r.stdout
    assert _log(amb, "restart") == [UNIT_VOXTRAL]      # dry-run não reinicia
    assert (_servidor(amb) / "server.py").read_text() == (REPO / "remote" / "voxtral_server.py").read_text()


def test_host_por_ambiente_e_argumento(amb):
    amb["TTS_REMOTE_HOST"] = ""
    assert _run(amb, "compare").returncode == 2
    assert "informe o host" in _run(amb, "compare").stderr
    amb["TTS_REMOTE_HOST"] = "rtx-fake"
    assert _run(amb, "compare").returncode == 0
    assert _run(amb, "compare", "outro-host").returncode == 0