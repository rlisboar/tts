#!/usr/bin/env bash

# sufixo por execução: os DOIS scripts do #225 usavam os MESMOS nomes em /tmp
RUN="$$"
# #225 — o assistente tem de CALAR quando o usuário fala, inclusive quando o
# servidor NÃO classifica barge (a janela de playback é uma ESTIMATIVA; quando ela
# fecha antes, o onset vira `speech_start` sem `barge_in` e nenhum `interrupted`
# vem — aí o áudio que o cliente ainda tem na fila segue tocando por cima).
#
# Protocolo ANTES/DEPOIS na MESMA máquina, célula a célula, trocando SÓ o cliente
# (`static/index.html`, a linha do corte no `speech_start`): o servidor fica
# IDÊNTICO, então o motor (limiares, janela, eco) não muda entre as fases — o que
# muda é só o que o cliente faz ao receber o onset.
#
#   A: MODO=vao (default)            — controle: o barge 20/20 tem de continuar
#   B: MODO=vao + eco_so_tocando=1   — regime em que o onset cai FORA da janela
#                                      (o defeito: onset sem barge com fila)
#   C: MIC_FILE=1 (eco)              — sentido do corte FALSO (sem humano)
#
# A métrica é a do harness: `=== CORTE (#225)` (com fila / cortou / tocando por
# cima) — o alvo é `tocando por cima: 0` nas três células.
#
# Uso: nohup ./evidence/225-celulas.sh > evidence/225-celulas.log 2>&1 &
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="$PWD/.venv-mlx/bin/python"
CLI_COM=/tmp/225-index-com-fix-$RUN.html
CLI_SEM=/tmp/225-index-sem-fix-$RUN.html

cp static/index.html "$CLI_COM"
"$PY" - <<'EOF'
import pathlib
p = pathlib.Path("static/index.html")
html = p.read_text()
bloco = """                           if (LX.ativos.length) {
                             lxCancelaPlayback();
                             lxLogEvento("⏹ cortei o assistente — você começou a falar");
                           }
"""
assert bloco in html, "bloco do #225 não está no index.html — nada a medir"
pathlib.Path("/tmp/225-index-sem-fix-$RUN.html").write_text(html.replace(bloco, "", 1))
print("variante sem fix gerada")
EOF

# se o script morrer no meio, o cliente volta COM o fix (não deixar a árvore ambígua)
trap 'cp "$CLI_COM" static/index.html' EXIT

celula() {   # $1=cliente  $2=arquivo de saída  $3..=env do harness
  local cli="$1" saida="$2"; shift 2
  cp "$cli" static/index.html
  echo "### $saida [$(basename "$cli")] $(date -u +%H:%M:%SZ)"
  env "$@" ./tests/live_barge_rep.sh > "evidence/$saida" 2>&1
  echo "  exit=$? · $(grep -oE 'CORTE \(#225\): .*' "evidence/$saida" | tail -1)"
}

for fase in antes depois; do
  if [ "$fase" = antes ]; then CLI="$CLI_SEM"; else CLI="$CLI_COM"; fi
  echo "=== FASE $fase (cliente: $(basename "$CLI")) ==="
  celula "$CLI" "225-$fase-vao-default.txt"        REP=20 MODO=vao
  celula "$CLI" "225-$fase-vao-eco-tocando.txt"    REP=20 MODO=vao TTS_LIVE_ECO_SO_TOCANDO=1
  celula "$CLI" "225-$fase-eco.txt"                REP=20 MIC_FILE=1
done

cp "$CLI_COM" static/index.html
echo "=== FIM $(date -u +%H:%M:%SZ) — cliente COM o fix restaurado ==="
