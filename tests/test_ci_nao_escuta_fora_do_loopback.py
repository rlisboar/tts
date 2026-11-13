"""#1155: o CI do mini não pode abrir socket FORA do loopback.

O incidente de 2026-11-12: o ALF (firewall do macOS) ficou com um diálogo modal
"Permitir conexões de entrada do Python" e enfileirou TODO fluxo de entrada do
Python — a produção junto. Não há GUI num ssh: o prompt fica pendente e a
produção sai do ar por fora (foram ~20 h sem ninguém notar, porque o smoke só
roda no deploy).

O CI roda no MINI, no mesmo Python da produção. Teste que escuta em `0.0.0.0`
obriga o ALF a decidir por um app que não é o que o dono está usando — então a
regra é: **o que o CI roda escuta só em `127.0.0.1`**.

Isso não é hipótese: as suítes de navegador (`navegador`/`instancia`) escutam em
`0.0.0.0` de propósito, porque a UI é aberta pelo IP de LAN. Elas ficam de fora
porque o CI não as roda — e é isso que a célula garante que continue verdadeiro.

Bidirecional: arquivo na lista que parar de escutar fora do loopback também
falha, senão a lista vira desculpa e ninguém revisa.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_SH = REPO / "tests" / "run_sh.sh"

#: Escuta fora do loopback POR DESENHO — a UI é aberta pelo IP de LAN, e o
#: estágio que as roda (`navegador`) não entra no CI. Se uma destas for para o
#: `rapido`, o CI passa a abrir socket na interface e a entrada tem de sair.
FORA_DO_LOOPBACK = {
    "admin_ui_flow.sh": "a suíte abre a UI pelo IP de LAN (#437/#466)",
    "voz_desfazer_ui.sh": "idem: o roteiro abre a UI pelo IP de LAN",
}

#: `0.0.0.0` e `::` são "qualquer interface"; `localhost`/`127.0.0.1` não contam.
_FORA = re.compile(r"""['"]?(?:0\.0\.0\.0|::)['"]?""")
#: linha de comentário não conta: `ultima_chave_ui.sh` CITA o endereço para
#: explicar por que NÃO escuta nele.
_DECLARA = re.compile(r"""host\s*=\s*['\"]|--host\s+|bind\s*\(|Server\s*\(|listen\s*\(""")


def _estagio(nome: str) -> list[str]:
    """Suítes de um estágio, perguntando ao próprio runner (sem regex no fonte)."""
    r = subprocess.run(["bash", str(RUN_SH), "lista"], cwd=str(REPO),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    for linha in r.stdout.splitlines():
        if linha.startswith(f"{nome}:"):
            return linha.split(":", 1)[1].split()
    pytest.fail(f"estágio `{nome}` não está na lista do run_sh.sh")


def _escuta_fora(p: Path) -> bool:
    for linha in p.read_text(errors="replace").splitlines():
        if linha.lstrip().startswith("#"):
            continue
        if _FORA.search(linha) and _DECLARA.search(linha):
            return True
    return False


def _achados() -> set[str]:
    """Arquivos que o CI roda e que escutam fora do loopback."""
    achados: set[str] = set()
    # pytest inteiro: o CI roda `python -m pytest tests/`
    for arq in sorted((REPO / "tests").glob("*.py")):
        if _escuta_fora(arq):
            achados.add(arq.name)
    # e o estágio rápido das .sh, que o CI roda também
    for nome in _estagio("rapido"):
        arq = REPO / "tests" / f"{nome}.sh"
        assert arq.is_file(), f"suíte do estágio rápido não existe: {arq}"
        if _escuta_fora(arq):
            achados.add(arq.name)
    return achados


def test_ci_nao_escuta_fora_do_loopback():
    novos = sorted(_achados() - set(FORA_DO_LOOPBACK))
    assert not novos, (
        "arquivo que o CI roda escuta fora do loopback (0.0.0.0/::) — o ALF do "
        "mini pode enfileirar a PRODUÇÃO (#1155). Use 127.0.0.1 ou justifique em "
        f"FORA_DO_LOOPBACK: {novos}")


def test_lista_de_excecoes_nao_apodrece():
    """Exceção que não escuta mais fora do loopback tem de sair da lista."""
    rapidos = set(_estagio("rapido"))
    for nome in FORA_DO_LOOPBACK:
        arq = REPO / "tests" / nome
        assert arq.is_file(), f"FORA_DO_LOOPBACK cita {nome}, que não existe"
        assert nome not in rapidos, (
            f"{nome} está no estágio `rapido`, que o CI roda — ou tira a exceção "
            "ou tira a suíte do estágio rápido")
        assert _escuta_fora(arq), (
            f"{nome} não escuta mais fora do loopback — remova de FORA_DO_LOOPBACK")
