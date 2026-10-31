"""A suíte passa num worktree SEM os arquivos de estado do dono (#659).

O worktree nasce sem `settings.json`, `.apikey`, `.apikeys.json` e
`.speaker-profiles.json` — de propósito: eles multiplicariam os segredos do dono e
esconderiam célula que lê o caminho real por fora do isolamento do `conftest`.

A guarda monta um worktree de verdade (com `.venv-mlx` e os pesos por symlink) e
roda a fatia da suíte que já caiu por isso. Se alguém voltar a ler
`app.BASE/<arquivo>` em vez do caminho EM USO, esta célula fica vermelha.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv-mlx" / "bin" / "python"

#: a fatia que quebrava, um `pytest` por grupo (`-k` repetido na mesma chamada só
#: vale o último): guarda do dono, sondas do runner, chaves e credenciais
FATIA = [("tests/test_run_sh.py", "dono or metrics or knob or j3 or import_com"),
         ("tests/test_chaves_isoladas.py", "guarda"),
         ("tests/test_tmpdir_sem_credencial.py", "basetemp"),
         ("tests/test_api.py", "save_preserva or save_avisa"),
         # sem o `voices/` do dono a resolução de voz caía em caminho que pede
         # modelo: estas duas usam a voz de fixture versionada
         ("tests/test_app.py", "marca_dagua_no_v1 or preaquece_o_marcador")]

_sem_git = pytest.mark.skipif(not (REPO / ".git").exists(), reason="precisa do repo git")
_sem_venv = pytest.mark.skipif(not PY.is_file(), reason="precisa da .venv-mlx")


@_sem_git
@_sem_venv
def test_suite_passa_em_worktree_sem_estado_do_dono(tmp_path):
    wt = tmp_path / "wt"
    git = ["git", "-C", str(REPO)]
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(wt), "HEAD"],
                   check=True, capture_output=True)
    try:
        # o `.venv-mlx` é entrada RASTREADA (symlink): num worktree o
        # checkout já a materializa — recriar dá FileExistsError. Só liga quando falta.
        if not (wt / ".venv-mlx").exists() and not (wt / ".venv-mlx").is_symlink():
            (wt / ".venv-mlx").symlink_to(REPO / ".venv-mlx")
        # pesos por symlink, como o `tools/worktrees.sh` faz (não são estado)
        manifesto = (REPO / "models" / "sha256sums.txt").read_text().splitlines()
        for linha in manifesto:
            partes = linha.split("  ", 1)
            if len(partes) != 2:
                continue
            rel = partes[1]
            origem = REPO / rel
            if origem.is_file() and not (wt / rel).exists():
                (wt / rel).parent.mkdir(parents=True, exist_ok=True)
                (wt / rel).symlink_to(origem)
        # `voices` também é entrada RASTREADA (symlink): num worktree o checkout a
        # materializa apontando para as vozes do dono. Aqui ela tem de ficar VAZIA.
        if (wt / "voices").is_symlink():
            (wt / "voices").unlink()
        (wt / "voices").mkdir(exist_ok=True)
        # o estado do dono NÃO entra (nem as vozes clonadas: a pasta pode existir
        # vazia, porque o app a cria no import)
        for arq in ("settings.json", ".apikey", ".apikeys.json", ".speaker-profiles.json"):
            assert not (wt / arq).exists(), f"{arq} veio junto (não devia)"
        assert not list((wt / "voices").glob("*")) if (wt / "voices").exists() else True, \
            "voices/ veio com arquivos do dono (não devia)"

        # `TTS_METRICS_DIR` próprio da rodada filha: sem ele o app do worktree grava
        # `state/metrics` DENTRO do worktree e a guarda do conftest (que olha
        # `app.BASE`) acusa no teardown da sessão filha.
        env = {**os.environ, "PATH": os.environ.get("PATH", ""),
               "HOME": os.environ.get("HOME", "/tmp"), "PYTHONDONTWRITEBYTECODE": "1",
               "TTS_METRICS_DIR": str(tmp_path / "metricas")}
        for arq in ("SETTINGS_PATH", "APIKEYS_PATH", "LEGACY_APIKEY_PATH", "VOICES_DIR"):
            env.pop(f"TTS_{arq}", None)
        # `TTS_DONO_DIR` do filho é um "dono" de MENTIRA (#710): sem ele o
        # `_raiz_do_dono()` do filho resolve para a raiz PRINCIPAL e qualquer célula
        # que mexa no dono muda o arquivo do dono de verdade (foi o que aconteceu às
        # 10:21, antes da célula da guarda passar a usar dono falso).
        dono_falso = tmp_path / "dono"
        dono_falso.mkdir()
        (dono_falso / "settings.json").write_text('{"model": "sintetico"}')
        env["TTS_DONO_DIR"] = str(dono_falso)
        for arquivo, filtro in FATIA:
            r = subprocess.run([str(PY), "-m", "pytest", arquivo, "-q", "-k", filtro],
                               cwd=str(wt), capture_output=True, text=True,
                               timeout=900, env=env)
            assert r.returncode == 0, (f"{arquivo} -k {filtro}\n"
                                       + r.stdout[-2500:] + r.stderr[-1500:])
    finally:
        subprocess.run([*git, "worktree", "remove", "--force", str(wt)],
                       capture_output=True)


def test_symlink_de_estado_e_ignorado_pelo_git():
    """Num worktree `voices`/`.venv-mlx` são SYMLINK — e `voices/` no `.gitignore`
    (com barra) só casa diretório, então eles escapavam e iam para o commit.

    A forma é ANCORADA (`/voices`): na raiz o link casa, e uma pasta de mesmo nome
    mais fundo não é sombreada.

    Custa caro: `voices` é o dado REAL do dono (o commit apontaria para o caminho
    dele) e `voices/voices` nasce quando o link cai dentro da pasta real."""
    criados = []
    try:
        for nome in ("voices", ".venv-mlx", "outputs", "state"):
            alvo = REPO / nome
            if alvo.exists() or alvo.is_symlink():
                continue
            alvo.symlink_to(REPO / "tests" / "fixtures", target_is_directory=True)
            criados.append(alvo)
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-v",
                            "voices", ".venv-mlx", "outputs", "state"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        for nome in ("voices", ".venv-mlx", "outputs", "state"):
            assert f"\t{nome}" in r.stdout, f"{nome} (symlink) não está ignorado: {r.stdout}"
    finally:
        for alvo in criados:
            alvo.unlink()


# --- tools/integra.sh: raiz de qualquer worktree e recusa de estado --------------
def _integra(*args: str, cwd: Path, pasta: Path) -> subprocess.CompletedProcess:
    """Roda o script DA ÁRVORE DE TRABALHO, com cwd dentro do worktree — é o caso do
    agente (`tools/integra.sh` a partir do próprio worktree) sem precisar commitar
    cópia: o script em `REPO` já é a versão editada."""
    return subprocess.run([str(REPO / "tools" / "integra.sh"), *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=900,
                          env={**os.environ, "TTS_INTEGRA_SEM_HOOK": "1",
                               "TTS_WT_DIR": str(pasta)})


def _worktree(temporario: Path, nome: str) -> Path:
    wt = temporario / nome
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-q", "-B", f"wt/{nome}",
                    str(wt), "main"], check=True, capture_output=True)
    # o hook roda no worktree: sem venv ele cai no python do sistema (sem ruff)
    (wt / ".venv-mlx").symlink_to(REPO / ".venv-mlx")
    # (o script roda de `REPO`, então não precisa commitar cópia aqui)
    return wt


def tmp_dir_de(worktrees) -> Path:
    return worktrees[0].parent


@pytest.fixture
def worktrees(tmp_path):
    """Dois worktrees descartáveis; devolve o que serve de "onde eu estou"."""
    criados = []
    # rodada anterior pode ter morrido entre criar e limpar: branch presa em
    # worktree fantasma faz `worktree add` falhar com 128
    subprocess.run(["git", "-C", str(REPO), "worktree", "prune"], capture_output=True)
    try:
        for nome in ("agentea", "agenteb"):
            subprocess.run(["git", "-C", str(REPO), "branch", "-D", f"wt/{nome}"],
                           capture_output=True)
            criados.append(_worktree(tmp_path, nome))
        yield criados
    finally:
        for wt in criados:
            subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force",
                            str(wt)], capture_output=True)
            subprocess.run(["git", "-C", str(REPO), "branch", "-D", f"wt/{wt.name}"],
                           capture_output=True)


def test_integra_acha_a_raiz_chamado_de_dentro_de_outro_worktree(worktrees):
    """Chamado de dentro de um worktree, a raiz sai do `.git` comum (#690).

    Antes, `dirname(script)/..` apontava para o próprio worktree e o script
    procurava `…-wt/<agente>-wt` — recusava com "não achei o worktree"."""
    de_dentro = worktrees[1]
    r = _integra("agentea", cwd=de_dentro, pasta=tmp_dir_de(worktrees))
    assert "não achei o worktree" not in r.stderr, r.stderr
    # agentea está em main, sem commit: o fluxo segue até o fim
    assert r.returncode == 0, r.stderr + r.stdout


def test_integra_recusa_branch_que_leva_estado(worktrees):
    """Estado do dono no commit — arquivo ou SYMLINK (modo 120000) — é recusado."""
    wt = worktrees[0]
    (wt / "voices").symlink_to(REPO / "tests" / "fixtures")
    (wt / "settings.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(wt), "add", "-f", "voices", "settings.json"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "core.hooksPath=/dev/null", "commit",
                    "-q", "-m", "estado"], check=True, capture_output=True)

    r = _integra("agentea", cwd=worktrees[1], pasta=worktrees[0].parent)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "leva estado do dono" in r.stderr
    assert "voices" in r.stderr and "settings.json" in r.stderr
    assert "symlink rastreado" in r.stderr, r.stderr


def test_worktrees_falha_quando_venv_nao_esta_ignorado(tmp_path):
    """`worktrees.sh` confere `git check-ignore .venv-mlx` no fim (#685).

    Um repositório de mentira, sem a regra no `.gitignore`: o script tem de parar
    em vez de entregar um worktree cujo symlink entraria no commit."""
    repo = tmp_path / "falso"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "worktrees.sh").write_text(
        (REPO / "tools" / "worktrees.sh").read_text())
    (repo / "tools" / "worktrees.sh").chmod(0o755)
    (repo / ".gitignore").write_text("# sem regra para o venv\n")
    (repo / "models").mkdir()
    (repo / "models" / "sha256sums.txt").write_text("")
    for cmd in (["init", "-q", "-b", "main"],
                ["-c", "user.email=x@y", "-c", "user.name=x", "commit", "-q",
                 "--allow-empty", "-m", "raiz"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)

    r = subprocess.run(["bash", str(repo / "tools" / "worktrees.sh"), "cria", "x"],
                       cwd=str(repo), capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "não está no .gitignore" in r.stderr


def test_integra_recusa_raiz_suja(worktrees):
    """Integração é sobre árvore limpa: WIP na raiz seria atropelado pelo `--ff-only`.

    (O caso "raiz fora da main" não dá para montar aqui sem trocar o HEAD da raiz
    compartilhada — a checagem existe no script e o fluxo de integração a exercita.)"""
    de_dentro, wt = worktrees[1], worktrees[0]
    common = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    raiz = (Path(common) if Path(common).is_absolute() else REPO / common).resolve().parent
    alvo = raiz / "WIP-do-teste.txt"
    alvo.write_text("x\n")
    try:
        r = _integra("agentea", cwd=de_dentro, pasta=wt.parent)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "trabalho não commitado" in r.stderr, r.stderr
    finally:
        alvo.unlink(missing_ok=True)
