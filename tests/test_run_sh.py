"""Contrato do runner das suítes `.sh` (#387).

O runner só vale enquanto a lista estiver certa: suíte renomeada, apagada ou
esquecida fora dos estágios não pode passar despercebida. Este arquivo cobra o
contrato ESTÁTICO (não executa as suítes — quem executa é o `run_sh.sh`).

Cresceu junto do `tests/SHELL-SUITES.md`, que é a explicação; se um dos dois
divergir, o teste cai.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RUNNER = AQUI / "run_sh.sh"
DOC = AQUI / "SHELL-SUITES.md"

# `serial.sh` é o caso de teste em si, invocado por `serial_lock.sh` (a suíte é o
# wrapper com relógio). `run_sh.sh` é o próprio runner.
AJUDANTES = {"serial.sh", "run_sh.sh"}


def _estagios() -> dict[str, list[str]]:
    """Lê os arrays do runner. O runner é DADO aqui: nada de duplicar a lista."""
    fonte = RUNNER.read_text()
    estagios: dict[str, list[str]] = {}
    for nome in ("RAPIDO", "NAVEGADOR", "INSTANCIA"):
        m = re.search(rf"^{nome}=\((.*?)\)$", fonte, re.M | re.S)
        assert m, f"array {nome} sumiu do run_sh.sh"
        estagios[nome] = m.group(1).split()
    return estagios


def test_todo_nome_da_lista_existe_e_e_executavel():
    for estagio, nomes in _estagios().items():
        assert nomes, f"estágio {estagio} vazio"
        for nome in nomes:
            alvo = AQUI / f"{nome}.sh"
            assert alvo.is_file(), f"{estagio}: {nome}.sh não existe"
            assert alvo.stat().st_mode & 0o111, f"{estagio}: {nome}.sh sem bit de execução"


def test_suite_nova_nao_fica_fora_dos_estagios():
    """Toda `.sh` de `tests/` está em UM estágio (ou é ajudante declarado)."""
    dentro = {f"{n}.sh" for nomes in _estagios().values() for n in nomes}
    no_disco = {p.name for p in AQUI.glob("*.sh")}
    faltando = no_disco - dentro - AJUDANTES
    assert not faltando, f"suíte fora de qualquer estágio: {sorted(faltando)}"


def test_nenhuma_suite_esta_em_dois_estagios():
    vistos: dict[str, str] = {}
    for estagio, nomes in _estagios().items():
        for nome in nomes:
            assert nome not in vistos, f"{nome} está em {vistos.get(nome)} e em {estagio}"
            vistos[nome] = estagio


def test_ajudante_declarado_e_usado_e_nao_e_suite():
    for nome in AJUDANTES - {"run_sh.sh"}:
        assert (AQUI / nome).is_file(), f"ajudante {nome} sumiu"
        quem_chama = [p.name for p in AQUI.glob("*.sh")
                      if nome in p.read_text() and p.name != nome]
        assert quem_chama, f"{nome} está como ajudante mas ninguém o invoca"


def test_doc_explica_os_tres_estagios():
    doc = DOC.read_text()
    for estagio in ("rapido", "navegador", "instancia"):
        assert estagio in doc, f"o doc não fala do estágio {estagio}"


def test_runner_isola_log_e_tem_teto_portatil():
    fonte = RUNNER.read_text()
    # log por execução: duas rodadas simultâneas não truncam uma a outra (#243/#224)
    assert "mktemp -d" in fonte
    # `timeout` do coreutils NÃO existe no macOS: o teto é do perl
    assert re.search(r"perl -e 'alarm shift", fonte)
    assert re.search(r"^\s*(?:TETO|_com_teto)", fonte, re.M)


# ───────────────────────────── execução: BASE e teto (#535) ──────────────────
# Os testes acima são estáticos. Os de baixo RODAM o runner com suítes-sonda de
# `tests/fixtures/run_sh/` (via `SUITES=`), sem tocar nas listas reais. Precisam de
# bash + perl (a mesma dependência do `rapido`).

SONDAS = AQUI / "fixtures" / "run_sh"


def _rodar(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
    base.update(env or {})
    return subprocess.run(["bash", str(RUNNER), *args], cwd=str(AQUI.parent),
                          capture_output=True, text=True, timeout=120,
                          env={**base, "TTS_SERIAL_ESPERA": "60"})


def _sonda_saida(tmp_path: Path) -> str:
    p = tmp_path / "sonda.txt"
    return str(p)


def test_navegador_nao_exporta_base_nem_os_escapes_do_dono(tmp_path):
    """#535: o estágio `navegador` é auto-contido e NÃO pode encostar no app do
    dono. Com BASE exportado, `admin_ui_flow`/`cfg_sujo_e_erros` deixavam de subir
    a própria instância e escreviam no `settings.json` dele."""
    saida = _sonda_saida(tmp_path)
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_env.sh"),
                                 "SONDA_SAIDA": saida,
                                 "BASE": "http://127.0.0.1:7860",
                                 "ADMIN_UI_NO_DONO": "", "NO_DONO": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    texto = Path(saida).read_text()
    assert "BASE=[<vazio>]" in texto, texto
    assert "ADMIN_UI_NO_DONO=[<vazio>]" in texto and "NO_DONO=[<vazio>]" in texto, texto


def test_instancia_exporta_base_e_liga_os_escapes_no_7860(tmp_path):
    """O outro lado: em `instancia` o alvo É o app de pé — BASE e escapes valem
    (o `TTS_RUN_SH_PULA_PRECHECK` evita precisar do app só para o teste)."""
    saida = _sonda_saida(tmp_path)
    r = _rodar("instancia", env={"SUITES": str(SONDAS / "sonda_env.sh"),
                                 "SONDA_SAIDA": saida,
                                 "BASE": "http://127.0.0.1:7860",
                                 "ADMIN_UI_NO_DONO": "", "NO_DONO": "",
                                 "TTS_RUN_SH_PULA_PRECHECK": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    texto = Path(saida).read_text()
    assert "BASE=[http://127.0.0.1:7860]" in texto, texto
    assert "ADMIN_UI_NO_DONO=[1]" in texto and "NO_DONO=[1]" in texto, texto


def test_suite_que_dorme_alem_do_teto_reprova_pelo_alarme(tmp_path):
    """Mordida do teto de TRABALHO: 6 s de sono com teto de 2 s tem de reprovar."""
    r = _rodar("rapido", env={"SUITES": str(SONDAS / "trava_lenta.sh"),
                              "TETO_S": "2", "SONDA_SONO": "6",
                              "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FALHA" in r.stdout and "trava_lenta" in r.stdout
    assert "estourado DEPOIS da trava" in r.stdout, r.stdout


def test_tempo_esperando_a_trava_nao_come_o_teto(tmp_path):
    """O defeito medido: com o teto contado da largada, a fila da trava matava a
    suíte antes de ela trabalhar. Presa a trava por 3 s, 0,5 s de trabalho com
    teto de 2 s tem de passar."""
    trava = tmp_path / "lock"
    trava.mkdir()
    dono = subprocess.Popen(["sleep", "30"])
    (trava / "pid").write_text(str(dono.pid))
    (trava / "qual").write_text("dona-de-teste")
    threading.Timer(3.0, lambda: subprocess.run(["rm", "-rf", str(trava)])).start()
    try:
        r = _rodar("rapido", env={"SUITES": str(SONDAS / "trava_curta.sh"),
                                  "TETO_S": "2",
                                  "TTS_SERIAL_LOCK": str(trava)})
    finally:
        dono.kill()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok" in r.stdout and "trava_curta" in r.stdout


# --- #535 (achado do PM): o runner NOMEIA quem escreveu no estado do dono ────
def test_runner_nomeia_a_suite_que_escreveu_no_estado_do_dono(tmp_path):
    """Sem atribuição, "o settings mudou" é fato sem autor — foi o que travou a
    investigação de 01:24. A sonda acrescenta uma linha no `settings.json` real."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh")})
    assert "escrita(s) no estado REAL do dono" in r.stdout, r.stdout
    assert "suja_o_dono.sh escreveu settings.json" in r.stdout, r.stdout
    # sem o knob é AVISO: a suíte em si passou
    assert r.returncode == 0, r.stdout


def test_knob_exige_dono_intocado_vira_falha(tmp_path):
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh"),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 1, r.stdout


def test_suite_que_nao_escreve_nao_aparece(tmp_path):
    """Controle: suíte limpa não gera linha nenhuma."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_env.sh"),
                                 "SONDA_SAIDA": str(tmp_path / "s.txt")})
    assert "estado REAL do dono" not in r.stdout, r.stdout
