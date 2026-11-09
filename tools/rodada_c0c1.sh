#!/bin/zsh
# Janela C0 → C1 do #876 (motor do STT do Live): UM comando, com rollback.
#
# C0 = whisper (default) · C1 = parakeet (`live_stt_engine=parakeet`, commit
# d107b9e). A troca é por `POST /api/settings`, SEM restart — as duas pontas
# rodam no MESMO build e com o MESMO corpus/semente, senão a comparação não
# vale. As fases de eco e ventoinha entram no mesmo run.
#
#   tools/rodada_c0c1.sh --sha <sha-no-ar> [--corpus /tmp/live-bench-audio]
#                        [--min-eco 5] [--min-ventoinha 5] [--ciclos 20]
#
# O que este script NÃO faz: deploy, restart da produção e abertura de modelo na
# 7890 antes da hora. Ele assume a instância de bancada JÁ no ar na 7890, com
# estado sintético (`TTS_STATE_DIR` em /tmp) — quem sobe é o passo 3 do runbook.
#
# Saída: `--dir` (padrão /tmp/rodada-876-<epoch>) com c0.json, c1.json,
# eco-c0.json, eco-c1.json, ventoinha-c0.json, ventoinha-c1.json, rss.csv e o
# relatório final. O `trap` derruba o bench, DEVOLVE a chave anterior e derruba
# a 7890 mesmo se uma fase falhar.
set -u
RAIZ="${0:A:h:h}"
# worktree não tem `.venv-mlx` (gitignored): cai no python3 do sistema se faltar.
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || print -r -- python3)"
URL="ws://127.0.0.1:7890/api/live/ws"
APP="http://127.0.0.1:7890"
SHA=""; CORPUS=/tmp/live-bench-audio; DIR=""; CICLOS=20; MIN_ECO=5; MIN_VENT=5
SEMENTE=875
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --corpus) CORPUS="${2:-}"; shift 2 ;;
    --dir) DIR="${2:-}"; shift 2 ;;
    --ciclos) CICLOS="${2:-20}"; shift 2 ;;
    --min-eco) MIN_ECO="${2:-5}"; shift 2 ;;
    --min-ventoinha) MIN_VENT="${2:-5}"; shift 2 ;;
    --semente) SEMENTE="${2:-875}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) print -r -- "sobrando: $1" >&2; exit 2 ;;
  esac
done
DIR="${DIR:-/tmp/rodada-876-$(date +%s)}"
mkdir -p "$DIR" || exit 1
LOG_CORRIDA="$DIR/corrida.log"

fala() { print -r -- "$*" | tee -a "$LOG_CORRIDA" }

# ── rollback: sempre, em qualquer saída ─────────────────────────────────────
ANTERIOR=""
SAMPLER=""
limpa() {
  local rc=$?
  print -r -- "== limpeza (rc=$rc)" | tee -a "$LOG_CORRIDA"
  pkill -f "live_bench" 2>/dev/null
  pkill -f "fase_ruido.py" 2>/dev/null
  [ -n "$SAMPLER" ] && kill "$SAMPLER" 2>/dev/null
  if [ -n "$ANTERIOR" ]; then
    curl -s -m 5 -o /dev/null -H "Content-Type: application/json" \
      -X POST "$APP/api/settings" -d "{\"live_stt_engine\": \"$ANTERIOR\"}" \
      && fala "   chave live_stt_engine devolvida para '$ANTERIOR'"
  fi
  pkill -f "port 7890" 2>/dev/null
  sleep 2
  fala "   7890 listeners: $(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')"
  fala "   uvicorn 7860: $(pgrep -f 'port 7860' | wc -l | tr -d ' ')"
  fala "   prod health: $(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:7860/health)"
  exit $rc
}
trap limpa EXIT INT TERM

# ── pré-voo ─────────────────────────────────────────────────────────────────
fala "== pré-voo  $(date -Iseconds)"
[ -x "$PY" ] || { fala "sem venv em $PY"; exit 1; }
[ -d "$CORPUS" ] || { fala "corpus ausente: $CORPUS"; exit 1; }
[ "$(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')" -ge 1 ] \
  || { fala "nada escutando na 7890 — suba a instância de bancada primeiro"; exit 1; }
[ "$(pgrep -f 'port 7860' | wc -l | tr -d ' ')" -eq 1 ] \
  || { fala "esperava 1 uvicorn na 7860 (produção intocada)"; exit 1; }
PROD_PID="$(pgrep -f 'port 7860' | head -1)"
NO_AR="$(cd "$RAIZ" && git rev-parse --short HEAD 2>/dev/null)"
fala "   prod pid=$PROD_PID no ar em $NO_AR"
if [ -n "$SHA" ] && [ "${NO_AR}" != "$(print -r -- "$SHA" | cut -c1-7)" ]; then
  fala "   [aviso] SHA pedido ($SHA) difere do worktree ($NO_AR)"
fi
ANTERIOR="$(curl -s -m 5 "$APP/api/settings" \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("live_stt_engine") or "")')"
fala "   live_stt_engine anterior: '${ANTERIOR}'"
fala "   corpus: $(ls "$CORPUS" | wc -l | tr -d ' ') arquivos · semente $SEMENTE"

# amostrador de memória (1/min): é a curva do #891
( while :; do
    printf '%s,%s,%s\n' "$(date -Iseconds)" \
      "$(ps -o rss= -p "$PROD_PID" 2>/dev/null | tr -d ' ' | awk '{print int($1/1024)}')" \
      "$(sysctl -n vm.swapusage 2>/dev/null | sed -E 's/.*used = ([0-9.]+)M.*/\1/')" \
      >> "$DIR/rss.csv"
    sleep 60
  done ) &
SAMPLER=$!

# ── fases ───────────────────────────────────────────────────────────────────
roda_ciclos() {           # $1 = rótulo (c0|c1)
  fala "== ciclos $1  $(date -Iseconds)"
  TTS_LIVE_DUMP_DIR="$DIR/dump-$1" "$PY" -I "$RAIZ/tools/live_bench.py" \
    --audio "$CORPUS" --url "$URL" --seed "$SEMENTE" --ciclos "$CICLOS" \
    --teto-turno-s 300 --rotulo "$1" --out "$DIR/$1.json" \
    > "$DIR/$1.log" 2>&1
  fala "   rc=$? $(grep -E '^S[1-5] ' "$DIR/$1.log" | head -1 | cut -c1-90)"
}
roda_fase() {             # $1 = fase (eco|ventoinha)  $2 = rótulo
  fala "== fase $1 $2  $(date -Iseconds)"
  "$PY" "$RAIZ/tools/fase_ruido.py" "$1" --url "$URL" --audio "$CORPUS" \
    --minutos "$([ "$1" = eco ] && print -r -- "$MIN_ECO" || print -r -- "$MIN_VENT")" \
    --out "$DIR/$1-$2.json" >> "$DIR/$1-$2.log" 2>&1
  fala "   rc=$? $(tail -1 "$DIR/$1-$2.log" | cut -c1-90)"
}

MARCA_C0="$(date +%H:%M:%S)"
roda_ciclos c0
roda_fase eco c0
roda_fase ventoinha c0

fala "== troca para C1 (live_stt_engine=parakeet)"
curl -s -m 5 -o /dev/null -H "Content-Type: application/json" \
  -X POST "$APP/api/settings" -d '{"live_stt_engine": "parakeet"}' \
  || { fala "POST /api/settings falhou"; exit 1; }
fala "   agora: $(curl -s -m 5 "$APP/api/settings" \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("live_stt_engine"))')"
MARCA_C1="$(date +%H:%M:%S)"

roda_ciclos c1
roda_fase eco c1
roda_fase ventoinha c1

# ── relatório ───────────────────────────────────────────────────────────────
fala "== relatório  $(date -Iseconds)"
"$PY" "$RAIZ/tools/relatorio_rodada.py" --dir "$DIR" --sha "${SHA:-$NO_AR}" \
  --log /tmp/tts-studio.log --janela-c0 "${MARCA_C0}-${MARCA_C1}" \
  --janela-c1 "${MARCA_C1}-$(date +%H:%M:%S)" 2>&1 | tee -a "$LOG_CORRIDA"
fala "== fim  $(date -Iseconds) — saída em $DIR"
