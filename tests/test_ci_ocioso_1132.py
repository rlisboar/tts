"""#1132 — o CI no mini só larga a suíte com o mini OCIOSO (`tools/ci_mini_ocioso.sh`).

O runner do GitHub é self-hosted NO mini: a suíte cheia disputa CPU/GPU com a produção (7860) e com
as bancadas. A trava de suíte vive no Mac de dev e não protege o mini; o porteiro é o equivalente lá.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
PORTEIRO = REPO / "tools" / "ci_mini_ocioso.sh"
BANCADA = REPO / "tools" / "bancada_lock.sh"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="precisa de bash")


def _env(tmp_path, status, **extra):
    """`curl` falso devolve o conteúdo de `status` (arquivo); apagado = app fora do ar."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    shim = bin_ / "curl"
    shim.write_text(f'#!/bin/sh\n[ -f "{status}" ] && cat "{status}" || exit 7\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    e = {k: v for k, v in os.environ.items() if not k.startswith("TTS_CI") and k != "GITHUB_OUTPUT"}
    e.update(PATH=f"{bin_}:{e['PATH']}", HOME=str(tmp_path), GITHUB_OUTPUT=str(tmp_path / "saida"),
             GITHUB_STEP_SUMMARY=str(tmp_path / "resumo"), TTS_CI_ESTAVEL="0", TTS_CI_ESPERA="3",
             TTS_BANCADA_LOCK=str(tmp_path / "bancada.lock"))
    e.update(extra)
    return e


def _roda(tmp_path, status, **extra):
    r = subprocess.run([BASH, str(PORTEIRO)], env=_env(tmp_path, status, **extra),
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    saida = tmp_path / "saida"
    return r, (saida.read_text().strip() if saida.exists() else "")


def _status(tmp_path, jobs=0, live=0):
    p = tmp_path / "status.json"
    p.write_text(f'{{"jobs_active": {jobs}, "live_sessions": {live}}}')
    return p


def test_mini_ocioso_larga_na_hora(tmp_path):
    r, out = _roda(tmp_path, _status(tmp_path))
    assert r.returncode == 0 and out == "ocioso=true", (r.stdout, r.stderr, out)


@pytest.mark.parametrize("jobs,live", [(3, 0), (0, 1), (2, 1)])
def test_com_job_ou_live_espera_e_esgotar_e_neutro(tmp_path, jobs, live):
    t0 = time.time()
    r, out = _roda(tmp_path, _status(tmp_path, jobs, live))
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)    # NEUTRO: nunca vermelho
    assert out == "ocioso=false", out
    assert time.time() - t0 >= 2 and "NÃO rodei a suíte" in r.stdout, r.stdout
    assert "A suíte NÃO rodou" in (tmp_path / "resumo").read_text()
    assert "::warning" in r.stdout


def test_larga_assim_que_o_mini_fica_ocioso_durante_a_espera(tmp_path):
    st = _status(tmp_path, jobs=4)
    p = subprocess.Popen([BASH, str(PORTEIRO)], env=_env(tmp_path, st, TTS_CI_ESPERA="30"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         stdin=subprocess.DEVNULL)
    time.sleep(2)
    _status(tmp_path, jobs=0)
    out = p.communicate(timeout=40)[0]
    assert p.returncode == 0 and "esperando: produção ocupada" in out and "pode rodar" in out, out
    assert (tmp_path / "saida").read_text().strip() == "ocioso=true"


def test_bancada_lock_segura_o_ci_e_soltar_libera(tmp_path):
    st = _status(tmp_path)
    lock = tmp_path / "bancada.lock"
    dono = subprocess.Popen(["sleep", "20"])
    try:
        lock.write_text(f"agente=speech\npid={dono.pid}\ndesde=1\n")
        r, out = _roda(tmp_path, st)
        assert out == "ocioso=false" and "bancada (speech)" in r.stdout, (r.stdout, out)
        (tmp_path / "saida").unlink()
        lock.unlink()
        r2, out2 = _roda(tmp_path, st)
        assert out2 == "ocioso=true", (r2.stdout, out2)
    finally:
        dono.kill()
        dono.wait()


def test_bancada_lock_orfao_nao_trava_o_ci_para_sempre(tmp_path):
    st = _status(tmp_path)
    lock = tmp_path / "bancada.lock"
    morto = subprocess.Popen(["true"])
    morto.wait()
    lock.write_text(f"agente=speech\npid={morto.pid}\ndesde=1\n")          # dono morto
    assert _roda(tmp_path, st)[1] == "ocioso=true"
    (tmp_path / "saida").unlink()
    vivo = subprocess.Popen(["sleep", "20"])
    try:
        lock.write_text(f"agente=speech\npid={vivo.pid}\ndesde=1\n")
        velho = time.time() - 5 * 3600                                    # > 4 h
        os.utime(lock, (velho, velho))
        assert _roda(tmp_path, st)[1] == "ocioso=true"
    finally:
        vivo.kill()
        vivo.wait()


def test_exige_estabilidade_um_vao_curto_nao_basta(tmp_path):
    """Entre lotes da narração há vãos curtos: o porteiro só larga depois de TTS_CI_ESTAVEL s
    SEGUIDOS ocioso; um job no meio zera a contagem."""
    st = _status(tmp_path)
    t0 = time.time()
    r, out = _roda(tmp_path, st, TTS_CI_ESTAVEL="6", TTS_CI_ESPERA="30")
    assert out == "ocioso=true" and time.time() - t0 >= 5, (r.stdout, time.time() - t0)
    (tmp_path / "saida").unlink()
    st_ocupado = _status(tmp_path, jobs=1)
    p = subprocess.Popen([BASH, str(PORTEIRO)], env=_env(tmp_path, st_ocupado, TTS_CI_ESTAVEL="6",
                                                          TTS_CI_ESPERA="12"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         stdin=subprocess.DEVNULL)
    for _ in range(4):                      # vãos de ~3 s entre jobs: nunca 6 s seguidos
        time.sleep(3)
        _status(tmp_path, jobs=0)
        time.sleep(3)
        _status(tmp_path, jobs=1)
    out2 = p.communicate(timeout=40)[0]
    assert (tmp_path / "saida").read_text().strip() == "ocioso=false", out2


def test_app_fora_do_ar_nao_segura_o_ci(tmp_path):
    r, out = _roda(tmp_path, tmp_path / "inexistente.json")           # curl falha
    assert out == "ocioso=true", (r.stdout, out)


def test_bancada_lock_helper_pega_status_e_solta(tmp_path):
    e = {**os.environ, "TTS_BANCADA_LOCK": str(tmp_path / "b.lock"), "HOME": str(tmp_path)}
    r = subprocess.run([BASH, str(BANCADA), "pega", "speech", str(os.getpid())], env=e, capture_output=True,
                       text=True)
    assert r.returncode == 0 and f"pid={os.getpid()}" in r.stdout
    assert oct((tmp_path / "b.lock").stat().st_mode & 0o777) == "0o600"
    assert "PRESENTE" in subprocess.run([BASH, str(BANCADA), "status"], env=e, capture_output=True,
                                        text=True).stdout
    subprocess.run([BASH, str(BANCADA), "solta"], env=e, check=True, capture_output=True)
    assert not (tmp_path / "b.lock").exists()


# --- #1150: `pega` atômico e que ESPERA dono vivo -----------------------------------------

def _env_b(tmp_path, **extra):
    return {**os.environ, "TTS_BANCADA_LOCK": str(tmp_path / "b.lock"), "HOME": str(tmp_path),
            "TTS_BANCADA_PASSO": "0.2", **extra}


def _vivo(tmp_path):
    """processo longo que faz de dono vivo"""
    return subprocess.Popen(["sleep", "60"])


def test_dois_pega_simultaneos_um_espera_o_outro(tmp_path):
    d1, d2 = _vivo(tmp_path), _vivo(tmp_path)
    try:
        e = _env_b(tmp_path, TTS_BANCADA_ESPERA="30")
        p1 = subprocess.Popen([BASH, str(BANCADA), "pega", "audio-ml", str(d1.pid)], env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([BASH, str(BANCADA), "pega", "speech", str(d2.pid)], env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import time
        time.sleep(1.5)
        # exatamente UM largou; o outro ainda espera e o lock é do vencedor (não sobrescrito)
        rcs = [p1.poll(), p2.poll()]
        assert sorted(x is None for x in rcs) == [False, True], rcs
        vencedor, perdedor = (p1, p2) if p1.poll() == 0 else (p2, p1)
        dono = (tmp_path / "b.lock").read_text()
        assert f"pid={d1.pid if vencedor is p1 else d2.pid}" in dono, dono
        # solta: o que esperava assume
        subprocess.run([BASH, str(BANCADA), "solta"], env=e, check=True, capture_output=True)
        assert perdedor.wait(timeout=10) == 0
        assert f"pid={d2.pid if vencedor is p1 else d1.pid}" in (tmp_path / "b.lock").read_text()
    finally:
        for d in (d1, d2):
            d.kill()


def test_pega_esgota_o_teto_com_dono_vivo_e_nao_sobrescreve(tmp_path):
    d1, d2 = _vivo(tmp_path), _vivo(tmp_path)
    try:
        e = _env_b(tmp_path, TTS_BANCADA_ESPERA="1")
        assert subprocess.run([BASH, str(BANCADA), "pega", "a", str(d1.pid)], env=e,
                              capture_output=True).returncode == 0
        r = subprocess.run([BASH, str(BANCADA), "pega", "b", str(d2.pid)], env=e,
                           capture_output=True, text=True, timeout=20)
        assert r.returncode == 1 and "OCUPADA por a" in r.stderr, (r.returncode, r.stderr)
        assert f"pid={d1.pid}" in (tmp_path / "b.lock").read_text()
    finally:
        for d in (d1, d2):
            d.kill()


def test_pega_toma_o_lock_de_dono_morto_e_status_acusa_orfao(tmp_path):
    e = _env_b(tmp_path, TTS_BANCADA_ESPERA="5")
    morto = subprocess.Popen(["true"]); morto.wait()
    (tmp_path / "b.lock").write_text(f"agente=morto\npid={morto.pid}\ndesde=1\n")
    st = subprocess.run([BASH, str(BANCADA), "status"], env=e, capture_output=True, text=True).stdout
    assert "ÓRFÃO" in st, st
    vivo = _vivo(tmp_path)
    try:
        r = subprocess.run([BASH, str(BANCADA), "pega", "novo", str(vivo.pid)], env=e,
                           capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr
        assert f"pid={vivo.pid}" in (tmp_path / "b.lock").read_text()
    finally:
        vivo.kill()


def test_guarda_fresca_de_outro_pega_nao_gira_o_laco_quente(tmp_path):
    """qa #1150: com lock órfão e a guarda `mkdir` de outro `pega` fresca, o laço dava `continue`
    sem dormir (28 s de CPU). Um `sleep` de fachada no PATH conta as dormidas."""
    import time
    morto = subprocess.Popen(["true"]); morto.wait()
    (tmp_path / "b.lock").write_text(f"agente=morto\npid={morto.pid}\ndesde=1\n")
    (tmp_path / "b.lock.guarda").mkdir()
    fachada = tmp_path / "bin"; fachada.mkdir()
    conta = tmp_path / "dormiu"
    (fachada / "sleep").write_text(f'#!/bin/sh\necho x >> "{conta}"\nexec /bin/sleep "$@"\n')
    (fachada / "sleep").chmod(0o755)
    e = _env_b(tmp_path, TTS_BANCADA_ESPERA="30", PATH=f"{fachada}:{os.environ['PATH']}")
    p = subprocess.Popen([BASH, str(BANCADA), "pega", "a", str(os.getpid())], env=e,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        assert p.poll() is None
        n = len(conta.read_text().split()) if conta.exists() else 0
        assert 2 <= n <= 12, f"dormidas em 1,5 s: {n} (0 = laço quente)"
    finally:
        p.kill(); p.wait()


def test_mesmo_dono_pedindo_de_novo_e_idempotente(tmp_path):
    vivo = _vivo(tmp_path)
    try:
        e = _env_b(tmp_path, TTS_BANCADA_ESPERA="1")
        for _ in range(2):
            r = subprocess.run([BASH, str(BANCADA), "pega", "a", str(vivo.pid)], env=e,
                               capture_output=True, text=True, timeout=20)
            assert r.returncode == 0, r.stderr
    finally:
        vivo.kill()


# --- o workflow ----------------------------------------------------------------------------

def _wf():
    return yaml.safe_load(WORKFLOW.read_text())


def test_os_dois_jobs_do_mini_esperam_o_porteiro_e_pulam_a_suite_se_ocupado():
    jobs = _wf()["jobs"]
    for nome in ("testes", "testes-lentos"):
        passos = jobs[nome]["steps"]
        porteiro = [p for p in passos if p.get("id") == "ocioso"]
        assert len(porteiro) == 1 and "tools/ci_mini_ocioso.sh" in porteiro[0]["run"], nome
        depois = passos[passos.index(porteiro[0]) + 1:]
        com_pytest = [p for p in depois if "pytest" in p.get("run", "")]
        assert com_pytest, nome
        for p in com_pytest:
            assert p.get("if") == "steps.ocioso.outputs.ocioso == 'true'", (nome, p.get("name"))
        assert int(jobs[nome]["env"]["TTS_CI_ESPERA"]) > 0


def test_testes_e_lentos_rodam_em_serie_no_mini():
    jobs = _wf()["jobs"]
    assert "testes" in jobs["testes-lentos"]["needs"]                  # o lento espera o rápido
    assert "!cancelled()" in jobs["testes-lentos"]["if"]               # mas roda mesmo se ele falhar
    for nome in ("testes", "testes-lentos"):
        assert jobs[nome]["concurrency"]["group"] == "tts-mini-suite", nome
        assert jobs[nome]["concurrency"]["cancel-in-progress"] is False


def test_o_teto_do_job_cobre_a_espera_mais_o_trabalho():
    jobs = _wf()["jobs"]
    assert jobs["testes"]["timeout-minutes"] * 60 >= int(jobs["testes"]["env"]["TTS_CI_ESPERA"]) + 20 * 60
    assert (jobs["testes-lentos"]["timeout-minutes"] * 60
            >= int(jobs["testes-lentos"]["env"]["TTS_CI_ESPERA"]) + 45 * 60)
