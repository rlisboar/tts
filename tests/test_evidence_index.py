"""Prova nova em `evidence/` entra com número de task e cabeçalho (#390).

`evidence/` é onde o time guarda a MEDIÇÃO (captura, log, script de célula). Sem
regra, ele virou 304 arquivos sem índice: 29 sem número de task no nome e sem
proveniência no corpo, então "já está provado em evidence" não era verificável.

O histórico fica como está — o que se cobra aqui é o arquivo NOVO. A válvula é o
`evidence/INDEX.md`: arquivo já indexado é aceito; arquivo fora do padrão precisa
ser regerado no índice (`tests/gera_evidence_index.py`), que é o mesmo commit que
acrescenta a prova.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
EV = RAIZ / "evidence"
INDICE = EV / "INDEX.md"
PREFIXO = re.compile(r"^\d+-")


def _no_indice() -> set[str]:
    texto = INDICE.read_text()
    return set(re.findall(r"^\| `([^`]+)` \|", texto, re.M))


def _novos() -> list[str]:
    """Só ADDED em `evidence/` (`??` ou `A`): arquivo que já existia não muda de
    nome retroativamente, e marcar `M` faria toda edição de prova velha reprovar."""
    r = subprocess.run(["git", "status", "--porcelain", "--", "evidence"],
                       cwd=RAIZ, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    nomes = []
    for linha in r.stdout.splitlines():
        if linha[:2] not in ("??", "A ", "AM"):
            continue
        caminho = linha[3:].strip().strip('"')
        if " -> " in caminho:
            caminho = caminho.split(" -> ")[-1]
        if caminho.startswith("evidence/"):
            nomes.append(caminho.split("/", 1)[1])
    return nomes


def test_indice_existe_e_cobre_o_acervo():
    assert INDICE.is_file(), "evidence/INDEX.md sumiu"
    arquivos = {p.name for p in EV.iterdir() if p.is_file()} - {"INDEX.md"}
    faltando = arquivos - _no_indice()
    assert not faltando, (
        f"{len(faltando)} arquivo(s) em evidence/ fora do índice — regenere com "
        f"tests/gera_evidence_index.py: {sorted(faltando)[:8]}")


def test_prova_nova_tem_prefixo_e_cabecalho():
    problemas = []
    for nome in _novos():
        arq = EV / nome
        if not arq.is_file() or nome == "INDEX.md":
            continue
        if not PREFIXO.match(nome):
            problemas.append(f"{nome}: sem prefixo NNN- (número da task)")
            continue
        if arq.suffix in (".md", ".txt", ".sh", ".py", ".json"):
            linhas = arq.read_text(errors="replace").splitlines()
            primeira = next((l.strip() for l in linhas if l.strip()), "")
            if len(primeira) < 8:
                problemas.append(f"{nome}: primeira linha não diz o que a prova mostra")
    assert not problemas, "arquivo novo de evidence fora do padrão:\n  " + "\n  ".join(problemas)
