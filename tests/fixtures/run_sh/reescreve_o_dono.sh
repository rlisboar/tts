#!/usr/bin/env bash
# Sonda do #635 (decisão do PM): reescreve o `settings.json` do "dono" com o MESMO
# conteúdo — o caso do app do dono, que roda num rev antigo e regrava o arquivo sem
# mudar byte nenhum (só o mtime). O veredito do `TTS_EXIGE_DONO_INTOCADO` é por
# SHA256: isto tem de virar NOTA, não reprovação.
#
# `TTS_DONO_DIR` aponta para uma CÓPIA (o arquivo real não é tocado).
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# SEM fallback: `TTS_DONO_DIR` ausente tem de parar a sonda, não escolher sozinha.
# A queda para `$AQUI/../../..` fazia a sonda escrever na RAIZ do repo — e na árvore
# principal a raiz é o `settings.json` do DONO (virou `{}` três vezes, #710).
if [ -z "${TTS_DONO_DIR:-}" ]; then
    echo "✖ ${0##*/}: TTS_DONO_DIR não veio (sem fallback — veja tests/run_sh.sh)" >&2
    exit 2
fi
ALVO="${TTS_DONO_DIR:-$AQUI/../../..}/settings.json"
CONTEUDO="$(cat "$ALVO")"
# espera ANTES de escrever: o `stat` só tem resolução de SEGUNDO, e o retrato
# "antes" é tirado no instante em que a suíte começa — escrita no mesmo segundo
# não mudaria o mtime e a nota nem apareceria
sleep "${SONDA_ESPERA:-1}"
printf '%s' "$CONTEUDO" > "$ALVO"        # mesma string, byte a byte
sleep 1                                  # segue viva para o amostrador ver
echo "reescrevi o settings do dono com o mesmo conteúdo"
