#!/bin/zsh
# Rotação do log de produção do TTS-STUDIO SEM root (#1128).
#
# O log da 7860 saía em `/tmp/tts-studio.log`: o macOS o varre (`tmp_cleaner`, 00:00) e
# o reboot o zera — o histórico de `barge_*` que mede o S3 (#870) se perdeu assim. Agora
# vive em `~/Library/Logs/tts-studio/` (dir 0700, arquivo 0600). O `newsyslog` exigiria
# sudo (`/etc/newsyslog.d`); este script faz a rotação no próprio usuário, por launchd.
#
#   remote/log_rotate.sh rotaciona     # uma passada (é o que o agente roda a cada hora)
#   remote/log_rotate.sh install       # instala o agente com.local.tts-log-rotate
#   remote/log_rotate.sh uninstall
#
# Como rotaciona: passou de TTS_LOG_MAX_MB (padrão 50) → copia para `.1`, comprime
# (`.1.gz`), desloca as gerações (`.2.gz`, `.3.gz`; teto TTS_LOG_GERACOES, padrão 3) e
# TRUNCA O ORIGINAL NO LUGAR (`: > arquivo`). O launchd abre o log em append, então o
# processo continua escrevendo no mesmo inode, sem perder linha nem precisar de restart.
# Geração mais velha que TTS_LOG_DIAS (padrão 14) também sai. Tudo 0600 (umask 077).
#
# O mascaramento de segredo é do app (#1106, `uvicorn.access`): este script não lê nem
# imprime o conteúdo do log.
set -u
umask 077
LABEL="com.local.tts-log-rotate"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DIR="${TTS_LOG_DIR:-$HOME/Library/Logs/tts-studio}"
LOG="${TTS_LOG_ARQ:-$DIR/tts-studio.log}"
MAX_MB="${TTS_LOG_MAX_MB:-50}"
GER="${TTS_LOG_GERACOES:-3}"
DIAS="${TTS_LOG_DIAS:-14}"
case "$MAX_MB" in ''|*[!0-9]*|0*) MAX_MB=50 ;; esac
case "$GER"    in ''|*[!0-9]*|0*) GER=3 ;; esac
case "$DIAS"   in ''|*[!0-9]*|0*) DIAS=14 ;; esac

rotaciona() {
  [ -f "$LOG" ] || { print -r -- "sem $LOG — nada a rotacionar"; return 0; }
  chmod 700 "$DIR" 2>/dev/null; chmod 600 "$LOG" 2>/dev/null
  local tam=$(stat -f %z "$LOG")
  if [ "$tam" -gt $((MAX_MB * 1024 * 1024)) ]; then
    local i
    /bin/rm -f "$LOG.$GER.gz"
    for ((i = GER - 1; i >= 1; i--)); do
      [ -f "$LOG.$i.gz" ] && mv -f "$LOG.$i.gz" "$LOG.$((i + 1)).gz"
    done
    # copia → trunca no lugar (mesmo inode; append do launchd segue valendo) → comprime
    cp "$LOG" "$LOG.1" && : > "$LOG" && gzip -f "$LOG.1" \
      && print -r -- "rotacionado: $tam bytes → $LOG.1.gz"
  else
    print -r -- "log com $tam bytes (teto $((MAX_MB * 1024 * 1024))) — sem rotação"
  fi
  # idade: gerações comprimidas velhas demais saem
  find "$DIR" -maxdepth 1 -name 'tts-studio.log.*.gz' -mtime "+$DIAS" -exec /bin/rm -f {} + 2>/dev/null
  return 0
}

case "${1:-}" in
  rotaciona) rotaciona ;;
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$DIR"; chmod 700 "$DIR"
    # O agente do launchd NÃO lê `~/Documents` (TCC do macOS): "can't open input file" na 1ª
    # passada (achado no deploy do #1064). O script se COPIA para `~/.tts-studio` e o plist
    # aponta para a cópia — atualizar o script do repo exige reinstalar (`install` de novo).
    ALVO="$HOME/.tts-studio/log_rotate.sh"
    mkdir -p "$HOME/.tts-studio"
    [ "${0:A}" = "$ALVO" ] || { cp -f "${0:A}" "$ALVO" && chmod 755 "$ALVO"; }
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>$ALVO</string><string>rotaciona</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/rotacao.log</string>
  <key>StandardErrorPath</key><string>$DIR/rotacao.log</string>
</dict></plist>
PL
    chmod 600 "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$PLIST" && print -r -- "instalado: $LABEL (a cada hora)" ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; /bin/rm -f "$PLIST"; print -r -- "removido: $LABEL" ;;
  *) print -r -- "uso: $0 rotaciona|install|uninstall" >&2; exit 2 ;;
esac
