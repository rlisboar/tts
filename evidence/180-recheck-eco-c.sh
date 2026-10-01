#!/usr/bin/env bash
# Hipótese: a sessão do #225 media o eco com a alavanca C ligada por ENV? Mede o
# mesmo cell com TTS_LIVE_ECO_SO_TOCANDO=1 (REP=10) para comparar com 3/10, 6/10, 7/20.
set -uo pipefail
cd "$(dirname "$0")/.."
E=evidence
_T=/tmp/tts-rod-modelo.lock
while ! mkdir "$_T" 2>/dev/null; do sleep 0.05; done
echo $$ > "$_T/pid"; echo "eco-c" > "$_T/qual"
trap 'rm -rf "$_T"' EXIT
export TTS_SERIAL=0
MIC_FILE=1 TTS_LIVE_ECO_SO_TOCANDO=1 REP=10 ./tests/live_barge_rep.sh > "$E/180-recheck-eco-c.txt" 2>&1
echo "exit=$?" >> "$E/180-recheck-eco-c.txt"
grep -E "^(=== RESUMO|=== CONTAGEM|=== MOTOR)" "$E/180-recheck-eco-c.txt"
