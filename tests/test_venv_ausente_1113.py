"""#1113 — worktree sem `.venv-mlx`: hook, `integra.sh` e `worktrees.sh liga`.

No trem da chave (#1111) os worktrees foram criados com `git worktree add` cru, sem
o `tools/worktrees.sh cria` que liga `.venv-mlx` e os pesos. O hook caía no
`python3` do sistema e morria em `No module named ruff`, sem dizer a causa — e o
caminho de menor resistência virou `--no-verify`. Agora:

- o hook para ANTES de ruff/pytest, dizendo o conserto (`liga --aqui` no worktree);
- o `integra.sh` recusa o worktree sem venv antes de rodar o hook;
- `tools/worktrees.sh liga [<agente>|--aqui]` adota o worktree feito por fora.

Tudo em repo FALSO sob tmp — o repo de verdade não é tocado.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", "-c", "user.email=x@y", "-c", "user.name=x", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _raiz(tmp_path: Path, *, venv: bool = True) -> Path:
    """Raiz falsa: hook e scripts REAIS do repo, `.gitignore` como o de verdade."""
    raiz = tmp_path / "raiz"
    (raiz / "tools").mkdir(parents=True)
    (raiz / ".githooks").mkdir()
    for rel in ("tools/worktrees.sh", "tools/integra.sh", ".githooks/pre-commit"):
        (raiz / rel).write_text((REPO / rel).read_text())
        (raiz / rel).chmod(0o755)
    (raiz / ".gitignore").write_text("/.venv-mlx\n/models/*.bin\n")
    (raiz / "models").mkdir()
    (raiz / "models" / "peso.bin").write_bytes(b"peso")
    (raiz / "models" / "sha256sums.txt").write_text(
        "deadbeef  models/peso.bin\n")
    (raiz / "a.txt").write_text("a\n")
    if venv:
        (raiz / ".venv-mlx" / "bin").mkdir(parents=True)
        (raiz / ".venv-mlx" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
        (raiz / ".venv-mlx" / "bin" / "python").chmod(0o755)
    _git("init", "-q", "-b", "main", cwd=raiz)
    _git("add", "-A", cwd=raiz)
    _git("commit", "-q", "-m", "raiz", cwd=raiz)
    return raiz


def _wt_cru(raiz: Path, nome: str = "agente") -> Path:
    """Worktree do jeito ERRADO (o do trem): `git worktree add` cru, sem links."""
    wt = raiz.parent / f"{raiz.name}-wt" / nome
    _git("worktree", "add", "-q", "-b", f"wt/{nome}", str(wt), cwd=raiz)
    return wt


def _shim_python3(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """`python3` falso no PATH: anota se alguém caiu nele (a queda antiga)."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "python3.log"
    (bin_ / "python3").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
    (bin_ / "python3").chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "TTS_INTEGRA_SEM_HOOK")}
    env["PATH"] = f"{bin_}:{env['PATH']}"
    return env, log


def test_hook_em_worktree_sem_venv_para_antes_de_tudo_e_diz_o_conserto(tmp_path):
    raiz = _raiz(tmp_path)
    wt = _wt_cru(raiz)
    env, log = _shim_python3(tmp_path)
    r = subprocess.run([str(wt / ".githooks" / "pre-commit")], cwd=str(wt), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "sem .venv-mlx" in r.stderr and "tools/worktrees.sh liga --aqui" in r.stderr, r.stderr
    assert not log.exists(), "o hook caiu no python3 do sistema (a queda antiga)"


def test_hook_na_raiz_sem_venv_tambem_para(tmp_path):
    raiz = _raiz(tmp_path, venv=False)
    env, log = _shim_python3(tmp_path)
    r = subprocess.run([str(raiz / ".githooks" / "pre-commit")], cwd=str(raiz), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "árvore principal sem venv" in r.stderr, r.stderr
    assert not log.exists()


def test_integra_recusa_worktree_sem_venv_antes_do_hook(tmp_path):
    raiz = _raiz(tmp_path)
    wt = _wt_cru(raiz)
    (wt / "b.txt").write_text("b\n")
    _git("add", "b.txt", cwd=wt)
    _git("commit", "-q", "-m", "mudanca", cwd=wt)
    env, log = _shim_python3(tmp_path)
    antes = _git("rev-parse", "main", cwd=raiz)
    r = subprocess.run([str(raiz / "tools" / "integra.sh"), "agente"], cwd=str(raiz),
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "sem .venv-mlx" in r.stdout + r.stderr
    assert "tools/worktrees.sh liga agente" in r.stdout + r.stderr
    assert _git("rev-parse", "main", cwd=raiz) == antes        # nada integrado
    assert not log.exists()


def test_liga_aqui_adota_o_worktree_cru_e_o_hook_passa_do_portao(tmp_path):
    raiz = _raiz(tmp_path)
    wt = _wt_cru(raiz)
    r = subprocess.run(["bash", str(wt / "tools" / "worktrees.sh"), "liga", "--aqui"],
                       cwd=str(wt), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (wt / ".venv-mlx").is_symlink()
    assert os.readlink(wt / ".venv-mlx") == str(raiz / ".venv-mlx")
    assert (wt / "models" / "peso.bin").is_symlink()          # o peso do manifesto
    assert _git("status", "--porcelain", cwd=wt) == ""         # links ignorados
    # idempotente: de novo não quebra nem aninha (`x/x`, #703)
    r2 = subprocess.run(["bash", str(wt / "tools" / "worktrees.sh"), "liga", "--aqui"],
                        cwd=str(wt), capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0, r2.stderr
    assert not (raiz / ".venv-mlx" / ".venv-mlx").exists()


def test_liga_recusa_a_arvore_principal(tmp_path):
    raiz = _raiz(tmp_path)
    r = subprocess.run(["bash", str(raiz / "tools" / "worktrees.sh"), "liga", "--aqui"],
                       cwd=str(raiz), capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "árvore principal" in r.stderr
