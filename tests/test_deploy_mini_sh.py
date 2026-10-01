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
url=""; tem_chave=0; so_codigo=0; arquivo=""; metodo=GET
for ((i = 1; i <= $#; i++)); do
  case "${@[i]}" in
    http*) url="${@[i]}" ;;
    -X) metodo="${@[i+1]}" ;;
    -H) [[ "${@[i+1]}" == X-API-Key:* ]] && tem_chave=1 ;;
    -w) so_codigo=1 ;;
  esac
done
print -r -- "$metodo $url" >> "$T_CURL_LOG"
codigo=200; corpo=""; arquivo=""
case "$url" in
  */health)  corpo='{"ok":true}' ;;
  */api/voices)
      if [ $tem_chave = 1 ]; then codigo="${T_CURL_CHAVE_CODE:-200}"; else codigo="${T_CURL_SEM_CHAVE_CODE:-401}"; fi ;;
  */api/tts/jobs/*/pieces/*) codigo="${T_CURL_PIECE_CODE:-200}"; corpo="RIFF-fake-audio" ;;
  */api/tts/jobs/*)
      if [ $tem_chave = 1 ]; then corpo="{\\"status\\":\\"${T_CURL_JOB_STATUS:-done}\\"}"; else codigo=401; fi ;;
  */api/tts)
      if [ $tem_chave = 1 ] && [ "$metodo" = POST ]; then corpo='{"job_id":"j1"}'
      elif [ $tem_chave = 1 ]; then corpo='{"job_id":"j1"}'
      else codigo=401; fi ;;
  */api/build)
      if [ $tem_chave = 1 ]; then
        # `codigo` = hash dos módulos do mini AGORA (ou o forçado pelo teste) e
        # boot_ms curto: é a instância viva que o smoke confere. ${=} força a
        # separação por espaço (zsh não separa variável sem aspas).
        c="${T_CURL_BUILD_CODIGO:-$(cd "$T_MINI_DIR" && cat ${=T_MODULOS} 2>/dev/null | /usr/bin/shasum -a 256 | cut -c1-8)}"
        corpo="{\\"ok\\":true,\\"codigo\\":\\"$c\\",\\"boot_ms\\":${T_CURL_BOOT_MS:-1000}}"
      else codigo=401; fi ;;
  */api/live/ws*) codigo="${T_CURL_WS_CODE:-101}" ;;
  */) arquivo="$T_MINI_DIR/static/index.html"; nonce=1 ;;
esac
if [ $so_codigo = 1 ]; then print -rn -- "$codigo"
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
    env.update(
        PATH=f"{shims}:{env['PATH']}",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"), GIT_CONFIG_SYSTEM="/dev/null",
        T_SSH_LOG=str(tmp_path / "ssh.log"),
        T_LAUNCH_LOG=str(tmp_path / "launch.log"),
        T_CURL_LOG=str(tmp_path / "curl.log"),
        T_PIP_LOG=str(tmp_path / "pip.log"),
        T_MINI_DIR=str(tmp_path / "mini"),
        T_MODULOS=" ".join(MODULOS),
        TTS_MINI_HOST="mini-fake",
        TTS_MINI_DIR=str(tmp_path / "mini"),
        TTS_MINI_LABEL=LABEL,
        TTS_MINI_ESPERA="0",
        TTS_MINI_REPO=str(tmp_path / "dev"),
        TTS_PUBLIC_HOST="tts.exemplo.test",
        # sem TTS_MINI_PREVIEW de propósito: o teste de F1 prova o DEFAULT (/tmp do
        # mini), não um override que mascararia a regressão para dentro da árvore
    )
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
    (semente / "requirements.txt").write_text("fastapi\nwebsockets\n")
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
  freeze) print -r -- "fastapi==1.0" ;;
  install)
    print -r -- "$*" >> "$T_PIP_LOG"
    if [[ " $* " == *" --dry-run "* ]]; then
      print -r -- "${T_PIP_DRY:-Requirement already satisfied: fastapi in ./.venv-mlx}"
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
    (dev / "static" / "index.html").write_text("<html>v2</html>\n")
    _git("add", "-A", cwd=dev, env=env)
    _git("commit", "-qm", "v2", cwd=dev, env=env)
    (dev / "static" / "index.html").write_text("<html>v3</html>\n")
    _git("add", "-A", cwd=dev, env=env)
    _git("commit", "-qm", "v3", cwd=dev, env=env)
    # o mini: clone de produção, parado no commit antigo
    _git("clone", "-q", str(origem), str(tmp_path / "mini"), cwd=tmp_path, env=env)
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([ZSH, "-f", str(SCRIPT), *args], env=env, capture_output=True,
                          text=True, cwd=str(REPO))


def _saida(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def _log(env: dict[str, str], nome: str) -> list[str]:
    p = Path(env[f"T_{nome.upper()}_LOG"])
    return p.read_text().splitlines() if p.exists() else []


APP_SEM_ROTAS = "# app.py versao 1 (sem as rotas novas)\n"
APP_COM_ROTAS = ('# app.py versao 2\n'
                 '@app.get("/api/build")\n'
                 'def build(): ...\n'
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


def test_smoke_avisa_quando_o_ws_nao_sobe(amb):
    amb["T_CURL_WS_CODE"] = "401"      # produção sem a rota (ou sem websockets)
    r = _run(amb, "smoke")
    assert r.returncode == 1
    assert "/api/live/ws devolveu 401" in r.stdout


def test_smoke_avisa_sem_a_chave_do_mini(amb):
    (Path(amb["TTS_MINI_DIR"]) / ".apikey").unlink()
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
    # fica FORA da árvore do mini: o dry-run não pode sujar a produção (F1)
    preview = Path("/tmp/deploy-mini-requirements-preview")
    alvo = _git("show", "HEAD:requirements.txt", cwd=Path(amb["TTS_MINI_REPO"]), env=amb)
    assert preview.read_text().strip() == alvo.strip() == "fastapi\nwebsockets"
    assert not (Path(amb["TTS_MINI_DIR"]) / ".deploy-mini-requirements-preview").exists(), \
        "o preview voltou para dentro da árvore do mini"

    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert any("install -r requirements.txt" in l for l in _log(amb, "pip"))


def test_deploy_nao_reinicia_quando_ja_esta_no_ar(amb):
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_LAUNCH_LOG"]).unlink()
    r = _run(amb, "deploy", "--apply")
    assert r.returncode == 0, _saida(r)
    assert "nada a fazer" in r.stdout
    assert _log(amb, "launch") == []


def test_deploy_reinicia_quando_o_processo_esta_velho(amb):
    """Código no alvo no disco mas instância viva antiga: não é 'nada a fazer' —
    reinicia (o smoke então acusa o codigo forjado, que é o do processo velho)."""
    assert _run(amb, "deploy", "--apply").returncode == 0
    Path(amb["T_LAUNCH_LOG"]).unlink()
    amb["T_CURL_BUILD_CODIGO"] = "deadbeef"
    r = _run(amb, "deploy", "--apply")
    assert "nada a fazer" not in r.stdout
    assert _log(amb, "launch") == [f"kickstart -k gui/{os.getuid()}/{LABEL}"]
    assert r.returncode == 1 and "instância não é o rev alvo" in r.stdout


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
