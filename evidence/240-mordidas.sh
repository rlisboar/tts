#!/bin/zsh
# Mordida do #240: preview de deps em caminho FIXO em /tmp volta a falso-vermelhar
# sob execuções paralelas. Reintroduz a forma quebrada, roda o teste, restaura.
# Sem `pipestatus` (a ferramenta roda sh).
#
#   ./evidence/240-mordidas.sh
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
PY=./.venv-mlx/bin/python
S=remote/deploy_mini.sh
BAK=$(mktemp)
cp "$S" "$BAK"

morde() { # $1 = rótulo, $2 = patch, $3 = teste
  python3 -c "$2"
  local saida rc
  saida=$($PY -m pytest "$3" -q 2>&1)
  rc=$?
  saida=${saida##*$'\n'}
  print -r -- "== $1"
  print -r -- "   teste: $3"
  print -r -- "   resultado: $saida"
  cp "$BAK" "$S"
  if [ $rc -eq 0 ]; then
    print -r -- "   [ATENÇÃO] NÃO mordeu (passou com a forma quebrada)"
  else
    print -r -- "   [ok] mordeu (falhou, rc=$rc)"
  fi
}

morde "1 — default volta a ser caminho FIXO (colide entre execuções)" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
n = s.replace("TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview-$$",
              "TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview")
assert n != s, "patch não casou"
p.write_text(n)' \
  tests/test_deploy_mini_sh.py::test_dois_deploys_em_paralelo_nao_disputam_o_preview

morde "2 — default fixo: asserção estática também pega" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
n = s.replace("TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview-$$",
              "TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview")
assert n != s, "patch não casou"
p.write_text(n)' \
  tests/test_deploy_mini_sh.py::test_preview_default_e_unico_por_execucao_e_fora_da_arvore

morde "3 — preview volta para dentro da árvore do mini (F1 do #232)" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
n = s.replace("TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview-$$",
              "TTS_MINI_PREVIEW:-.deploy-mini-requirements-preview")
assert n != s, "patch não casou"
p.write_text(n)' \
  tests/test_deploy_mini_sh.py::test_deploy_dry_run_nao_toca_em_nada

# contra o BACKUP (e não contra o HEAD): o script pode ter trabalho não commitado
if cmp -s "$S" "$BAK"; then print -r -- "script restaurado byte a byte (igual ao backup)"
else print -r -- "[ATENÇÃO] o script ficou DIFERENTE do backup — restaure antes de confiar na suíte"; fi
rm -f "$BAK"
