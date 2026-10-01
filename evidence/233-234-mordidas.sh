#!/bin/zsh
# Mordida dos testes novos (F1 do #233; F2/F3/F4 e os dois buracos do smoke no #234).
# Método do gate: reintroduz a forma quebrada, roda o teste específico, restaura.
#
#   ./evidence/233-234-mordidas.sh
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
PY=./.venv-mlx/bin/python
S=remote/deploy_mini.sh
BAK=$(mktemp)
cp "$S" "$BAK"

morde() { # $1 = rótulo, $2 = patch (python inline), $3 = teste
  python3 -c "$2"
  local saida rc
  # sem pipe: `pipestatus` se perde dentro de $( ) e o rc viria do tail
  saida=$($PY -m pytest "$3" -q 2>&1)
  rc=$?
  saida=${saida##*$'\n'}
  print -r -- "== $1"
  print -r -- "   teste: $3"
  print -r -- "   resultado: $saida"
  cp "$BAK" "$S"
  if git diff --quiet -- "$S"; then print -r -- "   (nada restaurado? diff limpo — árvore já era igual)"; fi
  if [ $rc -eq 0 ]; then print -r -- "   [ATENÇÃO] o teste NÃO mordeu (passou com a forma quebrada)"; else print -r -- "   [ok] mordeu (falhou)"; fi
}

# F1 (#233): preview volta para dentro da árvore do mini
morde "F1 — preview de deps na árvore do mini" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
s = s.replace("REQ_PREVIEW=\"${TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview}\"",
              "REQ_PREVIEW=\"${TTS_MINI_PREVIEW:-.deploy-mini-requirements-preview}\"")
p.write_text(s)' \
  tests/test_deploy_mini_sh.py::test_deploy_mostra_o_delta_de_deps_do_rev_alvo_e_instala

# F2 (#234): alvo fixo no HEAD
morde "F2 — TTS_MINI_REV ignorado (alvo fixo em HEAD)" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
s = s.replace("ALVO_REV=\"${TTS_MINI_REV:-HEAD}\"", "ALVO_REV=\"HEAD\"")
p.write_text(s)' \
  tests/test_deploy_mini_sh.py::test_alvo_por_rev_sobe_o_corte_e_nao_o_head

# F3 (#234): switch --detach em vez de branch prod-<sha>
morde "F3 — mini termina DETACHED" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
s = s.replace("git switch --quiet -C '\''prod-${ALVO_SHA[1,12]}'\'' '\''$ALVO_SHA'\''",
              "git checkout --quiet --detach '\''$ALVO_SHA'\''")
p.write_text(s)' \
  tests/test_deploy_mini_sh.py::test_mini_termina_em_branch_prod_e_nao_detached

# F4 (#234): compare sem a checagem de ancestralidade
morde "F4 — compare sem merge-base (não-descendente passa batido)" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
s = s.replace("""    if git -C \"$LOCAL_REPO\" merge-base --is-ancestor \"$no_ar_sha\" \"$ALVO_SHA\" 2>/dev/null; then
      print -r -- \"  alvo é descendente da produção (subida direta)\"
    else
      print -r -- \"  [aviso] produção NÃO é ancestral do alvo — é rewind/desvio, confira o motivo\"
    fi""", "")
p.write_text(s)' \
  tests/test_deploy_mini_sh.py::test_compare_avisa_alvo_nao_descendente_do_que_esta_no_ar

# buraco do smoke (a): paridade do index sem ignorar o nonce injetado
morde "SMOKE — paridade do index sem tirar o nonce do CSP" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
s = s.replace("curl -s -m 10 \"$base/\" 2>/dev/null | sem_nonce | shasum",
              "curl -s -m 10 \"$base/\" 2>/dev/null | shasum")
p.write_text(s)' \
  tests/test_deploy_mini_sh.py::test_deploy_apply_publica_puxa_instala_reinicia_e_smoke

# buraco do smoke (b): rota ausente no rev alvo vira FALHA
morde "SMOKE — rota ausente no rev alvo tratada como falha" '
from pathlib import Path
p = Path("remote/deploy_mini.sh"); s = p.read_text()
s = s.replace("tem_rota() { git -C \"$LOCAL_REPO\" show \"${1}:app.py\" 2>/dev/null | grep -qF \"$2\"; }",
              "tem_rota() { return 0; }")
p.write_text(s)' \
  tests/test_deploy_mini_sh.py::test_smoke_nao_exige_rota_ausente_no_rev_alvo

rm -f "$BAK"
if git diff --quiet -- "$S"; then print -r -- "árvore do script restaurada (byte a byte)"
else print -r -- "[ATENÇÃO] o script ficou DIFERENTE do backup — restaure antes de confiar na suíte"
fi
