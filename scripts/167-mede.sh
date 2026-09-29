#!/usr/bin/env bash
# #167 — medição dos DOIS sentidos do tradeoff da janela de playback.
# Serializado pela própria trava de modelo (tests/serial.sh).
#
# #216: as duas alavancas viraram DEFAULT. As células continuam válidas, mas o
# "baseline" só é baseline com `=0` explícito — sem isto ele mediria o novo default.
set -uo pipefail
cd "$(dirname "$0")/.."
E=evidence

# comportamento ANTERIOR ao #167 (as duas alavancas desligadas)
OFF="TTS_LIVE_BARGE_JANELA_TURNO=0 TTS_LIVE_PLAYBACK_DURACAO=0"

echo "### $(date -u +%FT%TZ) baseline REP=20 (duas alavancas OFF)"
env $OFF REP=20 ./tests/live_barge_rep.sh > "$E/167-rep20-baseline.txt" 2>&1
echo "exit=$?"

echo "### $(date -u +%FT%TZ) duracao REP=20"
TTS_LIVE_PLAYBACK_DURACAO=1 REP=20 ./tests/live_barge_rep.sh > "$E/167-rep20-duracao.txt" 2>&1
echo "exit=$?"

echo "### $(date -u +%FT%TZ) duracao + janela-de-turno REP=20"
TTS_LIVE_PLAYBACK_DURACAO=1 TTS_LIVE_BARGE_JANELA_TURNO=1 REP=20 ./tests/live_barge_rep.sh > "$E/167-rep20-duracao-turno.txt" 2>&1
echo "exit=$?"

echo "### $(date -u +%FT%TZ) MIC_FILE (sentido do eco) duracao REP=10"
MIC_FILE=1 TTS_LIVE_PLAYBACK_DURACAO=1 REP=10 ./tests/live_barge_rep.sh > "$E/167-micfile10-duracao.txt" 2>&1
echo "exit=$?"

echo "### $(date -u +%FT%TZ) MIC_FILE baseline REP=10"
env $OFF MIC_FILE=1 REP=10 ./tests/live_barge_rep.sh > "$E/167-micfile10-baseline.txt" 2>&1
echo "exit=$?"

echo "### $(date -u +%FT%TZ) live_ui.sh BARGE_ESTRITO=1 com a duracao ligada"
BARGE_ESTRITO=1 TTS_LIVE_PLAYBACK_DURACAO=1 ./tests/live_ui.sh > "$E/167-liveui-estrito-duracao.txt" 2>&1
echo "exit=$?"

echo "### FIM $(date -u +%FT%TZ)"