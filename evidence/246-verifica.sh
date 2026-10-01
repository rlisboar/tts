#!/usr/bin/env bash

# sufixo por execução: o diretório de cenários hostis era compartilhado
RUN="$$"
# #246 — verificação do achado: o `chat_dsh_effort` do ARQUIVO do dono decidia o
# veredito da suíte (o filho hostil do #206 acusava com o campo em low).
#
#   1) hostil low  — o caso do achado: esperado VERDE
#   2) hostil high — o teste SEGUE o campo (não é só o low): esperado VERDE
#   3) MORDIDA: cliente do Live com `effort` HARDCODED em off + dono em low
#      -> esperado 1 failed (a asserção nova ainda pega código que crava valor)
#   4) restaura o app.py e confere verde de novo
#
# `rm -f __pycache__/app.cpython-*.pyc` entre as fases: `cp`/replace preservam
# tamanho e mtime(1s) e o .pyc continuaria válido rodando BYTECODE VELHO (#238).
#
# Uso: ./evidence/246-verifica.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY=./.venv-mlx/bin/python
mkdir -p "/tmp/246-$RUN"
"$PY" - <<'EOF'
import json, pathlib
d = json.loads(pathlib.Path("settings.json").read_text())
for v in ("low", "high"):
    h = dict(d); h["chat_dsh_effort"] = v
    pathlib.Path(f"/tmp/246-$RUN/hostil-{v}.json").write_text(json.dumps(h))
EOF

roda() { QA206_SETTINGS="/tmp/246-$RUN/hostil-$1.json" PYTHONPATH=tests "$PY" \
         -m pytest tests/test_live_dsh.py -q -p _estado_hostil 2>&1 | tail -3; }

echo "### 1) hostil low (o caso do achado) — esperado: 25 passed"
roda low
echo
echo "### 2) hostil high — esperado: 25 passed"
roda high
echo
echo "### 3) MORDIDA: effort HARDCODED em off no _live_pipe_novo + dono em low — esperado: 1 failed"
"$PY" - <<'EOF'
import pathlib
p = pathlib.Path("app.py"); s = p.read_text()
a = '                effort=cfg["effort"], cwd=BASE / "outputs" / ".dsh-cwd",'
assert s.count(a) == 1, s.count(a)
p.write_text(s.replace(a, '                effort="off", cwd=BASE / "outputs" / ".dsh-cwd",'))
EOF
rm -f __pycache__/app.cpython-*.pyc
roda low
echo
echo "### 4) app.py restaurado + bytecode limpo — esperado: 25 passed"
"$PY" - <<'EOF'
import pathlib
p = pathlib.Path("app.py"); s = p.read_text()
a = '                effort="off", cwd=BASE / "outputs" / ".dsh-cwd",'
assert s.count(a) == 1, s.count(a)
p.write_text(s.replace(a, '                effort=cfg["effort"], cwd=BASE / "outputs" / ".dsh-cwd",'))
EOF
rm -f __pycache__/app.cpython-*.pyc
roda low
