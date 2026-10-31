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
