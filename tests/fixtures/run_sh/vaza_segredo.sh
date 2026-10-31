#!/usr/bin/env bash
# Sonda do #723: deixa o segredo do dono num arquivo de /tmp e NÃO limpa, como as
# suítes faziam (o app semeia a chave do ambiente e a persiste no arquivo da rodada).
set -uo pipefail
[ -n "${TTS_ROD_API_KEY:-}" ] || { echo "✖ sem TTS_ROD_API_KEY" >&2; exit 2; }
# `mktemp`: o PID pode repetir DENTRO de uma rodada cheia e aí o arquivo já existe
# no retrato do início — a guarda compara conjuntos e o descartaria
ARQ="$(mktemp /tmp/vaza_segredo_XXXXXXXXXX)"   # X no FIM: o mktemp do BSD exige
printf '{"keys":[{"secret":"%s"}]}\n' "$TTS_ROD_API_KEY" > "$ARQ"
chmod 600 "$ARQ"
echo "deixei o segredo em $ARQ"
