#!/bin/zsh
# Túnel reverso do TTS-STUDIO (Mac → VPS): acesso pela internet, processamento local.
#
#   internet ──▶ https://seu-dominio (nginx na VPS) ──▶ 127.0.0.1:PORTA (VPS)
#            ──▶ túnel SSH (o Mac conecta PRA FORA; nada de portas abertas em casa)
#            ──▶ IP-LAN-do-Mac:7860
#
# O destino é o IP da LAN do Mac, NÃO 127.0.0.1: a API exige chave fora do
# loopback — via 127.0.0.1 a internet entraria SEM chave (loopback é isento).
#
# Uso:
#   ./tunnel.sh usuario@vps [porta]            # sobe o túnel (foreground, reconecta)
#   ./tunnel.sh install usuario@vps [porta]    # gera chave SSH + LaunchAgent (auto-start)
#   ./tunnel.sh uninstall                      # remove o LaunchAgent
#
# Na VPS (uma vez): nginx com proxy_pass http://127.0.0.1:PORTA e a chave
# pública em authorized_keys com permitlisten — passo a passo no README.
set -u
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
KEY="${TTS_TUNNEL_KEY:-$HOME/.ssh/tts_tunnel}"
PLIST_LABEL="studio.tts.tunnel"
PLIST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

cmd="${1:-}"
case "$cmd" in
  install|uninstall) shift ;;
esac
DEST="${1:-}"; RPORT="${2:-7860}"

lan_ip() {
  local IF
  IF="${TTS_TUNNEL_IF:-$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')}"
  [ -n "$IF" ] && ipconfig getifaddr "$IF" 2>/dev/null
  return 0
}

case "$cmd" in
  uninstall)
    launchctl bootout "gui/$UID/$PLIST_LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "LaunchAgent removido ($PLIST_LABEL)."
    exit 0 ;;
  install)
    [ -n "$DEST" ] || { echo "uso: $0 install usuario@vps [porta]"; exit 1; }
    [ -f "$KEY" ] || ssh-keygen -t ed25519 -N "" -C tts-tunnel -f "$KEY" -q
    # launchd não lê ~/Documents (TCC) — instala uma cópia fora da proteção
    RUNTIME="$HOME/.tts-studio/tunnel.sh"
    mkdir -p "$HOME/.tts-studio" && cp "$SELF" "$RUNTIME" && chmod +x "$RUNTIME"
    # NOTA: 'restrict' quebra o forward em alguns OpenSSH ("server has disabled
    # port forwarding") — hardening equivalente com no-* + permitlisten/permitopen
    echo "Chave pública — adicione na VPS (~/.ssh/authorized_keys), numa única linha:"
    echo
    echo "command=\"\",no-pty,no-agent-forwarding,no-X11-forwarding,permitlisten=\"127.0.0.1:${RPORT}\",permitopen=\"127.0.0.1:${RPORT}\" $(cat "$KEY.pub")"
    echo
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$PLIST_LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>$RUNTIME</string><string>$DEST</string><string>$RPORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/tts-tunnel.log</string>
  <key>StandardErrorPath</key><string>/tmp/tts-tunnel.log</string>
</dict></plist>
EOF
    launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null \
      || launchctl load "$PLIST" 2>/dev/null || true
    echo "Instalado — sobe no login e reconecta sozinho. Log: /tmp/tts-tunnel.log"
    echo "Depois de autorizar a chave na VPS, teste: https://seu-dominio/health"
    exit 0 ;;
esac

[ -n "$DEST" ] || { echo "uso: $0 usuario@vps [porta]  |  $0 install usuario@vps [porta]"; exit 1; }

while true; do
  TGT="$(lan_ip)"
  if [ -z "$TGT" ]; then
    echo "[$(date '+%H:%M:%S')] sem IP de LAN (Wi-Fi off?) — tentando de novo em 10s"
    sleep 10
    continue
  fi
  SSH_OPTS=(-N -R "127.0.0.1:${RPORT}:${TGT}:7860"
            -o ServerAliveInterval=30 -o ServerAliveCountMax=3
            -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new
            -o ConnectTimeout=10)
  [ -f "$KEY" ] && SSH_OPTS+=(-i "$KEY")
  echo "[$(date '+%H:%M:%S')] túnel: ${DEST}:127.0.0.1:${RPORT} → ${TGT}:7860"
  ssh "${SSH_OPTS[@]}" "$DEST"
  rc=$?
  if [ $rc -eq 0 ] || [ $rc -eq 130 ] || [ $rc -eq 143 ]; then
    echo "[$(date '+%H:%M:%S')] túnel encerrado."
    exit $rc
  fi
  echo "[$(date '+%H:%M:%S')] túnel caiu (exit $rc) — reconectando em 5s…"
  sleep 5
done
