#!/usr/bin/env bash
# #180 — RECHEQUE do gate com o DEFAULT do produto (SEM env de engine) e md5
# ANTES/DEPOIS. As suítes internas pegam a trava de modelo (tests/serial.sh),
# então isto espera a árvore viva sossegar em vez de atropelar quem está medindo.
#
# Células (o que cada uma tem de dar):
#   A estrito verde          → BARGE_ESTRITO=1, default: OK (corte <50 ms é duro)
#   B controle estrito        → A + BARGE_SIMULA_SEM=1: REPROVA (o estrito morde)
#   C escape                  → B com BARGE_ESTRITO=0: passa com WARN (escape vivo)
#   D vão                     → MODO=vao REP=10: barge no vão com o default
#   E eco                     → MIC_FILE=1 REP=10: sentido do eco sem regressão
#
# Este script SEGURA a trava de modelo entre as células (TTS_SERIAL=0 dentro):
# sem isto cada suíte solta a trava no fim e a próxima rodada de terceiros
# (que re-tenta a cada 0,5 s) ganha a corrida, e as cinco células nunca saem.
set -uo pipefail
cd "$(dirname "$0")/.."
E=evidence
# Trava de modelo: espera AGULHADA (0,05 s). O dono atual solta e re-pega entre
# células em ms; quem espera a 0,5 s (serial.sh) perde a corrida e nunca entra.
_T=/tmp/tts-rod-modelo.lock; t0=$SECONDS
while ! mkdir "$_T" 2>/dev/null; do
  p="$(cat "$_T/pid" 2>/dev/null || true)"
  if [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null; then rm -rf "$_T"; continue; fi
  if [ $((SECONDS - t0)) -gt 1800 ]; then echo "trava não liberou em 1800 s"; exit 1; fi
  sleep 0.05
done
echo $$ > "$_T/pid"; echo "180-recheck.sh" > "$_T/qual"
trap 'rm -rf "$_T"' EXIT
export TTS_SERIAL=0
ARQS="app.py live_turns.py live_pipeline.py tests/live_ui.sh tests/live_barge_rep.sh static/index.html"
_md5() { md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }

# tira qualquer env de engine do MEU shell: a célula tem de medir o DEFAULT
limpa() { env -u TTS_LIVE_BARGE_JANELA_TURNO -u TTS_LIVE_PLAYBACK_DURACAO \
              -u TTS_LIVE_ECO_SO_TOCANDO "$@"; }

{
  echo "# #180 recheque — $(date -u +%FT%TZ) · HEAD=$(git rev-parse --short HEAD)"
  echo "## md5 ANTES"
  for a in $ARQS; do echo "$(_md5 "$a")  $a"; done
} > "$E/180-recheck-md5.txt"

celula() {  # nome arquivo comando...
  local nome="$1" arq="$2"; shift 2
  echo "=== $nome"
  limpa "$@" > "$arq" 2>&1
  echo "$nome exit=$?"
}

celula A "$E/180-recheck-estrito.txt"        BARGE_ESTRITO=1 ./tests/live_ui.sh
celula B "$E/180-recheck-controle-estrito.txt" BARGE_ESTRITO=1 BARGE_SIMULA_SEM=1 ./tests/live_ui.sh
celula C "$E/180-recheck-controle-tolerante.txt" BARGE_ESTRITO=0 BARGE_SIMULA_SEM=1 ./tests/live_ui.sh
celula D "$E/180-recheck-vao.txt"            MODO=vao REP=10 ./tests/live_barge_rep.sh
celula E "$E/180-recheck-eco.txt"            MIC_FILE=1 REP=10 ./tests/live_barge_rep.sh

echo "## md5 DEPOIS"
for a in $ARQS; do echo "$(_md5 "$a")  $a"; done

echo "=== RESUMOS"
for f in "$E"/180-recheck-estrito.txt "$E"/180-recheck-controle-estrito.txt \
         "$E"/180-recheck-controle-tolerante.txt "$E"/180-recheck-vao.txt "$E"/180-recheck-eco.txt; do
  echo "-- $f"
  grep -E "^(✔ OK|✖ FALHOU|=== RESUMO|=== CORTE|=== MOTOR)|⚠ sem barge|\[controle\]" "$f" | head -8
done
echo "=== FIM $(date -u +%FT%TZ)"
