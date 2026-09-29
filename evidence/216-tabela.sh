#!/usr/bin/env bash
# #216 — tabela única das rodadas de `evidence/216-*.txt` (não roda nada: só lê).
#
# Métricas por rodada, nos dois sentidos:
#   ok    — injeções em que o assistente FOI cortado (`interrupted`) = o defeito do
#           #167 ("fala no vão não interrompe"). Quanto MAIOR, melhor.
#   cortes— `interrupted` CUMULATIVOS da rodada (contador do cliente). No sentido do
#           eco a maior parte deles não tem humano falando: é o motor se cortando
#           sozinho. Quanto MENOR, melhor.
#   barges— `barge_in` cumulativos (o motor armou interrupção).
#   vao   — falhas cuja assinatura é `speech_start` SEM barge (o onset virou turno
#           novo em vez de interrupção: o defeito clássico).
#
# Uso: ./evidence/216-tabela.sh [glob]
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

glob="${1:-evidence/216-*.txt}"
printf '%-16s %-6s %5s %5s %7s %7s %7s\n' rodada sentido ok falhas cortes barges audio
for f in $glob; do
    case "$f" in *resumo*) continue;; esac
    rot="$(basename "$f" .txt)"; rot="${rot#216-}"
    case "$rot" in eco-*) sentido=eco;; true-*) sentido=true;; vao-*) sentido=vao;; *) sentido="?";; esac
    ok=$(grep -c '✔ barge' "$f" || true)
    falhas=$(grep -c '✖ SEM barge' "$f" || true)
    cortes=$(grep -h '=== CONTAGEM' "$f" | tail -1 | sed -n 's/.*"interrupted": \([0-9]*\).*/\1/p')
    barges=$(grep -h '=== CONTAGEM' "$f" | tail -1 | sed -n 's/.*"barge_in": \([0-9]*\).*/\1/p')
    audio=$(grep -h '=== CONTAGEM' "$f" | tail -1 | sed -n 's/.*"audio": \([0-9]*\).*/\1/p')
    printf '%-16s %-6s %5s %5s %7s %7s %7s\n' "$rot" "$sentido" \
        "${ok:--}" "${falhas:--}" "${cortes:--}" "${barges:--}" "${audio:--}"
done