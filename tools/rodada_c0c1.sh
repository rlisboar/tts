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
#                    E  eco_falso_duck ≤ 5 % (PONTO falsos/respostas; o IC95 vai
#                       junto só como resolução — com n=10 e zero falso o Wilson
#                       dá [0; 27,8] e o ramo LIGA nunca passaria)
#                    E  eco_falso_corte ≤ C1/F0  E  S1, S2, S3 e S4 sem piora
#                    E  RSS ≤ C1/F0
#   não liga     ⇔  qualquer um dos acima falhar (ou faltar amostra)
#   eco entre 5 % e 10 % → INDETERMINADO naquele critério; > 10 % → NÃO LIGA
#
#   tools/rodada_c0c1.sh --app-dir ~/Documents/tts-rod --sha <sha-no-ar> \
#       [--corpus /tmp/live-bench-audio] [--estado /tmp/bench-mini/.state-local] \
#       [--ciclos 10] [--aquecimento 1] [--min-eco 3] [--min-ventoinha 3]
#
# ORDEM CONTRABALANÇADA (f0: c0, c1 → f1: c1, c0): só o MOTOR inverte entre os
# blocos de flag. Assim o motor novo cai no fim de um bloco e no começo do outro,
# e a deriva térmica/de RSS não fica colada num motor só. Cada bloco roda 1 ciclo
# de AQUECIMENTO fora da amostra, com marca própria: o turno de aquecimento é o
# que paga o cache miss do `_cond_for` (~1500 ms) depois do restart, e a janela do
# `stt` começa no FIM dele. A ordem executada é lida do `marcas.txt` no relatório.
#
# O script sobe e derruba a instância da 7890 (nunca a 7860) e, no `trap`,
# devolve `live_stt_engine` ao valor lido do estado sintético e mata tudo que
# subiu — sempre pelo PID registrado, nunca por padrão de linha de comando.
set -u
RAIZ="${0:A:h:h}"
APP_DIR="${RAIZ}"
PY=""
URL="ws://127.0.0.1:7890/api/live/ws"
APP="http://127.0.0.1:7890"
SHA=""; CORPUS=/tmp/live-bench-audio; ESTADO=/tmp/bench-mini/.state-local
DIR=""; CICLOS=10; AQUEC=1; MIN_ECO=3; MIN_VENT=3; SEMENTE=875
MODULOS=(app.py common.py backends.py tts_worker.py live_pipeline.py live_turns.py dsh_client.py)
while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir) APP_DIR="${2:-}"; shift 2 ;;
    --sha) SHA="${2:-}"; shift 2 ;;
    --corpus) CORPUS="${2:-}"; shift 2 ;;
    --estado) ESTADO="${2:-}"; shift 2 ;;
    --dir) DIR="${2:-}"; shift 2 ;;
    --ciclos) CICLOS="${2:-10}"; shift 2 ;;
    --aquecimento) AQUEC="${2:-1}"; shift 2 ;;
    --min-eco) MIN_ECO="${2:-3}"; shift 2 ;;
    --min-ventoinha) MIN_VENT="${2:-3}"; shift 2 ;;
    --semente) SEMENTE="${2:-875}"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) print -r -- "sobrando: $1" >&2; exit 2 ;;
  esac
done
PY="$APP_DIR/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || print -r -- python3)"
DIR="${DIR:-/tmp/rodada-876-$(date +%s)}"
mkdir -p "$DIR" || exit 1
LOG_CORRIDA="$DIR/corrida.log"

fala() { print -r -- "$*" | tee -a "$LOG_CORRIDA"; }

# `codigo` do /api/build: sha256-8 dos módulos na ordem do app (mesma conta do
# remote/deploy_mini.sh). É o que separa "código novo no disco" de "código novo
# CARREGADO" — o `--sha` só vale se o hash bater com o rev pedido.
codigo_esperado() {
  { for m in "${MODULOS[@]}"; do
      git -C "$APP_DIR" show "${1}:$m" 2>/dev/null || print -rn -- '<ausente>'
    done } | shasum -a 256 | cut -c1-8
}

# ── rollback: sempre, em qualquer saída ─────────────────────────────────────
ANTERIOR=""; SAMPLER=""; APP_PID=""; SUBIU=0
derruba() {
  pkill -f "live_bench" 2>/dev/null
  pkill -f "fase_ruido.py" 2>/dev/null
  if [ -n "$APP_PID" ]; then
    kill "$APP_PID" 2>/dev/null
    local i
    for i in $(seq 1 20); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$APP_PID" 2>/dev/null
    APP_PID=""; SUBIU=0
  fi
  # o `pkill`/`kill` é ASSÍNCRONO: sem esperar o listener soltar, o `sobe`
  # seguinte leva 200 do processo VELHO e a célula roda com a flag errada, calada.
  local j
  for j in $(seq 1 20); do
    [ "$(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')" -eq 0 ] && break
    sleep 0.5
  done
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
[ -n "$SHA" ] || { fala "--sha é obrigatório (o rev que o deploy subiu)"; exit 1; }
[ -d "$CORPUS" ] || { fala "corpus ausente: $CORPUS"; exit 1; }
[ -f "$ESTADO/settings.json" ] || { fala "estado sintético ausente: $ESTADO/settings.json"; exit 1; }
[ "$(lsof -nP -iTCP:7890 -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')" -eq 0 ] \
  || { fala "7890 já ocupada — derrube a outra instância antes"; exit 1; }
# NÃO rodar em paralelo com o mede_pos_llm.sh (#1022): ele sobe app E LLM falso
# e as duas cópias do OmniVoice na GPU não cabem no swap do mini. As portas não
# colidem (7890 × 7895/7899) — o recurso escasso é a GPU.
for p in 7895 7899; do
  [ "$(lsof -nP -iTCP:$p -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')" -eq 0 ] \
    || { fala "porta $p ocupada (mede_pos_llm.sh no ar?) — rode em SÉRIE"; exit 1; }
done
[ -z "$(pgrep -f 'live_bench.py|fase_ruido.py')" ] \
  || { fala "já há bancada rodando — uma por vez"; exit 1; }
[ "$(pgrep -f 'port 7860' | wc -l | tr -d ' ')" -eq 1 ] \
  || { fala "esperava 1 uvicorn na 7860 (produção intocada)"; exit 1; }
PROD_PID="$(pgrep -f 'port 7860' | head -1)"
# O SHA tem de ser o que a produção CARREGOU. Aviso não serve: célula medida em
# build errado é número perdido.
ESPERADO="$(codigo_esperado "$SHA")"
NO_AR="$(curl -s -m 5 http://127.0.0.1:7860/api/build 2>/dev/null \
  | sed -n 's/.*"codigo": *"\([0-9a-f]*\)".*/\1/p' | head -1)"
fala "   sha pedido=$SHA · codigo esperado=$ESPERADO · no ar=${NO_AR:-<vazio>} · prod pid=$PROD_PID"
[ -n "$NO_AR" ] || { fala "não consegui ler o /api/build da produção"; exit 1; }
[ "$NO_AR" = "$ESPERADO" ] \
  || { fala "código no ar ($NO_AR) ≠ rev pedido ($ESPERADO) — deploy diferente do combinado"; exit 1; }
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
  # `nohup` faz exec: `$!` já é o PID do uvicorn e é ele que o `trap` mata
  APP_PID="$!"
  SUBIU=1
  local i
  for i in $(seq 1 60); do
    [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$APP/health")" = "200" ] && break
    sleep 2
  done
  [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$APP/health")" = "200" ] \
    || { fala "   7890 não subiu (pid $APP_PID)"; exit 1; }
  fala "   health 200 (pid $APP_PID)"
}

# ── fases ───────────────────────────────────────────────────────────────────
marca() { print -r -- "$1 $2 $(date +%H:%M:%S)" >> "$DIR/marcas.txt"; }
roda_ciclos() {           # $1 = célula (f0-c0 …)
  fala "== ciclos $1  $(date -Iseconds)"
  # AQUECIMENTO: +$AQUEC ciclo(s) fora da amostra. O `live_bench` escreve a
  # marca `aquec` antes e `ciclos` no fim dele — a janela do stt começa ali.
  TTS_LIVE_DUMP_DIR="$DIR/dump-$1" "$PY" -I "$APP_DIR/tools/live_bench.py" \
    --audio "$CORPUS" --url "$URL" --seed "$SEMENTE" --ciclos "$((CICLOS + AQUEC))" \
    --aquecimento "$AQUEC" --marca "$DIR/marcas.txt" \
    --teto-turno-s 300 --rotulo "$1" --out "$DIR/$1.json" > "$DIR/$1.log" 2>&1
  fala "   rc=$? $(grep -E '^S1 ' "$DIR/$1.log" | cut -c1-80)"
}
roda_fase() {             # $1 = fase (eco|ventoinha)  $2 = célula
  fala "== fase $1 $2  $(date -Iseconds)"
  marca "$2" "$1"
  local min=$MIN_VENT
  [ "$1" = eco ] && min=$MIN_ECO
  "$PY" "$APP_DIR/tools/fase_ruido.py" "$1" --url "$URL" --audio "$CORPUS" \
    --minutos "$min" --out "$DIR/$1-$2.json" >> "$DIR/$1-$2.log" 2>&1
  fala "   rc=$? $(tail -1 "$DIR/$1-$2.log" | cut -c1-80)"
}
celula() {                # $1 = f0|f1  $2 = c0|c1
  local motor
  motor=parakeet; [ "$2" = c0 ] && motor=whisper
  curl -s -m 5 -o /dev/null -H "Content-Type: application/json" \
    -X POST "$APP/api/settings" -d "{\"live_stt_engine\": \"$motor\"}"
  roda_ciclos "$1-$2"; roda_fase eco "$1-$2"; roda_fase ventoinha "$1-$2"
}

# valor ORIGINAL da chave: lido do ARQUIVO de estado (a porta ainda está livre
# na pré-voo, então perguntar ao app daria vazio e o `trap` não restauraria nada)
ANTERIOR="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("live_stt_engine") or "")' \
  "$ESTADO/settings.json" 2>/dev/null)"
fala "   live_stt_engine no estado: '${ANTERIOR}'"

ORDEM=(f0-c0 f0-c1 f1-c1 f1-c0)
: > "$DIR/marcas.txt"
FLAG_ATUAL=""
for cel in "${ORDEM[@]}"; do
  flag="${cel%%-*}"
  if [ "$flag" != "$FLAG_ATUAL" ]; then
    derruba
    sobe "$([ "$flag" = f0 ] && print -r -- 0 || print -r -- 1)"
    FLAG_ATUAL="$flag"
  fi
  celula "${cel%%-*}" "${cel##*-}"
done
derruba
print -r -- "fim $(date +%H:%M:%S)" >> "$DIR/marcas.txt"

# ── relatório ───────────────────────────────────────────────────────────────
fala "== relatório  $(date -Iseconds)"
"$PY" "$APP_DIR/tools/relatorio_rodada.py" --dir "$DIR" --sha "$SHA" \
  --logs "$DIR/app-0.log" "$DIR/app-1.log" --marcas "$DIR/marcas.txt" \
  --aquecimento "$AQUEC" 2>&1 | tee -a "$LOG_CORRIDA"
fala "== fim  $(date -Iseconds) — saída em $DIR"
