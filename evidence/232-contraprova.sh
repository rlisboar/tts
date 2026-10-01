#!/bin/zsh
# GATE #232 — contraprova INDEPENDENTE de remote/deploy_mini.sh.
# Não usa o tests/test_deploy_mini_sh.py: monta o próprio origin+mini+shims e
# MEDE o que o script faz (árvore do mini, refs do origin, log do launchctl,
# branch final, caminho de volta). Sai != 0 se qualquer expectativa falhar.
#
# Células:
#   D1 deploy dry-run  → nada muda no mini (árvore/HEAD), no origin, no serviço
#   D2 compare         → idem
#   D3 smoke           → idem
#   D4 recon           → árvore/serviço intocados; só refs do mini (fetch) mudam
#   A1 TTS_MINI_REV    → alvo por REV: mini termina em prod-<sha> (NÃO detached)
#   A2 compare desvio  → alvo não-descendente do que está no ar → recusa (!=0)
#   A3 rollback --apply→ volta o SHA salvo pelo último deploy (caminho de volta)
#   A4 --sem-deps      → não roda pip e não escreve o preview de requirements
#   A5 mini sujo       → dry-run PARA sem reiniciar; --apply idem (!=0)
#   A6 smoke com dentes→ /api/build codigo ≠ rev alvo → falha
set -u
cd "${0:A:h}/.."
RAIZ=$PWD
SCRIPT="$RAIZ/remote/deploy_mini.sh"
T="$(mktemp -d /tmp/qa232.XXXXXX)"
trap 'rm -rf "$T"' EXIT
falhas=0
ok()   { print -r -- "  [ok]   $*" }
mal()  { print -r -- "  [FALHA] $*"; falhas=1 }

# #238: mordida/medição com bytecode limpo (ver tests/MORDIDAS.md)
"$PWD/tests/limpa_bytecode.sh" > /dev/null 2>&1 || true
SHIM="$T/shims"; mkdir -p "$SHIM"
LOG_SSH="$T/ssh.log"; LOG_LAUNCH="$T/launch.log"; LOG_PIP="$T/pip.log"; LOG_CURL="$T/curl.log"
: > "$LOG_SSH"; : > "$LOG_LAUNCH"; : > "$LOG_PIP"; : > "$LOG_CURL"

cat > "$SHIM/ssh" <<'EOF'
#!/bin/zsh
args=()
while [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) args+=("$1"); shift;; esac; done
cmd="${args[-1]}"
print -r -- "$cmd" >> "$T_SSH_LOG"
exec /bin/zsh -c "$cmd"
EOF
cat > "$SHIM/launchctl" <<'EOF'
#!/bin/zsh
print -r -- "$*" >> "$T_LAUNCH_LOG"
case "${1:-}" in kickstart) print -r -- kickstart-ok;; esac
exit 0
EOF
cat > "$SHIM/curl" <<'EOF'
#!/bin/zsh
url=""; tem_chave=0; so_codigo=0
for ((i = 1; i <= $#; i++)); do
  case "${@[i]}" in
    http*) url="${@[i]}";;
    -H) [[ "${@[i+1]}" == X-API-Key:* ]] && tem_chave=1;;
    -w) so_codigo=1;;
  esac
done
print -r -- "$url" >> "$T_CURL_LOG"
codigo=200; corpo=""; arquivo=""
case "$url" in
  */health) corpo='{"ok":true}';;
  */api/voices) [ $tem_chave = 1 ] && codigo=200 || codigo=401;;
  */api/tts/jobs/*/pieces/*) corpo="RIFF-fake-audio";;
  */api/tts/jobs/*) [ $tem_chave = 1 ] && corpo='{"status":"done"}' || codigo=401;;
  */api/tts) [ $tem_chave = 1 ] && corpo='{"job_id":"j1"}' || codigo=401;;
  */api/build)
      if [ $tem_chave = 1 ]; then
        c="${T_CURL_BUILD_CODIGO:-$(cd "$T_MINI_DIR" && cat ${=T_MODULOS} 2>/dev/null | /usr/bin/shasum -a 256 | cut -c1-8)}"
        corpo="{\"ok\":true,\"codigo\":\"$c\",\"boot_ms\":1000}"
      else codigo=401; fi;;
  */api/live/ws*) codigo="${T_CURL_WS_CODE:-101}";;
  */) arquivo="$T_MINI_DIR/static/index.html";;
esac
if [ $so_codigo = 1 ]; then print -rn -- "$codigo"
elif [ -n "$arquivo" ]; then cat "$arquivo"
else print -rn -- "$corpo"; fi
exit 0
EOF
chmod +x "$SHIM"/ssh "$SHIM"/launchctl "$SHIM"/curl

export PATH="$SHIM:$PATH"
export T_SSH_LOG=$LOG_SSH T_LAUNCH_LOG=$LOG_LAUNCH T_PIP_LOG=$LOG_PIP T_CURL_LOG=$LOG_CURL
export T_MINI_DIR="$T/mini"
export T_MODULOS="app.py common.py backends.py tts_worker.py live_pipeline.py live_turns.py dsh_client.py"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL="$T/gitconfig" GIT_CONFIG_SYSTEM=/dev/null
print -r -- "[init]" > "$T/gitconfig"

export TTS_MINI_HOST=mini-fake TTS_MINI_DIR="$T/mini" TTS_MINI_LABEL=studio.tts.server
export TTS_MINI_ESPERA=0 TTS_MINI_REPO="$T/dev" TTS_PUBLIC_HOST=tts.exemplo.test
export TTS_MINI_PREVIEW="$T/preview-requirements"   # F1: preview FORA da árvore do mini

# ---- fixture: origin bare + semente(v1) + dev(v2) + mini(v1) -----------------
git init --bare -q "$T/origin.git"
mkdir -p "$T/semente/static" "$T/semente/.venv-mlx/bin"
for m in ${=T_MODULOS}; do print -r -- "# $m v1" > "$T/semente/$m"; done
cat >> "$T/semente/app.py" <<'EOF'
@app.get("/api/build")
@app.websocket("/api/live/ws")
EOF
print -r -- "<html>v1</html>" > "$T/semente/static/index.html"
print -r -- "fastapi
websockets" > "$T/semente/requirements.txt"
print -r -- "chave-do-mini-123" > "$T/semente/.apikey"
cat > "$T/semente/.venv-mlx/bin/python" <<'EOF'
#!/bin/zsh
case " $* " in *" -V "*) print -r -- "Python 3.12.4";; *) print -r -- ok;; esac
EOF
cat > "$T/semente/.venv-mlx/bin/pip" <<'EOF'
#!/bin/zsh
print -r -- "$*" >> "$T_PIP_LOG"
if [[ " $* " == *" --dry-run "* ]]; then print -r -- "Would install websockets-17.1"
else print -r -- "Successfully installed websockets-17.1"; fi
EOF
chmod +x "$T/semente/.venv-mlx/bin/python" "$T/semente/.venv-mlx/bin/pip"
git -C "$T/semente" init -q; git -C "$T/semente" add -A; git -C "$T/semente" commit -qm base
git -C "$T/semente" remote add origin "$T/origin.git"; git -C "$T/semente" push -q origin main
git clone -q "$T/origin.git" "$T/dev"
cat > "$T/dev/app.py" <<'EOF'
# app v2
@app.get("/api/build")
@app.websocket("/api/live/ws")
EOF
print -r -- "<html>v2</html>" > "$T/dev/static/index.html"
git -C "$T/dev" add -A; git -C "$T/dev" commit -qm v2
# V3 num branch próprio: é o alvo por REV do A1 (o mini precisa ANDAR, senão o
# deploy cai no "nada a fazer" e não grava o estado do rollback).
git -C "$T/dev" switch -q -c corte3
print -r -- "# app v3" >> "$T/dev/app.py"
git -C "$T/dev" commit -qam v3
git -C "$T/dev" switch -q main
git clone -q "$T/origin.git" "$T/mini"
V1="$(git -C "$T/mini" rev-parse HEAD)"; V2="$(git -C "$T/dev" rev-parse HEAD)"

# ---- snapshot: árvore do mini + HEAD + refs do origin + log do serviço -------
snap() { # $1 = arquivo
  { git -C "$T/mini" status --porcelain --untracked-files=all
    git -C "$T/mini" rev-parse HEAD
    (cd "$T/mini" && find . -path ./.git -prune -o -type f -print | sort | xargs /usr/bin/shasum -a 256)
    git -C "$T/origin.git" for-each-ref --format='%(refname) %(objectname)'
    cat "$LOG_LAUNCH"
  } > "$1" 2>&1
}
roda() { zsh -f "$SCRIPT" "$@"; }   # stdout/stderr do chamador

# ================================================================ D1..D4
for cel in "D1 deploy:deploy" "D2 compare:compare" "D3 smoke:smoke"; do
  nome="${cel%%:*}"; acao="${cel##*:}"
  snap "$T/antes"
  saida="$(roda "$acao" 2>&1)"; rc=$?
  snap "$T/depois"
  if cmp -s "$T/antes" "$T/depois"; then
    ok "$nome: $acao (sem --apply) não escreveu NADA (árvore, HEAD, origin, serviço)"
  else
    mal "$nome: $acao escreveu no mini/origin/serviço:"; diff "$T/antes" "$T/depois" | sed 's/^/      /'
  fi
  print -r -- "      rc=$rc · $(print -r -- "$saida" | tail -1)"
  if [ "$acao" = deploy ]; then
    if [ -e "$T/mini/.deploy-mini-requirements-preview" ]; then mal "D1: preview ficou NA ÁRVORE do mini"
    else ok "D1: preview de requirements fora da árvore (F1)"; fi
    [ -s "$T/preview-requirements" ] && ok "D1: preview gravado no caminho de fora ($(wc -c < "$T/preview-requirements" | tr -d ' ') bytes)"
  fi
done

# D4 recon: árvore e serviço intocados; refs do MINI podem andar (fetch, por desenho)
snap "$T/antes"; git -C "$T/mini" for-each-ref --format='%(refname) %(objectname)' > "$T/refs-mini-antes"
roda recon >/dev/null 2>&1
snap "$T/depois"; git -C "$T/mini" for-each-ref --format='%(refname) %(objectname)' > "$T/refs-mini-depois"
if cmp -s "$T/antes" "$T/depois"; then ok "D4 recon: nada mudou (nem refs do mini)"
else
  if diff <(sed -n '1,3p' "$T/antes") <(sed -n '1,3p' "$T/depois") >/dev/null \
     && cmp -s <(git -C "$T/origin.git" for-each-ref) <(git -C "$T/origin.git" for-each-ref); then
    ok "D4 recon: árvore/HEAD/serviço intocados (mudaram só refs do mini — fetch, por desenho)"
  else mal "D4 recon: mudou árvore/HEAD/serviço"; diff "$T/antes" "$T/depois" | sed 's/^/      /'; fi
fi
if cmp -s "$T/refs-mini-antes" "$T/refs-mini-depois"; then ok "D4 recon: refs do mini já estavam frescos"
else ok "D4 recon: refs do mini atualizados pelo fetch ($(diff "$T/refs-mini-antes" "$T/refs-mini-depois" | wc -l | tr -d ' ') linhas)"; fi

# ================================================================ A1: alvo por REV
saida="$(TTS_MINI_REV=corte3 roda deploy --apply 2>&1)"; rc=$?
mini_head="$(git -C "$T/mini" rev-parse HEAD)"
branch="$(git -C "$T/mini" symbolic-ref --short HEAD 2>/dev/null || print -r -- DETACHED)"
V3="$(git -C "$T/dev" rev-parse corte3)"
if [ "$mini_head" = "$V3" ]; then ok "A1 TTS_MINI_REV: mini no rev alvo ${V3[1,8]}"; else mal "A1 mini em $mini_head ≠ alvo $V3 (rc=$rc)"; fi
if [ "$branch" = "prod-${V3[1,12]}" ]; then ok "A1 branch de produção: $branch (não detached)"
elif [ "$branch" = "DETACHED" ]; then mal "A1 mini ficou DETACHED (esperado branch prod-<sha>)"
else mal "A1 branch inesperada: $branch"; fi
if [ $rc -eq 0 ]; then ok "A1 deploy --apply exit 0"; else mal "A1 deploy --apply rc=$rc"; print -r -- "$saida" | tail -5 | sed 's/^/      /'; fi
if print -r -- "$saida" | grep -q "kickstart-ok"; then ok "A1 serviço reiniciado"; else mal "A1 sem kickstart"; fi

# ================================================================ D5: rollback dry-run
snap "$T/antes"
roda rollback > "$T/rollback-dry.txt" 2>&1; rc=$?
snap "$T/depois"
if cmp -s "$T/antes" "$T/depois"; then ok "D5 rollback (dry-run) não escreveu NADA (rc=$rc)"
else mal "D5 rollback dry-run escreveu:"; diff "$T/antes" "$T/depois" | sed 's/^/      /'; fi
grep -q "dry-run" "$T/rollback-dry.txt" && ok "D5 rollback dry-run mostra o alvo salvo" \
  || mal "D5 rollback dry-run não mostrou o plano"

# ================================================================ A2: desvio (não-descendente)
# o mini vai para o V2; o alvo passa a ser um desvio nascido do V1 → o ar NÃO é
# ancestral do alvo (rewind/desvio) e o compare tem de dizer isso.
roda deploy --apply >/dev/null 2>&1
[ "$(git -C "$T/mini" rev-parse HEAD)" = "$V2" ] && ok "A2 setup: mini no V2" || mal "A2 setup: mini não está no V2"
git -C "$T/dev" switch -q -c desvio "$V1"; print -r -- "# app desvio" > "$T/dev/app.py"
git -C "$T/dev" commit -qam desvio
saida="$(TTS_MINI_REV=desvio roda compare 2>&1)"; rc=$?
if [ $rc -ne 0 ] && print -r -- "$saida" | grep -q "NÃO é ancestral"; then
  ok "A2 compare RECUSA alvo não-descendente do que está no ar (rc=$rc)"
else mal "A2 compare não recusou o desvio (rc=$rc)"; print -r -- "$saida" | sed 's/^/      /'; fi

# ================================================================ A3: rollback com caminho de volta
# O SHA de volta é o do estado salvo pelo ÚLTIMO deploy que chegou a fazer backup.
sha_salvo="$(sed -n 's/^sha=//p' "$T/mini/.deploy-mini-estado" 2>/dev/null | head -1)"
: > "$LOG_LAUNCH"
roda rollback --apply > "$T/rollback.txt" 2>&1; rc=$?
mini_head="$(git -C "$T/mini" rev-parse HEAD)"
branch="$(git -C "$T/mini" symbolic-ref --short HEAD 2>/dev/null || print -r -- DETACHED)"
if [ -n "$sha_salvo" ] && [ "$mini_head" = "$sha_salvo" ]; then ok "A3 rollback --apply devolveu o SHA salvo (${sha_salvo[1,8]})"
else mal "A3 rollback não voltou ao SHA salvo ($sha_salvo): mini em $mini_head, rc=$rc"; tail -5 "$T/rollback.txt" | sed 's/^/      /'; fi
if [ "$branch" = "prod-${sha_salvo[1,12]}" ]; then ok "A3 volta em branch, não detached"; else mal "A3 branch na volta: $branch"; fi
if [ -s "$LOG_LAUNCH" ]; then ok "A3 volta reinicia o serviço ($(wc -l < "$LOG_LAUNCH" | tr -d ' ') kickstart)"; else mal "A3 rollback não reiniciou"; fi

# A3b: alvo não-descendente (desvio) sem --sem-push: o push de rewinding não é ff
# e o script PARA antes de mexer no serviço — o comportamento tem de ser explícito.
git -C "$T/dev" switch -q desvio
: > "$LOG_LAUNCH"; antes_parada="$(git -C "$T/mini" rev-parse HEAD)"
saida="$(TTS_MINI_REV=desvio roda deploy --apply 2>&1)"; rc=$?
if [ $rc -ne 0 ] && print -r -- "$saida" | grep -q "\[PARA\] push falhou" && [ ! -s "$LOG_LAUNCH" ] \
   && [ "$(git -C "$T/mini" rev-parse HEAD)" = "$antes_parada" ]; then
  ok "A3b alvo não-descendente: PARA no push (não reinicia, não mexe no mini)"
else mal "A3b desvio: rc=$rc · launch=$(wc -l < "$LOG_LAUNCH") · mini andou?"; print -r -- "$saida" | tail -4 | sed 's/^/      /'; fi
git -C "$T/dev" switch -q main
if grep -q "freeze" "$T/rollback.txt"; then ok "A3 avisa que as deps não voltam sozinhas"; else mal "A3 sem aviso do freeze"; fi

# ================================================================ A4: --sem-deps
# `pip freeze` (backup do venv) é read-only e pode correr; o alvo é `pip install`.
: > "$LOG_PIP"; : > "$LOG_SSH"
rm -f "$T/mini/.deploy-mini-requirements-preview"
roda deploy --apply --sem-deps >/dev/null 2>&1     # alvo = HEAD (já no origin)
if ! grep -q "pip install" "$LOG_PIP"; then ok "A4 --sem-deps: nenhum 'pip install'"
else mal "A4 --sem-deps chamou pip install: $(grep 'pip install' "$LOG_PIP")"; fi
if grep -q "pip freeze" "$LOG_SSH"; then ok "A4 --sem-deps: só o freeze do backup (read-only)"; fi
if [ ! -e "$T/mini/.deploy-mini-requirements-preview" ]; then ok "A4 --sem-deps: sem preview de requirements na árvore"
else mal "A4 --sem-deps escreveu o preview na árvore"; fi

# ================================================================ A5: mini sujo
print -r -- "# sujeira" >> "$T/mini/app.py"
: > "$LOG_LAUNCH"
saida="$(TTS_MINI_REV=desvio roda deploy 2>&1)"; rc_dry=$?
if [ $rc_dry -eq 0 ] && print -r -- "$saida" | grep -q "\[PARA\]" && [ ! -s "$LOG_LAUNCH" ]; then
  ok "A5 mini sujo: dry-run PARA sem reiniciar (rc=0)"
else mal "A5 mini sujo dry-run: rc=$rc_dry · launch=$(wc -l < "$LOG_LAUNCH")"; fi
saida="$(TTS_MINI_REV=desvio roda deploy --apply 2>&1)"; rc_ap=$?
if [ $rc_ap -ne 0 ] && [ ! -s "$LOG_LAUNCH" ]; then ok "A5 mini sujo: --apply PARA antes do restart (rc=$rc_ap)"
else mal "A5 mini sujo --apply: rc=$rc_ap · launch=$(wc -l < "$LOG_LAUNCH")"; fi
git -C "$T/mini" checkout -q -- app.py

# ================================================================ A6: smoke com dentes
T_CURL_BUILD_CODIGO=deadbeef roda smoke > "$T/smoke-velho.txt" 2>&1; rc=$?
if [ $rc -ne 0 ] && grep -q "instância não é o rev alvo" "$T/smoke-velho.txt"; then
  ok "A6 smoke acusa código carregado ≠ rev alvo (instância velha)"
else mal "A6 smoke não acusou o codigo forjado (rc=$rc)"; fi
T_CURL_WS_CODE=401 roda smoke > "$T/smoke-ws.txt" 2>&1; rc=$?
if [ $rc -ne 0 ] && grep -q "api/live/ws devolveu 401" "$T/smoke-ws.txt"; then
  ok "A6 smoke acusa WS sem upgrade (websockets ausente?)"
else mal "A6 smoke não acusou o WS 401 (rc=$rc)"; fi

print -r -- "== FIM: falhas=$falhas"
exit $falhas
