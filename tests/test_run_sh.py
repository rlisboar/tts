"""Contrato do runner das suítes `.sh` (#387).

O runner só vale enquanto a lista estiver certa: suíte renomeada, apagada ou
esquecida fora dos estágios não pode passar despercebida. Este arquivo cobra o
contrato ESTÁTICO (não executa as suítes — quem executa é o `run_sh.sh`).

Cresceu junto do `tests/SHELL-SUITES.md`, que é a explicação; se um dos dois
divergir, o teste cai.
"""
from __future__ import annotations

import re
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
