#!/usr/bin/env bash
# Sonda do #723 (P2 do infra): sobrescreve um arquivo que JÁ existia com `cp -p` de
# outro que carrega o segredo. mtime fica o do original e o nome já estava no
# retrato — só a comparação por HASH pega.
set -uo pipefail
[ -n "${TTS_ROD_API_KEY:-}" ] || { echo "✖ sem TTS_ROD_API_KEY" >&2; exit 2; }
[ -n "${ALVO:-}" ] && [ -n "${FONTE:-}" ] || { echo "✖ sem ALVO/FONTE" >&2; exit 2; }
printf '{"k":"%s"}\n' "$TTS_ROD_API_KEY" > "$FONTE"
touch -t 202001010000 "$FONTE"          # mtime VELHO, para o `-newermt` não pegar
cp -p "$FONTE" "$ALVO"
echo "sobrescrevi $ALVO com cp -p"
