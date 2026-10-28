#!/usr/bin/env bash
# Sonda do #535: dorme 0,5 s DEPOIS da trava. Se o tempo na fila comesse o teto,
# esta suíte morreria esperando o lock.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$AQUI/../../serial.sh"; serial_pega || exit 1
echo "peguei a trava"
sleep 0.5
echo "trabalhei"
