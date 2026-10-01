#!/usr/bin/env bash

# sufixo por execução: os DOIS scripts do #225 usavam os MESMOS nomes em /tmp
RUN="$$"
# #225 — célula EXTRA (só se as células do 225-celulas.sh não pegarem o defeito).
#
# Mesmo protocolo ANTES/DEPOIS trocando só o cliente, mas com VAO_MS maior: o alvo
# é o instante em que a janela de playback do servidor JÁ FECHOU e o cliente ainda
# tem fila (é aí que o onset vira `speech_start` sem barge e nada cortava).
#
# Uso: VAO_MS=2500 nohup ./evidence/225-extra.sh > evidence/225-extra.log 2>&1 &
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
VAO_MS="${VAO_MS:-2500}"
celula() {
  local cli="$1" saida="$2"; shift 2
  cp "$cli" static/index.html
  echo "### $saida [$(basename "$cli")] $(date -u +%H:%M:%SZ)"
  env "$@" ./tests/live_barge_rep.sh > "evidence/$saida" 2>&1
  echo "  exit=$? · $(grep -oE 'CORTE \(#225\): .*' "evidence/$saida" | tail -1)"
}
trap 'cp /tmp/225-index-com-fix-$RUN.html static/index.html' EXIT
for fase in antes depois; do
  if [ "$fase" = antes ]; then CLI=/tmp/225-index-sem-fix-$RUN.html; else CLI=/tmp/225-index-com-fix-$RUN.html; fi
  celula "$CLI" "225-$fase-vao-${VAO_MS}ms.txt" REP=20 MODO=vao VAO_MS="$VAO_MS"
done
cp /tmp/225-index-com-fix-$RUN.html static/index.html
echo "=== FIM $(date -u +%H:%M:%SZ) — cliente COM o fix restaurado ==="
