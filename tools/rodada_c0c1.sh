#!/bin/zsh
# Janela 2×2 do #876 (motor do STT) × #870 (duck rápido): UM comando, com rollback.
#
# C0 = whisper (default) · C1 = parakeet (`live_stt_engine=parakeet`, d107b9e)
# F0 = `TTS_LIVE_BARGE_DUCK_RAPIDO` desligada · F1 = ligada
#
# POR QUE 2×2: o #870 é ENV (o motor nasce com a flag desligada), então a
# pergunta "a flag pode ligar?" não se responde sem comparar F0 × F1 — e ela só
# vale se o motor novo (C1) aguentar. Cada célula é medida lado a lado com as
# outras; a troca de motor é por `POST /api/settings` (sem restart) e a de flag
# é por restart da instância de BANCADA (a produção não reinicia).
#
# LIMITES DECLARADOS ANTES DA RODADA (não se move depois de ver número):
#   liga a flag  ⇔  no C1: pausa p95 ≤ 800 ms  E  duck p50 ≤ 300 ms
#                    E  eco_falso_duck ≤ 5 % (IC95 superior ≤ 5 %)
#                    E  eco_falso_corte ≤ C1/F0  E  S1 e S2 sem piora vs C1/F0
#   não liga     ⇔  qualquer um dos acima falhar (ou faltar amostra)
#
#   tools/rodada_c0c1.sh --app-dir ~/Documents/tts-rod --sha <sha-no-ar> \
#       [--corpus /tmp/live-bench-audio] [--estado /tmp/bench-mini/.state-local] \
#       [--ciclos 10] [--min-eco 3] [--min-ventoinha 3]
#
# O script sobe e derruba a instância da 7890 (nunca a 7860) e, no `trap`,
# devolve `live_stt_engine` ao valor lido no início e mata tudo que subiu.
set -u
RAIZ="${0:A:h:h}"
APP_DIR="${RAIZ}"
PY=""
URL="ws://127.0.0.1:7890/api/live/ws"
APP="http://127.0.0.1:7890"
SHA=""; CORPUS=/tmp/live-bench-audio; ESTADO=/tmp/bench-mini/.state-local
DIR=""; CICLOS=10; MIN_ECO=3; MIN_VENT=3; SEMENTE=875
while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir) APP_DIR="${2:-}"; shift 2 ;;
    --sha) SHA="${2:-}"; shift 2 ;;
    --corpus) CORPUS="${2:-}"; shift 2 ;;
    --estado) ESTADO="${2:-}"; shift 2 ;;
    --dir) DIR="${2:-}"; shift 2 ;;
    --ciclos) CICLOS="${2:-10}"; shift 2 ;;
    --min-eco) MIN_ECO="${2:-3}"; shift 2 ;;
    --min-ventoinha) MIN_VENT="${2:-3}"; shift 2 ;;
    --semente) SEMENTE="${2:-875}"; shift 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) print -r -- "sobrando: $1" >&2; exit 2 ;;
  esac
done
PY="$APP_DIR/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || print -r -- python3)"
DIR="${DIR:-/tmp/rodada-876-$(date +%s)}"
mkdir -p "$DIR" || exit 1
LOG_CORRIDA="$DIR/corrida.log"

fala() { print -r -- "$*" | tee -a "$LOG_CORRIDA" }

# ── rollback: sempre, em qualquer saída ─────────────────────────────────────
ANTERIOR=""; SAMPLER=""; SUBIU=0
derruba() {
  pkill -f "live_bench" 2>/dev/null
  pkill -f "fase_ruido.py" 2>/dev/null
  [ "$SUBIU" = 1 ] && { pkill -f "port 7890" 2>/dev/null; sleep 2; SUBIU=0 }
}
limpa() {
  local rc=$?
  print -r -- "== limpeza (rc=$rc)" | tee -a "$LOG_CORRIDA"
  [ -n "$SAMPLER" ] && kill "$SAMPLER" 2>/dev/null
  if [ -n "$ANTERIOR" ] && [ "$SUBIU" = 1 ]; then
    curl -s -m 5 -o /dev/null -H "Content-Type: application/json" \
      -X POST "$APP/api/settings" -d "{\"live_stt_engine\": \"$ANTERIOR\"}" \
      && fala "   chave live_stt_engine devolvida para '$ANTERIOR'"
  fi
  derruba
  fala "   7890 listeners: $(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')"
  fala "   uvicorn 7860: $(pgrep -f 'port 7860' | wc -l | tr -d ' ')"
  fala "   prod health: $(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:7860/health)"
  exit $rc
}
trap limpa EXIT INT TERM

# ── pré-voo (antes de subir qualquer coisa) ─────────────────────────────────
fala "== pré-voo  $(date -Iseconds)"
[ -d "$APP_DIR" ] || { fala "sem app-dir: $APP_DIR"; exit 1; }
[ -d "$CORPUS" ] || { fala "corpus ausente: $CORPUS"; exit 1; }
[ -f "$ESTADO/settings.json" ] || { fala "estado sintético ausente: $ESTADO/settings.json"; exit 1; }
[ "$(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')" -eq 0 ] \
  || { fala "7890 já ocupada — derrube a outra instância antes"; exit 1; }
[ "$(pgrep -f 'port 7860' | wc -l | tr -d ' ')" -eq 1 ] \
  || { fala "esperava 1 uvicorn na 7860 (produção intocada)"; exit 1; }
PROD_PID="$(pgrep -f 'port 7860' | head -1)"
NO_AR="$(cd "$APP_DIR" && git rev-parse --short HEAD 2>/dev/null)"
fala "   prod pid=$PROD_PID no ar em $NO_AR"
if [ -n "$SHA" ] && [ "${NO_AR}" != "$(print -r -- "$SHA" | cut -c1-7)" ]; then
  fala "   [aviso] SHA pedido ($SHA) difere do worktree ($NO_AR)"
fi
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

# ── instância de bancada (o flag é ENV: sobe uma vez por estado) ────────────
sobe() {                  # $1 = 0|1 (duck rápido)
  fala "== sobe 7890 (duck_rapido=$1)  $(date -Iseconds)"
  local K B M
  K="$("$PY" -c 'import json;print(json.load(open("settings.json"))["chat_api_key"])' 2>/dev/null)"
  B="$("$PY" -c 'import json;print(json.load(open("settings.json"))["chat_base_url"])' 2>/dev/null)"
  M="$("$PY" -c 'import json;print(json.load(open("settings.json"))["chat_model"])' 2>/dev/null)"
  ( cd "$APP_DIR" && \
    TTS_STATE_DIR="$ESTADO" TTS_OUTPUTS_DIR="$DIR/outputs" \
    TTS_CHAT_BACKEND_LIVE=openai TTS_CHAT_BASE_URL="$B" TTS_CHAT_MODEL="$M" \
    TTS_CHAT_API_KEY="$K" TTS_MARCA_PREWARM_BOOT=0 TTS_LIVE_TTL_S=1800 \
    TTS_LIVE_BARGE_DUCK_RAPIDO="$1" TTS_ALLOWED_HOSTS=127.0.0.1,localhost \
    nohup "$PY" -m uvicorn app:app --host 127.0.0.1 --port 7890 --log-level warning \
      > "$DIR/app-$1.log" 2>&1 & )
  SUBIU=1
  local i
  for i in $(seq 1 60); do
    [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$APP/health")" = "200" ] && break
    sleep 2
  done
  [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$APP/health")" = "200" ] \
    || { fala "   7890 não subiu"; exit 1; }
  fala "   health 200"
}

# ── fases ───────────────────────────────────────────────────────────────────
roda_ciclos() {           # $1 = célula (f0-c0 …)
  fala "== ciclos $1  $(date -Iseconds)"
  TTS_LIVE_DUMP_DIR="$DIR/dump-$1" "$PY" -I "$APP_DIR/tools/live_bench.py" \
    --audio "$CORPUS" --url "$URL" --seed "$SEMENTE" --ciclos "$CICLOS" \
    --teto-turno-s 300 --rotulo "$1" --out "$DIR/$1.json" > "$DIR/$1.log" 2>&1
  fala "   rc=$? $(grep -E '^S1 ' "$DIR/$1.log" | cut -c1-80)"
}
roda_fase() {             # $1 = fase (eco|ventoinha)  $2 = célula
  fala "== fase $1 $2  $(date -Iseconds)"
  local min=$MIN_VENT
  [ "$1" = eco ] && min=$MIN_ECO
  "$PY" "$APP_DIR/tools/fase_ruido.py" "$1" --url "$URL" --audio "$CORPUS" \
    --minutos "$min" --out "$DIR/$1-$2.json" >> "$DIR/$1-$2.log" 2>&1
  fala "   rc=$? $(tail -1 "$DIR/$1-$2.log" | cut -c1-80)"
}
celula() {                # $1 = f0|f1
  local marca
  marca="$(date +%H:%M:%S)"
  print -r -- "$1 $marca" >> "$DIR/marcas.txt"
  curl -s -m 5 -o /dev/null -H "Content-Type: application/json" \
    -X POST "$APP/api/settings" -d '{"live_stt_engine": "whisper"}'
  roda_ciclos "$1-c0"; roda_fase eco "$1-c0"; roda_fase ventoinha "$1-c0"
  curl -s -m 5 -o /dev/null -H "Content-Type: application/json" \
    -X POST "$APP/api/settings" -d '{"live_stt_engine": "parakeet"}'
  roda_ciclos "$1-c1"; roda_fase eco "$1-c1"; roda_fase ventoinha "$1-c1"
}

ANTERIOR="$(curl -s -m 5 "$APP/api/settings" 2>/dev/null \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("live_stt_engine") or "")' 2>/dev/null)"

sobe 0; celula f0; derruba
sobe 1; celula f1; derruba

# ── relatório ───────────────────────────────────────────────────────────────
fala "== relatório  $(date -Iseconds)"
"$PY" "$APP_DIR/tools/relatorio_rodada.py" --dir "$DIR" --sha "${SHA:-$NO_AR}" \
  --log /tmp/tts-studio.log --marcas "$DIR/marcas.txt" 2>&1 | tee -a "$LOG_CORRIDA"
fala "== fim  $(date -Iseconds) — saída em $DIR"
