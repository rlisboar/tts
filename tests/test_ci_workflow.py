"""O gate do CI (#293) tem de continuar sendo um gate — e barato.

POR QUE ESTE ARQUIVO EXISTE: o workflow é o único lugar do repo onde um erro de
edição não quebra nada visível (ninguém roda YAML antes do push, e um `on:` que
sumiu só aparece quando o check deixa de existir). Aqui o `ci.yml` é lido como
dado e as propriedades que fazem dele um gate são cobradas:

  • dispara em push E pull_request para `main`;
  • o estágio estático vem PRIMEIRO, com `timeout`, e roda a lista DERIVADA do
    hook — se alguém colar a lista à mão no YAML, a derivação vira decorativa e
    o workflow passa a ter uma cópia que envelhece em silêncio;
  • os jobs caros dependem dele (`needs:`) — suíte vermelha por import morto não
    pode gastar runner;
  • a suíte é o subconjunto HERMÉTICO: nada de `-m worker_real` (modelo real) e
    nada de `secrets.` (o CI não usa credencial do dono).

NUANCE: o estágio estático não pode instalar o venv do app. Se `estatica` passar
a instalar `requirements.txt`, ele deixa de ficar vermelho em segundos — que é a
única razão de existir separado.
"""

import re
import os
import subprocess
import sys

import yaml

from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REPO = BASE
WORKFLOW = BASE / ".github" / "workflows" / "ci.yml"


def _wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _gatilhos(wf: dict) -> dict:
    # `on` sem quotes no YAML 1.1 do pyyaml vira o booleano True.
    return wf.get("on") or wf.get(True) or {}


def _passos(job: dict) -> str:
    return "\n".join(str(p.get("run", "")) for p in job.get("steps", []))


def test_workflow_existe_e_e_yaml_valido():
    assert WORKFLOW.is_file(), "o gate do CI sumiu"
    assert isinstance(_wf(), dict)


def test_dispara_em_push_e_pr_para_main():
    g = _gatilhos(_wf())
    for gatilho in ("push", "pull_request"):
        assert gatilho in g, f"o worker não dispara em {gatilho}"
        assert "main" in (g[gatilho] or {}).get("branches", []), \
            f"{gatilho} não é limitado a main"


def test_estatica_e_o_primeiro_job_e_roda_a_lista_derivada():
    jobs = _wf()["jobs"]
    assert list(jobs)[0] == "estatica", "o estágio estático deixou de ser o primeiro"
    estatica = jobs["estatica"]
    assert estatica.get("timeout-minutes"), "estática sem timeout"
    passos = _passos(estatica)
    assert "_pyflakes_gate.py" in passos, \
        "o job estático tem de rodar a lista DERIVADA do hook, não uma cópia"
    assert "ruff" in passos, "o estágio estático tem de instalar o ruff"
    assert "requirements.txt" not in passos, \
        "estática instalando o venv do app deixa de ficar vermelha em segundos"


def test_jobs_caros_dependem_do_estatico_e_tem_timeout():
    for nome, job in _wf()["jobs"].items():
        if nome == "estatica":
            continue
        assert job.get("needs") == "estatica", f"`{nome}` não depende do estágio estático"
        assert job.get("timeout-minutes"), f"`{nome}` sem timeout de job"


def test_suite_e_o_subconjunto_hermetico():
    passos = _passos(_wf()["jobs"]["testes"])
    assert "worker_real" not in passos, "o caminho com modelo real não é do CI"
    assert "--timeout=" in passos, "sem --timeout um teste pendurado come o job"


def test_suite_roda_no_runner_do_mini_nao_no_hospedado():
    """Medido na #293: no `ubuntu-latest` a suíte dá 36 falhas em 642 passes (MLX
    sem Metal, pid/`ps` do dsh, ffmpeg do webm, `/tmp` do macOS). Vermelho de
    ambiente em todo PR não é gate — o job da suíte é do runner do mini."""
    alvo = _wf()["jobs"]["testes"]["runs-on"]
    alvos = [alvo] if isinstance(alvo, str) else alvo
    assert "self-hosted" in alvos and "tts-mini" in alvos, \
        f"a suíte voltou para runner onde ela não passa: {alvo}"


def test_estatica_continua_hospedada_e_barata():
    """A estática não pode virar self-hosted: ela é a que fica vermelha em
    segundos, e o mini é produção.

    #783: a imagem é FIXA (`ubuntu-24.04`), não `-latest` — o runner hospedado
    troca de versão sem aviso e o job de segundos vira surpresa."""
    alvo = _wf()["jobs"]["estatica"]["runs-on"]
    assert alvo.startswith("ubuntu-"), alvo
    assert alvo != "ubuntu-latest", "imagem do runner voltou a ser móvel"


def test_sem_credencial_do_dono():
    assert "secrets." not in WORKFLOW.read_text(), \
        "o CI é hermético: credencial do dono não entra (nem versionada, nem em secret)"


def _coleta(*markexpr: str) -> set[str]:
    """Node ids de uma coleta; `markexpr` vazio = suíte inteira."""
    cmd = [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"]
    if markexpr:
        cmd += ["-m", markexpr[0]]
    # #1131: a coleta aninhada da suíte INTEIRA não executa nada, mas o guarda do conftest a
    # leria como "suíte cheia fora da trava" (rc 75) quando esta célula roda sozinha
    env = {**os.environ, "TTS_FILA": "0"}
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=600, env=env)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-1000:]
    return {l.strip() for l in r.stdout.splitlines() if "::" in l and not l.startswith(" ")}


def _seletor(job: str) -> str:
    """A expressão `-m` que o job passa ao pytest (o YAML é a fonte, não o teste)."""
    passos = _wf()["jobs"][job]["steps"]
    texto = "\n".join(p.get("run", "") for p in passos)
    # depois de `pytest`, o `-m` é o do seletor (antes dele vem `python -m pytest`)
    achado = re.search(r"""pytest[^\n]*?-m +"([^"]+)"|pytest[^\n]*?-m +([^\s"']+)""", texto)
    if not achado:
        raise AssertionError(f"o job {job} não tem seletor `-m` — a partição virou decorativa")
    return achado.group(1) or achado.group(2)


def test_os_dois_jobs_particionam_a_suite():
    """Os seletores dos DOIS jobs têm de particionar a suíte (#783).

    O rápido roda o seletor de `testes`, o lento o de `testes-lentos`. Se um
    deles for editado (um `-m` a mais, um `-k` que escapa), testes somem dos
    dois jobs e a suíte fica verde por ausência — o pior tipo de gate quebrado."""
    todos = _coleta()
    rapidos = _coleta(_seletor("testes"))
    lentos = _coleta(_seletor("testes-lentos"))
    assert rapidos | lentos == todos, (
        f"fora dos dois jobs: {sorted(todos - (rapidos | lentos))[:10]}")
    assert not (rapidos & lentos), (
        f"nos dois jobs ao mesmo tempo: {sorted(rapidos & lentos)[:10]}")
