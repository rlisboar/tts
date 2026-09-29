#!/usr/bin/env bash
# PROVA DE EFEITO do #220 — um TERCEIRO escrevendo um módulo do build durante a
# rodada não pode derrubar o par de testes do /api/build.
#
# O QUE ELE MONTA: um escritor de verdade (outro PROCESSO, como os colegas agentes)
# alternando o conteúdo de `live_turns.py` a cada 50 ms, com `os.replace` para a
# leitura nunca pegar arquivo pela metade. `live_turns.py` está em `_BUILD_MODULOS`,
# então a árvore viva muda entre o import do app e a asserção — que é exatamente a
# corrida do ticket (medida no gate do frontend em 20:09Z).
#
# AS TRÊS FASES:
#   1. sem escritor  -> o par passa e usa a prova EXATA (expectativa congelada == boot);
#   2. com escritor  -> o par passa IGUAL, agora pela tolerância PROVADA por mtime
#      (e o probe mostra que a asserção ANTIGA era moeda: passa em ~metade das
#      amostras, conforme a fase do escritor);
#   3. mordida       -> sabotada a expectativa congelada, o par TEM de sair 1: sem
#      isso o verde da fase 1 poderia ser só "a asserção nunca foi comparada".
#
# O QUE NÃO PROVA: nada sobre o /api/build em si (isso é o #190/#214) e nada sobre
# a janela de milissegundos entre o import do app e o import do módulo de teste —
# um save ali ainda derruba, e está escrito no cabeçalho do test_api.py.
#
# A ÁRVORE É COMPARTILHADA: enquanto este script roda, o `live_turns.py` fica com
# uma linha de comentário a mais em ~metade do tempo (o trap devolve os bytes
# originais no fim, inclusive em Ctrl-C).
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
ALVO="$RAIZ/live_turns.py"
BAK="$(mktemp -t 220-live_turns.XXXXXX)"
PARAR="$(mktemp -t 220-parar.XXXXXX)"
rm -f "$PARAR"

cp "$ALVO" "$BAK"
_restaura() {
  [ -n "${ESCRITOR:-}" ] && kill "$ESCRITOR" 2>/dev/null
  ESCRITOR=""
  if [ -f "$BAK" ]; then
    cp "$BAK" "$ALVO.220.tmp" && mv "$ALVO.220.tmp" "$ALVO"
    rm -f "$BAK"
  fi
  rm -f "$PARAR"
}
trap '_restaura' EXIT INT TERM

cd "$RAIZ" || exit 1

escreve() {                      # escritor: outro PROCESSO, atômico
  "$PY" - "$ALVO" "$PARAR" <<'PY' &
import os, pathlib, sys, time
alvo, parar = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
base = alvo.read_bytes()
tmp = alvo.with_suffix(".220tmp")
while not parar.exists():
    tmp.write_bytes(base + b"\n# escrito por um TERCEIRO durante a rodada\n")
    os.replace(tmp, alvo)          # atômico: a leitura nunca pega arquivo pela metade
    time.sleep(0.05)
    tmp.write_bytes(base)
    os.replace(tmp, alvo)
    time.sleep(0.05)
PY
  ESCRITOR=$!
}

probe() {                        # o par do teste, in-process, com o escritor vivo
  "$PY" - <<'PY'
import hashlib, pathlib, sys, time
sys.path.insert(0, ".")
import app
from fastapi.testclient import TestClient

def hash_agora():
    h = hashlib.sha256()
    for nome in app._BUILD_MODULOS:
        h.update((app.BASE / nome).read_bytes())
    return h.hexdigest()[:8]

no_import = hash_agora()                 # o que ESTE processo viu ao importar
c = TestClient(app.app)
hdr = {"X-API-Key": app._primary_api_key()}
codigo = c.get("/api/build", headers=hdr).json()["codigo"]
print(f"    codigo={codigo} (boot) · hash no import={no_import}")
if codigo == no_import:
    print("    · a expectativa congelada bate com o boot: prova EXATA aplicável")
else:
    print("    · escrita ENTRE o boot e o import: a tolerância por mtime é que segura")
# a asserção ANTIGA (`codigo == disco de agora`): com um escritor de DOIS estados
# ela é uma moeda — passa quando a árvore está na fase do boot e falha quando não.
velhas = 0
for _ in range(10):
    time.sleep(0.07)                     # 70 ms: não é múltiplo do ciclo do escritor,
    if codigo == hash_agora():           # senão as amostras travam numa fase só
        velhas += 1
print(f"    · a asserção ANTIGA passaria em {velhas}/10 amostras com a árvore se mexendo")
print("    ✔ o par de hoje não depende da fase do escritor")
sys.exit(0)
PY
}

SAIDA="$(mktemp -t 220-saida.XXXXXX)"
echo "══ 1/3 — SEM escritor (árvore parada: o par tem de usar a prova EXATA)"
"$PY" -m pytest tests/test_api.py -q -rP -k "build" > "$SAIDA" 2>&1
rc_sem=$?
grep -E "^_{4,} test_build|^[0-9]+ passed|\[220\]|failed" "$SAIDA" | tail -6
echo "══ 1/3 — saiu $rc_sem"
echo
echo "══ 2/3 — COM escritor em live_turns.py (a árvore viva se mexendo)"
escreve
"$PY" -m pytest tests/test_api.py -q -rP -k "build" > "$SAIDA" 2>&1
rc_com=$?
grep -E "^_{4,} test_build|^[0-9]+ passed|\[220\]|failed" "$SAIDA" | tail -6
echo "══ 2/3 — saiu $rc_com"
grep -q "\[220\] escritos depois do boot" "$SAIDA" && tolerou=1 || tolerou=0
rm -f "$SAIDA"
probe || rc_com=1
kill "$ESCRITOR" 2>/dev/null
_restaura

# ── 3/3 MORDIDA: a expectativa congelada tem de MORDER (senão o verde é de fachada)
TESTE="$RAIZ/tests/test_api.py"
cp "$TESTE" "$TESTE.220.bak"
"$PY" - "$TESTE" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
alvo = "_HASH_NO_IMPORT = _hash_dos_modulos()"
assert s.count(alvo) == 1, "padrão da expectativa não casou 1x"
p.write_text(s.replace(alvo, '_HASH_NO_IMPORT = "00000000"   # mordida'))
PY
echo "══ 3/3 — MORDIDA: expectativa congelada sabotada (tem de sair 1, árvore parada)"
"$PY" -m pytest tests/test_api.py -q -rP -k "build" > "$SAIDA" 2>&1
rc_mord=$?
grep -E "^_{4,} test_build|^[0-9]+ passed|failed" "$SAIDA" | tail -5
echo "══ 3/3 — saiu $rc_mord"
cp "$TESTE.220.bak" "$TESTE.220.tmp" && mv "$TESTE.220.tmp" "$TESTE"
rm -f "$TESTE.220.bak"
echo

echo
rc=0
[ "$rc_sem" = 0 ] || { echo "  ✘ sem escritor o par tinha de passar (saiu $rc_sem)"; rc=1; }
[ "$rc_com" = 0 ] || { echo "  ✘ com o escritor o par NÃO podia cair (saiu $rc_com)"; rc=1; }
[ "$tolerou" = 1 ] || { echo "  ✘ a rodada com escritor não passou pela tolerância provada"; rc=1; }
[ "$rc_mord" != 0 ] || { echo "  ✘ a expectativa sabotada tinha de derrubar o par"; rc=1; }
[ "$rc" = 0 ] && echo "✔ o par não é refém da árvore viva — e morde quando a expectativa mente" \
              || echo "✘ o par segue refém (ou o verde é de fachada)"
exit $rc