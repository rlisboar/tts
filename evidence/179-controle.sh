#!/usr/bin/env bash

# sufixo por execução: os logs e o resumo não podem ser de outra rodada
RUN="$$"
# CONTROLE do #179 — prova os DOIS lados do modo do barge do tests/live_ui.sh.
#
# O ticket inverteu o default (estrito) e deixou `BARGE_ESTRITO=0` como escape.
# Um "verde" sozinho não prova nada: em modo tolerante uma rodada COM barge passa
# pelo ramo normal e a tolerância nunca é exercitada. Por isso o par usa
# `BARGE_SIMULA_SEM=1` (faz a rodada se comportar como a que não teve barge, sem
# depender da janela de playback):
#
#   esperado EXIT=0  · normal                      (estrito, e o barge acontece)
#   esperado EXIT=0  · BARGE_ESTRITO=0             (escape: verde)
#   esperado EXIT=0  · BARGE_ESTRITO=0 + SIMULA_SEM (escape TOLERA: verde COM aviso)
#   esperado EXIT=1  · BARGE_ESTRITO=1 + SIMULA_SEM (estrito REPROVA a MESMA rodada)
#   esperado EXIT=1  · BARGE_ESTRITO=   + SIMULA_SEM (vazio não relaxa)
#
# Uso: ./evidence/179-controle.sh   (logs em /tmp/179-*-$$.log)
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
resumo="/tmp/179-controle-$RUN.txt"
: > "$resumo"

# 5 rodadas em fila, e o time inteiro usa a mesma trava de modelo: a espera padrão
# (1800 s) morreria no meio e o EXIT=1 da ESPERA passaria por vermelho do teste.
export TTS_SERIAL_ESPERA="${TTS_SERIAL_ESPERA:-7200}"

roda() {
  local rot="$1"; shift
  local out="/tmp/179-${rot}-$RUN.log"
  env "$@" ./tests/live_ui.sh > "$out" 2>&1
  local rc=$?
  local avisos; avisos=$(grep -c "tolerado por BARGE_ESTRITO=0" "$out" || true)
  printf '%-28s EXIT=%s  avisos=%s  log=%s\n' "$rot" "$rc" "$avisos" "$out" | tee -a "$resumo"
  grep -q "trava de modelo não liberou" "$out" \
    && echo "    (ESPERA pela trava esgotou — o EXIT não é do teste)" | tee -a "$resumo"
  grep -m1 "corte real da rodada" "$out" | sed 's/^/    /' | tee -a "$resumo"
  grep -m1 "corte no interrupted" "$out" | sed 's/^/    /' | tee -a "$resumo"
}

roda normal
roda escape0 BARGE_ESTRITO=0
roda escape0_simula BARGE_ESTRITO=0 BARGE_SIMULA_SEM=1
roda estrito1_simula BARGE_ESTRITO=1 BARGE_SIMULA_SEM=1
roda vazio_simula BARGE_ESTRITO= BARGE_SIMULA_SEM=1

echo "--- esperado: 0 / 0 / 0 / 1 / 1 ---" | tee -a "$resumo"