#!/usr/bin/env bash
# #216 — COMPLEMENTO da matriz: cada alavanca SOZINHA (pós-#167) e repetição dos
# dois extremos (baseline e ABC) para dimensionar a VARIÂNCIA do harness.
#
# Por que: `evidence/216-combos.sh` (a matriz principal) mediu baseline, C, AB, AC,
# BC e ABC nos dois sentidos. Aqui entram:
#   • A sozinha e B sozinha — as medições registradas no código para elas são de
#     ANTES do #167 (o motor mudou: calibração a cada início de áudio, braço do
#     barge durante a calibração), então a matriz só fica coerente re-medindo-as;
#   • 2ª rodada de baseline e de ABC no sentido VERDADEIRO — o harness satura perto
#     do teto (20/20) e a decisão do default depende de saber se 18/20 contra 20/20
#     é regressão ou cauda. Sem repetição isso vira palpite.
#
# Uso: ./evidence/216-combos2.sh   (sequencial — o serial.sh trava o modelo)
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

rodar true-a   $A
rodar true-b   $B
rodar eco-a    $A MIC_FILE=1
rodar eco-b    $B MIC_FILE=1
rodar true-baseline2
rodar true-abc2  $A $B $C

echo "=== resumo ==="
tail -8 evidence/216-resumo.txt