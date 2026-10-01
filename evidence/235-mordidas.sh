#!/bin/zsh
# Mordida dos três itens do #235: nonce na paridade do index, presença de rota
# decidida pelo REV ALVO e rc fiel. Método do gate: reintroduz a forma quebrada,
# roda o teste específico, restaura. Sem `pipestatus` (a ferramenta roda sh).
#
#   ./evidence/235-mordidas.sh
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
PY=./.venv-mlx/bin/python
S=remote/deploy_mini.sh
BAK=$(mktemp)
cp "$S" "$BAK"

morde() { # $1 = rótulo, $2 = patch (python inline), $3 = teste
  python3 -c "$2"
  local saida rc
  print -r -- "== $1"
  print -r -- "   patch aplicado: $(git diff --numstat -- $S | awk '{print $1"+/"$2"-"}')"
  # sem pipe no $( ): o rc tem de ser o do pytest
  saida=$($PY -m pytest "$3" -q 2>&1)
  rc=$?
  saida=${saida##*$'\n'}
  print -r -- "   teste: $3"
  print -r -- "   resultado: $saida"
  cp "$BAK" "$S"
  if [ $rc -eq 0 ]; then
    print -r -- "   [ATENÇÃO] NÃO mordeu (passou com a forma quebrada)"
  else
    print -r -- "   [ok] mordeu (falhou, rc=$rc)"
  fi
}

# item 1: paridade do index volta a comparar byte a byte (nonce do CSP no meio)
morde "1 — paridade do index SEM normalizar o nonce" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
n = s.replace("curl -s -m 10 \"$base/\" 2>/dev/null | sem_nonce | shasum",
              "curl -s -m 10 \"$base/\" 2>/dev/null | shasum")
assert n != s, "patch 1 não casou"
p.write_text(n)' \
  tests/test_deploy_mini_sh.py::test_deploy_apply_publica_puxa_instala_reinicia_e_smoke

# item 2: presença de rota passa a ser decidida pelo HEAD em vez do REV ALVO
morde "2 — rota decidida pelo HEAD (não pelo rev alvo)" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
n = s.replace("tem_rota() { git -C \"$LOCAL_REPO\" show \"${1}:app.py\" 2>/dev/null | grep -qF \"$2\"; }",
              "tem_rota() { git -C \"$LOCAL_REPO\" show \"HEAD:app.py\" 2>/dev/null | grep -qF \"$2\"; }")
assert n != s, "patch 2 não casou"
p.write_text(n)' \
  tests/test_deploy_mini_sh.py::test_smoke_nao_exige_rota_ausente_no_rev_alvo

# item 3: rc deixa de ser fiel (smoke falho não derruba mais)
morde "3 — rc sempre 0 (ponto inesperado não derruba)" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
n = s.replace("exit $falhas", "exit 0")
assert n != s, "patch 3 não casou"
p.write_text(n)' \
  tests/test_deploy_mini_sh.py::test_rc_fiel_esperado_para_o_alvo_nao_derruba_e_inesperado_derruba

rm -f "$BAK"
if git diff --quiet -- "$S"; then print -r -- "árvore do script restaurada (byte a byte)"
else print -r -- "[ATENÇÃO] o script ficou DIFERENTE do backup — restaure antes de confiar na suíte"
fi
