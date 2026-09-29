#!/usr/bin/env bash
# #179/#167 — MODO=vao REP=20 nas TRÊS configurações que importam para o ship.
#
# POR QUE AS TRÊS: as alavancas do #167 nascem no worktree com o default que a
# task_df63483d (#216) decidiu virar ship (JANELA_TURNO=1, PLAYBACK_DURACAO=1,
# ECO_SO_TOCANDO=0). Sem fixar o env, "verde" não diz QUAL produto foi medido.
#   • OFF     (as três em 0) = produto de HOJE-1 (comportamento anterior ao #167)
#   • default (sem env)      = o produto que o #216 decidiu entregar (A+B)
#   • ABC     (as três em 1) = a combinação que o #216 mediu e NÃO adotou
# O hash do app.py/live_turns.py é impresso ANTES e DEPOIS de cada rodada: a
# árvore é compartilhada e o audio-ml edita app.py em paralelo.
#
# Uso: ./evidence/179-vao-flags.sh [celula...]     # sem args = as três
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_hash() { printf 'app.py=%s live_turns.py=%s\n' \
  "$(shasum app.py | cut -c1-12)" "$(shasum live_turns.py | cut -c1-12)"; }

roda() {  # $1=rotulo  $2=env das flags ("" = default do worktree)
  local rot="$1"
  local envflags="$2"
  local saida="evidence/179-vao-$rot.txt"
  {
    echo "=== #179 MODO=vao REP=20 · flags=$rot (${envflags:-default do worktree}) ==="
    echo "antes: $(_hash)"
    echo "--- saida do harness ---"
  } > "$saida"
  # shellcheck disable=SC2086
  env $envflags MODO=vao REP=20 ./tests/live_barge_rep.sh >> "$saida" 2>&1
  local rc=$?
  {
    echo "--- fim (EXIT=$rc) ---"
    echo "depois: $(_hash)"
  } >> "$saida"
  echo "$rot: EXIT=$rc · $(grep -c '✔ barge' "$saida") barges em $(grep -c 'injeta' "$saida") injecoes · $(grep -m1 '^=== RESUMO' "$saida" || echo 'sem resumo')"
}

celulas=("$@")
[ ${#celulas[@]} -eq 0 ] && celulas=(flags-off default flags-on)

for c in "${celulas[@]}"; do
  case "$c" in
    flags-off) roda "flags-off" "TTS_LIVE_BARGE_JANELA_TURNO=0 TTS_LIVE_PLAYBACK_DURACAO=0 TTS_LIVE_ECO_SO_TOCANDO=0" ;;
    default)   roda "default"   "" ;;
    flags-on)  roda "flags-on"  "TTS_LIVE_BARGE_JANELA_TURNO=1 TTS_LIVE_PLAYBACK_DURACAO=1 TTS_LIVE_ECO_SO_TOCANDO=1" ;;
    *) echo "celula desconhecida: $c (use flags-off|default|flags-on)" >&2; exit 2 ;;
  esac
done