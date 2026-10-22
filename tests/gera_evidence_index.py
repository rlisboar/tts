"""Regera `evidence/INDEX.md` (#390).

Uso: `./.venv-mlx/bin/python tests/gera_evidence_index.py`

O índice é derivado do que está versionado (task pelo prefixo numérico do nome, data
do último commit, primeira linha como resumo). É a fonte que o
`tests/test_evidence_index.py` usa para reconhecer arquivo antigo fora do padrão —
por isso ele precisa ser regenerado no mesmo commit que acrescenta prova nova.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
EV = RAIZ / "evidence"

CAB = """# Índice de `evidence/` (#390)

Gerado a partir dos arquivos versionados: task (prefixo numérico do nome), data do
último commit e a primeira linha do arquivo. É o que torna verificável a frase
"já está provado em evidence" — sem isto, achar a prova exigia abrir arquivo por
arquivo, e 29 deles nem número de task no nome tinham (o histórico fica como está).

Regra para arquivo NOVO (cobrada por `tests/test_evidence_index.py`): prefixo `NNN-`
com o número da task e uma primeira linha de cabeçalho dizendo o que a prova mostra.
Depois de acrescentar, regenere: `./.venv-mlx/bin/python tests/gera_evidence_index.py`.

| arquivo | task | data | o que mostra |
|---|---|---|---|
"""


def _resumo(arq: Path) -> str:
    if arq.suffix in (".md", ".txt", ".sh", ".py", ".json"):
        try:
            for linha in arq.read_text(errors="replace").splitlines():
                limpa = linha.strip().lstrip("#/ *- ").strip()
                if len(limpa) > 3:
                    return limpa
        except OSError:
            pass
    return "(imagem)" if arq.suffix in (".png", ".jpg", ".jpeg") else "(sem descrição)"


def _data(arq: Path) -> str:
    r = subprocess.run(["git", "log", "-1", "--format=%as", "--", str(arq)],
                       cwd=RAIZ, capture_output=True, text=True)
    return r.stdout.strip() or "—"


def gera() -> int:
    linhas = []
    for arq in sorted(EV.iterdir()):
        if not arq.is_file() or arq.name == "INDEX.md":
            continue
        m = re.match(r"(\d+)-", arq.name)
        task = f"#{int(m.group(1))}" if m else "—"
        resumo = _resumo(arq).replace("|", "\\|")[:110]
        linhas.append(f"| `{arq.name}` | {task} | {_data(arq)} | {resumo} |")
    (EV / "INDEX.md").write_text(CAB + "\n".join(linhas) + "\n")
    return len(linhas)


if __name__ == "__main__":
    print(f"evidence/INDEX.md regerado com {gera()} entradas")
