#!/usr/bin/env bash
# Sonda do #592: pega a trava e depois SUJA o `pid` do lock com um PID alheio,
# como a concorrência faz. O rótulo do estouro tem de continuar dizendo
# "trabalho": antes ele era adivinhado por "sou eu o pid do lock?" e, com o pid
# trocado, a suíte que TRABALHAVA era acusada de estouro de FILA.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$AQUI/../../serial.sh"; serial_pega || exit 1
TRAVA="${TTS_SERIAL_LOCK:-/tmp/tts-rod-modelo.lock}"
echo 424242 > "$TRAVA/pid"        # pid alheio: a adivinhação antiga inverte aqui
echo "dono-da-trava-reescrito=$(cat "$TRAVA/pid")"
sleep "${SONDA_SONO:-6}"
echo "dormi (não devia chegar aqui)"
