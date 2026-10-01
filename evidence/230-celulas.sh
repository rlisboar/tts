#!/usr/bin/env bash
# Gate #230 — células comportamentais POR FORA do autor, sempre na configuração
# que SHIPPA (nenhuma env de flag): o default do #216 (A+B ligadas, C fora).
#
#   1. MODO=vao  — cenário do defeito do #167: barge tem de disparar no DEFAULT
#   2. MIC_FILE=1 — sentido do ECO no DEFAULT (corte falso por loop do wav)
#   3. MIC_FILE=1 com as DUAS alavancas em 0 — baseline da mesma sessão de máquina
#   4. tests/live_ui.sh — corte do playback é exigência DURA (#179) no DEFAULT
#
# Uso: setsid ./evidence/230-celulas.sh > evidence/230-celulas.log 2>&1 &
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
RAIZ="$PWD"

limpa_flags() {
  unset TTS_LIVE_BARGE_JANELA_TURNO TTS_LIVE_PLAYBACK_DURACAO TTS_LIVE_ECO_SO_TOCANDO
}

echo "### [1/4] MODO=vao REP=20 no DEFAULT $(date -u +%H:%M:%SZ)"
limpa_flags
REP=20 MODO=vao ./tests/live_barge_rep.sh > evidence/230-vao-default.txt 2>&1
echo "  exit=$? · $(grep -cE '✔ barge' evidence/230-vao-default.txt) barges de 20"

echo "### [2/4] MIC_FILE=1 REP=20 no DEFAULT $(date -u +%H:%M:%SZ)"
limpa_flags
MIC_FILE=1 REP=20 ./tests/live_barge_rep.sh > evidence/230-eco-default.txt 2>&1
echo "  exit=$? · cortes=$(grep -o '"interrupted": [0-9]*' evidence/230-eco-default.txt | tail -1)"

echo "### [3/4] MIC_FILE=1 REP=20 BASELINE (A=B=0) $(date -u +%H:%M:%SZ)"
limpa_flags
TTS_LIVE_BARGE_JANELA_TURNO=0 TTS_LIVE_PLAYBACK_DURACAO=0 \
  MIC_FILE=1 REP=20 ./tests/live_barge_rep.sh > evidence/230-eco-baseline.txt 2>&1
echo "  exit=$? · cortes=$(grep -o '"interrupted": [0-9]*' evidence/230-eco-baseline.txt | tail -1)"

echo "### [4/4] tests/live_ui.sh no DEFAULT (barge ESTRITO) $(date -u +%H:%M:%SZ)"
limpa_flags
./tests/live_ui.sh > evidence/230-live_ui.txt 2>&1
echo "  exit=$? · $(grep -E 'EXIT|✔|✖' evidence/230-live_ui.txt | tail -2)"

echo "### FIM $(date -u +%H:%M:%SZ)"
