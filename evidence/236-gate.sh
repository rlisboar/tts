#!/bin/zsh
# GATE DELTA #236 — só o diff do smoke (#235): nonce normalizado, rota decidida
# pelo REV alvo, rc fiel. Harness próprio (shims), sem rede e sem Chromium.
# Sai != 0 se qualquer expectativa falhar.
set -u
cd "${0:A:h}/.."
RAIZ=$PWD
SCRIPT="$RAIZ/remote/deploy_mini.sh"
T="$(mktemp -d /tmp/qa236.XXXXXX)"
trap 'rm -rf "$T"' EXIT
falhas=0
ok()  { print -r -- "  [ok]   $*" }
mal() { print -r -- "  [FALHA] $*"; falhas=1 }

SHIM="$T/shims"; mkdir -p "$SHIM"
LOG_PIP="$T/pip.log"; : > "$LOG_PIP"
cat > "$SHIM/ssh" <<'EOF'
#!/bin/zsh
args=()
while [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) args+=("$1"); shift;; esac; done
exec /bin/zsh -c "${args[-1]}"
EOF
cat > "$SHIM/launchctl" <<'EOF'
#!/bin/zsh
case "${1:-}" in kickstart) print -r -- kickstart-ok;; esac
exit 0
EOF
cat > "$SHIM/curl" <<'EOF'
#!/bin/zsh
# Shim do app: o /api/build e o WS respondem só se o ALVO tiver a rota (knobs
# T_CURL_BUILD_CODE/T_CURL_WS_CODE simulam rota ausente) e o index sai com nonce
# por resposta, como o app real (CSP).
url=""; tem_chave=0; so_codigo=0
for ((i = 1; i <= $#; i++)); do
  case "${@[i]}" in
    http*) url="${@[i]}";;
    -H) [[ "${@[i+1]}" == X-API-Key:* ]] && tem_chave=1;;
    -w) so_codigo=1;;
  esac
done
codigo=200; corpo=""; arquivo=""; nonce=""
case "$url" in
  */health) corpo='{"ok":true}';;
  */api/voices) [ $tem_chave = 1 ] && codigo=200 || codigo=401;;
  */api/tts/jobs/*/pieces/*) corpo="RIFF-fake-audio";;
  */api/tts/jobs/*) [ $tem_chave = 1 ] && corpo='{"status":"done"}' || codigo=401;;
  */api/tts) [ $tem_chave = 1 ] && corpo='{"job_id":"j1"}' || codigo=401;;
  */api/build) [ $tem_chave = 1 ] && { codigo="${T_CURL_BUILD_CODE:-200}"; corpo="{\"ok\":true,\"codigo\":\"$(cd "$T_MINI_DIR" && cat ${=T_MODULOS} 2>/dev/null | /usr/bin/shasum -a 256 | cut -c1-8)\",\"boot_ms\":1000}"; } || codigo=401;;
  */api/live/ws*) codigo="${T_CURL_WS_CODE:-101}";;
  */) arquivo="$T_MINI_DIR/static/index.html"; nonce=1;;
esac
if [ $so_codigo = 1 ]; then print -rn -- "$codigo"
elif [ -n "$arquivo" ]; then
  if [ -n "$nonce" ]; then sed -E 's/<html/<html nonce="rnd${RANDOM}"/' "$arquivo"; else cat "$arquivo"; fi
else print -rn -- "$corpo"; fi
EOF
chmod +x "$SHIM"/ssh "$SHIM"/launchctl "$SHIM"/curl

export PATH="$SHIM:$PATH" T_MINI_DIR="$T/mini" T_PIP_LOG="$LOG_PIP"
export T_MODULOS="app.py common.py backends.py tts_worker.py live_pipeline.py live_turns.py dsh_client.py"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL="$T/gitconfig" GIT_CONFIG_SYSTEM=/dev/null
export TTS_MINI_HOST=mini-fake TTS_MINI_DIR="$T/mini" TTS_MINI_LABEL=studio.tts.server
export TTS_MINI_ESPERA=0 TTS_MINI_REPO="$T/dev" TTS_PUBLIC_HOST=tts.exemplo.test
export TTS_MINI_PREVIEW="$T/preview" PYTHONDONTWRITEBYTECODE=1
print -r -- "[init]" > "$T/gitconfig"

# fixture: origin bare + semente (COM rotas) + dev + branch corte-velho (SEM rotas)
git init --bare -q "$T/origin.git"
mkdir -p "$T/semente/static"
for m in ${=T_MODULOS}; do print -r -- "# $m" > "$T/semente/$m"; done
cat >> "$T/semente/app.py" <<'EOF'
@app.get("/api/build")
@app.websocket("/api/live/ws")
EOF
print -r -- "<html>v1</html>" > "$T/semente/static/index.html"
print -r -- "fastapi" > "$T/semente/requirements.txt"
print -r -- "k" > "$T/semente/.apikey"
git -C "$T/semente" init -q; git -C "$T/semente" add -A; git -C "$T/semente" commit -qm base
git -C "$T/semente" remote add origin "$T/origin.git"; git -C "$T/semente" push -q origin main
git clone -q "$T/origin.git" "$T/dev"
git -C "$T/dev" switch -q -c corte-velho
printf '# app sem rotas (anterior ao #190 e ao Live)\n' > "$T/dev/app.py"
print -r -- "<html>velho</html>" > "$T/dev/static/index.html"
git -C "$T/dev" commit -qam corte-velho
git -C "$T/dev" switch -q main
git clone -q "$T/origin.git" "$T/mini"

roda() { zsh -f "$SCRIPT" "$@"; }

# ── 1) alvo SEM as rotas: o deploy fecha verde (rc fiel no esperado-para-o-alvo)
saida="$(TTS_MINI_REV=corte-velho roda deploy --apply 2>&1)"; rc=$?
if [ $rc -eq 0 ] && print -r -- "$saida" | grep -q "ausente NESTE rev" \
   && ! print -r -- "$saida" | grep -q "\[FALHA\]"; then
  ok "alvo sem rotas: deploy --apply rc=0 e smoke sem FALHA (esperado-para-o-alvo não derruba)"
else mal "alvo sem rotas: rc=$rc"; print -r -- "$saida" | grep -E "FALHA|ausente" | head -4 | sed 's/^/      /'; fi
print -r -- "      linhas: $(print -r -- "$saida" | grep -c 'ausente NESTE rev') ausente(s)"

# ── 2) MESMO estado do mini, alvo = HEAD (que TEM as rotas) → o shim devolve 404
saida="$(T_CURL_BUILD_CODE=404 T_CURL_WS_CODE=404 roda smoke 2>&1)"; rc=$?
if [ $rc -ne 0 ] && print -r -- "$saida" | grep -q "\[FALHA\].*api/build" \
   && print -r -- "$saida" | grep -q "api/live/ws devolveu 404"; then
  ok "alvo com rotas + rota que não responde: smoke ACUSA (rc=$rc)"
else mal "alvo com rotas: rc=$rc"; print -r -- "$saida" | grep -E "FALHA|ausente" | head -4 | sed 's/^/      /'; fi

# ── 3) a decisão vem do CÓDIGO DO REV: mesmo mini/shim, só o alvo muda
saida="$(T_CURL_BUILD_CODE=404 T_CURL_WS_CODE=404 TTS_MINI_REV=corte-velho roda smoke 2>&1)"; rc=$?
if [ $rc -eq 0 ] && ! print -r -- "$saida" | grep -q "\[FALHA\]"; then
  ok "trocar só o alvo (corte-velho) volta a NÃO acusar → decisão é do rev, não do HEAD"
else mal "alvo corte-velho com shim 404: rc=$rc"; print -r -- "$saida" | grep "FALHA" | head -3 | sed 's/^/      /'; fi

# ── 4) nonce: o shim injeta nonce por resposta; a paridade tem de fechar
saida="$(TTS_MINI_REV=corte-velho roda smoke 2>&1)"; rc=$?
if [ $rc -eq 0 ] && print -r -- "$saida" | grep -q "index.html = blob do rev alvo"; then
  ok "paridade do index fecha COM nonce por resposta (normalização ativa)"
else mal "paridade com nonce: rc=$rc"; print -r -- "$saida" | grep -E "index|FALHA" | head -3 | sed 's/^/      /'; fi

# ── mordidas no script (backup/restore byte a byte)
bak="$(mktemp)"; cp "$SCRIPT" "$bak"
morde() { # $1 rótulo · $2 antigo · $3 novo · $4 teste · $5 expectativa (MORDE|PASSA)
  cp "$bak" "$SCRIPT"
  ./.venv-mlx/bin/python - "$SCRIPT" "$2" "$3" "${6:-1}" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
n = int(sys.argv[4])
assert s.count(sys.argv[2]) == n, f"casou {s.count(sys.argv[2])}x (esperado {n})"
p.write_text(s.replace(sys.argv[2], sys.argv[3]))
PY
  ./tests/limpa_bytecode.sh > /dev/null
  ./.venv-mlx/bin/python -m pytest "$4" -q > $T/m236.log 2>&1; local rc=$?
  cp "$bak" "$SCRIPT"; cmp -s "$bak" "$SCRIPT" || mal "restauração falhou ($1)"
  local obtido=PASSA; [ $rc -ne 0 ] && obtido=MORDE
  [ "$obtido" = "$5" ] && ok "mordida $1 → $obtido" || { mal "mordida $1 → $obtido (esperado $5)"; tail -2 $T/m236.log | sed 's/^/      /'; }
}
morde "nonce (normalização removida)" '| sem_nonce | shasum -a 256' '| shasum -a 256' \
      "tests/test_deploy_mini_sh.py::test_deploy_apply_publica_puxa_instala_reinicia_e_smoke" MORDE 2
morde "rota decidida pelo HEAD" 'git -C "$LOCAL_REPO" show "${1}:app.py"' 'git -C "$LOCAL_REPO" show "HEAD:app.py"' \
      "tests/test_deploy_mini_sh.py::test_smoke_nao_exige_rota_ausente_no_rev_alvo" MORDE
morde "rc sempre 0" 'POS_DEPLOY=1 REV_ALVO="$ALVO_SHA" smoke' 'POS_DEPLOY=1 REV_ALVO="$ALVO_SHA" smoke; falhas=0' \
      "tests/test_deploy_mini_sh.py::test_rc_fiel_esperado_para_o_alvo_nao_derruba_e_inesperado_derruba" MORDE
cp "$bak" "$SCRIPT"; cmp -s "$bak" "$SCRIPT" && ok "script restaurado byte a byte ($(md5 -q "$SCRIPT"))"

print -r -- "== FIM: falhas=$falhas"
exit $falhas
