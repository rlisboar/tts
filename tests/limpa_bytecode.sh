#!/usr/bin/env bash
# Invalida o BYTECODE dos módulos do repo — obrigatório antes de MEDIR uma mordida.
#
# POR QUE (task #238, achado do veredito do #230): a mordida típica é
# `cp app.py /tmp/bak` → editar → rodar o teste → `cp /tmp/bak app.py`. O `cp`
# preserva TAMANHO e o mtime tem granularidade de 1 s; se o teste roda na MESMA
# segunda, o `.pyc` continua "válido" (o CPython valida por mtime(segundos)+tamanho
# da fonte) e o interpretador roda o BYTECODE VELHO — a mordida não morde e o gate
# dá verde em cima de um fix que não funciona.
#
# USO:   tests/limpa_bytecode.sh [modulo.py ...]     (sem args: os módulos do build)
#        ou, dentro de um harness: PYTHONDONTWRITEBYTECODE=1 antes de invocar python
#        (mata o problema na origem — preferir quando o harness é Python).
#
# Todo script de mordida chama isto ANTES de rodar o teste que tem de cair.
set -uo pipefail
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MODULOS=("$@")
if [ "${#MODULOS[@]}" -eq 0 ]; then
  MODULOS=(app.py common.py tts_worker.py backends.py live_pipeline.py live_turns.py dsh_client.py)
fi

removidos=0
for m in "${MODULOS[@]}"; do
  base="${m%.py}"
  for alvo in "$RAIZ/__pycache__/$base".cpython-*.pyc "$RAIZ/tests/__pycache__/$base".cpython-*.pyc; do
    [ -e "$alvo" ] || continue
    rm -f "$alvo" && removidos=$((removidos + 1))
  done
done
echo "  [bytecode] $removidos .pyc removido(s) — a próxima medição compila da fonte"
