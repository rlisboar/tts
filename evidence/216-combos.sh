#!/usr/bin/env bash
# #216 — MEDIÇÃO DA COMBINAÇÃO das três alavancas do #167 (e de cada par).
#
# Por que existe: as três alavancas nascem DESLIGADAS e cada uma tem, no código,
# uma medição que a justificou EM ISOLADO. O que ninguém mediu é a COMBINAÇÃO —
# e é ela que decide se o default pode entregar o fix do #167 (barge nos vãos)
# sem regredir o outro sentido (barge falso por eco).
#
# Protocolo (o mesmo das medições anteriores, para ser comparável):
#   ./tests/live_barge_rep.sh com REP=20, nos DOIS sentidos:
#     • barge VERDADEIRO — sem MIC_FILE (onset do humano durante o playback)
#     • barge FALSO por eco — MIC_FILE=1 (mic falso tocando o wav em loop)
#
# Alavancas: A=playback_por_duracao · B=barge_janela_turno · C=eco_so_tocando
# Configurações: baseline (nenhuma), C, AB, AC, BC, ABC.
# (A e B sozinhas já estavam medidas em evidence/167-*; o baseline é re-medido
#  aqui para o controle ser da MESMA sessão de máquina.)
#
# Uso: ./evidence/216-combos.sh          (sequencial — o serial.sh trava o modelo)
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

: > evidence/216-resumo.txt

for sentido in "" "MIC_FILE=1"; do
    sufixo=$([ -n "$sentido" ] && echo "eco" || echo "true")
    echo "──── sentido: $([ -n "$sentido" ] && echo 'FALSO por eco' || echo 'VERDADEIRO') ────"
    rodar "$sufixo-baseline" $sentido
    rodar "$sufixo-c"        $C $sentido
    rodar "$sufixo-ab"       $A $B $sentido
    rodar "$sufixo-ac"       $A $C $sentido
    rodar "$sufixo-bc"       $B $C $sentido
    rodar "$sufixo-abc"      $A $B $C $sentido
done

echo "=== resumo ==="
cat evidence/216-resumo.txt