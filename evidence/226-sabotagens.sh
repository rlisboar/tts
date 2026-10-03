#!/usr/bin/env bash
# GATE #226 — sabotagens do par do /api/build (provas do ticket, por fora).
#
# S1a: `app._BUILD_TS_HASH = 0` (passado) → TODO módulo parece "escrito depois do
#      boot": a TOLERÂNCIA é forçada e o par NÃO pode cair (árvore parada).
# S1b: `_BUILD_TS_HASH` no futuro → nenhum mtime o alcança. Com o fix do #242 o
#      skew liga a TOLERÂNCIA para o lado seguro (tudo conta como escrito) e o par
#      passa POR ALI — antes (89d0dd8) a tolerância ficava inerte e o próprio teste
#      novo do #220 caía (`assert "live_turns.py" in escritos`).
# S1c: `_HASH_NO_IMPORT` sabotado → o ramo exato tem de MORDER (cai).
# S2 : par revertido à forma ANTIGA (expectativa recalculada na asserção) com um
#      ESCRITOR de verdade rodando em live_turns.py → tem de cair (é o defeito #220).
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./.venv-mlx/bin/python
TA=tests/test_api.py
AP=app.py
bak_ta="$(mktemp)"; bak_ap="$(mktemp)"
cp "$TA" "$bak_ta"; cp "$AP" "$bak_ap"
restaura() { cp "$bak_ta" "$TA"; cp "$bak_ap" "$AP"; }
trap restaura EXIT INT TERM

roda() { "$PY" -m pytest "$TA" -q -rP -k build 2>&1; }
veredito() { # $1 rótulo · $2 rc · $3 esperado (0 = tem de passar)
  if [ "$3" = 0 ] && [ "$2" != 0 ]; then echo "  [FALHA] $1: caiu (rc=$2) e tinha de passar"
  elif [ "$3" = 1 ] && [ "$2" = 0 ]; then echo "  [FALHA] $1: passou e tinha de cair"
  else echo "  [ok]    $1: rc=$2 (esperado $( [ "$3" = 0 ] && echo pass | tr 'a-z' 'A-Z' || echo FALHAR))"; fi
}

echo "# gate #226 — sabotagens — $(date -u +%FT%TZ)"
echo "  md5 antes: $(md5 -q "$TA" "$AP" | tr '\n' ' ')"

# S1a — tolerância forçada
$PY - "$AP" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
alvo = "_BUILD_TS_HASH = time.time()"
assert s.count(alvo) == 1
p.write_text(s.replace(alvo, "_BUILD_TS_HASH = 0.0   # S1a: tudo parece escrito depois do boot"))
PY
saida="$(roda)"; rc=$?
echo "$saida" | grep -q "\[220\] escritos depois do boot" && echo "  (o par entrou pela TOLERÂNCIA provada)" \
  || echo "  [FALHA] S1a: a tolerância não foi acionada"
veredito "S1a tolerância forçada (TS=0)" "$rc" 0
cp "$bak_ap" "$AP"

# S1b — skew (TS no futuro): a tolerância é ASSUMIDA (#242)
$PY - "$AP" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
alvo = "_BUILD_TS_HASH = time.time()"
assert s.count(alvo) == 1
p.write_text(s.replace(alvo, "_BUILD_TS_HASH = time.time() + 3600   # S1b: skew — nada escrito"))
PY
saida="$(roda)"; rc=$?
echo "$saida" | grep -q "\[220\] escritos depois do boot" && echo "  (skew: o par entrou pela TOLERÂNCIA assumida — #242)" \
  || echo "  [FALHA] S1b: com o skew a tolerância tinha de valer"
veredito "S1b skew: tolerância assumida (TS futuro)" "$rc" 0
cp "$bak_ap" "$AP"

# S1c — expectativa congelada mentindo: o exato tem de morder
$PY - "$TA" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
alvo = "_HASH_NO_IMPORT = _hash_do_snapshot(_SNAPSHOT)"
assert s.count(alvo) == 1
p.write_text(s.replace(alvo, '_HASH_NO_IMPORT = "00000000"   # S1c'))
PY
roda > /dev/null 2>&1; rc=$?
veredito "S1c expectativa sabotada morde" "$rc" 1
cp "$bak_ta" "$TA"

# S2 — mordida do ticket: forma ANTIGA com escritor de verdade
$PY - "$TA" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
ini = s.index("def _confere_que_o_codigo_e_do_boot(")
fim = s.index("\n\n\n@pytest.fixture()", ini)
antiga = '''def _confere_que_o_codigo_e_do_boot(d: dict) -> None:
    """FORMA ANTIGA (sabotagem): expectativa recalculada no instante da asserção."""
    assert d["codigo"] == app._BUILD_CODIGO, "o campo tem de ser o hash do BOOT"
    assert d["codigo"] == _hash_dos_modulos(), "ANTIGA: expectativa == disco de agora"
'''
p.write_text(s[:ini] + antiga + s[fim:])
print("  (par revertido à forma antiga)")
PY
$PY - live_turns.py <<'PY' &
import os, pathlib, sys, time
alvo = pathlib.Path(sys.argv[1]); base = alvo.read_bytes(); tmp = alvo.with_suffix(".226tmp")
fim = time.time() + 40
while time.time() < fim:
    tmp.write_bytes(base + b"\n# terceiro (gate #226)\n"); os.replace(tmp, alvo)
    time.sleep(0.05)
    tmp.write_bytes(base); os.replace(tmp, alvo)
    time.sleep(0.05)
PY
ESCRITOR=$!
sleep 1
roda > /dev/null 2>&1; rc=$?
kill "$ESCRITOR" 2>/dev/null; wait "$ESCRITOR" 2>/dev/null
veredito "S2 forma antiga COM escritor cai" "$rc" 1
restaura

echo "  md5 depois: $(md5 -q "$TA" "$AP" | tr '\n' ' ')"
