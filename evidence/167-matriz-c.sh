#!/usr/bin/env bash
# #167 — medição DEPOIS da direção (c): janela = TURNO; referência de energia = áudio audível.
#
# Mesmo protocolo da matriz "antes" (REP=20, os dois sentidos), agora com as TRÊS
# alavancas ligadas (janela por duração do chunk + janela pelo turno + eco só
# quando há áudio tocando), e o `live_ui.sh` estrito com o fix.
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

LIGA="TTS_LIVE_PLAYBACK_DURACAO=1 TTS_LIVE_BARGE_JANELA_TURNO=1 TTS_LIVE_ECO_SO_TOCANDO=1"

echo "=== c-true (REP=20) ==="
env REP=20 $LIGA ./tests/live_barge_rep.sh > evidence/167-c-true.txt 2>&1
echo "  → $(grep -c '✔ barge' evidence/167-c-true.txt)/20 barge"

echo "=== c-eco (REP=20, MIC_FILE=1) ==="
env REP=20 $LIGA MIC_FILE=1 ./tests/live_barge_rep.sh > evidence/167-c-eco.txt 2>&1
echo "  → $(grep -c '✔ barge' evidence/167-c-eco.txt)/20 barge"

echo "=== live_ui.sh BARGE_ESTRITO=1 com o fix ==="
env $LIGA BARGE_ESTRITO=1 ./tests/live_ui.sh > evidence/167-c-live_ui.txt 2>&1
echo "  → exit $? · $(grep -c 'SEM barge' evidence/167-c-live_ui.txt) sem-barge · $(grep -c 'corte no interrupted' evidence/167-c-live_ui.txt) medição de corte"

echo "=== resumo ==="
for r in c-true c-eco; do
    printf '%-8s %2s/20 barge · %2s barge_falso\n' "$r" \
        "$(grep -c '✔ barge' "evidence/167-$r.txt" || true)" \
        "$(grep -c "'barge_falso': True" "evidence/167-$r.txt" || true)"
done
grep -h "corte no interrupted\|✖ FALHOU\|✔ OK\|SEM barge" evidence/167-c-live_ui.txt | head -8