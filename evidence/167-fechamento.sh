#!/usr/bin/env bash
# #167 — PROVA DE FECHAMENTO, com o DEFAULT da árvore (sem env de alavanca).
#
# O #216 virou o default (Config e app) e a matriz que o justificou foi medida com
# as alavancas por ENV. Aqui a rodada é a do PRODUTO: nenhum TTS_LIVE_* exportado,
# então o que corre é o que o dono recebe.
#
#   vao  — o cenário do defeito (#167): injeção 900 ms DEPOIS do turno terminar.
#          Alvo do ticket: 0 falha em REP=20.
#   eco  — MIC_FILE=1: mic falso tocando o wav; todo corte é FALSO. Controle do
#          outro lado do tradeoff (menor é melhor).
#   true — modo padrão: satura perto do teto (controle de "não quebrou o normal").
#   ui   — tests/live_ui.sh no regime ESTRITO (exigência dura do #179): o corte do
#          barge tem de ser medido de verdade.
#
# Uso: ./evidence/167-fechamento.sh
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

# nenhuma alavanca por env: o default da árvore é o objeto da prova
unset TTS_LIVE_PLAYBACK_DURACAO TTS_LIVE_BARGE_JANELA_TURNO TTS_LIVE_ECO_SO_TOCANDO

rodar() {   # rodar <rotulo> <arquivo> <env...>
    local rotulo="$1" arq="$2"; shift 2
    echo "=== $rotulo ($(date +%H:%M:%S)) ==="
    env REP=20 "$@" ./tests/live_barge_rep.sh > "evidence/$arq" 2>&1
    local ok falhas
    ok="$(grep -c '✔ barge' "evidence/$arq" || true)"
    falhas="$(grep -c '✖ SEM barge' "evidence/$arq" || true)"
    echo "  → evidence/$arq · ok=$ok/20 · falhas=$falhas"
}

rodar vao  167-fechamento-vao.txt  MODO=vao
rodar eco  167-fechamento-eco.txt  MIC_FILE=1
rodar true 167-fechamento-true.txt

echo "=== live_ui (ESTRITO) ($(date +%H:%M:%S)) ==="
./tests/live_ui.sh > evidence/167-fechamento-live_ui.txt 2>&1
echo "  → exit $? · $(grep -c 'corte no interrupted' evidence/167-fechamento-live_ui.txt) medição(ões) de corte"

echo "=== resumo ==="
for f in vao eco true; do
    arq="evidence/167-fechamento-$f.txt"
    printf '%-6s ok=%s/20 falhas=%s cortes=%s\n' "$f" \
        "$(grep -c '✔ barge' "$arq" || true)" \
        "$(grep -c '✖ SEM barge' "$arq" || true)" \
        "$(grep -h '=== MOTOR' "$arq" | tail -1)"
done
tail -6 evidence/167-fechamento-live_ui.txt