#!/usr/bin/env bash
# Sonda EXTERNA da produção (#1152): só `GET /health`, pela LAN e pelo hostname público.
# Nasceu da queda de 2026-11-11 19:49 → 11-12 15:26: o firewall do mini (ALF) segurou as
# conexões de entrada do Python por ~20 h e ninguém viu, porque o smoke só roda no deploy.
#
#   tools/probe_externo.sh uma        # uma passada (para launchd StartInterval=300 / cron)
#   tools/probe_externo.sh laco       # passa a cada TTS_PROBE_INTERVALO s (padrão 300) até ser morta
#   tools/probe_externo.sh status     # contadores de falhas seguidas e o alarme em aberto
#   tools/probe_externo.sh instala    # LaunchAgent com.local.tts-probe-externo (a cada 5 min)
#   tools/probe_externo.sh desinstala
#
# Roda no Mac de dev E no mini (cada um vê a produção do SEU ponto de vista). NUNCA usa chave:
# /health é público por desenho, a URL não carrega segredo e nada de chave é lido ou gravado.
# Alarme: depois de TTS_PROBE_FALHAS (padrão 2) falhas SEGUIDAS no mesmo alvo → linha ALARME no
# log, arquivo $ESTADO/ALARME.<alvo> (lan|publico) e, se houver, TTS_PROBE_ALARME_CMD (recebe PROBE_ALVO/PROBE_FALHAS/
# PROBE_MSG no ambiente — é o gancho para avisar o PM). Repete a cada 12 passadas enquanto durar
# e loga RECUPERADO quando voltar (apaga só o ALARME do alvo recuperado).
set -u
umask 077
LAN="${TTS_PROBE_LAN:-http://192.168.15.34:7860/health}"
PUBLICO="${TTS_PROBE_PUBLICO:-https://tts.the-dudes.com/health}"
ESTADO="${TTS_PROBE_ESTADO:-$HOME/.tts-studio/probe}"
LOG="${TTS_PROBE_LOG:-$HOME/Library/Logs/tts-studio/probe-externo.log}"
N="${TTS_PROBE_FALHAS:-2}"
INTERVALO="${TTS_PROBE_INTERVALO:-300}"
TIMEOUT="${TTS_PROBE_TIMEOUT:-10}"
LABEL=com.local.tts-probe-externo
mkdir -p "$ESTADO" "$(dirname "$LOG")"

_log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG"; }

# 0 = 200 e corpo com "ok"; senão 1. Só /health; nenhum header, nenhuma chave.
_sonda() { # $1 = url
  local tmp cod; tmp="$(mktemp)"
  cod="$(curl -s -m "$TIMEOUT" -o "$tmp" -w '%{http_code}' "$1" 2>/dev/null)" || cod=000
  if [ "$cod" = 200 ] && grep -q '"ok"' "$tmp" 2>/dev/null; then rm -f "$tmp"; return 0; fi
  rm -f "$tmp"; return 1
}

_alarma() { # $1 = alvo $2 = falhas $3 = mensagem
  local msg="$3"
  _log "ALARME $1 falhas_seguidas=$2 — $msg"
  printf '%s %s falhas=%s %s\n' "$(date '+%F %T')" "$1" "$2" "$msg" > "$ESTADO/ALARME.$1"
  if [ -n "${TTS_PROBE_ALARME_CMD:-}" ]; then
    PROBE_ALVO="$1" PROBE_FALHAS="$2" PROBE_MSG="$msg" bash -c "$TTS_PROBE_ALARME_CMD" >/dev/null 2>&1 || _log "aviso: TTS_PROBE_ALARME_CMD saiu ≠ 0"
  fi
}

_passada() {
  local alvo url f arq
  for alvo in lan publico; do
    case "$alvo" in lan) url="$LAN" ;; publico) url="$PUBLICO" ;; esac
    arq="$ESTADO/falhas.$alvo"; f=0; [ -f "$arq" ] && f="$(cat "$arq" 2>/dev/null)"; f="${f:-0}"
    if _sonda "$url"; then
      if [ "$f" -ge "$N" ]; then
        _log "RECUPERADO $alvo após $f falha(s) seguidas"
        [ -n "${TTS_PROBE_ALARME_CMD:-}" ] && PROBE_ALVO="$alvo" PROBE_FALHAS=0 PROBE_MSG="recuperado" bash -c "$TTS_PROBE_ALARME_CMD" >/dev/null 2>&1
        rm -f "$ESTADO/ALARME.$alvo"      # só o DESTE alvo: recuperar a LAN não apaga o alarme do público
      else
        _log "ok $alvo"
      fi
      echo 0 > "$arq"
    else
      f=$((f + 1)); echo "$f" > "$arq"
      _log "FALHA $alvo ($f seguida(s)) $url"
      if [ "$f" -eq "$N" ] || { [ "$f" -gt "$N" ] && [ $(( (f - N) % 12 )) -eq 0 ]; }; then
        _alarma "$alvo" "$f" "/health $alvo sem 200 há $f passada(s) (a produção pode estar inacessível POR FORA — ver deploy_mini.sh smoke e o log do ALF do mini)"
      fi
    fi
  done
}

case "${1:-}" in
  uma) _passada ;;
  laco) while :; do _passada; sleep "$INTERVALO"; done ;;
  status)
    for alvo in lan publico; do printf '%s: %s falha(s) seguida(s)\n' "$alvo" "$(cat "$ESTADO/falhas.$alvo" 2>/dev/null || echo 0)"; done
    achou=0
    for alvo in lan publico; do
      [ -f "$ESTADO/ALARME.$alvo" ] && { echo "ALARME em aberto ($alvo): $(cat "$ESTADO/ALARME.$alvo")"; achou=1; }
    done
    [ "$achou" = 1 ] || echo "sem alarme" ;;
  instala)
    ag="$HOME/Library/LaunchAgents/$LABEL.plist"; aqui="$(cd "$(dirname "$0")" && pwd)/probe_externo.sh"
    # o launchd NÃO lê ~/Documents (TCC, #1128): copia o script para ~/.tts-studio e roda de lá
    mkdir -p "$HOME/.tts-studio" "$HOME/Library/LaunchAgents"; cp "$aqui" "$HOME/.tts-studio/probe_externo.sh"; chmod 700 "$HOME/.tts-studio/probe_externo.sh"
    ENVXML=""
    if [ -n "${TTS_PROBE_ALARME_CMD:-}" ]; then
      cmd_xml="$(printf '%s' "$TTS_PROBE_ALARME_CMD" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
      ENVXML="<key>EnvironmentVariables</key><dict><key>TTS_PROBE_ALARME_CMD</key><string>$cmd_xml</string></dict>"
    fi
    cat > "$ag" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$HOME/.tts-studio/probe_externo.sh</string><string>uma</string></array>
${ENVXML}<key>StartInterval</key><integer>300</integer>
<key>RunAtLoad</key><true/>
</dict></plist>
PL
    chmod 600 "$ag"
    # Sem gancho o alarme é PASSIVO (log + arquivo que ninguém lê) — e a premissa
    # do incidente é justamente "ninguém viu". Avisa alto, sem impedir a instalação.
    if [ -z "${TTS_PROBE_ALARME_CMD:-}" ]; then
      echo "aviso: TTS_PROBE_ALARME_CMD não definido — o alarme fica só no log ($LOG) e em $ESTADO/ALARME.*." >&2
      echo "       ex.: TTS_PROBE_ALARME_CMD='osascript -e \"display notification \\\"\$PROBE_MSG\\\" with title \\\"produção \$PROBE_ALVO\\\"\"'" >&2
    fi
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    if launchctl bootstrap "gui/$(id -u)" "$ag"; then echo "instalado: $LABEL (a cada 300 s) — log $LOG"
    else echo "ERRO: launchctl bootstrap falhou — a sonda NÃO está agendada (plist em $ag)" >&2; exit 1; fi ;;
  desinstala)
    # Remove o que o `instala` criou: o agente, o plist e a CÓPIA do script
    # (~/.tts-studio, por TCC). O estado vai junto — contador órfão confundiria
    # uma reinstalação. O log fica: é diagnóstico, e o caminho é impresso.
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/.tts-studio/probe_externo.sh"
    rm -rf "$ESTADO"
    echo "removido: $LABEL (agente, plist e cópia do script) — log preservado em $LOG" ;;
  *) echo "uso: $0 uma | laco | status | instala | desinstala" >&2; exit 2 ;;
esac
