#!/usr/bin/env bash
# #180 — só a célula do ECO (faltou na rodada anterior), com o DEFAULT e md5.
# Mesma trava agulhada do 180-recheck.sh; escreve o resumo no arquivo próprio.
set -uo pipefail
cd "$(dirname "$0")/.."
E=evidence
ARQS="app.py live_turns.py live_pipeline.py tests/live_ui.sh tests/live_barge_rep.sh static/index.html"
_md5() { md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }
_T=/tmp/tts-rod-modelo.lock; t0=$SECONDS
while ! mkdir "$_T" 2>/dev/null; do
  p="$(cat "$_T/pid" 2>/dev/null || true)"
  if [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null; then rm -rf "$_T"; continue; fi
  if [ $((SECONDS - t0)) -gt 1800 ]; then echo "trava não liberou"; exit 1; fi
  sleep 0.05
done
echo $$ > "$_T/pid"; echo "180-recheck-eco.sh" > "$_T/qual"
trap 'rm -rf "$_T"' EXIT
export TTS_SERIAL=0

{ echo "# #180 eco — $(date -u +%FT%TZ) · HEAD=$(git rev-parse --short HEAD)"; echo "## md5 ANTES"
  for a in $ARQS; do echo "$(_md5 "$a")  $a"; done; } > "$E/180-recheck-eco2-md5.txt"

env -u TTS_LIVE_BARGE_JANELA_TURNO -u TTS_LIVE_PLAYBACK_DURACAO -u TTS_LIVE_ECO_SO_TOCANDO \
  MIC_FILE=1 REP=10 ./tests/live_barge_rep.sh > "$E/180-recheck-eco2.txt" 2>&1
echo "exit=$?" >> "$E/180-recheck-eco2.txt"
{ echo "## md5 DEPOIS"; for a in $ARQS; do echo "$(_md5 "$a")  $a"; done; } >> "$E/180-recheck-eco2-md5.txt"
grep -E "^(=== RESUMO|=== CORTE|=== MOTOR|=== CONTAGEM)" "$E/180-recheck-eco2.txt"
