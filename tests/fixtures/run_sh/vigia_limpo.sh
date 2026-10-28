#!/usr/bin/env bash
# Sonda do #592: arma o vigia do teto, TROCA o trap de EXIT (como o admin_ui_flow
# faz) e chama `_teto_matar` no trap novo. Devolve o PID do vigia em
# `SONDA_SAIDA`; o TESTE confere que ele morreu depois da suíte — vigia vivo
# manda ALRM num PID que pode ter sido reusado por outra suíte.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$AQUI/../../serial.sh"; serial_pega || exit 1
trap 'serial_solta; _teto_matar' EXIT
[ -n "${_TETO_PID:-}" ] || { echo "vigia=não-armado"; exit 1; }
printf 'vigia_pid=%s\n' "$_TETO_PID" > "${SONDA_SAIDA:?sonda precisa de SONDA_SAIDA}"
