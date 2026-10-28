#!/usr/bin/env bash
# Sonda do #535: escreve no `settings.json` REAL do dono. O runner tem de NOMEAR
# esta suíte na lista de escritas.
set -uo pipefail
printf '\n' >> settings.json
echo "sujei o settings do dono"
