#!/usr/bin/env bash
# Sonda do #635: anexa no histórico de métricas do "dono" — que é o que o app DELE
# também faz pelo polling da tela. Tem de virar NOTA, nunca FALHA do knob.
set -uo pipefail
# SEM fallback (#710): ausente tem de parar, não escrever na raiz do repo.
if [ -z "${TTS_DONO_DIR:-}" ]; then
    echo "✖ ${0##*/}: TTS_DONO_DIR não veio (sem fallback — veja tests/run_sh.sh)" >&2
    exit 2
fi
DONO="$TTS_DONO_DIR"
mkdir -p "$DONO/state/metrics"
printf '{"rota":"/api/status","bk":"sonda"}\n' >> "$DONO/state/metrics/$(date +%F).jsonl"
echo "anexei uma linha nas métricas do dono"
