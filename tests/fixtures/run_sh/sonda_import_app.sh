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
# Marcador: `pre_prompt` = "ISO" no alvo, "DONO" no arquivo real (restaurado no
# trap). Chaves SINTÉTICAS nos dois lados — nenhum segredo do dono cai no basetemp.
# As escritas explícitas depois do import são conferidas de quebra: têm de cair no
# isolamento e nunca no arquivo do dono.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(cd "$AQUI/../../.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$RAIZ" || exit 1

ORIG="$RAIZ"
# backup FORA do `$TMPDIR` do pytest: o guard `test_basetemp_nao_carrega_secret_do_dono`
# varre o basetemp e aqui moram as cópias das chaves REAIS do dono (`-t` usaria o
# basetemp quando `TMPDIR` aponta para ele — falso vermelho do guard).
BAK="$(mktemp -d /tmp/dono-sonda.XXXXXX)"
# O worktree NÃO tem os arquivos de estado do dono (#659): os que faltarem são
# criados aqui como estado SINTÉTICO e removidos no fim (não viram resíduo).
_NOVOS=""
for f in settings.json .apikey .apikeys.json; do
    if [ -f "$ORIG/$f" ]; then cp -p "$ORIG/$f" "$BAK/$f.orig"
    else _NOVOS="$_NOVOS $f"; fi
done
trap 'for f in settings.json .apikey .apikeys.json; do
        if [ -f "$BAK/$f.orig" ]; then cp -p "$BAK/$f.orig" "$ORIG/$f"
        else case " $_NOVOS " in *" $f "*) rm -f "$ORIG/$f" ;; esac; fi
      done; rm -rf "$BAK"' EXIT
[ -f "$ORIG/settings.json" ] || printf '{}\n' > "$ORIG/settings.json"
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
# o dono fica com o marcador "DONO" durante a sonda (o trap restaura)
_marca "$ORIG/settings.json" DONO
_chaves "$ORIG"
# baseline do dono JÁ sujo (o `.orig` é só para o trap)
cp -p "$ORIG/settings.json" "$BAK/settings.json.base"
cp -p "$ORIG/.apikey" "$BAK/.apikey.base"
cp -p "$ORIG/.apikeys.json" "$BAK/.apikeys.json.base"
ISO="${SONDA_ISO:?a sonda precisa de SONDA_ISO}"
cp "$ORIG/settings.json" "$ISO/settings.json"
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
    # `settings.json` é comparado pelo CONTEÚDO e não byte a byte: um servidor
    # perdido de outro teste pode reescrevê-lo (formato/ordem) durante a rodada —
    # o que se cobra aqui é a marca do isolamento não ter vazado para ele.
    if ! grep -q "voz-do-iso" "$ORIG/settings.json" &&
       cmp -s "$BAK/.apikey.base" "$ORIG/.apikey" &&
       cmp -s "$BAK/.apikeys.json.base" "$ORIG/.apikeys.json"; then
        _fala "arquivos do dono INTOCADOS e estado lido do isolamento (caminho por env)"
        exit 0
    fi
    _fala "✖ a rodada encostou em arquivo do DONO mesmo com o env" >&2
    exit 1
fi

# modo atributo: sem env, patch DEPOIS do import (o defeito)
unset TTS_SETTINGS_PATH TTS_APIKEYS_PATH TTS_LEGACY_APIKEY_PATH
"$PY" - "$ISO" <<'PY'
import pathlib, sys, app
# patch DEPOIS do import: a leitura já aconteceu no arquivo do dono
app.SETTINGS_PATH = pathlib.Path(sys.argv[1], "settings.json")
app.APIKEYS_PATH = pathlib.Path(sys.argv[1], ".apikeys.json")
app.LEGACY_APIKEY_PATH = pathlib.Path(sys.argv[1], ".apikey")
assert app._settings.get("pre_prompt") == "DONO", app._settings.get("pre_prompt")
PY
# O veredito do modo é o MARCADOR (a leitura veio do dono, não do isolamento) —
# não a escrita: desde o #601 o import não grava, então o arquivo ficaria igual
# mesmo com o patch por atributo.
_fala "atributo DEPOIS do import: o estado do DONO entrou na RAM (defeito)"
