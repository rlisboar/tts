#!/bin/bash
# GATE #232 — MORDIDAS: reverte cada forma corrigida e confere que a suíte CAI.
# Restaura byte-a-byte entre as mordidas (cmp) e recusa rodar se o arquivo mudar
# de md5 no meio (árvore viva).
set -u
cd "$(dirname "$0")/.."
RUN="$$"   # sufixo por execução (paralelo não troca os arquivos de mordida)
S=remote/deploy_mini.sh
T=tests/test_deploy_mini_sh.py
PY=./.venv-mlx/bin/python
E=evidence/232-mordidas.txt
bak="$(mktemp)"; cp "$S" "$bak"
LIMPA_BYTECODE="$(cd "$(dirname "$0")/.." && pwd)/tests/limpa_bytecode.sh"
md5_ini="$(md5 -q "$S")"

morde() { # $1 = nome · $2 = antigo · $3 = novo · $4 = expectativa (MORDE|PASSA) · resto = testes
  local nome="$1" velho="$2" novo="$3" esperado="$4"; shift 4
  cp "$bak" "$S"
  "$PY" - "$S" "$velho" "$novo" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
assert s.count(sys.argv[2]) == 1, f"casou {s.count(sys.argv[2])}x: {sys.argv[2]!r}"
p.write_text(s.replace(sys.argv[2], sys.argv[3]))
PY
  "$LIMPA_BYTECODE" app.py tests/test_api.py > /dev/null   # #238: senão o .pyc velho mente
  "$PY" -m pytest "$@" -q > /tmp/mordida-$RUN.log 2>&1
  local rc=$?
  cp "$bak" "$S"
  cmp -s "$bak" "$S" || { echo "  [restaura] FALHOU byte-a-byte"; return 1; }
  local obtido=PASSA; [ $rc -ne 0 ] && obtido=MORDE
  if [ "$obtido" = "$esperado" ]; then
    echo "  [ok]     $nome → $obtido (esperado $esperado)"
  else
    echo "  [FALHA]  $nome → $obtido (esperado $esperado)"; tail -3 /tmp/mordida-$RUN.log | sed 's/^/      /'
  fi
}

echo "# mordidas do gate #232 — $(date -u +%FT%TZ)"
echo "  md5 do script antes: $md5_ini"
echo "## bug 1: \"\$1:requirements.txt\" (zsh come o :r)"
morde "deps_preview sem chaves" 'show "${1}:requirements.txt"' 'show "$1:requirements.txt"' MORDE \
      "$T::test_deploy_mostra_o_delta_de_deps_do_rev_alvo_e_instala"
echo "## bug 2: \"\$ALVO_SHA:refs/heads/main\""
morde "push sem chaves" 'push origin "${ALVO_SHA}:refs/heads/main"' 'push origin "$ALVO_SHA:refs/heads/main"' MORDE \
      "$T::test_deploy_apply_publica_puxa_instala_reinicia_e_smoke"
echo "## status read-only do zsh (o corpo TODO tem de usar o nome)"
"$PY" - "$bak" <<'PY'
import pathlib, sys
s = pathlib.Path(sys.argv[1]).read_text()
ini = s.index("smoke_tts() {")
fim = s.index("\nsmoke() {")
corpo = s[ini:fim]
assert corpo.count("estado_job") >= 5, corpo.count("estado_job")
pathlib.Path("/tmp/232-status-$RUN.sh").write_text(s[:ini] + corpo.replace("estado_job", "status") + s[fim:])
print(f"  (corpo do smoke_tts: {corpo.count('estado_job')} usos renomeados)")
PY
cp /tmp/232-status-$RUN.sh "$S"
"$LIMPA_BYTECODE" > /dev/null
"$PY" -m pytest "$T::test_smoke_faz_sintese_real_e_pode_pular" "$T::test_smoke_falha_quando_o_job_de_sintese_erra" -q > /tmp/mordida-$RUN.log 2>&1
rc=$?; cp "$bak" "$S"
if [ $rc -ne 0 ]; then echo "  [ok]     var status no smoke_tts → MORDE (esperado MORDE)"
else echo "  [FALHA]  var status no smoke_tts → PASSA (esperado MORDE)"; tail -3 /tmp/mordida-$RUN.log | sed 's/^/      /'; fi

echo "## compare: aviso de alvo NÃO-descendente — a suíte usa?"
"$PY" - "$bak" <<'PY'
import pathlib, sys
s = pathlib.Path(sys.argv[1]).read_text()
velho = '''    if git -C "$LOCAL_REPO" merge-base --is-ancestor "$no_ar_sha" "$ALVO_SHA" 2>/dev/null; then
      print -r -- "  alvo é descendente da produção (subida direta)"
    else
      print -r -- "  [aviso] produção NÃO é ancestral do alvo — é rewind/desvio, confira o motivo"
    fi'''
assert s.count(velho) == 1
pathlib.Path("/tmp/232-ancestral-$RUN.sh").write_text(
    s.replace(velho, '    print -r -- "  alvo é descendente da produção (subida direta)"'))
PY
cp /tmp/232-ancestral-$RUN.sh "$S"
"$LIMPA_BYTECODE" > /dev/null
"$PY" -m pytest "$T" -q > /tmp/mordida-$RUN.log 2>&1
rc=$?; cp "$bak" "$S"
if [ $rc -ne 0 ]; then echo "  [ok]     aviso de não-descendente → MORDE"
else echo "  [FALHA]  aviso de não-descendente → PASSA (nenhum teste cobre o desvio)"; fi
echo "## TTS_MINI_REV (alvo por rev) — a suíte usa?"
morde "TTS_MINI_REV ignorado" 'ALVO_REV="${TTS_MINI_REV:-HEAD}"' 'ALVO_REV="HEAD"' MORDE "$T"
echo "## branch prod-<sha> (não detached) — a suíte usa?"
morde "switch --detach no deploy" "switch --quiet -C 'prod-\${ALVO_SHA[1,12]}' '\$ALVO_SHA'" \
      "switch --quiet --detach '\$ALVO_SHA'" MORDE "$T"
morde "switch --detach no rollback" "switch --quiet -C 'prod-\${sha[1,12]}' '\$sha'" \
      "switch --quiet --detach '\$sha'" MORDE "$T"

md5_fim="$(md5 -q "$S")"
echo "  md5 do script depois: $md5_fim"
[ "$md5_ini" = "$md5_fim" ] || echo "  [FALHA] md5 divergiu — árvore viva no meio das mordidas"
