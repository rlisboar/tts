#!/usr/bin/env bash
# #216 — o cenário que DECIDE se o fix entrega algo: o VÃO de geração.
#
# O modo padrão do harness injeta com o playback ATIVO e satura perto de 20/20 em
# TODAS as configurações — ele não distingue o produto antigo do corrigido. O
# `MODO=vao` injeta depois do `turn_complete`, quando a janela dimensionada pela
# fila (900 ms após o último envio) já expirou: é ali que o onset do humano vira
# "turno novo" em vez de interrupção no produto antigo.
#
# Comparação: baseline (nenhuma alavanca) x ABC (as três) — e AB como controle,
# já que a alavanca da janela por turno (B) é a que responde pelo vão.
#
# Uso: ./evidence/216-vao.sh
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

A="TTS_LIVE_PLAYBACK_DURACAO=1"
B="TTS_LIVE_BARGE_JANELA_TURNO=1"
C="TTS_LIVE_ECO_SO_TOCANDO=1"

rodar() {   # rodar <rotulo> <env...>
    local rotulo="$1"; shift
    echo "=== vao-$rotulo ($(date +%H:%M:%S)) ==="
    env MODO=vao REP=20 "$@" ./tests/live_barge_rep.sh > "evidence/216-vao-$rotulo.txt" 2>&1
    local ok
    ok="$(grep -c '✔ barge' "evidence/216-vao-$rotulo.txt" || true)"
    echo "  → evidence/216-vao-$rotulo.txt · $ok/20 barge"
    printf '%-14s %2s/20 barge · %s\n' "vao-$rotulo" "$ok" \
        "$(grep -h '=== MOTOR' "evidence/216-vao-$rotulo.txt" | tail -1)" >> evidence/216-resumo.txt
}

rodar baseline
rodar ab   $A $B
rodar abc  $A $B $C

echo "=== resumo ==="
tail -5 evidence/216-resumo.txt