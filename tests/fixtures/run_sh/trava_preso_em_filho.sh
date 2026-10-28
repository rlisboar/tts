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
# o PID sai em `SONDA_SAIDA`: o TESTE cobra que o processo morreu de verdade (só a
# mensagem não provaria nada — o teto externo do runner também reprovaria)
"$PY" -c '
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if len(sys.argv) > 1 and sys.argv[1]:
    open(sys.argv[1], "w").write(str(os.getpid()))
time.sleep(600)
' "${SONDA_SAIDA:-}"
