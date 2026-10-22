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

import yaml

from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
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
    segundos, e o mini é produção."""
    assert _wf()["jobs"]["estatica"]["runs-on"] == "ubuntu-latest"


def test_sem_credencial_do_dono():
    assert "secrets." not in WORKFLOW.read_text(), \
        "o CI é hermético: credencial do dono não entra (nem versionada, nem em secret)"
