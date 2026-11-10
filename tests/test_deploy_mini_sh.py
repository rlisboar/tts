"""remote/deploy_mini.sh: deploy da produção (Mac mini / tts.the-dudes.com).

Sem rede: `ssh`, `launchctl` e `curl` são shims; o "mini" é um clone git de
verdade em tmp (com origin bare local), então push/fetch/merge/checkout são
exercitados de fato. O comando "remoto" roda no zsh local, como no
tests/test_deploy_sh.py — é assim que o quoting e os caminhos aparecem.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "remote" / "deploy_mini.sh"
ZSH = shutil.which("zsh")

pytestmark = [
    pytest.mark.skipif(ZSH is None, reason="precisa de zsh"),
    pytest.mark.skipif(os.uname().sysname != "Darwin", reason="shims são de macOS"),
]

CHAVE = "chave-do-mini-123"
MODULOS = ("app.py", "common.py", "backends.py", "tts_worker.py",
           "live_pipeline.py", "live_turns.py", "dsh_client.py")
LABEL = "studio.tts.server"


def _shim(path: Path, body: str) -> None:
    path.write_text("#!/bin/zsh\n" + body)
    path.chmod(0o755)


def _git(*args: str, cwd: Path, env: dict[str, str]) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stdout}{r.stderr}"
    return r.stdout.strip()


@pytest.fixture(scope="session")
def shims(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("shims-deploy-mini")
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
cmd="${args[-1]}"
print -r -- "$cmd" >> "$T_SSH_LOG"
exec /bin/zsh -c "$cmd"
""")
    _shim(d / "launchctl", """
print -r -- "$*" >> "$T_LAUNCH_LOG"
case "${1:-}" in
  kickstart) print -r -- kickstart-ok ;;
  list) print -r -- "80680\\t0\\t${TTS_MINI_LABEL:-studio.tts.server}" ;;
esac
exit 0
""")
    _shim(d / "curl", """
# curl falso: decide pela URL; -w '%{http_code}' imprime só o código
url=""; tem_chave=0; so_codigo=0; arquivo=""; metodo=GET; http1=0
for ((i = 1; i <= $#; i++)); do
  case "${@[i]}" in
    http*) url="${@[i]}" ;;
    -X) metodo="${@[i+1]}" ;;
    -m) tempo="${@[i+1]}"; tem_m=1 ;;
    -H) [[ "${@[i+1]}" == X-API-Key:* ]] && tem_chave=1
        # `-H @arquivo` (#1075/#1108): o curl real lê o header do arquivo
        if [[ "${@[i+1]}" == @* ]]; then
          hf="${${@[i+1]}#@}"
          [ -n "${T_CURL_HDR_LOG:-}" ] && print -r -- "$hf $(stat -f %Lp "$hf" 2>/dev/null)" >> "$T_CURL_HDR_LOG"
          grep -q '^X-API-Key: ' "$hf" 2>/dev/null && tem_chave=1
        fi ;;
    --http1.1) http1=1 ;;
    -w) so_codigo=1 ;;
  esac
done
# `-m` não numérico: o curl REAL sai rc=2 sem corpo (#1076). O shim ignorava o
# `-m`, então a guarda do TIMEOUT ficava sem mordida — com o valor cru na linha
# de comando toda leitura "divergia" e um deploy BOM era revertido pelo #345.
if [ "${tem_m:-0}" = 1 ]; then
  case "${tempo:-}" in
    ''|*[!0-9]*) print -r -- "curl: option -m: expected a proper numerical parameter" >&2; exit 2 ;;
  esac
fi
print -r -- "$metodo $url" >> "$T_CURL_LOG"
# argv inteiro (#1108): a célula da sonda procura a chave aqui — é o que `ps` veria
[ -n "${T_CURL_ARGV:-}" ] && print -r -- "$*" >> "$T_CURL_ARGV"
codigo=200; corpo=""; arquivo=""
case "$url" in
  */ttsproxy/ttsproxy/*) codigo="${T_CURL_PREFIXO_DUPLO_CODE:-401}" ;;
  */health)
      # `T_CURL_HOST_CODE` só vale para a requisição que manda `Host:` — é a
      # sonda da allowlist do #779, e as outras checagens de /health seguem 200
      if [ -n "${T_CURL_HOST_CODE:-}" ]; then
        case "$*" in *"Host: "*) codigo="$T_CURL_HOST_CODE"; corpo='' ;; *) codigo="${T_CURL_HEALTH_CODE:-200}"; corpo='{"ok":true}' ;; esac
      else
        codigo="${T_CURL_HEALTH_CODE:-200}"; corpo='{"ok":true}'
      fi ;;
  */api/voices)
      if [ $tem_chave = 1 ]; then codigo="${T_CURL_CHAVE_CODE:-200}"; else codigo="${T_CURL_SEM_CHAVE_CODE:-401}"; fi ;;
  */api/tts/jobs/*/pieces/*) codigo="${T_CURL_PIECE_CODE:-200}"; corpo="RIFF-fake-audio" ;;
  */api/tts/jobs/*)
      if [ $tem_chave = 1 ]; then corpo="{\\"status\\":\\"${T_CURL_JOB_STATUS:-done}\\"}"; else codigo=401; fi ;;
  */api/tts)
      # o job de síntese é quem escreveria o aviso de marca d'água no log (#938)
      [ -n "${T_CURL_LOG_MARCA:-}" ] && print -r -- "$T_CURL_LOG_MARCA" >> "$T_MINI_LOG"
      if [ $tem_chave = 1 ] && [ "$metodo" = POST ]; then corpo='{"job_id":"j1"}'
      elif [ $tem_chave = 1 ]; then corpo='{"job_id":"j1"}'
      else codigo=401; fi ;;
  */api/build)
      if [ $tem_chave = 1 ]; then
        codigo="${T_CURL_BUILD_CODE:-200}"
        # `codigo` = hash dos módulos do mini AGORA (ou o forçado pelo teste) e
        # boot_ms curto: é a instância viva que o smoke confere. ${=} força a
        # separação por espaço (zsh não separa variável sem aspas).
        c="${T_CURL_BUILD_CODIGO:-$(cd "$T_MINI_DIR" && cat ${=T_MODULOS} 2>/dev/null | /usr/bin/shasum -a 256 | cut -c1-8)}"
        # boot_ms coerente com o marcador do restart (#252): como o mini real,
        # o shim reporta a idade do processo desde .deploy-mini-boot; override
        # explícito (T_CURL_BOOT_MS) continua valendo para cenários dirigidos.
        bm="${T_CURL_BOOT_MS:-}"
        if [ -z "$bm" ]; then
          mk="$(cat "$T_MINI_DIR/.deploy-mini-boot" 2>/dev/null)"
          if [ -n "$mk" ]; then bm=$(( ( $(date +%s) - mk ) * 1000 )); fi
        fi
        bm="${bm:-1000}"
        [ "$codigo" = 200 ] && corpo="{\\"ok\\":true,\\"codigo\\":\\"$c\\",\\"boot_ms\\":$bm}"
      else codigo=401; fi ;;
  */api/live/ticket)
      # contrato do #1107: POST com a chave no header → {"ticket", "expires_in"}
      if [ $tem_chave = 1 ] && [ "$metodo" = POST ]; then
        codigo="${T_CURL_TICKET_CODE:-200}"
        [ "$codigo" = 200 ] && corpo='{"ticket":"tk-fake_0123-ABC","expires_in":60}'
      else codigo=401; fi ;;
  */api/live/ws*)
      # o upgrade é mecanismo h1: pelo Cloudflare em h2 o origin recebe SEM upgrade
      # e o middleware devolve 401 (navegador, h1, conecta). O shim imita isso para
      # que o probe sem --http1.1 false-vermelhe igual à produção (#247).
      print -r -- "WS $url h1=$http1" >> "$T_CURL_LOG"
      if [ $http1 = 1 ]; then codigo="${T_CURL_WS_CODE:-101}"; else codigo="${T_CURL_WS_CODE_H2:-401}"; fi ;;
  */) arquivo="$T_MINI_DIR/static/index.html"; nonce=1 ;;
esac
# `T_CURL_CODE_PRIMEIRO`: o código sai quebrado enquanto o serviço só foi
# reiniciado uma vez (o restart do próprio deploy) e normal depois do segundo —
# é como o teste distingue "o rev novo quebrou" de "o rollback salvou" sem
# depender do conteúdo dos módulos.
if [ $so_codigo = 1 ] && [ -n "${T_CURL_CODE_PRIMEIRO:-}" ]; then
  n=$(wc -l < "$T_LAUNCH_LOG" 2>/dev/null | tr -d ' ')
  [ "${n:-0}" -le 1 ] && codigo="$T_CURL_CODE_PRIMEIRO"
fi
if [ $so_codigo = 1 ]; then print -rn -- "$codigo"
elif [ -n "$arquivo" ] && [ -n "${T_CURL_INDEX_CORTA_N:-}" ] \\
     && [ "$(( $(cat "$T_CURL_INDEX_CONTA" 2>/dev/null || print 0) + 1 ))" -le "$T_CURL_INDEX_CORTA_N" ]; then
  # #1051: as primeiras N leituras do index saem CORTADAS no transporte (meio
  # arquivo + rc 28 do timeout do curl) — o falso vermelho do deploy do 48b11d5
  print -r -- "$(( $(cat "$T_CURL_INDEX_CONTA" 2>/dev/null || print 0) + 1 ))" > "$T_CURL_INDEX_CONTA"
  head -c 7 "$arquivo"
  exit 28
elif [ -n "$arquivo" ]; then
  # o app real injeta nonce="…" POR REQUEST no HTML servido (CSP): o shim imita
  if [ -n "${nonce:-}" ]; then sed -E 's/<html/<html nonce="rnd${RANDOM}"/' "$arquivo"
  else cat "$arquivo"; fi      # cat direto: preserva o \\n final do arquivo
else print -rn -- "$corpo"; fi
""")
    return d


@pytest.fixture
def amb(tmp_path: Path, shims: Path) -> dict[str, str]:
    """origin bare + clone do mini (em cb36edb falso) + clone de dev (com HEAD novo)."""
    env = dict(os.environ)
    # Um commit parcial (`git commit -- <paths>`) exporta GIT_INDEX_FILE para o
    # hook, e o hook roda este pytest: se o fixture herdasse, os subprocessos
    # git daqui (add -A no semente, commit...) escreveriam as entradas do
    # repositório-fixture (.venv-mlx/bin/pip, blob que não existe no nosso
    # object DB) NO índice do commit — o 'invalid object / Error building
    # trees' que derrubou commits desde o passo 2 (#257). Limpa o ambiente
    # herdado do git antes de semear os repositórios de teste.
    for _var in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(_var, None)
    env.update(
        PATH=f"{shims}:{env['PATH']}",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"), GIT_CONFIG_SYSTEM="/dev/null",
        T_SSH_LOG=str(tmp_path / "ssh.log"),
        T_LAUNCH_LOG=str(tmp_path / "launch.log"),
        T_CURL_LOG=str(tmp_path / "curl.log"),
        T_CURL_ARGV=str(tmp_path / "curl.argv"),
        T_CURL_HDR_LOG=str(tmp_path / "curl.hdr"),
        T_PIP_LOG=str(tmp_path / "pip.log"),
        T_PIP_FREEZE_N=str(tmp_path / "freeze.n"),
        T_MINI_DIR=str(tmp_path / "mini"),
        T_MINI_LOG=str(tmp_path / "mini.log"),
        TTS_MINI_LOG=str(tmp_path / "mini.log"),
        T_MODULOS=" ".join(MODULOS),
        TTS_MINI_HOST="mini-fake",
        TTS_MINI_DIR=str(tmp_path / "mini"),
        TTS_MINI_LABEL=LABEL,
        TTS_MINI_ESPERA="0",
        TTS_MINI_INDEX_PAUSA="0",                  # #1051: releitura sem dormir
        T_CURL_INDEX_CONTA=str(tmp_path / "index.conta"),
        TTS_MINI_REPO=str(tmp_path / "dev"),
        TTS_PUBLIC_HOST="tts.exemplo.test",
        # preview isolado por teste (#240): o /tmp GLOBAL do script é estado
        # compartilhado entre processos concorrentes — só UM teste o exercita
        TTS_MINI_PREVIEW=str(tmp_path / "preview-requirements"),
        TTS_MINI_LOCK_PREVIEW=str(tmp_path / "preview-lock"),
        # #721: o smoke lê `$HOME/Library/Application Support/.../.apikey`
        # antes do espelho do repo. Sem este HOME, o shim acha a chave do
        # dono e `test_smoke_avisa_sem_a_chave_do_mini` não vê o aviso.
        HOME=str(tmp_path / "home"),
    )
    (tmp_path / "home").mkdir()
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")

    origem = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(origem), cwd=tmp_path, env=env)

    semente = tmp_path / "semente"
    semente.mkdir()
    for m in MODULOS:
        (semente / m).write_text(f"# {m} versao 1\n")
    # o app.py do fixture espelha o de verdade nas rotas que o smoke checa por REV:
    # a base (v1) NÃO as tem; a v2 as introduz (como o #190 e o épico Live fizeram).
    (semente / "app.py").write_text(APP_SEM_ROTAS)
    (semente / "static").mkdir()
    (semente / "static" / "index.html").write_text("<html>v1</html>\n")
    (semente / "requirements.txt").write_text("fastapi\n")   # v2 acrescenta websockets
    (semente / ".venv-mlx" / "bin").mkdir(parents=True)
    _shim(semente / ".venv-mlx" / "bin" / "python", """
case " $* " in
  *" -V "*) print -r -- "Python 3.12.4" ;;
  *) case " $* " in
       *find_spec*) print -r -- "websockets ${T_DEP_WEBSOCKETS:-True}"
                    print -r -- "importlib_resources ${T_DEP_IMPORTLIB:-True}" ;;
       *) print -r -- "ok" ;;
     esac ;;
esac
exit 0
""")
    _shim(semente / ".venv-mlx" / "bin" / "pip", """
case "$1" in
  freeze)
    # 1ª chamada do run = freeze do ANTES (backup); seguintes = DEPOIS (#343).
    n=$(( $(cat "$T_PIP_FREEZE_N" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$T_PIP_FREEZE_N"
    if [ "$n" -gt 1 ] && [ -n "${T_PIP_FREEZE_DEPOIS:-}" ]; then
      print -r -- "${T_PIP_FREEZE_DEPOIS}"
    else
      print -r -- "${T_PIP_FREEZE:-fastapi==1.0}"
    fi ;;
  install)
    print -r -- "$*" >> "$T_PIP_LOG"
    if [[ " $* " == *" --dry-run "* ]]; then
      print -r -- "${T_PIP_DRY:-Requirement already satisfied: fastapi in ./.venv-mlx}"
    elif [ -n "${T_PIP_FALHA:-}" ]; then
      print -r -- "ERROR: ResolutionImpossible: conflito com requirements.lock"
      exit 1
    else
      print -r -- "Successfully installed websockets-17.1"
    fi ;;
esac
exit 0
""")
    (semente / ".apikey").write_text(CHAVE + "\n")
    _git("init", "-q", cwd=semente, env=env)
    _git("add", "-A", cwd=semente, env=env)
    _git("commit", "-qm", "base", cwd=semente, env=env)
    _git("remote", "add", "origin", str(origem), cwd=semente, env=env)
    _git("push", "-q", "origin", "main", cwd=semente, env=env)

    # dev: de onde o deploy publica — DOIS commits à frente do origin (v2 traz as
    # rotas; v3 é o HEAD), para haver um corte antigo de verdade a que apontar.
    dev = tmp_path / "dev"
    _git("clone", "-q", str(origem), str(dev), cwd=tmp_path, env=env)
    (dev / "app.py").write_text(APP_COM_ROTAS)
    (dev / "requirements.txt").write_text("fastapi\nwebsockets\n")
    # v2 introduz o LOCK (#343): a v1 (base) não tem, e é isso que exercita o
    # aviso "rev sem teto de versão".
    (dev / "requirements.lock").write_text("fastapi==1.0\n")
    (dev / "static" / "index.html").write_text("<html>v2</html>\n")
    _git("add", "-A", cwd=dev, env=env)
    _git("commit", "-qm", "v2", cwd=dev, env=env)
    (dev / "static" / "index.html").write_text("<html>v3</html>\n")
    _git("add", "-A", cwd=dev, env=env)
    _git("commit", "-qm", "v3", cwd=dev, env=env)
    # o mini: clone de produção, parado no commit antigo
    _git("clone", "-q", str(origem), str(tmp_path / "mini"), cwd=tmp_path, env=env)
    return env


# #1111: o script roda SEM terminal de controle, como do outro lado de um `ssh`
# sem `-t` (BatchMode) — que é onde os comandos de `distante` rodam em produção.
# O shim de `ssh` faz `exec zsh -c` LOCAL, então herdava o tty de quem roda a
# suíte. Sob `screen` + `timeout` sem `--foreground` o pytest fica num grupo de
# FUNDO desse tty, e o `read -r -d $'\0'` do `pesos_faltando` (zsh mexe no modo do
# terminal) leva SIGTTOU: o grupo para, o `timeout` sai com 1 e a suíte cheia
# morre sem resumo em `test_peso_ausente_*` (~39 %). `start_new_session` tira o
# tty de toda a árvore do script; `stdin` nulo fecha a outra porta.
_SEM_TTY = dict(stdin=subprocess.DEVNULL, start_new_session=True)


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([ZSH, "-f", str(SCRIPT), *args], env=env, capture_output=True,
                          text=True, cwd=str(REPO), **_SEM_TTY)


def _saida(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def _log(env: dict[str, str], nome: str) -> list[str]:
    p = Path(env[f"T_{nome.upper()}_LOG"])
    return p.read_text().splitlines() if p.exists() else []


APP_SEM_ROTAS = "# app.py versao 1 (sem as rotas novas)\n"
APP_COM_ROTAS = ('# app.py versao 2\n'
                 '@app.get("/api/build")\n'
                 'def build(): ...\n'
                 '@app.post("/api/live/ticket")\n'
                 'def live_ticket(): ...\n'
                 '@app.websocket("/api/live/ws")\n'
                 'async def live_ws(ws): ...\n')


def _branch(env: dict[str, str], qual: str = "mini") -> str:
    """Branch atual do repo — falha se estiver DETACHED (é o ponto do F3)."""
    cwd = Path(env["TTS_MINI_DIR"] if qual == "mini" else env["TTS_MINI_REPO"])
    return _git("symbolic-ref", "--short", "HEAD", cwd=cwd, env=env)


def _sha(env: dict[str, str], rev: str, qual: str = "dev") -> str:
    cwd = Path(env["TTS_MINI_DIR"] if qual == "mini" else env["TTS_MINI_REPO"])
    return _git("rev-parse", rev, cwd=cwd, env=env)


def _arvore(env: dict[str, str]) -> dict[str, str]:
    """Snapshot da ÁRVORE do mini (caminho → sha256), fora do .git: é o que o
    dry-run não pode mexer (o gate pediu snapshot, não só HEAD/launch)."""
    raiz = Path(env["TTS_MINI_DIR"])
    return {
        str(f.relative_to(raiz)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(raiz.rglob("*")) if f.is_file() and ".git" not in f.parts
    }


def _preview_do_output(saida: str) -> str:
    """Caminho do preview impresso pelo deploy (único por execução, #240)."""
    for linha in saida.splitlines():
        if "preview:" in linha:
            return linha.split("preview:")[1].split("(")[0].strip()
    raise AssertionError(f"o deploy não imprimiu o caminho do preview:\n{saida}")


def _head(env: dict[str, str], qual: str = "mini") -> str:
    cwd = Path(env["TTS_MINI_DIR"] if qual == "mini" else env["TTS_MINI_REPO"])
    return _git("rev-parse", "HEAD", cwd=cwd, env=env)


def _origin_head(env: dict[str, str]) -> str:
    return _git("rev-parse", "main", cwd=Path(env["TTS_MINI_REPO"]).parent / "origin.git", env=env)


# --------------------------------------------------------------------- recon

def test_recon_mostra_fatos_e_nao_vaza_a_chave(amb):
    r = _run(amb, "recon")
    assert r.returncode == 0, _saida(r)
    assert "HEAD " in r.stdout and "distância até origin/main" in r.stdout
    assert "dep: websockets" in r.stdout and "importlib_resources" in r.stdout
    assert LABEL in r.stdout and "POST /api/tts no log" in r.stdout
    assert CHAVE not in r.stdout, "a chave do mini vazou no recon"


def test_recon_avisa_sem_estado_de_deploy(amb):
    assert "(sem .deploy-mini-estado)" in _run(amb, "recon").stdout


# ------------------------------------------------------------------- compare

def test_compare_acusa_producao_atras_e_aponta_o_ff(amb):
    r = _run(amb, "compare")
    assert r.returncode == 1
    assert "[DIFERENTE] produção 2 commit(s) atrás do rev alvo" in r.stdout
    assert "alvo é descendente da produção" in r.stdout
    assert "[DIFERENTE] app.py" in r.stdout and "[DIFERENTE] static/index.html" in r.stdout
    assert CHAVE not in r.stdout


def test_compare_igual_depois_do_deploy(amb):
    assert _run(amb, "deploy", "--apply").returncode == 0
    r = _run(amb, "compare")
    assert r.returncode == 0, _saida(r)
    assert "[IGUAL] produção no rev alvo" in r.stdout
    assert "[ok] limpa" in r.stdout


def test_compare_acusa_arvore_suja_no_mini(amb):
    (Path(amb["TTS_MINI_DIR"]) / "app.py").write_text("# sujeira local no mini\n")
    r = _run(amb, "compare")
    assert r.returncode == 1
    assert "rastreados modificados no mini" in r.stdout


# --------------------------------------------------------------------- smoke

def test_smoke_falha_quando_o_ar_nao_e_o_rev_alvo(amb):
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "index.html" in r.stdout and "≠ rev alvo" in r.stdout
    assert "[ok] LAN /health 200" in r.stdout and "[ok] público /health 200" in r.stdout
    assert "[ok] LAN sem chave → 401" in r.stdout


def test_smoke_ok_quando_o_mini_esta_no_rev_alvo(amb):
    assert _run(amb, "deploy", "--apply").returncode == 0
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert r.stdout.count("/api/build codigo=") == 2      # LAN + público
    assert "WebSocket 101" in r.stdout


def test_smoke_falha_se_sem_chave_nao_da_401(amb):
    amb["T_CURL_SEM_CHAVE_CODE"] = "200"
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "sem chave devolveu 200" in r.stdout


def test_smoke_falha_quando_o_prefixo_duplo_responde_2xx(amb):
    """Bypass de auth pelo prefixo duplo (#778): 2xx sem chave reprova o deploy.

    Com `root_path` + remoção manual do prefixo, o Starlette tira o `root_path` de
    novo e a rota protegida casa SEM passar pela chave — qualquer aparelho da LAN
    lê settings/vozes e sintetiza com a voz do dono. 401 é o certo; 404 é rev
    anterior à correção; 2xx é o furo."""
    amb["T_CURL_PREFIXO_DUPLO_CODE"] = "200"
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "prefixo duplo sem chave devolveu 200" in r.stdout


def test_smoke_aceita_401_do_prefixo_duplo_na_rev_corrigida(amb):
    """A resposta da rev CORRIGIDA é 401: `_exige_chave` nega por padrão ANTES do
    roteamento e `_caminho_roteado` tira o prefixo uma vez só (`/ttsproxy/…` cai
    fora de `_CAMINHOS_PUBLICOS`). Sem esta célula, uma sonda que só aceitasse 404
    passaria aqui e ficaria vermelha no primeiro deploy real."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert "prefixo duplo sem chave → 401" in r.stdout


def test_start_server_exporta_o_host_publico():
    """O ÚNICO lugar que vale é o `start-server.sh` (#837).

    O plist não chega ao app: o agente sobe `ssh ... start-server.sh` e o sshd não
    herda o ambiente do launchd sem `SendEnv`/`AcceptEnv`. Se o `export` sair do
    script, a sonda inteira vira decoração — o app responde 421 e ninguém vê."""
    script = (Path(__file__).resolve().parent.parent
              / "remote" / "launchd" / "start-server.sh").read_text()
    assert "TTS_ALLOWED_HOSTS" in script, "o start-server.sh parou de exportar a allowlist"
    assert "tts.the-dudes.com" in script, "o host público saiu do default do export"


def test_smoke_reprova_quando_o_host_publico_da_421(amb):
    """421 = nome público fora de `TTS_ALLOWED_HOSTS` (#779/#837).

    O túnel entrega `Host: tts.the-dudes.com`; sem o nome na env o app recusa e a
    página fica fora do ar. O smoke tem de acusar isso mesmo com o túnel caído —
    por isso a checagem é na LAN, com o `Host` do público."""
    amb["T_CURL_HOST_CODE"] = "421"
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "Host público" in r.stdout and "421" in r.stdout
    assert "TTS_ALLOWED_HOSTS" in r.stdout


def test_smoke_avisa_mas_passa_no_404_da_rev_antiga(amb):
    """404 passa (é o caminho de rollback), mas AVISA: no rev antigo o bypass do
    #777 segue aberto. O operador não pode ler o verde como prova (#806)."""
    amb["T_CURL_PREFIXO_DUPLO_CODE"] = "404"
    assert _run(amb, "deploy", "--apply").returncode == 0
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert "[aviso]" in r.stdout and "NÃO está coberto" in r.stdout


def test_smoke_avisa_quando_o_ws_nao_sobe(amb):
    amb["T_CURL_WS_CODE"] = "401"      # produção sem a rota (ou sem websockets)
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "/api/live/ws devolveu 401" in r.stdout


def test_ws_probe_faz_handshake_h1_como_o_navegador(amb):
    """Achado do passo 2 NO AR (#247): o upgrade é mecanismo h1 — pelo Cloudflare
    (h2 entre cliente e edge) o pedido chega no origin SEM upgrade e o middleware
    devolve 401, embora o navegador (h1) conecte. Sem `--http1.1` o smoke
    false-vermelha o caminho público (o shim imita: h2 → 401)."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_CURL_LOG"]).unlink()          # o deploy já rodou um smoke; isola o meu
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    ws = [l for l in _log(amb, "curl") if l.startswith("WS ")]
    assert len(ws) == 2, ws                                # LAN + público
    assert all(l.endswith("h1=1") for l in ws), ws        # as duas com --http1.1
    assert r.stdout.count("WebSocket 101") == 2


TICKET_FAKE = "tk-fake_0123-ABC"


def _smoke_isolado(amb, tmp_path, **depois):
    """Deploy (fora da conta) e depois UM smoke com logs zerados e TMPDIR próprio.
    `depois` entra no ambiente só para o smoke (o deploy tem de sair verde)."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb.update(depois)
    for nome in ("T_CURL_LOG", "T_CURL_ARGV", "T_CURL_HDR_LOG"):
        Path(amb[nome]).unlink(missing_ok=True)
    tmpd = tmp_path / "tmpd-smoke"
    tmpd.mkdir()
    amb["TMPDIR"] = str(tmpd)
    return _run(amb, "smoke"), tmpd


def test_sonda_do_ws_usa_ticket_e_a_chave_nao_vai_a_url_nem_ao_argv(amb, tmp_path):
    """#1108: a sonda abria `/api/live/ws?key=<chave>` — a chave do dono caía no
    access log do mini a cada deploy (44 handshakes + 36 avisos `[live] auth por
    ?key=`) e ficava no argv do `curl` (visível em `ps`). Agora: POST
    /api/live/ticket com a chave num arquivo 0600 (`-H @arquivo`, apagado depois)
    e o WS com `?ticket=`. Voltar o `?key=` (ou a chave no `-H` do ticket) derruba
    esta célula."""
    r, tmpd = _smoke_isolado(amb, tmp_path)
    assert r.returncode == 0, _saida(r)
    log = _log(amb, "curl")
    # nenhuma URL da sonda (nem de nada no smoke) leva a chave na query
    assert not [l for l in log if "key=" in l], log
    tickets = [l for l in log if l.startswith("POST ") and l.endswith("/api/live/ticket")]
    assert len(tickets) == 2, log                                # LAN + público
    ws = [l for l in log if l.startswith("WS ")]
    assert len(ws) == 2, ws
    assert all(f"/api/live/ws?ticket={TICKET_FAKE} " in l for l in ws), ws
    # argv das chamadas do Live: a chave não aparece (é o que `ps` mostraria)
    argv_live = [l for l in Path(amb["T_CURL_ARGV"]).read_text().splitlines()
                 if "/api/live/" in l]
    assert len(argv_live) == 4, argv_live                        # 2 tickets + 2 WS
    assert not [l for l in argv_live if CHAVE in l], argv_live
    # o header foi por arquivo 0600, e o arquivo não sobra no TMPDIR
    hdr = Path(amb["T_CURL_HDR_LOG"]).read_text().splitlines()
    assert len(hdr) == 2, hdr
    assert all(l.endswith(" 600") for l in hdr), hdr
    assert all(not Path(l.rsplit(" ", 1)[0]).exists() for l in hdr), hdr
    assert not list(tmpd.glob("deploy-mini-hdr.*"))
    assert r.stdout.count("POST /api/live/ticket emitiu o ticket") == 2
    assert CHAVE not in _saida(r)


def test_sonda_reprova_sem_ticket_e_nunca_cai_para_a_chave(amb, tmp_path):
    """#1108: se a emissão do ticket falha, a UI não abre o Live — o smoke reprova.
    E a sonda NÃO degrada para `?key=` (seria a mesma gravação da chave no log)."""
    r, tmpd = _smoke_isolado(amb, tmp_path, T_CURL_TICKET_CODE="500")
    assert r.returncode == 1, _saida(r)
    assert r.stdout.count("POST /api/live/ticket não emitiu ticket") == 2
    log = _log(amb, "curl")
    assert not [l for l in log if "key=" in l], log
    ws = [l for l in log if l.startswith("WS ")]
    assert ws and all("/api/live/ws h1=" in l for l in ws), ws   # sem credencial
    assert not list(tmpd.glob("deploy-mini-hdr.*"))


def test_rev_sem_rota_do_ticket_sonda_o_ws_sem_credencial(amb, tmp_path):
    """#1108: rev anterior ao ticket (rollback antigo) — a sonda ainda prova o
    upgrade (o app aceita antes de autenticar), sem credencial e sem `?key=`."""
    dev = Path(amb["TTS_MINI_REPO"])
    (dev / "app.py").write_text(APP_COM_ROTAS.replace(
        '@app.post("/api/live/ticket")\ndef live_ticket(): ...\n', ""))
    _git("commit", "-qam", "rev sem ticket", cwd=dev, env=amb)
    r, _ = _smoke_isolado(amb, tmp_path)
    assert r.returncode == 0, _saida(r)
    assert r.stdout.count("rev sem /api/live/ticket") == 2
    log = _log(amb, "curl")
    assert not [l for l in log if "key=" in l or "/api/live/ticket" in l], log
    assert r.stdout.count("WebSocket 101") == 2


def test_smoke_avisa_sem_a_chave_do_mini(amb, tmp_path):
    """#721 lê primeiro a chave do estado. No dono esse arquivo existe, e
    apagar só o `.apikey` do repo não tira a chave — o aviso não saía."""
    (Path(amb["TTS_MINI_DIR"]) / ".apikey").unlink()
    casa = tmp_path / "home-sem-chave"
    casa.mkdir()
    amb["HOME"] = str(casa)
    r = _run(amb, "smoke")
    assert "[aviso] sem a chave do mini" in r.stdout


def test_smoke_forca_instancia_velha_quando_o_codigo_carregado_difere(amb):
    """Código novo no disco mas processo velho (#190/#214): o /api/build denuncia."""
    amb["T_CURL_BUILD_CODIGO"] = "deadbeef"
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "instância não é o rev alvo" in r.stdout


def test_smoke_faz_sintese_real_e_pode_pular(amb):
    """`/health` e auth não provam o caminho que a produção usa: o smoke roda um
    POST /api/tts de verdade e espera o job terminar com peça."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert r.stdout.count("POST /api/tts real") == 2         # LAN + público
    assert any(l.startswith("POST http") and l.endswith("/api/tts") for l in _log(amb, "curl"))

    Path(amb["T_CURL_LOG"]).unlink()
    r = _run(amb, "smoke", "--sem-tts")
    assert r.returncode == 0, _saida(r)
    assert "POST /api/tts real" not in r.stdout
    assert not any(l.endswith("/api/tts") for l in _log(amb, "curl"))


def test_smoke_falha_quando_o_job_de_sintese_erra(amb):
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb["T_CURL_JOB_STATUS"] = "error"
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "terminou em error" in r.stdout


def test_smoke_exige_as_rotas_quando_o_rev_alvo_as_tem(amb):
    """Caso OPOSTO do passo 1 (o do alvo antigo é o outro teste): no rev do HEAD as
    rotas /api/build e /api/live/ws EXISTEM, então o smoke tem de EXIGI-LAS — se as
    duas checagens só souberem ignorar, o passo 2 passa verde com a rota faltando."""
    assert _run(amb, "deploy", "--apply").returncode == 0     # alvo = dev HEAD (tem as rotas)

    amb["T_CURL_BUILD_CODE"] = "404"                          # rota existe e não responde
    r = _run(amb, "smoke")
    assert r.returncode == 1, _saida(r)
    assert '/api/build sem "codigo"' in r.stdout
    assert "ausente NESTE rev" not in r.stdout.split("/api/build")[0][-80:]

    amb.pop("T_CURL_BUILD_CODE")
    amb["T_CURL_WS_CODE"] = "500"                             # rota existe e não sobe
    r = _run(amb, "smoke")
    assert r.returncode == 1, _saida(r)
    assert "/api/live/ws devolveu 500" in r.stdout
    assert "ausente NESTE rev" not in r.stdout


def test_rc_fiel_esperado_para_o_alvo_nao_derruba_e_inesperado_derruba(amb):
    """Item 3 do #235: ponto ESPERADO para o rev alvo (rota que o corte não tem) sai
    [ok] e NÃO derruba o deploy; ponto INESPERADO (instância viva com outro código)
    derruba. O rc do script é o sinal de aceitação da subida — tem de ser fiel."""
    base = _sha(amb, "HEAD~2")                      # v1: sem as rotas
    amb["TTS_MINI_REV"] = base
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)             # esperado-para-o-alvo: passa
    assert "FALHA" not in r.stdout

    amb.pop("TTS_MINI_REV")
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb["T_CURL_BUILD_CODIGO"] = "deadbeef"         # inesperado: carregado ≠ alvo
    assert _run(amb, "deploy", "--apply").returncode == 1


def test_preview_default_e_unico_por_execucao_e_fora_da_arvore(amb):
    """#240: caminho FIXO em /tmp falso-vermelha sob execuções paralelas (um reescreve
    e apaga o arquivo do outro). O default tem de ser único por execução e fora da
    árvore do mini — asserção estática no script + duas execuções seguidas."""
    script = (REPO / "remote" / "deploy_mini.sh").read_text()
    linha = next(l for l in script.splitlines() if l.startswith("REQ_PREVIEW="))
    assert 'TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview-' in linha, linha
    assert "$$" in linha, f"default sem unicidade por execução: {linha}"

    amb.pop("TTS_MINI_PREVIEW")                     # só aqui o DEFAULT é exercitado
    p1 = _preview_do_output(_run(amb, "deploy").stdout)
    p2 = _preview_do_output(_run(amb, "deploy").stdout)
    assert p1 != p2, "duas execuções usaram o MESMO arquivo de preview"
    assert p1.startswith("/tmp/") and not p1.startswith(str(Path(amb["TTS_MINI_DIR"])))


def test_dois_deploys_em_paralelo_nao_disputam_o_preview(amb):
    """Reprodução do #240: duas rodadas simultâneas com o mesmo ambiente. Com caminho
    fixo, uma apaga o arquivo da outra (FileNotFoundError / conteúdo trocado)."""
    # sem override: os dois deploys usam o DEFAULT do script (único por execução) —
    # é o cenário de produção (dois deploys concorrentes), não o do harness isolado
    amb.pop("TTS_MINI_PREVIEW")
    procs = [subprocess.Popen([ZSH, "-f", str(SCRIPT), "deploy"], env=amb,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              **_SEM_TTY)
             for _ in range(2)]
    saidas = [p.communicate()[0] for p in procs]
    rcs = [p.returncode for p in procs]
    assert rcs == [0, 0], f"uma das rodadas paralelas falhou: {rcs}\n{saidas}"
    caminhos = [_preview_do_output(s) for s in saidas]
    assert len(set(caminhos)) == 2, f"as duas rodadas compartilharam o preview: {caminhos}"


# -------------------------------------------------------------------- deploy

def test_deploy_dry_run_nao_toca_em_nada(amb):
    antes = _arvore(amb)
    r = _run(amb, "deploy")
    assert r.returncode == 0, _saida(r)
    assert "(dry-run" in r.stdout and "publicar o rev alvo em origin/main (2 commit(s)" in r.stdout
    assert _log(amb, "launch") == []
    assert _head(amb, "mini") == _origin_head(amb)          # origin intacto
    assert "pip install -r requirements.txt" in r.stdout
    # árvore BYTE a byte: nem o preview de deps (que vai para o /tmp) entra aqui
    assert _arvore(amb) == antes, "o dry-run mexeu na árvore do mini"


def test_deploy_apply_publica_puxa_instala_reinicia_e_smoke(amb):
    antes = _head(amb, "mini")
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "push: ok" in r.stdout and "agora:" in r.stdout and "kickstart-ok" in r.stdout
    assert _head(amb, "mini") == _head(amb, "dev")          # produção no rev alvo
    assert _origin_head(amb) == _head(amb, "dev")           # publicado
    assert _log(amb, "launch") == [f"kickstart -k gui/{os.getuid()}/{LABEL}"]
    assert any("install -r requirements.txt" in l for l in _log(amb, "pip"))
    estado = (Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-estado").read_text()
    assert f"sha={antes}" in estado
    assert list(Path(amb["TTS_MINI_DIR"]).glob(".deploy-mini-freeze-*"))
    assert "[ok] LAN /api/build codigo=" in r.stdout


def test_deploy_apply_para_com_arvore_suja_no_mini_sem_reiniciar(amb):
    (Path(amb["TTS_MINI_DIR"]) / "app.py").write_text("# sujo\n")
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 1
    assert "[PARA] mini com rastreados modificados" in r.stdout
    assert _log(amb, "launch") == []
    assert _head(amb, "mini") != _head(amb, "dev")


def test_deploy_sem_push_para_antes_de_reiniciar(amb):
    """Sem push o mini não tem de onde puxar: o deploy para ANTES do restart."""
    r = _run(amb, "deploy", "--apply", "--sem-push")
    assert r.returncode == 1, _saida(r)
    assert "push: pulado (--sem-push)" in r.stdout
    assert "[aviso] o mini puxa do origin" in r.stdout
    assert "[PARA] o rev alvo não existe no mini depois do fetch" in r.stdout
    assert _head(amb, "mini") != _head(amb, "dev")          # origin não recebeu nada
    assert _log(amb, "launch") == []                        # não reiniciou à toa


def test_deploy_sem_deps_nao_roda_pip(amb):
    r = _run(amb, "deploy", "--apply", "--sem-deps")
    assert r.returncode == 0, _saida(r)
    assert "deps: pulado (--sem-deps)" in r.stdout
    assert _log(amb, "pip") == []           # nem o --dry-run


def test_deploy_mostra_o_delta_de_deps_do_rev_alvo_e_instala(amb):
    """O preview usa o requirements DO REV ALVO (o do mini, antes do pull, é o
    antigo: sem `websockets` ele diria 'nada a instalar' e a produção quebraria)."""
    amb["T_PIP_DRY"] = "Would install websockets-17.1"
    r = _run(amb, "deploy")
    assert r.returncode == 0, _saida(r)
    assert "Would install websockets-17.1" in r.stdout
    assert "nada a instalar" not in r.stdout
    # o arquivo de preview é o do REV ALVO (não o do mini, que ainda é o antigo) e
    # fica FORA da árvore do mini: o dry-run não pode sujar a produção (F1). O
    # caminho é único por execução (#240), então vem do próprio output.
    preview = Path(_preview_do_output(r.stdout))
    alvo = _git("show", "HEAD:requirements.txt", cwd=Path(amb["TTS_MINI_REPO"]), env=amb)
    assert preview.read_text().strip() == alvo.strip() == "fastapi\nwebsockets"
    assert not (Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-requirements-preview").exists(), \
        "o preview voltou para dentro da árvore do mini"

    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert any("install -r requirements.txt" in l for l in _log(amb, "pip"))


# ------------------------------------------------- teto de versão (`requirements.lock`, #343)

def test_dry_run_usa_o_lock_DO_REV_ALVO_como_teto(amb):
    """O deploy instala com `-c <lock do rev alvo>` — sem teto, torch/fastapi/uvicorn
    (sem pino na lista curada) podiam mover no deploy sem diff de código."""
    r = _run(amb, "deploy")
    assert r.returncode == 0, _saida(r)
    assert "-c requirements.lock" in r.stdout          # a linha de deps diz o comando
    # o teto passado ao pip é o arquivo escrito, e ele é o lock DO REV ALVO
    # (a v1, parada no mini, não tem lock).
    instalacoes = [l for l in _log(amb, "pip") if "--dry-run" in l]
    assert instalacoes, _log(amb, "pip")
    assert all(f"-c {amb['TTS_MINI_LOCK_PREVIEW']}" in l for l in instalacoes), instalacoes
    assert Path(amb["TTS_MINI_LOCK_PREVIEW"]).read_text().strip() == "fastapi==1.0"


def test_install_que_estoura_o_lock_para_antes_do_restart(amb):
    """Conflito com o teto = pip vermelho: parar sem reiniciar deixa a produção no
    processo antigo em vez de subir código cujo venv não foi satisfeito."""
    antes = _head(amb, "mini")
    amb["T_PIP_DRY"] = "Would install websockets-17.1"
    amb["T_PIP_FALHA"] = "1"
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 1, _saida(r)
    assert "[PARA] pip install falhou" in r.stdout
    assert "NÃO reiniciei" in r.stdout
    assert _log(amb, "launch") == [], "reiniciou com o venv possivelmente inconsistente"
    assert "ResolutionImpossible" in r.stdout
    # a INSTALAÇÃO (não só o preview) foi com o teto: sem o `-c` o conflito nem
    # existiria, então este assert é o que prende o comportamento de verdade
    reais = [l for l in _log(amb, "pip") if "install -r requirements.txt" in l]
    assert reais and all("-c " in l for l in reais), _log(amb, "pip")
    assert antes  # o rev anterior é o alvo do `rollback --apply` (estado já gravado)


def test_delta_de_deps_fica_em_arquivo_e_no_log(amb):
    """O que mudou no venv tem de ser verificável: delta em arquivo no mini e
    resumo no output (o rollback não desfaz deps, #345)."""
    amb["T_PIP_DRY"] = "Would install websockets-17.1"
    amb["T_PIP_FREEZE_DEPOIS"] = "fastapi==1.0\nwebsockets==17.1"
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "delta no venv" in r.stdout and "websockets==17.1" in r.stdout
    assert list(Path(amb["TTS_MINI_DIR"]).glob(".deploy-mini-deps-delta-*")), \
        "o delta não foi registrado em arquivo"


def test_venv_identico_e_dito_com_todas_as_letras(amb):
    """Nada mudou = dizer isso, não silêncio: é a prova de que o deploy não moveu
    versão nenhuma (o aceite do #343)."""
    amb["T_PIP_DRY"] = "Would install websockets-17.1"
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "venv IDÊNTICO ao freeze anterior" in r.stdout


def test_rev_sem_lock_avisa_e_instala_sem_teto(amb):
    """Corte antigo (sem `requirements.lock`) não pode quebrar o deploy: avisa e
    segue sem teto."""
    base = _sha(amb, "HEAD~2")                      # v1: sem lock
    assert _run(amb, "deploy", "--apply").returncode == 0      # sai do base
    r = _run(amb, "deploy")                          # dry-run para o rev antigo
    amb["TTS_MINI_REV"] = base
    r = _run(amb, "deploy")
    assert r.returncode == 0, _saida(r)
    assert "SEM teto de versão" in r.stdout


def test_deploy_nao_reinicia_quando_ja_esta_no_ar(amb):
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_LAUNCH_LOG"]).unlink()
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "nada a fazer" in r.stdout
    assert _log(amb, "launch") == []


def test_nada_a_fazer_com_smoke_vermelho_nao_sai_verde(amb):
    """Caminho "nada a fazer" + produção quebrada: o deploy não mudaria nada, mas
    sair 0 esconde o `[FALHA]` do smoke. Sem restart não há rollback a fazer — o
    veredito é ≠0 com o motivo dito (#495)."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_LAUNCH_LOG"]).unlink()
    amb["T_CURL_HEALTH_CODE"] = "502"            # /health vermelho, /api/build segue ok

    r = _run(amb, "deploy", "--apply")

    assert r.returncode == 1, _saida(r)
    assert "nada a fazer" in r.stdout
    assert "[FALHA]" in r.stdout
    assert "não há o que reverter" in r.stdout
    assert "[FALHA] LAN /health devolveu 502" in r.stdout
    assert _log(amb, "launch") == [], "nada a fazer continua sem reiniciar"


def test_nada_a_fazer_com_smoke_verde_continua_zero(amb):
    """Controle: o caminho normal (tudo batendo) tem de continuar saindo 0."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "nada a fazer" in r.stdout and "[FALHA]" not in r.stdout


def test_deploy_reinicia_quando_o_processo_esta_velho(amb):
    """Código no alvo no disco mas instância viva antiga: não é 'nada a fazer' —
    reinicia (o smoke então acusa o codigo forjado, que é o do processo velho) e,
    como o smoke fica vermelho, o rollback automático roda em seguida (#345)."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_LAUNCH_LOG"]).unlink()
    amb["T_CURL_BUILD_CODIGO"] = "deadbeef"
    r = _run(amb, "deploy", "--apply")
    assert "nada a fazer" not in r.stdout
    # um kickstart do deploy + um do rollback automático
    assert _log(amb, "launch") == [f"kickstart -k gui/{os.getuid()}/{LABEL}"] * 2
    assert r.returncode == 1 and "instância não é o rev alvo" in r.stdout
    assert "SMOKE VERMELHO" in r.stdout and "REVERTIDO" in r.stdout


def test_alvo_por_rev_sobe_o_corte_e_nao_o_head(amb):
    """F2: `TTS_MINI_REV` tem de valer — fixar ALVO_REV=HEAD deixaria a suíte verde."""
    corte = _sha(amb, "HEAD~1")                     # v2: existe, é antigo, e NÃO é o HEAD
    amb["TTS_MINI_REV"] = corte
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert f"rev alvo {corte[:12]}" in r.stdout
    assert _head(amb, "mini") == corte              # o mini ficou no CORTE
    assert _head(amb, "mini") != _head(amb, "dev")  # e não no HEAD


def test_mini_termina_em_branch_prod_e_nao_detached(amb):
    """F3: trocar `switch -C` por `--detach` deixaria a suíte verde — o `_branch`
    falha se o HEAD estiver solto."""
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert _branch(amb) == f"prod-{_head(amb, 'dev')[:12]}"

    r = _run(amb, "rollback", "--apply")
    assert r.returncode == 0, _saida(r)
    assert _branch(amb) == f"prod-{_head(amb, 'mini')[:12]}"


def test_compare_avisa_alvo_nao_descendente_do_que_esta_no_ar(amb):
    """F4: remover o `merge-base --is-ancestor` do compare deixaria a suíte verde."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    corte = _sha(amb, "HEAD~1")                     # v2 é ANTERIOR ao que está no ar
    amb["TTS_MINI_REV"] = corte
    r = _run(amb, "compare")
    assert r.returncode == 1
    assert "produção NÃO é ancestral do alvo" in r.stdout
    assert "rewind/desvio" in r.stdout


def test_smoke_nao_exige_rota_ausente_no_rev_alvo(amb):
    """O corte do passo 1 é anterior ao #190 e ao épico Live: o smoke tem de dizer
    "ausente NESTE rev", não "falha" — senão um alvo antigo nunca fecha verde."""
    base = _sha(amb, "HEAD~2")                      # v1: sem /api/build nem /api/live/ws
    amb["TTS_MINI_REV"] = base
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert r.stdout.count("/api/build ausente NESTE rev") == 2
    assert r.stdout.count("/api/live/ws ausente NESTE rev") == 2
    assert "FALHA" not in r.stdout


def test_redeploy_do_mesmo_alvo_preserva_o_alvo_de_rollback(amb):
    """Re-deploy do MESMO rev não pode sobrescrever o estado com o próprio alvo —
    senão o rollback vira no-op e perde a produção anterior."""
    antes = _head(amb, "mini")
    estado = Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-estado"
    assert _run(amb, "deploy", "--apply").returncode == 0
    assert f"sha={antes}" in estado.read_text()

    # 2º deploy, tudo batendo: "nada a fazer" e o estado intacto
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "nada a fazer" in r.stdout
    assert f"sha={antes}" in estado.read_text()

    # 3º deploy com a instância viva divergindo (não é "nada a fazer"): passa pelo
    # backup e tem de PRESERVAR o estado em vez de gravar o próprio alvo
    amb["T_CURL_BUILD_CODIGO"] = "deadbeef"
    r = _run(amb, "deploy", "--apply")
    assert "alvo de rollback anterior é preservado" in r.stdout
    assert f"sha={antes}" in estado.read_text(), "o estado foi sobrescrito com o alvo"


# ------------------------------------------------------------------ rollback

def test_rollback_dry_run_nao_muda_e_apply_volta_o_sha(amb):
    antes = _head(amb, "mini")
    assert _run(amb, "deploy", "--apply").returncode == 0
    r = _run(amb, "rollback")
    assert r.returncode == 0
    assert "(dry-run" in r.stdout and antes[:12] in r.stdout
    assert _head(amb, "mini") == _head(amb, "dev")

    r = _run(amb, "rollback", "--apply")
    assert r.returncode == 0, _saida(r)
    assert _head(amb, "mini") == antes
    assert "devolvido" in r.stdout or "agora:" in r.stdout
    assert len(_log(amb, "launch")) == 2
    assert "deploy-mini-freeze" in r.stdout       # avisa que as deps não voltam sozinhas


def test_rollback_sem_estado_avisa(amb):
    r = _run(amb, "rollback", "--apply")
    assert r.returncode == 0
    assert "[aviso] nenhum .deploy-mini-estado" in r.stdout
    assert _log(amb, "launch") == []


# ------------------------------------------ rollback AUTOMÁTICO em smoke vermelho (#345)


def test_smoke_devolve_o_veredito(amb):
    """O `smoke` devolvia 0 SEMPRE (o último comando era um `if` de condição falsa).
    Sem este contrato, o `smoke && ...` do deploy nunca dispara rollback."""
    assert _run(amb, "smoke").returncode == 1, "smoke verde/vermelho tem de sair no rc"
    assert "FALHA" in _run(amb, "smoke").stdout


def test_deploy_com_smoke_vermelho_reverte_para_o_rev_anterior(amb):
    """Produção em v2 e deploy de v3 que sobe quebrado (health vermelho no
    processo novo): o deploy volta sozinho para v2, reinicia, o smoke do rollback
    passa e sai ≠0 dizendo que reverteu."""
    anterior = _sha(amb, "HEAD~1")                 # v2
    amb["TTS_MINI_REV"] = anterior
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_LAUNCH_LOG"]).unlink()
    assert (Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-estado").read_text().startswith("sha=")

    amb.pop("TTS_MINI_REV", None)                  # alvo de volta ao HEAD (v3)
    amb["T_CURL_CODE_PRIMEIRO"] = "502"            # rev novo no ar e quebrado
    r = _run(amb, "deploy", "--apply")

    assert r.returncode == 1, _saida(r)
    assert "SMOKE VERMELHO" in r.stdout
    assert "REVERTIDO" in r.stdout and "smoke do rollback passou" in r.stdout
    assert _head(amb, "mini") == anterior, "a produção NÃO voltou para o rev anterior"
    assert _log(amb, "launch") == [f"kickstart -k gui/{os.getuid()}/{LABEL}"] * 2
    # e a árvore viva volta a ser a do rev anterior (paridade do index.html)
    assert (Path(amb["TTS_MINI_DIR"]) / "static" / "index.html").read_text() == "<html>v2</html>\n"


def test_rollback_automatico_sem_estado_diz_que_e_manual(amb):
    """Sem `.deploy-mini-estado` não há para onde voltar: o deploy não pode fingir
    que reverteu — sai ≠0 mandando reverter à mão."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    (Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-estado").unlink()
    Path(amb["T_LAUNCH_LOG"]).unlink()

    amb["T_CURL_BUILD_CODIGO"] = "deadbeef"
    r = _run(amb, "deploy", "--apply")

    assert r.returncode == 1, _saida(r)
    assert "SMOKE VERMELHO" in r.stdout
    assert "sem .deploy-mini-estado no mini" in r.stdout and "reverta à mão" in r.stdout
    assert len(_log(amb, "launch")) == 1, "não pode haver kickstart de rollback sem alvo"


def test_caminho_feliz_nao_faz_rollback(amb):
    """Controle negativo: verde não pode disparar segunda reinicialização."""
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "SMOKE VERMELHO" not in r.stdout and "REVERTIDO" not in r.stdout
    assert len(_log(amb, "launch")) == 1


# --- #1051: leitura do index.html cortada no transporte não derruba deploy bom,
# e divergência que persiste continua revertendo (#345).
def test_index_cortado_uma_vez_rele_e_nao_reverte(amb):
    """O falso vermelho do 48b11d5: a 1ª leitura pública volta cortada (rc 28,
    poucos bytes). Antes reprovava e revertia um deploy bom; agora relê, passa e
    o aviso diz rc/bytes da leitura ruim."""
    amb["T_CURL_INDEX_CORTA_N"] = "1"
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "SMOKE VERMELHO" not in r.stdout and "REVERTIDO" not in r.stdout
    assert "divergiu na leitura 1" in r.stdout and "curl rc=28, 7 bytes" in r.stdout
    assert "na leitura 2 (as anteriores foram transporte" in r.stdout
    assert len(_log(amb, "launch")) == 1, "releitura verde não pode reiniciar de novo"


def test_index_que_segue_divergente_ainda_reverte(amb):
    """Mordida do outro lado: divergência que não some em INDEX_LEITURAS leituras
    é [FALHA] e o #345 reverte para o rev anterior, como antes."""
    anterior = _sha(amb, "HEAD~1")                 # v2
    amb["TTS_MINI_REV"] = anterior
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_LAUNCH_LOG"]).unlink()
    amb.pop("TTS_MINI_REV", None)                  # alvo v3
    # só as leituras do smoke do deploy saem erradas: LAN gasta 3 e acabou;
    # as do smoke do rollback (contador > 3) voltam a bater
    amb["T_CURL_INDEX_CORTA_N"] = "3"
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 1, _saida(r)
    assert "[FALHA] LAN index.html" in r.stdout and "em 3 leitura(s)" in r.stdout
    assert "SMOKE VERMELHO" in r.stdout and "REVERTIDO" in r.stdout
    assert _head(amb, "mini") == anterior, "divergência real tem que reverter"
    assert len(_log(amb, "launch")) == 2


def test_index_leituras_1_reprova_na_primeira_divergencia(amb):
    """TTS_MINI_INDEX_LEITURAS=1 devolve o comportamento antigo (sem releitura)."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb["T_CURL_INDEX_CORTA_N"] = "1"
    amb["TTS_MINI_INDEX_LEITURAS"] = "1"
    r = _run(amb, "smoke")
    assert r.returncode == 1, _saida(r)
    assert "[FALHA] LAN index.html" in r.stdout and "em 1 leitura(s)" in r.stdout
    assert "relendo" not in r.stdout
    assert "[ok] público index.html" in r.stdout      # contador passou de 1: bate


@pytest.mark.parametrize("valor", ["abc", "", "0", "-1"])
def test_index_leituras_invalido_cai_no_padrao_e_nao_gira(amb, valor):
    """`[ n -ge abc ]` errava e o laço relia para sempre (gate #1069). Valor
    inválido vale o padrão 3: divergência persistente reprova em 3 leituras."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb["T_CURL_INDEX_CORTA_N"] = "99"             # LAN e público sempre cortados
    amb["TTS_MINI_INDEX_LEITURAS"] = valor
    try:
        r = subprocess.run([ZSH, "-f", str(SCRIPT), "smoke"], env=amb, capture_output=True,
                           text=True, cwd=str(REPO), timeout=60, **_SEM_TTY)
    except subprocess.TimeoutExpired:
        pytest.fail(f"INDEX_LEITURAS={valor!r}: laço da paridade não parou (teto inerte)")
    assert r.returncode == 1, _saida(r)
    assert r.stdout.count("em 3 leitura(s)") == 2, _saida(r)   # LAN + público


# --- #1076: as outras duas guardas da paridade do index.html também precisam
# morder. `INDEX_TIMEOUT` cru ia para o `-m` do curl: com valor não numérico o
# curl real sai rc=2 SEM corpo, toda leitura diverge e o #345 reverte um deploy
# bom. `INDEX_PAUSA` cru vira `sleep abc` (rc 1, barulho no stderr).
@pytest.mark.parametrize("valor", ["abc", "3s", "", "-1"])
def test_index_timeout_invalido_cai_no_padrao_e_deploy_passa(amb, valor):
    """`TTS_MINI_INDEX_TIMEOUT` inválido não pode virar `-m` inválido: cai no
    padrão e o deploy segue verde (sem a guarda, todo `-m` sai rc=2 e reverte)."""
    amb["TTS_MINI_INDEX_TIMEOUT"] = valor
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "SMOKE VERMELHO" not in r.stdout and "REVERTIDO" not in r.stdout
    assert "expected a proper numerical parameter" not in r.stderr
    assert "[ok] público index.html" in r.stdout


@pytest.mark.parametrize("valor", ["abc", "", "-1"])
def test_index_pausa_invalida_cai_no_padrao_sem_barulho(amb, valor):
    """`TTS_MINI_INDEX_PAUSA` inválido não pode chegar no `sleep`: a releitura
    tem de acontecer calada, e o deploy fechar verde."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb["T_CURL_INDEX_CORTA_N"] = "1"              # força a releitura (e o sleep)
    amb["TTS_MINI_INDEX_PAUSA"] = valor
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert "divergiu na leitura 1" in r.stdout     # a releitura aconteceu
    assert "invalid time interval" not in r.stderr


# --- #252: a novidade do boot é ancorada no marcador do restart (.deploy-mini-boot),
# não numa janela fixa de 120 s — que mentia quando o processo novo demorava a
# carregar o modelo (ou quando o smoke rodava tarde depois do restart).
def test_smoke_pos_deploy_ancora_o_boot_no_marcador_do_restart(amb):
    """boot_ms alto (modelo carregando) NÃO é mais 'instância antiga': o boot do
    processo vivo (agora - boot_ms) ≥ marcador do restart → ok."""
    assert _run(amb, "deploy", "--apply").returncode == 0  # o deploy grava o marcador
    # restart há 1 h (marcador reescrito) e processo de pé há 3 min: a janela
    # fixa de 120 s mentia ("instância antiga"); a âncora diz processo novo.
    (Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-boot").write_text(f"{int(time.time()) - 3600}\n")
    amb["T_CURL_BOOT_MS"] = "180000"        # boot há 3 min: > janela fixa
    amb["TTS_MINI_POS_DEPLOY"] = "1"
    amb["TTS_MINI_REV"] = _sha(amb, "HEAD", "mini")
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert "processo novo (boot " in r.stdout and "marcador do restart" in r.stdout
    assert "instância antiga" not in r.stdout


def test_smoke_pos_deploy_falha_quando_o_processo_e_anterior_ao_restart(amb):
    """Boot ANTES do marcador = instância velha no ar depois do restart: FALHA."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb["T_CURL_BOOT_MS"] = "360000000"     # processo vivo "começou" há ~100 dias
    amb["TTS_MINI_POS_DEPLOY"] = "1"
    amb["TTS_MINI_REV"] = _sha(amb, "HEAD", "mini")
    r = _run(amb, "smoke")
    assert r.returncode == 1, _saida(r)
    assert "[FALHA] processo anterior ao restart" in r.stdout


def test_smoke_pos_deploy_sem_marcador_nao_mente(amb):
    """Sem marcador não dá para afirmar nada: boot curto → ok pela janela; boot
    longo → aviso honesto ('não verificada'), nunca 'instância antiga' como fato."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    (Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-boot").unlink()  # apaga a âncora
    amb["T_CURL_BOOT_MS"] = "999999999"
    amb["TTS_MINI_POS_DEPLOY"] = "1"
    amb["TTS_MINI_REV"] = _sha(amb, "HEAD", "mini")
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert "[aviso] sem marcador de restart" in r.stdout
    assert "instância antiga" not in r.stdout


def test_smoke_fora_do_pos_deploy_avisa_sem_afirmar(amb):
    """Fora do pós-deploy o smoke não tem marcador para ancorar: boot longo vira
    aviso neutro — o texto antigo dizia 'instância antiga' até com instância NOVA."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    amb["T_CURL_BOOT_MS"] = "331893"        # o caso real do gate do passo 2
    amb["TTS_MINI_REV"] = _sha(amb, "HEAD", "mini")
    r = _run(amb, "smoke")
    assert r.returncode == 0, _saida(r)
    assert "só no pós-deploy" in r.stdout
    assert "instância antiga" not in r.stdout


# --- #938: o deploy confere os pesos do manifesto (models/ NÃO é versionado).
# Um mini sem peso sobe degradado e silencioso: a marca d'água desliga e o smoke
# passava verde. O deploy tem de achar o que falta, sincronizar e, se não der,
# parar ANTES do restart.
def _publica_manifesto(env: dict[str, str], arquivos: dict[str, bytes]) -> None:
    """Põe no rev alvo (dev) um manifesto com os arquivos dados e os commita."""
    dev = Path(env["TTS_MINI_REPO"])
    (dev / "models").mkdir(exist_ok=True)
    linhas = []
    for nome, conteudo in arquivos.items():
        (dev / "models" / nome).write_bytes(conteudo)
        linhas.append(f"{hashlib.sha256(conteudo).hexdigest()}  models/{nome}")
    (dev / "models" / "sha256sums.txt").write_text("\n".join(linhas) + "\n")
    _git("add", "models", cwd=dev, env=env)
    _git("commit", "-qm", "manifesto de pesos", cwd=dev, env=env)


def _peso_fora_do_git(env: dict[str, str], nome: str) -> Path:
    """Tira o peso do índice (models/ é gitignored de verdade) e devolve o caminho."""
    dev = Path(env["TTS_MINI_REPO"])
    _git("rm", "--cached", "-q", f"models/{nome}", cwd=dev, env=env)
    _git("commit", "-qm", f"{nome} fora do git", cwd=dev, env=env)
    return dev / "models" / nome


def test_peso_ausente_no_mini_e_sincronizado_antes_do_restart(amb):
    _publica_manifesto(amb, {"peso.bin": b"peso-de-teste"})
    origem_local = _peso_fora_do_git(amb, "peso.bin")
    assert origem_local.exists()
    assert not (Path(amb["TTS_MINI_DIR"]) / "models" / "peso.bin").exists()

    r = _run(amb, "deploy", "--apply")

    assert r.returncode == 0, _saida(r)
    assert "sincronizando" in r.stdout, _saida(r)
    assert (Path(amb["TTS_MINI_DIR"]) / "models" / "peso.bin").read_bytes() == b"peso-de-teste"
    assert len(_log(amb, "launch")) == 1, "reiniciou mesmo com o peso recuperado"


def test_manifesto_multilinha_nao_reescreve_peso_valido(amb):
    """O `shasum` sem aspas do #938 hasheava só o 1º caminho (zsh não faz split
    por newline): o 2º virava 'faltando' e o tar reescrevia o arquivo bom."""
    bom = b"peso-que-ja-esta-certo"
    _publica_manifesto(amb, {"bom.bin": bom, "novo.bin": b"peso-novo"})
    _peso_fora_do_git(amb, "novo.bin")
    _peso_fora_do_git(amb, "bom.bin")      # os dois fora do git (models/ é ignorado)
    mini = Path(amb["TTS_MINI_DIR"]) / "models"
    mini.mkdir(exist_ok=True)
    # bom.bin existe SÓ no mini (não está na árvore local): se o tar o incluísse,
    # o `tar -T` falharia por ausência na origem
    (mini / "bom.bin").write_bytes(bom)
    antes = (mini / "bom.bin").stat().st_mtime_ns

    r = _run(amb, "deploy", "--apply")

    assert r.returncode == 0, _saida(r)
    assert "sincronizando" in r.stdout, _saida(r)
    assert (mini / "novo.bin").read_bytes() == b"peso-novo"
    assert (mini / "bom.bin").stat().st_mtime_ns == antes, "peso válido foi reescrito"


def _manifesto_cru(amb, linhas: list[str]) -> None:
    dev = Path(amb["TTS_MINI_REPO"])
    (dev / "models").mkdir(exist_ok=True)
    (dev / "models" / "sha256sums.txt").write_text("\n".join(linhas) + "\n")
    _git("add", "models", cwd=dev, env=amb)
    _git("commit", "-qm", "manifesto cru", cwd=dev, env=amb)


def test_caminho_com_volta_para_fora_do_models_e_rejeitado(amb):
    _manifesto_cru(amb, ["0" * 64 + "  ../fora.bin"])

    r = _run(amb, "deploy", "--apply")

    assert r.returncode != 0, _saida(r)
    assert "fora da whitelist" in _saida(r), _saida(r)
    assert _log(amb, "launch") == []


def test_metacaractere_no_manifesto_nao_executa_no_mini(amb):
    """Caminho ia cru para uma string de shell remota: `;` e `>>` executavam."""
    _manifesto_cru(amb, ["0" * 64 + "  models/x;>>MARCADOR"])

    r = _run(amb, "deploy", "--apply")

    assert r.returncode != 0, _saida(r)
    assert "fora da whitelist" in _saida(r), _saida(r)
    assert not (Path(amb["TTS_MINI_DIR"]) / "MARCADOR").exists(), "o `>>` vazou para o shell"


def test_peso_que_nao_da_para_recuperar_para_antes_do_restart(amb):
    _publica_manifesto(amb, {"peso.bin": b"x"})
    origem_local = _peso_fora_do_git(amb, "peso.bin")
    origem_local.unlink()                      # nem no mini, nem na árvore local

    r = _run(amb, "deploy", "--apply")

    assert r.returncode != 0, _saida(r)
    assert "NÃO reiniciei" in _saida(r), _saida(r)
    assert _log(amb, "launch") == [], "não podia ter reiniciado sem o peso"


def test_smoke_reprova_quando_o_log_acusa_sem_marca(amb):
    """Peso ausente = áudio sem marca; o log avisa e o smoke NÃO pode passar."""
    Path(amb["T_MINI_LOG"]).write_text("boot\n")
    amb["T_CURL_LOG_MARCA"] = "[marca] áudio saindo SEM marca d'água"

    r = _run(amb, "deploy", "--apply")

    print("LOG:", Path(amb["T_MINI_LOG"]).read_text())
    print("SAIDA:", _saida(r)[-1500:])
    assert r.returncode != 0, _saida(r)
    assert "SEM marca" in _saida(r), _saida(r)
    assert "REVERTIDO" in r.stdout, "smoke vermelho tem de reverter (o deploy já reiniciou)"


def test_smoke_nao_reprova_por_aviso_anterior_ao_restart(amb):
    """A âncora é o tamanho do log no restart: aviso velho não reprova deploy novo."""
    Path(amb["T_MINI_LOG"]).write_text("[marca] áudio saindo SEM marca d'água\n")

    r = _run(amb, "deploy", "--apply")

    assert r.returncode == 0, _saida(r)
    assert "[ok] marca d'água aplicada" in r.stdout, _saida(r)


# ------------------------------------------------- #1111: suíte sob tty de fundo

_TIMEOUT_BIN = shutil.which("timeout") or "/usr/bin/timeout"


@pytest.mark.skipif(not (shutil.which("script") and Path(_TIMEOUT_BIN).exists()),
                    reason="precisa de `script` (pty) e `timeout`")
def test_suite_sob_tty_de_fundo_nao_morre_no_peso_ausente(tmp_path):
    """#1111: a suíte cheia morria em silêncio (rc=1, sem resumo, ~39 %) em
    `screen` + `timeout` sem `--foreground` — mesmo sozinha na máquina. Reprodução
    com tty REAL: `script` dá um pty; o `timeout` põe o pytest num grupo de FUNDO
    dele (é o que o screen fazia); a saída vai para arquivo. O `read -d` do
    `pesos_faltando` (zsh, via shim de ssh) mexia no terminal → SIGTTOU → grupo
    parado → `timeout` sai 1. Com o `_SEM_TTY` no `_run` o filho do teste não tem
    terminal e a célula passa; tirar o `start_new_session` derruba esta."""
    saida = tmp_path / "pytest-aninhado.log"
    cmd = (f"cd {REPO} && {_TIMEOUT_BIN} 120 {sys.executable} -m pytest "
           f"tests/test_deploy_mini_sh.py -k peso_ausente_no_mini -q -p no:cacheprovider "
           f"> {saida} 2>&1; echo \"rc=$?\" >> {saida}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    r = subprocess.run(["script", "-q", "/dev/null", "/bin/bash", "-c", cmd],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=180, env=env)
    log = saida.read_text() if saida.exists() else ""
    assert r.returncode == 0, (r.returncode, log)
    assert log.rstrip().endswith("rc=0"), log
    assert "1 passed" in log, log


def test_todo_disparo_do_script_vai_sem_tty():
    """Guarda estática do #1111: toda chamada que roda o `deploy_mini.sh` neste
    arquivo passa o `_SEM_TTY` (um `subprocess` novo sem ele reabre a porta)."""
    import ast
    arvore = ast.parse(Path(__file__).read_text())
    sem = []
    for no in ast.walk(arvore):
        if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                and no.func.attr in ("run", "Popen")
                and isinstance(no.func.value, ast.Name) and no.func.value.id == "subprocess"):
            continue
        if not (no.args and isinstance(no.args[0], ast.List)
                and any(isinstance(e, ast.Call) and getattr(e.func, "id", "") == "str"
                        and getattr(e.args[0], "id", "") == "SCRIPT" for e in no.args[0].elts)):
            continue
        if not any(k.arg is None and getattr(k.value, "id", "") == "_SEM_TTY"
                   for k in no.keywords):
            sem.append(no.lineno)
    assert not sem, f"disparo do script sem _SEM_TTY nas linhas {sem}"
