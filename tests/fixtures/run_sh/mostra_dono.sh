#!/usr/bin/env bash
# Sonda do #1018: mostra o que o runner pôs no "dono" da sonda.
#
# O conteúdo tem de ser SINTÉTICO (regra #728). Se o runner voltar a COPIAR o
# arquivo real, o marcador que só existe na raiz aparece aqui e a célula cai.
# O runner só imprime a saída da suíte quando ela FALHA, então grava também em
# `SONDA_SAIDA` (arquivo) — mesmo padrão da `sonda_env.sh`.
set -uo pipefail
[ -n "${TTS_DONO_DIR:-}" ] || { echo "✖ sem TTS_DONO_DIR" >&2; exit 2; }
exec > >(tee "${SONDA_SAIDA:-/dev/stdout}") 2>&1
printf 'dir=%s\n' "$TTS_DONO_DIR"   # a célula cobra que o trap apague no fim
for a in settings.json .apikeys.json .apikey .speaker-profiles.json; do
    if [ -f "$TTS_DONO_DIR/$a" ]; then
        printf '%s=%s\n' "$a" "$(cat "$TTS_DONO_DIR/$a")"
        printf '  modo %s\n' "$(stat -f %Lp "$TTS_DONO_DIR/$a" 2>/dev/null \
            || stat -c %a "$TTS_DONO_DIR/$a")"
    else
        printf '%s=(ausente)\n' "$a"
    fi
done
