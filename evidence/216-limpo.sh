#!/usr/bin/env bash
# #216 — repetição LIMPA das células de controle (a suíte `pytest tests/` rodou por
# cima das rodadas de 16:58-17:04 e o harness é sensível a carga).
#
#   eco-baselineC — o controle do sentido do eco, agora sem pytest no meio: o
#                   baseline de 16:11 deu 34 cortes e o de 16:59 (sob carga) deu
#                   20. Sem um terceiro ponto, "AB melhora o eco" não se sustenta.
#   eco-abC       — o par com o baseline limpo, para a comparação ser da mesma
#                   janela de máquina.
#   vao-quieto-*B — a célula do AMP=0.2 saiu com arquivo VAZIO (stdout perdido, sem
#                   traceback) e o 0/20 não é confiável. Repetida.
#
# Uso: TTS_SERIAL_ESPERA=7200 ./evidence/216-limpo.sh
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

A="TTS_LIVE_PLAYBACK_DURACAO=1"
B="TTS_LIVE_BARGE_JANELA_TURNO=1"
C="TTS_LIVE_ECO_SO_TOCANDO=1"

rodar() {   # rodar <rotulo> <env...>
    local rotulo="$1"; shift
    echo "=== $rotulo ($(date +%H:%M:%S)) ==="
    env REP=20 "$@" ./tests/live_barge_rep.sh > "evidence/216-$rotulo.txt" 2>&1
    local ok
    ok="$(grep -c '✔ barge' "evidence/216-$rotulo.txt" || true)"
    echo "  → evidence/216-$rotulo.txt · $ok/20 barge"
    printf '%-14s %2s/20 barge · %s\n' "$rotulo" "$ok" \
        "$(grep -h '=== MOTOR' "evidence/216-$rotulo.txt" | tail -1)" >> evidence/216-resumo.txt
}

rodar eco-baselineC          MIC_FILE=1
rodar eco-abC       $A $B    MIC_FILE=1
rodar vao-quieto-abB  $A $B  MODO=vao AMP=0.2
rodar vao-quieto-abcB $A $B $C MODO=vao AMP=0.2

echo "=== resumo ==="
tail -5 evidence/216-resumo.txt