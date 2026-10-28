#!/usr/bin/env bash
# Sonda do #635: anexa no histórico de métricas do "dono" — que é o que o app DELE
# também faz pelo polling da tela. Tem de virar NOTA, nunca FALHA do knob.
set -uo pipefail
DONO="${TTS_DONO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
mkdir -p "$DONO/state/metrics"
printf '{"rota":"/api/status","bk":"sonda"}\n' >> "$DONO/state/metrics/$(date +%F).jsonl"
echo "anexei uma linha nas métricas do dono"
