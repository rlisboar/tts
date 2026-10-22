#!/bin/zsh
# Túnel reverso do TTS-STUDIO (Mac → VPS): acesso pela internet, processamento local.
#
#   internet ──▶ https://seu-dominio (nginx na VPS) ──▶ 127.0.0.1:PORTA (VPS)
#            ──▶ túnel SSH (o Mac conecta PRA FORA; nada de portas abertas em casa)
#            ──▶ IP-LAN-do-Mac:7860
#
# O destino é o IP da LAN do Mac, NÃO 127.0.0.1: a API exige chave fora do
# loopback — via 127.0.0.1 a internet entraria SEM chave (loopback é isento).
# Isso é VALIDADO antes de abrir o ssh (`_motivo_destino`): destino fora de
# 10/8, 172.16/12 e 192.168/16 é recusado com o motivo, em vez de virar um túnel
# "de pé servindo nada". `TTS_TUNNEL_IF` escolhe a interface (padrão: a da rota
# default) e, por ser escolha manual, falha na hora em vez de insistir.
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

# Interface de onde sai o destino: `TTS_TUNNEL_IF` manda (é o override de quem
# sabe o que faz); sem ele, a interface da rota default.
_interface_alvo() {
  if [ -n "${TTS_TUNNEL_IF:-}" ]; then
    print -r -- "$TTS_TUNNEL_IF"
    return
  fi
  route -n get default 2>/dev/null | awk '/interface:/{print $2}'
}

# As três faixas privadas do RFC1918. `100.64/10` (CGNAT, típico de VPN/tailnet)
# fica FORA de propósito: a VPS chega no nosso IP de LAN, não nesse.
lan_valida() {
  case "$1" in
    10.*|192.168.*) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
  esac
  return 1
}

# Por que validar (o invariante do projeto): pela 127.0.0.1 a API entra SEM
# chave — loopback é isento (app.py `_is_local`). Um túnel que caia em loopback
# abre a instância para a internet em silêncio; um que caia em `utun` (VPN com a
# rota default) aponta para um IP que a VPS não alcança e o túnel fica "de pé,
# servindo nada". Validar aqui é o que dá sentido ao resto do script.
_motivo_destino() { # $1 = interface, $2 = ip; imprime o motivo e devolve 1, ou 0
  if [ -z "$1" ]; then
    print -r -- "sem interface de saída (rota default ausente — Wi-Fi desligado?)"
    return 1
  fi
  if [ -z "$2" ]; then
    print -r -- "interface $1 sem endereço de LAN"
    return 1
  fi
  if ! lan_valida "$2"; then
    print -r -- "destino $2 em $1 NÃO é IP de LAN (esperado 10/8, 172.16/12 ou 192.168/16)"
    return 1
  fi
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

# Sem escolha manual de interface, o Wi-Fi pode estar associando (é o caso do
# LaunchAgent no login): insiste por um tempo — mas só por um tempo, porque com
# VPN na rota default o endereço não muda e esperar para sempre esconde o motivo.
TENTATIVAS_MAX="${TTS_TUNNEL_TENTATIVAS:-6}"
tentativas=0
while true; do
  IF_TUNEL="$(_interface_alvo)"
  TGT="$(ipconfig getifaddr "$IF_TUNEL" 2>/dev/null)"
  if ! motivo="$(_motivo_destino "$IF_TUNEL" "$TGT")"; then
    echo "[$(date '+%H:%M:%S')] $motivo"
    case "$TGT" in
      127.*|::1|localhost) echo "  $IF_TUNEL devolve loopback: use a interface da LAN (ex.: en0), nunca 127.0.0.1." ;;
    esac
    if [ -n "${TTS_TUNNEL_IF:-}" ]; then   # escolha manual não muda sozinha: falha claro
      echo "  destino tem de ser IP de LAN: por loopback a API entra SEM chave."
      exit 2
    fi
    tentativas=$((tentativas + 1))
    if [ "$tentativas" -ge "$TENTATIVAS_MAX" ]; then
      echo "  $TENTATIVAS_MAX tentativas sem destino de LAN — desistindo (define TTS_TUNNEL_IF)."
      exit 2
    fi
    sleep 10
    continue
  fi
  tentativas=0
  SSH_OPTS=(-N -R "127.0.0.1:${RPORT}:${TGT}:7860"
            -o ServerAliveInterval=30 -o ServerAliveCountMax=3
            -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new
            -o ConnectTimeout=10
            -o IdentitiesOnly=yes -o BatchMode=yes)
  [ -f "$KEY" ] && SSH_OPTS+=(-i "$KEY")
  echo "[$(date '+%H:%M:%S')] túnel: ${DEST}:127.0.0.1:${RPORT} → ${TGT}:7860 (LAN ok, via $IF_TUNEL)"
  ssh "${SSH_OPTS[@]}" "$DEST"
  rc=$?
  if [ $rc -eq 0 ] || [ $rc -eq 130 ] || [ $rc -eq 143 ]; then
    echo "[$(date '+%H:%M:%S')] túnel encerrado."
    exit $rc
  fi
  echo "[$(date '+%H:%M:%S')] túnel caiu (exit $rc) — reconectando em 5s…"
  sleep 5
done
