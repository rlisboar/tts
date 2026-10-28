#!/usr/bin/env bash
# Sonda do #592: suíte VERDE que só usa a trava, sem teto armado (o caso de rodar
# `./tests/voz_check_ui.sh` à mão). Tem de sair 0 — o trap de EXIT já devolveu 1
# por causa do `[ -n "" ] && …` do `_teto_matar` e o runner marcava FALHA.
# `set -e` é essencial aqui: com ele o bash ADOTA o status do trap (sem `-e` o
# status original é preservado e o defeito não aparece) — as suítes usam `-e`.
set -euo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unset TTS_TETO_POS_TRAVA
source "$AQUI/../../serial.sh"; serial_pega || exit 1
echo "trabalhei e passei"
