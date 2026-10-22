#!/bin/zsh
# Probe de disponibilidade da PRODUÇÃO (tts.the-dudes.com / Mac mini).
#
# Por que existe (#344, achado P1 da auditoria #286): nada vigiava a produção. O
# `/health` respondia, o `/ready` do conector respondia e ambos eram lidos por
# ninguém; produção caída era descoberta pelo uso. Este probe fecha o ciclo
# básico: histórico de 200/401, contador de falhas consecutivas e ALERTA.
#
# Rodar NO MINI (LaunchAgent com StartInterval). Subcomandos:
#   ./remote/health_probe.sh check       # uma rodada (é o que o agente chama)
#   ./remote/health_probe.sh status      # estado + últimas linhas do log
#   ./remote/health_probe.sh install     # instala o LaunchAgent (StartInterval 120)
#   ./remote/health_probe.sh uninstall
#
# O que ele confere, e por quê:
#   • `/health` na LAN **e** no host público, esperando 200 + `"ok":true`. Os dois
#     caminhos são independentes (LAN = app de pé; público = conector + app) e é a
#     diferença entre eles que diz onde está o problema;
#   • `boot_ts` de cada resposta vai para o log: reinício aparece como mudança de
#     valor (útil no histórico, não vira falha);
#   • `/api/voices` SEM chave no host público tem de dar **401** — é o teste canônico
#     do invariante do túnel (200 = destino virou loopback e a auth é no-op).
#
# ALERTA: depois de `TTS_PROBE_FALHAS` falhas consecutivas (padrão 3, ~6 min) ele
# cria `~/Library/Logs/tts-health-ALERTA.txt` e tenta uma notificação do macOS. O
# arquivo-flag é o canal mínimo (existe, é visível e não depende de serviço de
# terceiro); o passo de notificação de verdade está no DEPLOY.md §9 — enquanto o
# dono não escolher o canal, é por aqui que se descobre sem abrir a máquina.
set -u
LABEL="com.local.tts-health-probe"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/tts-health-probe.log"
FLAG="$HOME/Library/Logs/tts-health-ALERTA.txt"
CONTADOR="$HOME/Library/Logs/tts-health-falhas"
HOST="${TTS_PROBE_HOST:-192.168.15.34}"
PORTA="${TTS_PROBE_PORT:-7860}"
PUBLICO="${TTS_PROBE_PUBLIC:-tts.the-dudes.com}"
FALHAS_LIMITE="${TTS_PROBE_FALHAS:-3}"
INTERVALO="${TTS_PROBE_INTERVALO:-120}"
umask 077

_log() { print -r -- "$(date '+%F %T') $*" >> "$LOG" }
_sinal() { # $1 = ok|falha, $2 = rótulo, $3 = detalhe
  # o /health carrega boot_ts/rev: guardá-los faz o histórico distinguir reinício
  # (valor novo) de instância travada (valor repetido/ausente).
  local boot rev
  boot="$(print -r -- "$3" | sed -n 's/.*"boot_ts": *\([0-9]*\).*/\1/p' | head -1)"
  rev="$(print -r -- "$3" | sed -n 's/.*"rev": *"\([^"]*\)".*/\1/p' | head -1)"
  _log "$1 $2 boot_ts=${boot:-?} rev=${rev:-?}"
}

_dispara_alerta() {
  local n="$1"
  {
    print -r -- "ALERTA: produção sem resposta em $n verificações consecutivas"
    print -r -- "quando: $(date '+%F %T')"
    print -r -- "LAN:     http://$HOST:$PORTA/health"
    print -r -- "público: https://$PUBLICO/health"
    print -r -- "log:     $LOG"
    print -r -- ""
    print -r -- "Ação: ver o log do app (/tmp/tts-studio.log no mini), o agente"
    print -r -- "studio.tts.server e o conector ($LABEL de túnel). Limpar este arquivo"
    print -r -- "quando resolver — o probe volta a alertar na próxima sequência."
  } > "$FLAG"
  chmod 600 "$FLAG" 2>/dev/null || true
  osascript -e "display notification \"Produção do TTS-STUDIO sem resposta ($n falhas)\" with title \"ALERTA\"" 2>/dev/null || true
  _log "ALERTA emitido após $n falhas consecutivas (flag em $FLAG)"
}

check() {
  local n falhas_lan falhas_pub auth code corpo
  n=$(cat "$CONTADOR" 2>/dev/null || echo 0)
  [ -n "$n" ] || n=0
  falhas_lan=0; falhas_pub=0

  # LAN: app local de pé?
  code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://$HOST:$PORTA/health" 2>/dev/null || print -r -- 000)"
  corpo="$(curl -s -m 8 "http://$HOST:$PORTA/health" 2>/dev/null || true)"
  if [ "$code" = 200 ] && print -r -- "$corpo" | grep -q '"ok"'; then
    _sinal ok lan "$corpo"
  else
    _sinal falha "lan(http=$code)" "$corpo"; falhas_lan=1
  fi

  # Público: conector + DNS + TLS + app. 000 = sem resposta; 5xx = origin fora.
  code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$PUBLICO/health" 2>/dev/null || print -r -- 000)"
  corpo="$(curl -s -m 10 "https://$PUBLICO/health" 2>/dev/null || true)"
  if [ "$code" = 200 ] && print -r -- "$corpo" | grep -q '"ok"'; then
    _sinal ok publico "$corpo"
  else
    _sinal falha "publico(http=$code)" "$corpo"; falhas_pub=1
  fi

  # Invariante do túnel: rota autenticada sem chave tem de dar 401. 200 aqui
  # significa que o destino do ingress virou loopback e a auth é no-op.
  if [ "${TTS_PROBE_AUTH:-1}" = 1 ]; then
    auth="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$PUBLICO/api/voices" 2>/dev/null || print -r -- 000)"
    case "$auth" in
      401) _log "ok auth(401 sem chave — invariante de pé)" ;;
      200) _log "ALERTA auth(200 sem chave — destino do túnel é LOOPBACK, auth é no-op)"; falhas_pub=1 ;;
      *)   _log "falha auth(http=$auth)"; falhas_pub=1 ;;
    esac
  fi

  if [ "$falhas_lan" = 0 ] && [ "$falhas_pub" = 0 ]; then
    if [ "$n" != 0 ]; then _log "recuperado (havia $n falhas)"; fi
    print -r -- 0 > "$CONTADOR"
    [ -f "$FLAG" ] && { rm -f "$FLAG"; _log "flag de alerta limpa"; }
    return 0
  fi
  n=$((n + 1))
  print -r -- "$n" > "$CONTADOR"
  _log "falha $n/$FALHAS_LIMITE"
  [ "$n" -ge "$FALHAS_LIMITE" ] && _dispara_alerta "$n"
  return 1
}

case "${1:-check}" in
  check) check; exit $? ;;
  status)
    if [ -f "$FLAG" ]; then print -r -- "EM ALERTA:"; cat "$FLAG"; else print -r -- "sem alerta aberto."; fi
    print -r -- "falhas consecutivas: $(cat "$CONTADOR" 2>/dev/null || echo 0)"
    print -r -- "--- últimas linhas:"; tail -8 "$LOG" 2>/dev/null
    exit 0 ;;
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    # Corrige a permissão dos logs existentes (o app já grava 0644 em /tmp):
    # histórico operacional com a chave lida por curl não é segredo, mas 0600 é
    # o padrão das outras peças.
    [ -f /tmp/tts-studio.log ] && chmod 600 /tmp/tts-studio.log 2>/dev/null || true
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>$(cd "$(dirname "$0")" && pwd)/$(basename "$0")</string>
    <string>check</string>
  </array>
  <key>StartInterval</key><integer>$INTERVALO</integer>
  <key>RunAtLoad</key><true/>
  <!-- produção (:7860) tem prioridade sobre a vigia -->
  <key>ProcessType</key><string>Background</string>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
    chmod 600 "$PLIST"
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      print -r -- "probe instalado (cada ${INTERVALO}s; alerta após $FALHAS_LIMITE falhas). Log: $LOG"
      zsh "$0" check >/dev/null 2>&1 || true
      print -r -- "uma rodada já foi feita — veja: zsh $0 status"
    else
      print -r -- "AVISO: $LABEL NÃO ficou carregado (confira: launchctl print gui/$UID/$LABEL)" >&2
      exit 1
    fi
    exit 0 ;;
  uninstall)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    print -r -- "probe removido (log e flag ficam em ~/Library/Logs)."
    exit 0 ;;
esac
print -r -- "uso: $0 check | status | install | uninstall"
exit 1
