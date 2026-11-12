#!/usr/bin/env bash
# Porteiro do CI no MINI (#1132): o job só larga a suíte com o mini OCIOSO.
#
# O runner do GitHub é self-hosted NO mini (`runs-on: [self-hosted, tts-mini]`), então a suíte
# cheia disputa CPU/GPU/memória com a PRODUÇÃO (7860) e com as bancadas (a H6 quase foi
# contaminada). A trava de suíte (`tools/fila_suite.sh`) vive no Mac de dev e não protege o
# mini; este porteiro é o equivalente LÁ. "Ocioso" =
#   • 0 jobs TTS e 0 sessões Live na 7860 (`/api/status`, loopback);
#   • sem bancada: `~/.tts-studio/bancada.lock` ausente (ou órfão: pid morto / mais velho que
#     TTS_CI_BANCADA_TTL_H horas — bancada que morreu não pode travar o CI para sempre);
#   • estável por TTS_CI_ESTAVEL segundos seguidos (entre lotes da narração há vãos curtos).
#
#   tools/ci_mini_ocioso.sh              # espera até TTS_CI_ESPERA s; grava `ocioso=true|false`
#                                        # em $GITHUB_OUTPUT; SEMPRE sai 0 (esgotar a espera é
#                                        # NEUTRO — os passos seguintes viram skipped, não vermelho)
#
# Amostra a cada 1 s (um vão mais curto que o intervalo passaria despercebido).
# Env: TTS_CI_ESPERA (padrão 480 s) · TTS_CI_ESTAVEL (padrão 30 s) · TTS_CI_BANCADA_TTL_H (4)
#      TTS_CI_STATUS_URL (padrão http://127.0.0.1:7860/api/status) · TTS_BANCADA_LOCK (arquivo)
# App fora do ar (sem resposta): não há produção a proteger — conta como ocioso, com aviso.
set -u
ESPERA="${TTS_CI_ESPERA:-480}"
ESTAVEL="${TTS_CI_ESTAVEL:-30}"
TTL_H="${TTS_CI_BANCADA_TTL_H:-4}"
URL="${TTS_CI_STATUS_URL:-http://127.0.0.1:7860/api/status}"
LOCK="${TTS_BANCADA_LOCK:-$HOME/.tts-studio/bancada.lock}"
for v in ESPERA ESTAVEL TTL_H; do
  case "${!v}" in ''|*[!0-9]*) echo "[ci-ocioso] $v inválido (${!v}) — usando o padrão" >&2
    case $v in ESPERA) ESPERA=480;; ESTAVEL) ESTAVEL=30;; TTL_H) TTL_H=4;; esac ;;
  esac
done

_saida() { # $1 = true|false
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "ocioso=$1" >> "$GITHUB_OUTPUT"
  return 0
}

# imprime o motivo de NÃO estar ocioso; vazio = ocioso
_ocupado() {
  if [ -e "$LOCK" ]; then
    local pid idade
    pid="$(sed -n 's/^pid=//p' "$LOCK" 2>/dev/null | head -1)"
    idade=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
    if [ "$idade" -gt $((TTL_H * 3600)) ]; then :   # bancada velha demais: órfã
    elif [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then :  # dono morto: órfã
    else echo "bancada ($(sed -n 's/^agente=//p' "$LOCK" 2>/dev/null | head -1))"; return; fi
  fi
  local corpo
  corpo="$(curl -s -m 5 "$URL" 2>/dev/null)" || corpo=""
  [ -n "$corpo" ] || { echo "" ; return; }                  # app fora do ar: nada a proteger
  /usr/bin/python3 - "$corpo" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(0)                       # resposta ilegível: não segura o CI
j, l = int(d.get("jobs_active") or 0), int(d.get("live_sessions") or 0)
if j or l:
    print(f"produção ocupada (jobs={j}, live={l})")
PY
}

t0=$(date +%s); estavel_desde=""; ult=""
while :; do
  motivo="$(_ocupado)"
  agora=$(date +%s)
  if [ -z "$motivo" ]; then
    [ -n "$estavel_desde" ] || estavel_desde=$agora
    if [ $((agora - estavel_desde)) -ge "$ESTAVEL" ]; then
      echo "[ci-ocioso] mini ocioso há $((agora - estavel_desde))s — pode rodar"; _saida true; exit 0
    fi
  else
    estavel_desde=""
    [ "$motivo" = "$ult" ] || { echo "[ci-ocioso] esperando: $motivo"; ult="$motivo"; }
  fi
  if [ $((agora - t0)) -ge "$ESPERA" ]; then
    echo "[ci-ocioso] o mini não ficou ocioso em ${ESPERA}s (${motivo:-instável}) — NÃO rodei a suíte (neutro)"
    echo "::warning title=CI não rodou a suíte::mini ocupado (${motivo:-instável}) por ${ESPERA}s; rode de novo quando ele estiver ocioso"
    [ -n "${GITHUB_STEP_SUMMARY:-}" ] && echo "⚠ **A suíte NÃO rodou**: o mini estava ocupado (${motivo:-instável}) por ${ESPERA}s. Resultado neutro, não verde." >> "$GITHUB_STEP_SUMMARY"
    _saida false; exit 0
  fi
  sleep 1
done
