#!/usr/bin/env bash
# #223 — VERIFICAÇÃO POR FORA do commit 588e4d5 e de `tests/live_lock_freeze.sh`.
#
# Reproduz, item por item, o que o gate pediu, SEM aceitar a evidência colada:
#   1. MORDIDA=1 na árvore do fix sai 0 e devolve o app.py byte a byte (md5 e o
#      `git diff` do hunk de terceiro idênticos antes/depois);
#   2. o revert do script é MESMO a forma pré-#209 — conferido no ARQUIVO durante a
#      rodada revertida e replicado À MÃO pelo texto verbatim do `4729512^`;
#   3. a cena B (controle) congela nos DOIS estados e é PERMANENTE (2º probe);
#   4. o limiar é RELATIVO e medido na rodada — conferido no código E no número
#      impresso (max(0,25 s, 25× o /health ocioso) daquela cena);
#   5. cabeçalho: o que NÃO prova + a ressalva da árvore compartilhada;
#   6. o par determinístico (`test_busy_e_mandado_fora_do_live_lock`) verde;
#   7. o `trap` devolve o app.py com o script MORTO NO MEIO da rodada revertida.
#
# Uso: ./evidence/223-verifica.sh          (grava em evidence/223-verifica.txt)
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ" || exit 1
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

APP=app.py
E=evidence/223-verifica.txt
md5_de() { md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }
falhas=0
veredito() {  # veredito <rótulo> <condição-ok:0>
    if [ "$2" = 0 ]; then echo "  ✔ $1"; else echo "  ✘ $1"; falhas=$((falhas + 1)); fi
}

ANTES="$(md5_de $APP)"
DIFF_ANTES="$(git diff "$APP" | md5_de /dev/stdin)"

echo "### GATE #223 — VERIFICAÇÃO POR FORA — $(date -u +%FT%TZ)"
echo "commit revisado: 588e4d5 · script: tests/live_lock_freeze.sh"
echo "app.py md5 ANTES: $ANTES"
echo

# ─── item 1 ─────────────────────────────────────────────────────────────────
echo "══ ITEM 1 — MORDIDA=1 na árvore do fix: tem de sair 0 e restaurar byte a byte"
RUN="$$"
MORDIDA=1 ./tests/live_lock_freeze.sh > /tmp/223-item1-$RUN.log 2>&1
rc1=$?
cat /tmp/223-item1-$RUN.log
DEPOIS="$(md5_de $APP)"
DIFF_DEPOIS="$(git diff "$APP" | md5_de /dev/stdin)"
veredito "saiu 0 na árvore do fix (saiu $rc1)" "$([ "$rc1" = 0 ] && echo 0 || echo 1)"
veredito "app.py byte a byte igual (md5 $DEPOIS)" "$([ "$ANTES" = "$DEPOIS" ] && echo 0 || echo 1)"
veredito "hunk de terceiro intacto (git diff do app.py com o mesmo hash)" \
    "$([ "$DIFF_ANTES" = "$DIFF_DEPOIS" ] && echo 0 || echo 1)"
veredito "as DUAS direções aparecem no log (fix 0 · revertido 1)" \
    "$([ "$(grep -c 'mordida confirmada nas duas direções' /tmp/223-item1-$RUN.log 2>/dev/null || echo 0)" -ge 1 ] \
        && echo 0 || echo 1)"
echo

# ─── item 2 ─────────────────────────────────────────────────────────────────
echo "══ ITEM 2 — o revert do script é a forma pré-#209 (conferido no ARQUIVO)"
echo "  · revert À MÃO, pelo texto VERBATIM do commit 4729512^ (não por indentação)"
$PY evidence/223-revert-hand.py revert || { echo "  ✘ revert à mão falhou"; falhas=$((falhas + 1)); }
echo "  · o bloco do `busy` no app.py DURANTE a rodada (é o que o servidor importa):"
awk '/^    with _live_lock:$/{p=1} p{print "      " $0} /^    if ocupado:|^    if ocupado:$/{if(p)exit}' $APP | head -20
echo "  · diff contra o bloco do `git show 4729512^:app.py`:"
$PY - <<'PY'
import pathlib, subprocess
git = subprocess.run(["git", "show", "4729512^:app.py"], capture_output=True, text=True, check=True).stdout
PRE = pathlib.Path("app.py").read_text()
# recorta o handler do `busy` no app.py revertido à mão e procura-o no git
i = PRE.index("    with _live_lock:\n        # teto atômico com a inserção")
bloco = PRE[i:PRE.index("    if not ocupado:", i) if "    if not ocupado:" in PRE[i:i+2000] else i + 800]
alvo = bloco[:bloco.index("    if ocupado:")] if "    if ocupado:" in bloco else bloco
print("      · o trecho revertido está LITERALMENTE no 4729512^:", alvo in git)
PY
echo "  · rodada revertida (o script tem de MORRER aqui):"
MORDIDA=0 ./tests/live_lock_freeze.sh > /tmp/223-item2-$RUN.log 2>&1
rc2=$?
grep -E "cena A CONGELOU|saiu|✘" /tmp/223-item2-$RUN.log | sed 's/^/      /'
veredito "com o revert à mão o script saiu 1 (saiu $rc2)" "$([ "$rc2" = 1 ] && echo 0 || echo 1)"
veredito "a cena A congelou na rodada revertida" \
    "$(grep -q 'cena A CONGELOU' /tmp/223-item2-$RUN.log && echo 0 || echo 1)"
$PY evidence/223-revert-hand.py restore
veredito "app.py restaurado pelo caminho de volta (md5)" \
    "$([ "$(md5_de $APP)" = "$ANTES" ] && echo 0 || echo 1)"
echo

# ─── item 3 ─────────────────────────────────────────────────────────────────
echo "══ ITEM 3 — a cena B (controle) congela nos DOIS estados e é PERMANENTE"
n_b=$(grep -c "cena B: congelou como esperado" /tmp/223-item1-$RUN.log /tmp/223-item2-$RUN.log | awk -F: '{s+=$2} END{print s+0}')
n_perm=$(grep -c "congelamento PERMANENTE" /tmp/223-item1-$RUN.log /tmp/223-item2-$RUN.log | awk -F: '{s+=$2} END{print s+0}')
echo "  · 'cena B congelou' nas duas rodadas: $n_b/2 · 'PERMANENTE (2º probe)': $n_perm/2"
veredito "a cena B congelou no estado do fix E no revertido" "$([ "$n_b" = 2 ] && echo 0 || echo 1)"
veredito "o 2º probe também estourou nos dois (não foi lentidão)" "$([ "$n_perm" = 2 ] && echo 0 || echo 1)"
echo

# ─── item 4 ─────────────────────────────────────────────────────────────────
echo "══ ITEM 4 — o limiar é RELATIVO e medido na rodada (não constante)"
grep -nE "^FATOR|^PISO_S|^def limiar" tests/live_lock_freeze.sh | sed 's/^/      /'
$PY - <<'PY'
import pathlib, re
txt = "\n".join(pathlib.Path(f).read_text() for f in ("/tmp/223-item1-$RUN.log", "/tmp/223-item2-$RUN.log"))
pares = re.findall(r"ocioso (\d+) ms · probe (\d+) ms(?: · 2º probe (\d+) ms)? \(limiar (\d+) ms\)", txt)
ruim = []
for oc, pr, p2, lim in pares:
    esperado = round(max(0.25, 25 * int(oc) / 1000) * 1000)
    if abs(esperado - int(lim)) > 1:            # arredondamento de 1 ms
        ruim.append((oc, lim, esperado))
print(f"      · linhas de limiar conferidas: {len(pares)} · fora da fórmula max(250, 25×ocioso): {len(ruim)}")
for oc, lim, esp in ruim: print(f"        ocioso={oc} ms limiar impresso={lim} esperado={esp}")
PY
veredito "todo limiar impresso bate com max(0,25 s, 25× o ocioso da MESMA cena)" \
    "$($PY -c '
import pathlib, re, sys
txt = "\n".join(pathlib.Path(f).read_text() for f in ("/tmp/223-item1-$RUN.log", "/tmp/223-item2-$RUN.log"))
p = re.findall(r"ocioso (\d+) ms · probe \d+ ms(?: · 2º probe \d+ ms)? \(limiar (\d+) ms\)", txt)
sys.exit(0 if p and all(abs(max(250, 25*int(o)) - int(l)) <= 1 for o, l in p) else 1)'; echo $?)"
echo

# ─── item 5 ─────────────────────────────────────────────────────────────────
echo "══ ITEM 5 — cabeçalho declara o que NÃO prova e a árvore compartilhada"
for frase in "O QUE ELE \*\*NÃO\*\* PROVA" "A ÁRVORE É COMPARTILHADA" "TestClient"; do
    grep -qE "$frase" tests/live_lock_freeze.sh \
        && echo "  ✔ declara: $frase" || { echo "  ✘ falta: $frase"; falhas=$((falhas + 1)); }
done
echo

# ─── item 6 ─────────────────────────────────────────────────────────────────
echo "══ ITEM 6 — par determinístico intacto e verde"
$PY -m pytest tests/test_api.py::test_busy_e_mandado_fora_do_live_lock -q 2>&1 | tail -3 | sed 's/^/      /'
veredito "test_busy_e_mandado_fora_do_live_lock verde" \
    "$($PY -m pytest tests/test_api.py::test_busy_e_mandado_fora_do_live_lock -q >/dev/null 2>&1; echo $?)"
echo

# ─── item 7 ─────────────────────────────────────────────────────────────────
echo "══ ITEM 7 — o `trap` devolve o app.py com o script MORTO no meio da rodada"
echo "  · matando o script DURANTE a rodada revertida (SIGINT no processo e no filho)"
MORDIDA=1 ./tests/live_lock_freeze.sh > /tmp/223-item7-$RUN.log 2>&1 &
alvo=$!
for _ in $(seq 1 900); do
    grep -q '^        await ws.send_json(_live_erro(' $APP && break
    kill -0 $alvo 2>/dev/null || break
    sleep 0.2
done
echo "      estado no instante do sinal: app.py REVERTIDO=$(grep -qc '^        await ws.send_json(_live_erro(' $APP && echo sim || echo nao)"
filho1="$(pgrep -P $alvo | head -1)"; filho2="$(pgrep -P "${filho1:-1}" | head -1)"
kill -INT "${filho2:-0}" 2>/dev/null
kill -INT "$alvo" 2>/dev/null
wait "$alvo"; rc7=$?
tail -4 /tmp/223-item7-$RUN.log | sed 's/^/      /'
veredito "app.py devolvido byte a byte depois do sinal (md5 $(md5_de $APP))" \
    "$([ "$(md5_de $APP)" = "$ANTES" ] && echo 0 || echo 1)"
echo

echo "══ RESUMO: falhas=$falhas"
[ "$falhas" = 0 ] && echo "✔ verificação por fora: TODOS os itens do gate conferidos" \
                   || echo "✘ itens com falha: $falhas"
exit "$falhas"