#!/usr/bin/env bash
# Sonda do #711: deixa o segredo do dono num arquivo de /tmp e NÃO limpa, como as
# suítes faziam (o app semeia a chave do ambiente e a persiste no arquivo da rodada).
set -uo pipefail
[ -n "${TTS_ROD_API_KEY:-}" ] || { echo "✖ sem TTS_ROD_API_KEY" >&2; exit 2; }
printf '{"keys":[{"secret":"%s"}]}\n' "$TTS_ROD_API_KEY" > "/tmp/vaza_segredo_$$.json"
chmod 600 "/tmp/vaza_segredo_$$.json"
echo "deixei o segredo em /tmp/vaza_segredo_$$.json"
