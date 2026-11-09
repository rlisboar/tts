"""#1013: `rm` de PATH que sobrou depois do #992/#996.

O guard de `rm` do harness sai 0 SEM apagar. Um `rm` de PATH vira no-op mudo na
máquina do dev: o uninstall diz "removido" e o plist fica, e o sweeper do run.sh
não tira sessão velha nenhuma. O padrão da casa é `/bin/rm`. Cada célula põe um
`rm` no-op na frente do PATH (e um `launchctl` inofensivo) e cobra que o arquivo
SUMIU de verdade; sem o `/bin/rm`, todas caem.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ZSH = shutil.which("zsh") or "/bin/zsh"
ARQUIVOS = ("cloudflare.sh", "tunnel.sh", "run.sh", "remote/ci_runner.sh")


def _shim(dir_: Path, nome: str, corpo: str) -> None:
    p = dir_ / nome
    p.write_text(corpo)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def amb(tmp_path: Path) -> dict:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _shim(bin_, "rm", "#!/bin/sh\nexit 0\n")          # guard do harness: 0 sem apagar
    _shim(bin_, "launchctl", "#!/bin/sh\nexit 0\n")   # nunca toca o launchd real
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLOUDFLARE_")}
    env.update(HOME=str(home), PATH=f"{bin_}:{env.get('PATH', '')}")
    return {"env": env, "home": home, "tmp": tmp_path}


def _plist(home: Path, label: str) -> Path:
    p = home / "Library" / "LaunchAgents" / f"{label}.plist"
    p.write_text("<plist/>\n")
    return p


@pytest.mark.parametrize("script,label", [
    ("cloudflare.sh", "com.local.cloudflared-tts"),
    ("tunnel.sh", "studio.tts.tunnel"),
    ("remote/ci_runner.sh", "com.local.actions-runner-tts"),
])
def test_uninstall_apaga_o_plist_mesmo_com_rm_de_path_quebrado(amb, script, label):
    plist = _plist(amb["home"], label)
    r = subprocess.run([ZSH, "-f", str(REPO / script), "uninstall"], env=amb["env"],
                       capture_output=True, text=True, timeout=30, cwd=str(amb["tmp"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not plist.exists(), f"{script} disse que removeu e o plist ficou (rm de PATH)"


def test_sweeper_do_run_sh_apaga_sessao_velha_com_rm_de_path_quebrado(amb):
    """A linha do sweeper sai do próprio run.sh (sem o `&`) e roda com o rm no-op:
    sessão de 3 dias some, a de agora fica."""
    linha = next(l for l in (REPO / "run.sh").read_text().splitlines()
                 if l.startswith('find "$DSH_HOME/sessions"'))
    linha = re.sub(r"\s*&\s*$", "", linha)
    dsh = amb["tmp"] / "dsh"
    velha = dsh / "sessions" / "proj" / "velha"
    nova = dsh / "sessions" / "proj" / "nova"
    for d in (velha, nova):
        d.mkdir(parents=True)
        (d / "estado.json").write_text("{}")
    tres_dias = time.time() - 3 * 86400
    os.utime(velha, (tres_dias, tres_dias))
    r = subprocess.run([ZSH, "-f", "-c", linha], env={**amb["env"], "DSH_HOME": str(dsh)},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not velha.exists(), "sweeper não apagou a sessão velha (rm de PATH)"
    assert nova.exists(), "sweeper não pode tocar sessão recente"


def test_nenhum_rm_de_path_sobra_nos_quatro_scripts():
    """Cobre o que a célula dinâmica não alcança (o `tar && rm` do install do
    ci_runner): nenhum `rm` sem caminho absoluto nos quatro arquivos."""
    nu = re.compile(r"(^|[\s;&|({])rm\s+-")
    sobras = [f"{a}:{n}: {l.strip()}"
              for a in ARQUIVOS
              for n, l in enumerate((REPO / a).read_text().splitlines(), 1)
              if not l.lstrip().startswith("#") and nu.search(l)]
    assert sobras == [], "rm de PATH remanescente:\n" + "\n".join(sobras)
