#!/usr/bin/env bash
# GATE #215 item 3 (#210) — MORDIDA da adoção do `session_id` do cliente.
#
# O item não tem costura em processo: a linha vive DENTRO de `live_ws` (`sid = pedido
# or uuid...`), então a mutação é textual no `app.py`, como o `live_lock_freeze.sh`
# faz para o #209. Aqui a árvore é editada e RESTAURADA por patch inverso (o md5
# antes/depois é conferido): se um terceiro editar o app.py na janela, o hunk dele
# sobrevive — o mesmo cuidado do script irmão.
#
#   cena 1 — `app.py` COMO ESTÁ (o fix):  `evidence/215-ws.py --somente=item3` tem de sair 0
#   cena 2 — linha do #210 REVERTIDA (pré-fix): tem de sair 1
#
# Uso: ./evidence/215-210-mutacao.sh
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
APP="$RAIZ/app.py"
cd "$RAIZ" || exit 1

FIX='    sid = pedido or uuid.uuid4().hex[:10]'
PRE='    sid = cfg["session_id"] if retomado else uuid.uuid4().hex[:10]'

md5_de() { md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }
ANTES="$(md5_de "$APP")"
_restaurado=0

patch() {   # $1 = fix|pre  (alvo -> texto)
  "$PY" - "$1" <<'PYM'
import pathlib, sys
p = pathlib.Path("app.py"); s = p.read_text()
FIX = '    sid = pedido or uuid.uuid4().hex[:10]'
PRE = '    sid = cfg["session_id"] if retomado else uuid.uuid4().hex[:10]'
de, para = (FIX, PRE) if sys.argv[1] == "pre" else (PRE, FIX)
if s.count(de) != 1:
    sys.exit(f"linha do #210 não casou 1x (achei {s.count(de)}) indo para {sys.argv[1]}")
p.write_text(s.replace(de, para))
PYM
}

restaura() {
  [ "$_restaurado" = 1 ] && return 0
  _restaurado=1
  patch fix || echo "  ⚠ restauro por patch falhou — confira o app.py"
  if [ "$(md5_de "$APP")" = "$ANTES" ]; then
    echo "  ✔ app.py restaurado byte a byte (md5 $ANTES)"
  else
    echo "  ⚠ app.py != original: outro agente editou durante a medida"
  fi
}
trap 'restaura' EXIT

echo "══ cena 1 — app.py COMO ESTÁ (o fix): tem de sair 0"
PYTHONPATH=. "$PY" evidence/215-ws.py --somente=item3 2>&1 | grep -Ev "^1[0-9]:" | tail -12
rc_fix=${PIPESTATUS[0]}
echo "══ cena 1 saiu $rc_fix"

echo
echo "══ revertendo a linha do #210 (pré-fix: só adota se JÁ retomou)"
patch pre || { echo "  ✘ revert falhou"; exit 1; }
echo
echo "══ cena 2 — app.py REVERTIDO: tem de sair 1"
PYTHONPATH=. "$PY" evidence/215-ws.py --somente=item3 2>&1 | grep -Ev "^1[0-9]:" | tail -14
rc_pre=${PIPESTATUS[0]}
echo "══ cena 2 saiu $rc_pre"
echo

restaura
rc=0
[ "$rc_fix" != 0 ] && { echo "  ✘ no estado do FIX devia sair 0 e saiu $rc_fix"; rc=1; }
[ "$rc_pre" = 0 ] && { echo "  ✘ NÃO MORDEU: no pré-fix devia sair 1 e saiu 0"; rc=1; }
[ "$rc" = 0 ] && echo "✔ mordida confirmada (fix 0 · pré-fix $rc_pre)" \
              || echo "✘ mordida NÃO confirmada"
exit $rc