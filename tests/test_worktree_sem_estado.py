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
import tempfile
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
@pytest.mark.slow
@pytest.mark.timeout(600)   # #783: o teto default é 90 s; estes passam de 100
def test_suite_passa_em_worktree_sem_estado_do_dono(tmp_path):
    wt = tmp_path / "wt"
    git = ["git", "-C", str(REPO)]
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(wt), "HEAD"],
                   check=True, capture_output=True)
    try:
        # `tests/` do WORKING TREE por cima do checkout: o filho tem de rodar o que
        # está ENTRANDO, não o commit anterior. Sem isto, mexer numa célula que a
        # fatia roda reprova o próprio commit que a conserta (o hook roda antes).
        import shutil as _sh
        _sh.copytree(REPO / "tests", wt / "tests", dirs_exist_ok=True)
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
               # #783: a fatia roda `run_sh.sh` várias vezes; sem o modo rápido
               # cada invocação paga a varredura de /tmp inteira
               "GUARDA_RAPIDA": "1",
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


# --- tools/integra.sh e tools/worktrees.sh num repo FALSO em tmp (#705) ----------
# Tudo aqui roda num `git init` de mentira: o repo de verdade não pode receber
# commit de agente falso. Um fixture de sessão guarda o estado real e cobra que
# nada tenha mudado no fim do arquivo.
def _falso(destino: Path) -> Path:
    """Repo mínimo que os scripts aceitam: `main`, hook stub, `.gitignore` certo."""
    (destino / "tools").mkdir(parents=True)
    for nome in ("integra.sh", "worktrees.sh"):
        copia = destino / "tools" / nome
        copia.write_text((REPO / "tools" / nome).read_text())
        copia.chmod(0o755)
    (destino / ".gitignore").write_text("/.venv-mlx\n/voices\n/outputs\n/state\n"
                                       "settings.json\n.apikey*\n.speaker-profiles.json\n")
    (destino / "models").mkdir()
    (destino / "models" / "sha256sums.txt").write_text("")
    (destino / ".githooks").mkdir()
    (destino / ".githooks" / "pre-commit").write_text(
        # marca no próprio worktree (é o cwd do hook): prova que ele rodou
        "#!/bin/sh\n: > hook-rodou\n")
    (destino / ".githooks" / "pre-commit").chmod(0o755)
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"],
                ["-c", "user.email=x@y", "-c", "user.name=x", "commit", "-q", "-m", "raiz"]):
        subprocess.run(["git", "-C", str(destino), *cmd], check=True, capture_output=True)
    return destino


def _integra(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Roda o `integra.sh` DO REPO FALSO, de dentro de um worktree dele."""
    return subprocess.run([str(script), *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=900, env={**os.environ})


def _pasta_do_time() -> str:
    """Onde o time põe worktree (`<raiz>-wt`). Worktree em `/tmp` é de sonda."""
    return str(REPO.parent / f"{REPO.name}-wt")


def _estado_real() -> tuple[str, set[str], set[str]]:
    """(HEAD da main real, branches, worktrees do time) — o que um teste não pode mexer.

    Worktree em `/tmp` fica de fora: outra pessoa rodando a suíte ao lado cria e
    remove a dela, e isso não é sujeira nossa (medido: a guarda caiu assim)."""
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()
    ramos = subprocess.run(["git", "-C", str(REPO), "for-each-ref", "--format=%(refname)",
                            "refs/heads"], capture_output=True, text=True).stdout.split()
    wts = subprocess.run(["git", "-C", str(REPO), "worktree", "list", "--porcelain"],
                         capture_output=True, text=True).stdout.splitlines()
    pasta = _pasta_do_time()
    return head, set(ramos), {l for l in wts if l.startswith(f"worktree {pasta}")}


@pytest.fixture(scope="module", autouse=True)
def guarda_do_repo_real():
    """O repo REAL não pode mudar por causa deste arquivo (#705)."""
    antes = _estado_real()
    yield
    depois = _estado_real()
    # HEAD igual, ou só ANDOU por integração de outro agente (ancestral): o que não
    # pode é o arquivo ter deixado a main em outro lugar
    assert depois[0] == antes[0] or subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", antes[0], depois[0]]
    ).returncode == 0, "o HEAD da main REAL saiu do lugar durante o arquivo"
    assert depois[1] <= antes[1], f"branch criada no repo real: {depois[1] - antes[1]}"
    assert depois[2] <= antes[2], f"worktree criada no repo real: {depois[2] - antes[2]}"


@pytest.fixture
def falso(tmp_path) -> Path:
    return _falso(tmp_path / "falso")


def _cria(repo: Path, nome: str) -> Path:
    subprocess.run([str(repo / "tools" / "worktrees.sh"), "cria", nome], cwd=str(repo),
                   check=True, capture_output=True, text=True)
    return (repo.parent / f"{repo.name}-wt" / nome).resolve()


def test_integra_acha_a_raiz_chamado_de_dentro_de_outro_worktree(falso):
    """Chamado de dentro de um worktree, a raiz sai do `.git` comum (#690).

    Antes, `dirname(script)/..` apontava para o próprio worktree e o script
    procurava `…-wt/<agente>-wt` — recusava com "não achei o worktree"."""
    _cria(falso, "agentea")
    b = _cria(falso, "agenteb")
    r = _integra(b / "tools" / "integra.sh", "agentea", cwd=b)
    assert "não achei o worktree" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stderr + r.stdout


def test_integra_recusa_branch_que_leva_estado(falso):
    """Estado do dono no commit — arquivo ou SYMLINK (modo 120000) — é recusado."""
    a, b = _cria(falso, "agentea"), _cria(falso, "agenteb")
    (a / "voices").symlink_to(falso / "models", target_is_directory=True)
    (a / "settings.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(a), "add", "-f", "voices", "settings.json"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(a), "-c", "core.hooksPath=/dev/null", "commit",
                    "-q", "-m", "estado"], check=True, capture_output=True)

    r = _integra(b / "tools" / "integra.sh", "agentea", cwd=b)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "leva estado do dono" in r.stderr
    assert "symlink rastreado" in r.stderr and "settings.json" in r.stderr


def test_integra_recusa_raiz_suja(falso):
    """Integração é sobre árvore limpa: WIP na raiz seria atropelado pelo `--ff-only`."""
    _cria(falso, "agentea")
    b = _cria(falso, "agenteb")
    (falso / "WIP.txt").write_text("x\n")
    r = _integra(b / "tools" / "integra.sh", "agentea", cwd=b)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "trabalho não commitado" in r.stderr


def test_atalho_de_hook_so_vale_em_arvore_de_tmp(falso, tmp_path):
    """`TTS_INTEGRA_SEM_HOOK=1` não pode enganar no repo de verdade (#705).

    Em árvore sob tmp, pula (é o que deixa o teste rápido). Fora de tmp, o hook
    roda mesmo com o atalho — foi por ele que um teste integrou agente falso na
    main real sem gate."""
    a = _cria(falso, "agentea")          # o hook roda no worktree do AGENTE (a)
    b = _cria(falso, "agenteb")
    env = {**os.environ, "TTS_INTEGRA_SEM_HOOK": "1"}

    r = subprocess.run([str(b / "tools" / "integra.sh"), "agentea"], cwd=str(b),
                       capture_output=True, text=True, timeout=900, env=env)
    assert r.returncode == 0, r.stderr
    assert not (a / "hook-rodou").exists(), "o atalho devia pular o hook em tmp"

    # mesmo repo, agora "fora de tmp": o atalho é recusado e o hook roda
    fora = Path(tempfile.mkdtemp(prefix="integra-fora-", dir=str(Path.home())))
    try:
        copia = _falso(fora / "falso")
        alvo = _cria(copia, "agentea")
        c = _cria(copia, "agenteb")
        r = subprocess.run([str(c / "tools" / "integra.sh"), "agentea"], cwd=str(c),
                           capture_output=True, text=True, timeout=900, env=env)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "atalho ignorado" in r.stdout, r.stdout
        assert (alvo / "hook-rodou").exists(), "o hook tinha de ter rodado"
    finally:
        _limpa(fora)


def test_worktrees_falha_quando_venv_nao_esta_ignorado(tmp_path):
    """`worktrees.sh` confere `git check-ignore .venv-mlx` no fim (#685)."""
    repo = tmp_path / "falso"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "worktrees.sh").write_text((REPO / "tools" / "worktrees.sh").read_text())
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


def _limpa(raiz: Path) -> None:
    """Some com repo de mentira e worktrees dele (ficam fora de `tmp_path`)."""
    import shutil

    for wt in (raiz / "falso-wt").glob("*") if (raiz / "falso-wt").is_dir() else []:
        subprocess.run(["git", "-C", str(raiz / "falso"), "worktree", "remove", "--force",
                        str(wt)], capture_output=True)
    shutil.rmtree(raiz, ignore_errors=True)


# --- guarda contra CÓPIA do dado do dono (#727) ---------------------------------
def test_integra_recusa_worktree_com_copia_do_dado_do_dono(falso):
    """Cópia de voz/estado no worktree é recusada na integração.

    Copiar `voices/`/estado para "validar suíte" foi praxe: o dado copiado
    envelhece em silêncio e o worktree passa a rodar com estado que não é o dele."""
    (falso / "voices").mkdir()
    (falso / "voices" / "v1.wav").write_bytes(b"RIFF voz do dono\n")
    (falso / "settings.json").write_text('{"model": "do-dono"}\n')
    _cria(falso, "agentea")
    b = _cria(falso, "agenteb")

    # sem cópia: passa pela guarda (o worktree nasce vazio)
    assert "cópia do dado do dono" not in subprocess.run(
        [str(b / "tools" / "worktrees.sh"), "verifica", "agentea"], cwd=str(b),
        capture_output=True, text=True).stderr

    # com cópia: `verifica` reprova e a integração recusa antes do hook
    alvo = b.parent / "agentea"
    (alvo / "voices").mkdir(exist_ok=True)
    (alvo / "voices" / "v1.wav").write_bytes(b"RIFF voz do dono\n")
    (alvo / "settings.json").write_text('{"model": "do-dono"}\n')

    r = subprocess.run([str(b / "tools" / "worktrees.sh"), "verifica", "agentea"],
                       cwd=str(b), capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "v1.wav" in r.stderr and "settings.json" in r.stderr

    r = _integra(b / "tools" / "integra.sh", "agentea", cwd=b)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cópia do dado do dono" in r.stderr, r.stderr


def test_verifica_por_conteudo_nao_por_nome(falso):
    """Voz copiada com OUTRO nome também é cópia (compara sha, não caminho)."""
    (falso / "voices").mkdir()
    (falso / "voices" / "v1.wav").write_bytes(b"RIFF mesma voz\n")
    a = _cria(falso, "agentea")
    (a / "voices").mkdir()
    (a / "voices" / "fixture.wav").write_bytes(b"RIFF mesma voz\n")
    r = subprocess.run([str(a / "tools" / "worktrees.sh"), "verifica", "agentea"],
                       cwd=str(a), capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "fixture.wav" in r.stderr


def test_todo_script_que_importa_app_isola_o_estado_em_tmp():
    """#732: TODO `import app` executa depois de um `isola()` DE VERDADE.

    O `conftest` isola o pytest, mas script rodado à mão não passa por ele: um
    `_apply_settings` grava `settings.json` DENTRO do worktree e o diretório passa
    a ter estado do dono.

    Duas armadilhas que a varredura tem de pegar sem virar parser:
    - `isola()` dentro de COMENTÁRIO não isola nada — vale só a parte da linha
      antes do `#`;
    - `from app import X` é importação do app igual ao `import app`.
    """
    alvos = sorted(REPO.glob("smoke_*.py")) + sorted((REPO / "tests").glob("mede_*.py"))
    assert len(alvos) >= 4, f"varredura vazia: {alvos}"
    furos = []
    for caminho in alvos:
        marcou = False
        for n, linha in enumerate(caminho.read_text().splitlines(), 1):
            codigo = linha.split("#", 1)[0]        # comentário fora; heurística de linha
            if "isola()" in codigo:
                marcou = True
            elif "import app" in codigo or "from app import" in codigo:
                if not marcou:
                    furos.append(f"{caminho.name}:{n}: importação do app sem isola() antes")
    assert not furos, "\n".join(furos)
