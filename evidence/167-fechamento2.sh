#!/usr/bin/env bash
# #167 — FECHAMENTO, parte 2: as células que faltaram.
#
# A parte 1 (`167-fechamento.sh`) fez `vao` (20/20, o alvo do ticket) e `eco`
# (22 cortes falsos) e MORREU no meio — o runner levou o processo filho junto
# (SIGTERM no grupo). Aqui só o que falta: `true` (modo padrão, controle de "não
# quebrou o normal") e o `live_ui.sh` ESTRITO (#179), mais o resumo das quatro.
#
# Rodar DESTACADO (`start_new_session`), senão o próximo hang leva de novo:
#   ./.venv-mlx/bin/python evidence/167-dispara.py
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

# nenhuma alavanca por env: o default da árvore é o objeto da prova
unset TTS_LIVE_PLAYBACK_DURACAO TTS_LIVE_BARGE_JANELA_TURNO TTS_LIVE_ECO_SO_TOCANDO

echo "=== true (modo padrão) ($(date +%H:%M:%S)) ==="
env REP=20 ./tests/live_barge_rep.sh > evidence/167-fechamento-true.txt 2>&1
echo "  → ok=$(grep -c '✔ barge' evidence/167-fechamento-true.txt || true)/20 · falhas=$(grep -c '✖ SEM barge' evidence/167-fechamento-true.txt || true)"

echo "=== live_ui (ESTRITO) ($(date +%H:%M:%S)) ==="
./tests/live_ui.sh > evidence/167-fechamento-live_ui.txt 2>&1
echo "  → exit $? · $(grep -c 'corte no interrupted' evidence/167-fechamento-live_ui.txt || true) medição(ões) de corte"

echo "=== resumo ($(date +%H:%M:%S)) ==="
for f in vao eco true; do
    arq="evidence/167-fechamento-$f.txt"
    printf '%-6s ok=%s/20 falhas=%s cortes=%s\n' "$f" \
        "$(grep -c '✔ barge' "$arq" || true)" \
        "$(grep -c '✖ SEM barge' "$arq" || true)" \
        "$(grep -h '=== MOTOR' "$arq" | tail -1)"
done
tail -6 evidence/167-fechamento-live_ui.txt
echo "=== fim ($(date +%H:%M:%S)) ==="