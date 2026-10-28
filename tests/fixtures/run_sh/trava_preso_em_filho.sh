#!/usr/bin/env bash
# Sonda do #592: o CASO MEDIDO (suíte que fica 828 s com teto de 90 sem uma linha
# de log). O trabalho é UM filho em foreground que IGNORA SIGTERM — com o bash
# adiando o trap enquanto o filho vive, o ALRM sozinho nunca vira mensagem. O teto
# tem de (a) imprimir o motivo pelo VIGIA e (b) escalar até KILL para a suíte
# morrer, em vez de pendurar o gate.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$AQUI/../../serial.sh"; serial_pega || exit 1
PY="$(command -v python3)"
echo "trabalhando num filho surdo a TERM"
"$PY" -c '
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(600)
'
