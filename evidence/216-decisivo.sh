#!/usr/bin/env bash
# #216 — RODADA DECISIVA: só as células que faltam para decidir o default.
#
# Por que existe: a matriz principal (`216-combos.sh`) e os complementos
# (`216-combos2/3/4.sh`) rodaram em 4 shells concorrentes; a trava de modelo
# (`tests/serial.sh`, TTS_SERIAL_ESPERA=1800) fez as rodadas que esperavam mais de
# 30 min morrerem com o arquivo vazio (`eco-baseline2`, `eco-abc`, `eco-abc2`).
# Aqui vai UMA shell, com espera longa, e só o que decide:
#
#   1. eco-abcA / eco-abcB — a CÉLULA QUE DECIDE: as três alavancas no sentido do
#      eco (mic falso tocando o wav em loop), repetida para separar sinal de cauda;
#   2. eco-baselineB — controle da MESMA sessão (o eco-baseline de 16:11 é de antes
#      do conserto da telemetria);
#   3. eco-ac / eco-bc — os pares que faltavam, para ATRIBUIR o efeito a C (sem
#      eles, "ABC melhor/pior que AB" não diz qual alavanca respondeu);
#   4. vao-abcB / vao-abB — repetição da comparação que separa AB de ABC no
#      cenário do defeito (o vao-abc de 16:44 deu 15/20 contra 20/20 do vao-ab:
#      repetir diz se são 5 falhas reais ou cauda de uma rodada).
#
# Uso: TTS_SERIAL_ESPERA=7200 ./evidence/216-decisivo.sh
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

rodar eco-abcA     $A $B $C MIC_FILE=1
rodar eco-abcB     $A $B $C MIC_FILE=1
rodar eco-baselineB          MIC_FILE=1
rodar eco-ac       $A $C     MIC_FILE=1
rodar eco-bc       $B $C     MIC_FILE=1
rodar vao-abcB     $A $B $C
rodar vao-abB      $A $B

echo "=== resumo ==="
tail -8 evidence/216-resumo.txt