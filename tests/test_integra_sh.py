"""#911: o `integra.sh` tem de mergear o SHA que o HOOK exercitou, não a ref.

O script rebasa, roda o hook (a suíte cheia, ~25 min) e mergeava `wt/<agente>`
pela REF. Quem commitasse durante a suíte entrava na `main` sem nunca ter
passado pelo gate — foi assim que o #908 entrou.

O teste monta um repo descartável cujo hook REGISTRA o SHA exercitado e commita
outro em seguida. A `main` tem de parar no SHA registrado; o commit intruso fica
para a próxima rodada (e o script avisa).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

RAIZ_REAL = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ_REAL / "tools" / "integra.sh"
GIT = shutil.which("git") or "git"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run([GIT, *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    raiz = tmp_path / "tts-rod"
    raiz.mkdir()
    _git("init", "-q", "-b", "main", cwd=raiz)
    _git("config", "user.email", "teste@exemplo.invalid", cwd=raiz)
    _git("config", "user.name", "teste", cwd=raiz)
    (raiz / "a.txt").write_text("base\n")
    (raiz / ".gitignore").write_text("/.venv-mlx\n")
    _git("add", "a.txt", ".gitignore", cwd=raiz)
    _git("commit", "-q", "-m", "base", cwd=raiz)

    wt = tmp_path / "tts-rod-wt" / "agente"
    _git("worktree", "add", "-q", "-b", "wt/agente", str(wt), cwd=raiz)
    # o `integra.sh` exige o venv do worktree antes do hook (#1113)
    (wt / ".venv-mlx" / "bin").mkdir(parents=True)
    (wt / ".venv-mlx" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (wt / ".venv-mlx" / "bin" / "python").chmod(0o755)
    (wt / "b.txt").write_text("mudanca\n")
    _git("add", "b.txt", cwd=wt)
    _git("commit", "-q", "-m", "mudanca", cwd=wt)

    # hook: anota o que está sendo validado e commita POR CIMA no meio da rodada
    hook = wt / ".githooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        "#!/bin/sh\n"
        "set -e\n"
        f"git rev-parse HEAD > '{tmp_path}/validado'\n"
        "git commit -q --allow-empty -m 'intruso durante o hook'\n")
    hook.chmod(0o755)
    _git("add", ".githooks/pre-commit", cwd=wt)
    _git("commit", "-q", "-m", "hook", cwd=wt)      # o script exige worktree limpo
    return raiz, wt


def test_integra_mergeia_o_sha_validado_e_nao_a_ref(tmp_path):
    raiz, wt = _repo(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                        "TTS_INTEGRA_SEM_HOOK")}
    r = subprocess.run([str(SCRIPT), "agente"], cwd=raiz, env=env,
                       capture_output=True, text=True)

    assert r.returncode == 0, r.stdout + r.stderr
    validado = (tmp_path / "validado").read_text().strip()
    assert validado, "o hook nem rodou"

    main = _git("rev-parse", "main", cwd=raiz)
    assert main == validado, (
        "a main recebeu um commit que o hook não exercitou "
        f"(validado {validado[:7]}, main {main[:7]})")
    assert _git("rev-parse", "wt/agente", cwd=raiz) != main, (
        "o commit intruso ficou para a próxima rodada")
    assert "a branch andou durante o hook" in r.stdout, "o aviso tem de ser alto"
