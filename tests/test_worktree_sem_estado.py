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
         ("tests/test_api.py", "save_preserva or save_avisa")]

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
        # o estado do dono NÃO entra
        for arq in ("settings.json", ".apikey", ".apikeys.json", ".speaker-profiles.json"):
            assert not (wt / arq).exists(), f"{arq} veio junto (não devia)"

        env = {**os.environ, "PATH": os.environ.get("PATH", ""),
               "HOME": os.environ.get("HOME", "/tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
        for arq in ("settings.json", ".apikey", ".apikeys.json"):
            env.pop(f"TTS_{arq}", None)
        for arquivo, filtro in FATIA:
            r = subprocess.run([str(PY), "-m", "pytest", arquivo, "-q", "-k", filtro],
                               cwd=str(wt), capture_output=True, text=True,
                               timeout=900, env=env)
            assert r.returncode == 0, (f"{arquivo} -k {filtro}\n"
                                       + r.stdout[-2500:] + r.stderr[-1500:])
    finally:
        subprocess.run([*git, "worktree", "remove", "--force", str(wt)],
                       capture_output=True)
