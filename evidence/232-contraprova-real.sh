#!/bin/zsh
# GATE #232 — contraprova CONTRA A PRODUÇÃO (Mac mini .34), read-only.
# Prova o requisito 1 no alvo real: recon/compare/smoke/deploy-dry não podem
# escrever na árvore do mini, no origin nem derrubar o serviço.
# (o `smoke` com TTS real é rodado à parte: ele CRIA job → artefatos em outputs/.)
#
# Células: R1 recon · R2 compare · R3 smoke --sem-tts · R4 deploy (dry-run)
# Snapshot por célula: HEAD + status + refs + sha256 de TODOS os arquivos da
# árvore + agente launchd + pid/uptime do listener na 7860.
set -u
cd "${0:A:h}/.."
SSH=(ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=8 lisboa@192.168.15.34)
falhas=0
ok()  { print -r -- "  [ok]   $*" }
mal() { print -r -- "  [FALHA] $*"; falhas=1 }

snap() { # $1 = destino
  "${SSH[@]}" 'cd ~/Documents/tts-rod && { git rev-parse HEAD
    git status --porcelain --untracked-files=all
    git for-each-ref --format="REF %(objectname) %(refname)"
    find . -path ./.git -prune -o -path ./outputs -prune -o -type f -print0 | sort -z | xargs -0 shasum -a 256 2>/dev/null
    launchctl list | grep -i studio.tts.server
    lsof -nP -iTCP:7860 -sTCP:LISTEN 2>/dev/null | tail -1; }' > "$1" 2>&1
}

celula() { # $1 = nome · $2 = refs (0 = refs podem andar; 1 = não podem) · resto = comando
  local nome="$1" refs_fixas="$2"; shift 2
  snap "$T/antes"
  "$@" > "$T/saida" 2>&1; local rc=$?
  cp "$T/saida" "evidence/232-real-${nome// /_}.txt" 2>/dev/null
  snap "$T/depois"
  if [ "$refs_fixas" = 0 ]; then
    grep -v '^REF ' "$T/antes" > "$T/antes.f"; grep -v '^REF ' "$T/depois" > "$T/depois.f"
    if cmp -s "$T/antes.f" "$T/depois.f"; then
      ok "$nome: árvore/HEAD/serviço intocados · rc=$rc (refs do mini podem ter andado: fetch, por desenho)"
      diff <(grep '^REF ' "$T/antes") <(grep '^REF ' "$T/depois") | grep '^[<>]' | head -4 | sed 's/^/      ref: /'
    else
      mal "$nome: mudou no mini (fora de refs):"; diff "$T/antes.f" "$T/depois.f" | head -8 | sed 's/^/      /'
    fi
  elif cmp -s "$T/antes" "$T/depois"; then
    ok "$nome: nada mudou no mini (árvore/HEAD/refs/serviço) · rc=$rc"
  else
    mal "$nome: mudou no mini:"; diff "$T/antes" "$T/depois" | grep -vE '^[0-9,]+[acd][0-9,]+$' | head -8 | sed 's/^/      /'
  fi
  print -r -- "      última linha: $(tail -1 "$T/saida")"
}

T="$(mktemp -d /tmp/qa232real.XXXXXX)"
trap 'rm -rf "$T"' EXIT
print -r -- "== contraprova REAL em lisboa@192.168.15.34 — $(date -u +%FT%TZ)"
snap "$T/base"
print -r -- "  estado de partida: $(sed -n '1p' "$T/base") · arquivos na árvore: $(grep -c '^[0-9a-f]\{64\} ' "$T/base")"
print -r -- "  (outputs/ fora do hash: produção é USADA e um job mexe nele)"

celula "R1 recon" 0 ./remote/deploy_mini.sh recon
celula "R2 compare" 1 ./remote/deploy_mini.sh compare
celula "R3 smoke --sem-tts" 1 ./remote/deploy_mini.sh smoke --sem-tts
celula "R4 deploy dry-run" 1 ./remote/deploy_mini.sh deploy
# F1: o dry-run do `deploy` grava/atualiza o preview de requirements NA ÁRVORE do
# mini (untracked). Aqui a prova: o arquivo existe e tem o conteúdo do rev alvo.
if "${SSH[@]}" 'ls /tmp/deploy-mini-requirements-preview* >/dev/null 2>&1 && echo tem' | grep -q tem; then
  ok "R4 grava o preview FORA da árvore (/tmp no mini) — F1 corrigido"
else mal "R4: preview ausente em /tmp no mini"; fi
# o preview tem nome único por execução (#240): confere que NENHUM ficou na árvore
residuo="$("${SSH[@]}" "cd ~/Documents/tts-rod && git status --porcelain | grep -c deploy-mini-requirements-preview")"
if [ "${residuo:-1}" = 0 ]; then
  ok "nenhum preview na ÁRVORE do mini"
else
  mal "resíduo de preview na árvore do mini ($residuo)"
fi

# R5: smoke COM a síntese real — o POST cria job em produção, então a árvore MUDA
# (outputs/) por desenho. Aqui só se prova que o caminho texto→job→peça funciona.
print -r -- "== R5 smoke com TTS real (a árvore muda: job em outputs/)"
./remote/deploy_mini.sh smoke > "evidence/232-real-R5_smoke_tts.txt" 2>&1; rc=$?
if grep -q "POST /api/tts real: job" "evidence/232-real-R5_smoke_tts.txt"; then
  ok "R5 POST /api/tts real concluiu com peça (rc=$rc)"
else mal "R5 POST /api/tts real não concluiu (rc=$rc)"; grep -E "\[FALHA\]" "evidence/232-real-R5_smoke_tts.txt" | head -5 | sed 's/^/      /'; fi

print -r -- "== FIM: falhas=$falhas"
exit $falhas
