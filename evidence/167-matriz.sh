#!/usr/bin/env bash
# Matriz de medição do #167 — janela de barge nos VÃOS de geração.
#
# Roda o harness de rajada (REP=20) nas três configurações do motor, nos DOIS
# sentidos que o PM pediu (barge VERDADEIRO e barge FALSO por eco/`MIC_FILE=1`),
# e guarda cada rodada em evidence/167-<rotulo>.txt.
#
#   A duracao  — janela dimensionada pela DURAÇÃO REAL do chunk (direção a)
#   B turno    — janela mantida enquanto o TURNO está aberto (direção b)
#   E baseline — as duas flags OFF (controle, estado atual do default)
#
# Uso: ./evidence/167-matriz.sh   (sequencial: o serial.sh trava o modelo)
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

rodar() {   # rodar <rotulo> <env...>
    local rotulo="$1"; shift
    echo "=== $rotulo ($(date +%H:%M:%S)) ==="
    env REP=20 "$@" ./tests/live_barge_rep.sh > "evidence/167-$rotulo.txt" 2>&1
    echo "  → evidence/167-$rotulo.txt · $(grep -c '✔ barge' "evidence/167-$rotulo.txt" || true)/20 barge"
}

rodar duracao-true        TTS_LIVE_PLAYBACK_DURACAO=1
rodar turno-true          TTS_LIVE_BARGE_JANELA_TURNO=1
rodar duracao-eco         TTS_LIVE_PLAYBACK_DURACAO=1 MIC_FILE=1
rodar turno-eco           TTS_LIVE_BARGE_JANELA_TURNO=1 MIC_FILE=1
rodar baseline-eco        MIC_FILE=1

echo "=== resumo ==="
for r in duracao-true turno-true duracao-eco turno-eco baseline-eco; do
    f="evidence/167-$r.txt"
    [ -f "$f" ] || continue
    printf '%-14s %2s/20 barge · %2s barge_falso\n' "$r" \
        "$(grep -c '✔ barge' "$f" || true)" "$(grep -c "'barge_falso': True" "$f" || true)"
done