#!/usr/bin/env bash
# #216 — 4ª rodada: os PARES do sentido do eco que faltaram. O script da matriz
# principal (`evidence/216-combos.sh`) morreu no fim da rodada `eco-ab` (a shell que
# o criou foi derrubada), então `eco-ac`, `eco-bc` e `eco-abc` ficaram sem rodar.
# `eco-abc` vem PRIMEIRO: é a configuração que decide o default.
#
# Uso: ./evidence/216-combos4.sh
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

rodar eco-abc     $A $B $C MIC_FILE=1
rodar eco-ac      $A $C MIC_FILE=1
rodar eco-bc      $B $C MIC_FILE=1
rodar eco-abc3    $A $B $C MIC_FILE=1

echo "=== resumo ==="
tail -6 evidence/216-resumo.txt