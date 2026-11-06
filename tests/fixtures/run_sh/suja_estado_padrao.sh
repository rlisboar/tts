#!/usr/bin/env bash
# Sonda do #883: escreve no caminho PADRÃO de estado do app (o que uma suíte sem
# override faria), IGNORANDO `TTS_DONO_DIR`. O veredito do runner tem de olhar o
# MESMO default — desde o #721 ele saiu da raiz do repositório, e apontar para a
# raiz deixava a célula cega (arquivo ausente dos dois lados).
#
# `TTS_STATE_DIR` vence a regra da árvore: é o que permite a célula rodar sem
# encostar no Application Support real da máquina.
set -uo pipefail
if [ -z "${TTS_STATE_DIR:-}" ]; then
    echo "✖ ${0##*/}: TTS_STATE_DIR não veio (veja tests/run_sh.sh)" >&2
    exit 2
fi
mkdir -p "$TTS_STATE_DIR"
printf '{}\n' > "$TTS_STATE_DIR/settings.json"
# segue VIVA um instante depois de escrever: o amostrador precisa ver o marcador
# dela no instante da mudança para poder nomeá-la.
sleep "${SONDA_ESPERA:-1}"
echo "sujei o estado padrão do dono"
