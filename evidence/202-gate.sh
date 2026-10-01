#!/usr/bin/env bash
# GATE #202 — as suítes de tela não podem sujar o estado do DONO.
# Células: P (controle positivo, byte a byte), N (controle negativo: o inválido
# volta inválido), S (SIGINT no meio). Tudo com sha256 de settings.json e
# .apikeys.json antes/depois; o estado do dono é restaurado entre células.
set -uo pipefail
cd "$(dirname "$0")/.."
E=evidence
S_JSON=settings.json
S_KEYS=.apikeys.json
sha() { shasum -a 256 "$1" | awk '{print $1}'; }
estado() { # $1 = rótulo
  echo "  [$1] settings=$(sha "$S_JSON") keys=$(sha "$S_KEYS") speed=$(./.venv-mlx/bin/python -c 'import json;print(json.load(open("settings.json")).get("speed"))')"
}
falhas=0
ok()  { echo "  [ok]   $*"; }
mal() { echo "  [FALHA] $*"; falhas=1; }

RUN="$$"   # sufixo por execução: o trap RESTAURA estado — paralelo não pode trocar o backup
cp "$S_JSON" "/tmp/202-settings-$RUN.bak"; cp "$S_KEYS" "/tmp/202-keys-$RUN.bak"
trap 'cp "/tmp/202-settings-$RUN.bak" "$S_JSON"; cp "/tmp/202-keys-$RUN.bak" "$S_KEYS"' EXIT
BASE_S="$(sha "$S_JSON")"; BASE_K="$(sha "$S_KEYS")"

echo "# gate #202 — $(date -u +%FT%TZ) · pins: dsh_ui.sh=$(md5 -q tests/dsh_ui.sh) admin_ui_flow.sh=$(md5 -q tests/admin_ui_flow.sh)"
estado "base"

# ── P: controle POSITIVO — as duas suítes com o settings do dono VÁLIDO
echo "== P) controle positivo (byte a byte)"
for suite in tests/dsh_ui.sh tests/admin_ui_flow.sh; do
  antes="$(sha "$S_JSON")"
  "$suite" > "$E/202-P-$(basename "$suite").txt" 2>&1; rc=$?
  depois="$(sha "$S_JSON")"
  grep -cE "pageerror|console.error" "$E/202-P-$(basename "$suite").txt" >/dev/null || true
  erros=$(grep -cE "pageerror|console\.error" "$E/202-P-$(basename "$suite").txt" || true)
  [ "$rc" = 0 ] && ok "$suite EXIT=0" || mal "$suite saiu $rc"
  [ "$antes" = "$depois" ] && ok "$suite devolveu settings.json byte a byte" \
    || mal "$suite MUDOU settings.json ($antes → $depois)"
  [ "$(sha "$S_KEYS")" = "$BASE_K" ] && ok "$suite não tocou .apikeys.json" \
    || mal "$suite mexeu em .apikeys.json"
  echo "      pageerror/console.error: $erros"
done
[ "$(sha "$S_JSON")" = "$BASE_S" ] && ok "estado do dono intacto ao fim do P" || mal "settings divergiu no P"

# ── N: controle NEGATIVO — dsh inválido tem de voltar inválido
echo "== N) controle negativo (dsh inválido: normaliza para a rodada, devolve o inválido)"
./.venv-mlx/bin/python - <<'PY'
import json, pathlib
p = pathlib.Path("settings.json"); d = json.loads(p.read_text())
d["chat_dsh_bin"] = "/nao/existe/dsh"; d["chat_dsh_profile"] = "perfil-que-nao-existe"
p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
PY
N_S="$(sha "$S_JSON")"
tests/dsh_ui.sh > "$E/202-N-dsh-invalido.txt" 2>&1; rc=$?
[ "$rc" = 0 ] && ok "dsh_ui.sh EXIT=0 com dsh inválido" || mal "dsh_ui.sh saiu $rc com dsh inválido"
[ "$(sha "$S_JSON")" = "$N_S" ] && ok "o inválido VOLTOU inválido (não 'consertou' o dono)" \
  || mal "o arquivo voltou 'consertado': $(sha "$S_JSON") ≠ $N_S"
./.venv-mlx/bin/python -c "import json;d=json.load(open('settings.json'));print('      bin=',d.get('chat_dsh_bin'),'profile=',d.get('chat_dsh_profile'))"
cp "/tmp/202-settings-$RUN.bak" "$S_JSON"
[ "$(sha "$S_JSON")" = "$BASE_S" ] && ok "estado do dono restaurado após o N" || mal "não restaurei o estado do dono"

# ── S: SIGINT no meio do admin_ui_flow.sh
echo "== S) SIGINT no meio"
tests/admin_ui_flow.sh > "$E/202-S-sigint.txt" 2>&1 &
PID=$!
sleep 25
kill -INT "$PID" 2>/dev/null
wait "$PID" 2>/dev/null; rc=$?
sleep 1
[ "$(sha "$S_JSON")" = "$BASE_S" ] && ok "SIGINT: settings.json intacto" || mal "SIGINT: settings.json mudou"
orfaos=$(./.venv-mlx/bin/python - <<'PY'
import json
d = json.load(open(".apikeys.json"))
ks = d["keys"] if isinstance(d.get("keys"), list) else []
print(",".join(k.get("name","?") for k in ks if "teste-ui" in str(k.get("name","")) or "probe" in str(k.get("name",""))))
PY
)
[ -z "$orfaos" ] && ok "SIGINT: nenhuma chave teste-ui-*/probe-* órfã" || mal "SIGINT: chaves órfãs: $orfaos"
echo "      (rc do SIGINT=$rc)"

echo "== estado final"
estado "final"
[ "$(sha "$S_JSON")" = "$BASE_S" ] && [ "$(sha "$S_KEYS")" = "$BASE_K" ] \
  && ok "byte a byte idêntico ao snapshot inicial" || mal "estado final difere do inicial"
echo "== FIM: falhas=$falhas"
exit $falhas
