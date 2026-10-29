#!/usr/bin/env bash
# Sonda do #591 — a MORDIDA do "caminho por env ANTES do import".
#
# O import de `app` LÊ os arquivos de estado (`_settings` e `_load_apikeys`). Se o
# caminho só é trocado DEPOIS do import (patch de ATRIBUTO, como os launchers
# faziam), o processo nasce com o estado do DONO na RAM e só as escritas FUTURAS
# vão para o alvo de isolamento — a rodada roda com as configurações dele.
#
#   SONDA_MODO=env       caminhos no AMBIENTE antes do import  → lê do isolamento
#   SONDA_MODO=atributo  patch de atributo depois do import    → lê do DONO
#
# Marcador: `pre_prompt` = "ISO" no alvo, "DONO" no "dono". O DONO É SINTÉTICO E
# VIVE EM TMP (#541/#608): a sonda não escreve nem restaura o arquivo real — assim
# ela passa sob `TTS_EXIGE_DONO_INTOCADO=1`. As escritas explícitas depois do
# import continuam conferidas: têm de cair no isolamento e nunca no dono.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(cd "$AQUI/../../.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$RAIZ" || exit 1

# Tudo em tmp, com chave SINTÉTICA: o arquivo real do dono nem é lido.
BAK="$(mktemp -d /tmp/dono-sonda.XXXXXX)"
trap 'rm -rf "$BAK"' EXIT
DONO="$BAK/dono"
# o runner só mostra o stdout da suíte quando ela FALHA: o veredito também vai
# para `SONDA_SAIDA` (arquivo), que é o que o teste cobra.
_fala() { printf '%s\n' "$1"; [ -n "${SONDA_SAIDA:-}" ] && printf '%s\n' "$1" > "$SONDA_SAIDA"; return 0; }
# estado SINTÉTICO determinístico: marcador no settings e um par de chaves falso
_marca() { "$PY" - "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["pre_prompt"] = sys.argv[2]
open(sys.argv[1], "w").write(json.dumps(d))
PY
}
_chaves() {
    printf '{"enabled":true,"keys":[{"id":"a","name":"x","secret":"S1","created_at":""}]}' > "$1/.apikeys.json"
    printf 'S1\n' > "$1/.apikey"
}
# "dono" sintético, imóvel: se a rodada encostar nele, o hash muda
mkdir -p "$DONO"
printf '{"pre_prompt": ""}\n' > "$DONO/settings.json"
_marca "$DONO/settings.json" DONO
_chaves "$DONO"
_base() { cat "$DONO/settings.json" "$DONO/.apikey" "$DONO/.apikeys.json" | shasum -a 256 | cut -c1-16; }
BASE_ANTES="$(_base)"
ISO="${SONDA_ISO:?a sonda precisa de SONDA_ISO}"
cp "$DONO/settings.json" "$ISO/settings.json"
_marca "$ISO/settings.json" ISO
_chaves "$ISO"

if [ "${SONDA_MODO:-env}" = env ]; then
    # `TTS_*_PATH` no ambiente da linha de comando: o import já resolve para o tmp
    TTS_SETTINGS_PATH="$ISO/settings.json" TTS_APIKEYS_PATH="$ISO/.apikeys.json" \
    TTS_LEGACY_APIKEY_PATH="$ISO/.apikey" "$PY" - "$ISO" <<'PY'
import os, sys, app
iso = sys.argv[1]
for attr, env in (("SETTINGS_PATH", "TTS_SETTINGS_PATH"),
                  ("APIKEYS_PATH", "TTS_APIKEYS_PATH"),
                  ("LEGACY_APIKEY_PATH", "TTS_LEGACY_APIKEY_PATH")):
    assert str(getattr(app, attr)) == os.environ[env], (attr, getattr(app, attr))
# a LEITURA tem de ter vindo do isolamento, não do arquivo do dono
assert app._settings.get("pre_prompt") == "ISO", app._settings.get("pre_prompt")
# e a escrita explícita seguinte também fica no isolamento
app._settings["default_voice"] = "voz-do-iso"
app._save_settings()
PY
    if ! grep -q "voz-do-iso" "$ISO/settings.json"; then
        _fala "✖ a escrita explícita não foi para o isolamento — a sonda não provou nada" >&2
        exit 1
    fi
    if [ "$(_base)" = "$BASE_ANTES" ] && ! grep -q "voz-do-iso" "$DONO/settings.json"; then
        _fala "dono INTOCADO e estado lido do isolamento (caminho por env)"
        exit 0
    fi
    _fala "✖ a rodada encostou no DONO mesmo com o env" >&2
    exit 1
fi

# modo atributo: o env aponta o DONO no import e o patch para o isolamento vem
# DEPOIS — a leitura já aconteceu (defeito). Nada do arquivo real entra.
TTS_SETTINGS_PATH="$DONO/settings.json" TTS_APIKEYS_PATH="$DONO/.apikeys.json" \
TTS_LEGACY_APIKEY_PATH="$DONO/.apikey" "$PY" - "$DONO" "$ISO" <<'PY'
import pathlib, sys, app
dono, iso = (pathlib.Path(a) for a in sys.argv[1:3])
assert app._settings.get("pre_prompt") == "DONO", app._settings.get("pre_prompt")
# patch DEPOIS do import: a leitura já aconteceu; a escrita vai para o isolamento
app.SETTINGS_PATH = iso / "settings.json"
app.APIKEYS_PATH = iso / ".apikeys.json"
app.LEGACY_APIKEY_PATH = iso / ".apikey"
app._settings["default_voice"] = "voz-do-iso"
app._save_settings()
PY
# O veredito do modo é o MARCADOR (a leitura veio do dono, não do isolamento) —
# não a escrita: desde o #601 o import não grava, então o arquivo ficaria igual
# mesmo com o patch por atributo.
_fala "atributo DEPOIS do import: o estado do DONO entrou na RAM (defeito)"
