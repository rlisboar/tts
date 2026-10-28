#!/usr/bin/env bash
# Sonda do #591 — a MORDIDA do "caminho por env ANTES do import".
#
# O import de `app` resolve os caminhos de estado (TTS_*_PATH) e JÁ ESCREVE: ele
# roda `_sync_legacy_apikey_file()`, que normaliza o espelho legado `.apikey`. Se
# o caminho só é trocado DEPOIS do import (patch de ATRIBUTO, como os launchers
# faziam), essa escrita cai no arquivo secreto do DONO.
#
#   SONDA_MODO=env       caminhos no AMBIENTE antes do import  → o dono fica INTOCADO
#   SONDA_MODO=atributo  patch de atributo depois do import    → o dono é ESCRITO
#
# Em ambos os modos o arquivo de teste nasce SUJO (`.apikey` = "VELHO" contra a
# chave "S1" do `.apikeys.json`) para a normalização ter o que escrever, e os
# arquivos do dono são copiados/restaurados no trap. O alvo de isolamento
# (`SONDA_ISO`) e o do dono são medidos em SENTIDOS OPOSTOS.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(cd "$AQUI/../../.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$RAIZ" || exit 1

ORIG="$RAIZ"
BAK="$(mktemp -d -t dono-sonda.XXXXXX)"
cp -p "$ORIG/.apikey" "$BAK/.apikey.orig"      # `-p`: o restore devolve o mtime
cp -p "$ORIG/.apikeys.json" "$BAK/.apikeys.json.orig"
trap 'cp -p "$BAK/.apikey.orig" "$ORIG/.apikey"; cp -p "$BAK/.apikeys.json.orig" "$ORIG/.apikeys.json"; rm -rf "$BAK"' EXIT
# o runner só mostra o stdout da suíte quando ela FALHA: o veredito também vai
# para `SONDA_SAIDA` (arquivo), que é o que o teste cobra.
_fala() { printf '%s\n' "$1"; [ -n "${SONDA_SAIDA:-}" ] && printf '%s\n' "$1" > "$SONDA_SAIDA"; return 0; }
# par sujo determinístico: chave S1 no JSON, espelho legado velho
_suja() {
    printf '{"enabled":true,"keys":[{"id":"a","name":"x","secret":"S1","created_at":""}]}' > "$1/.apikeys.json"
    printf 'VELHO\n' > "$1/.apikey"
}

if [ "${SONDA_MODO:-env}" = env ]; then
    ISO="${SONDA_ISO:?modo env precisa de SONDA_ISO}"
    cp "$ORIG/.apikey" "$ORIG/.apikeys.json" "$ISO/"
    _suja "$ISO"
    # `TTS_*_PATH` no ambiente da linha de comando: o import já resolve para o tmp
    TTS_SETTINGS_PATH="$ISO/settings.json" TTS_APIKEYS_PATH="$ISO/.apikeys.json" \
    TTS_LEGACY_APIKEY_PATH="$ISO/.apikey" "$PY" - <<'PY'
import os, app  # o import basta: ele é quem escreve
# o caminho do settings também tem de vir do ambiente (não do default do módulo)
assert str(app.SETTINGS_PATH) == os.environ["TTS_SETTINGS_PATH"], app.SETTINGS_PATH
assert str(app.APIKEYS_PATH) == os.environ["TTS_APIKEYS_PATH"], app.APIKEYS_PATH
assert str(app.LEGACY_APIKEY_PATH) == os.environ["TTS_LEGACY_APIKEY_PATH"], app.LEGACY_APIKEY_PATH
PY
    if [ "$(cat "$ISO/.apikey")" != "S1" ]; then
        _fala "✖ o import não normalizou o espelho no alvo de isolamento — a sonda não provou nada" >&2
        exit 1
    fi
    if cmp -s "$BAK/.apikey.orig" "$ORIG/.apikey" && cmp -s "$BAK/.apikeys.json.orig" "$ORIG/.apikeys.json"; then
        _fala "arquivos do dono INTOCADOS (caminho por env)"
        exit 0
    fi
    _fala "✖ o import reescreveu arquivo do DONO mesmo com o env" >&2
    exit 1
fi

# modo atributo: sem env nenhum, e o patch vem DEPOIS do import (o defeito)
unset TTS_SETTINGS_PATH TTS_APIKEYS_PATH TTS_LEGACY_APIKEY_PATH
ISO="${SONDA_ISO:?modo atributo precisa de SONDA_ISO}"
_suja "$ORIG"                       # o dono fica sujo durante a sonda (trap restaura)
cp "$ORIG/.apikey" "$BAK/.apikey.base"   # baseline: o dono sujo
"$PY" - "$ISO" <<'PY'
import pathlib, sys, app
# patch DEPOIS do import: o `_sync_legacy_apikey_file()` do import já rodou no dono
app.SETTINGS_PATH = pathlib.Path(sys.argv[1], "settings.json")
app.APIKEYS_PATH = pathlib.Path(sys.argv[1], ".apikeys.json")
app.LEGACY_APIKEY_PATH = pathlib.Path(sys.argv[1], ".apikey")
PY
if cmp -s "$BAK/.apikey.base" "$ORIG/.apikey"; then
    _fala "✖ sem env o import não encostou no dono — a mordida não morde" >&2
    exit 1
fi
_fala "atributo DEPOIS do import: o \`.apikey\` do DONO foi reescrito (defeito)"
