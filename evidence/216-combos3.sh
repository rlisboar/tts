#!/usr/bin/env bash
# #216 — 3ª rodada: a comparação que DECIDE (baseline x ABC), repetida, nos dois
# sentidos. O harness satura perto do teto (20/20 no sentido verdadeiro) e as
# diferenças medidas (18 vs 20, 16 vs 20) estão dentro da cauda de UMA rodada de
# 20 — sem repetição, "regrediu" e "não regrediu" seriam indistinguíveis.
#
# Uso: ./evidence/216-combos3.sh   (sequencial — o serial.sh trava o modelo)
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

A="TTS_LIVE_PLAYBACK_DURACAO=1"
B="TTS_LIVE_BARGE_JANELA_TURNO=1"
C="TTS_LIVE_ECO_SO_TOCANDO=1"

rodar() {   # rodar <rotulo> <env...>
    local rotulo="$1"; shift
    echo "=== $rotulo ($(date +%H:%M:%S)) ==="
    env REP=20 "$@" ./tests/live_barge_rep.sh > "evidence/216-$rotulo.txt" 2>&1
    local ok
    ok="$(grep -c '✔ barge' "evidence/216-$rotulo.txt" || true)"
    echo "  → evidence/216-$rotulo.txt · $ok/20 barge"
    printf '%-14s %2s/20 barge · %s\n' "$rotulo" "$ok" \
        "$(grep -h '=== MOTOR' "evidence/216-$rotulo.txt" | tail -1)" >> evidence/216-resumo.txt
}

rodar eco-baseline2 MIC_FILE=1
rodar eco-abc2      $A $B $C MIC_FILE=1
rodar true-baseline3
rodar true-abc3     $A $B $C

echo "=== resumo ==="
tail -10 evidence/216-resumo.txt