#!/usr/bin/env bash
# #216 — a célula que mede a ALAVANCA C pelo lado em que ela existe: o LIMIAR.
#
# Por que existe: no vão com nada tocando, `eco_so_tocando` (C) devolve o regime de
# OCIOSO (limiar ~ -53 dBFS) em vez do regime do eco (limiar ~ -24 dBFS). Com a
# injeção do harness em escala CHEIA (pico 0,95) o estímulo passa dos dois limiares
# e C não aparece na medida — só o efeito colateral dela no tamanho da janela
# aparece (é o que fez `vao-abc` perder para `vao-ab`).
#
# Aqui a injeção vai em AMP=0.2 (~-30 dBFS RMS, medido e impresso pelo harness):
#   • ABAIXO do limiar de eco  -> sem C o onset NÃO deve virar barge;
#   • ACIMA do limiar de ocioso -> com C ele deve virar.
# Se as duas rodadas confirmarem isso, C tem um efeito PRÓPRIO medido (e não só um
# custo) e a decisão do default tem os dois lados na mesa.
#
# Uso: TTS_SERIAL_ESPERA=7200 ./evidence/216-quieto.sh
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

A="TTS_LIVE_PLAYBACK_DURACAO=1"
B="TTS_LIVE_BARGE_JANELA_TURNO=1"
C="TTS_LIVE_ECO_SO_TOCANDO=1"

rodar() {   # rodar <rotulo> <env...>
    local rotulo="$1"; shift
    echo "=== $rotulo ($(date +%H:%M:%S)) ==="
    env REP=20 AMP=0.2 MODO=vao "$@" ./tests/live_barge_rep.sh > "evidence/216-$rotulo.txt" 2>&1
    local ok
    ok="$(grep -c '✔ barge' "evidence/216-$rotulo.txt" || true)"
    echo "  → evidence/216-$rotulo.txt · $ok/20 barge"
    printf '%-14s %2s/20 barge · %s\n' "$rotulo" "$ok" \
        "$(grep -h '=== MOTOR' "evidence/216-$rotulo.txt" | tail -1)" >> evidence/216-resumo.txt
}

rodar vao-quieto-ab   $A $B
rodar vao-quieto-abc  $A $B $C

echo "=== resumo ==="
tail -4 evidence/216-resumo.txt