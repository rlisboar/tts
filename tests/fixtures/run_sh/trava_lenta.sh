#!/usr/bin/env bash
# Sonda do #535: pede a trava de modelo e dorme ALÉM do teto de trabalho — tem de
# reprovar pelo alarme pós-trava, não passar.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$AQUI/../../serial.sh"; serial_pega || exit 1
echo "peguei a trava"
sleep "${SONDA_SONO:-6}"
echo "dormi (não devia chegar aqui)"
