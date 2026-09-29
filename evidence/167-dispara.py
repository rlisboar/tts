"""Sobe `evidence/167-fechamento2.sh` DESTACADO da sessão/grupo do shell.

O fechamento do #167 já morreu uma vez porque o runner mandou SIGTERM no GRUPO do
processo e levou o filho (`live_barge_rep.sh`) junto, deixando evidência truncada.
`start_new_session=True` faz o filho virar líder da própria sessão, então o sinal
do runner não o alcança. Log: `evidence/167-fechamento2.log`.
"""

import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
log = (RAIZ / "evidence" / "167-fechamento2.log").open("wb")
p = subprocess.Popen(
    [str(RAIZ / "evidence" / "167-fechamento2.sh")],
    cwd=RAIZ, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    start_new_session=True,
)
print(f"pid={p.pid} log=evidence/167-fechamento2.log")