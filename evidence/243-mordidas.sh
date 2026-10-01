#!/bin/zsh
# Mordida do #243: a varredura estática tem de PEGAR a volta de um caminho fixo.
# Reintroduz a forma quebrada em três arquivos, roda o teste da varredura, restaura.
#   ./evidence/243-mordidas.sh
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
PY=./.venv-mlx/bin/python
T=tests/test_tmp_unico.py
morde() { # $1 = rótulo, $2 = patch (python inline)
  local bak; bak=$(mktemp)
  local alvo; alvo=$(print -r -- "$2" | grep -oE "[A-Za-z0-9_/.-]+\.(sh|py)" | head -1)
  cp "$alvo" "$bak"
  python3 -c "$2"
  local saida rc
  saida=$($PY -m pytest "$T" -q 2>&1); rc=$?
  saida=${saida##*$'\n'}
  print -r -- "== $1"
  print -r -- "   alvo: $alvo · teste: $T"
  print -r -- "   resultado: $saida"
  cp "$bak" "$alvo"; rm -f "$bak"
  if [ $rc -eq 0 ]; then print -r -- "   [ATENÇÃO] NÃO mordeu"; else print -r -- "   [ok] mordeu (falhou, rc=$rc)"; fi
}

morde "1 — 202-gate.sh perde o RUN (backup de settings volta a ser global)" '
from pathlib import Path
p = Path("evidence/202-gate.sh"); s = p.read_text()
n = s.replace("RUN=\"$$\"", "").replace("/tmp/202-settings-$RUN.bak", "/tmp/202-settings.bak")
assert n != s
p.write_text(n)'

morde "2 — 246-verifica.sh volta ao diretório compartilhado" '
from pathlib import Path
p = Path("evidence/246-verifica.sh"); s = p.read_text()
n = s.replace("/tmp/246-$RUN", "/tmp/246")
assert n != s, "patch 2 nao casou"
p.write_text(n)'

morde "3 — deploy_mini.sh perde o sufixo do preview (o caso do #240)" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
n = s.replace("deploy-mini-requirements-preview-$$", "deploy-mini-requirements-preview")
assert n != s
p.write_text(n)'
