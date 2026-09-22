#!/bin/zsh
# Túnel Cloudflare GERENCIADO REMOTAMENTE (Zero Trust) para o TTS-STUDIO.
#
#   internet ──▶ https://tts.seu-dominio (Cloudflare, TLS) ──▶ tunnel
#            ──▶ conector cloudflared NA MÁQUINA DO TTS ──▶ IP-LAN:7860
#
# Diferença para o tunnel.sh (SSH → VPS): aqui não há VPS nem nginx — o
# conector roda na própria máquina do TTS e o ingress mora no dashboard
# (Zero Trust → Networks → Tunnels). O `run --token` só precisa do token:
# nada de config.yml nem de credenciais locais no host de produção.
#
# O destino é o IP DA LAN, nunca 127.0.0.1: a API dispensa chave no loopback —
# por 127.0.0.1 a internet entraria sem chave (a auth viraria no-op).
#
# Uso (na máquina com `cloudflared tunnel login` feito — normalmente o Macbook):
#   ./cloudflare.sh provision [nome] [hostname] [ip-destino]
#     ex.: ./cloudflare.sh provision tts-mac-mini tts.the-dudes.com 192.168.15.34
#
# Uso (na máquina que roda o TTS):
#   ./cloudflare.sh install <token> [label]
#   ./cloudflare.sh status | uninstall
set -u
LABEL="${CLOUDFLARE_LABEL:-com.local.cloudflared-tts}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/cloudflared-tts.log"
BIN="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"
CERT="$HOME/.cloudflared/cert.pem"
API="https://api.cloudflare.com/client/v4"

# O cert.pem do `tunnel login` é um JSON em base64 com zoneID/accountID/apiToken.
_cert_json() {
  python3 - "$CERT" <<'PY'
import base64, json, re, sys
t = open(sys.argv[1]).read()
print(json.dumps(json.loads(base64.b64decode(re.sub(r"-----[^-]+-----", "", t).replace("\n", "")))))
PY
}

_api() { # _api METHOD PATH [BODY]
  local out
  if [ -n "${3:-}" ]; then
    out="$(curl -s -X "$1" -H "Authorization: Bearer $CF_TOKEN" \
          -H "Content-Type: application/json" --data "$3" "$API$2")"
  else
    out="$(curl -s -X "$1" -H "Authorization: Bearer $CF_TOKEN" "$API$2")"
  fi
  print -r -- "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("success") else "erro: "+json.dumps(d.get("errors")))' >&2
  print -r -- "$out"
}

case "${1:-}" in
  provision)
    NAME="${2:-tts-mac-mini}"; HOST="${3:-tts.the-dudes.com}"; ORIG="${4:-}"
    [ -f "$CERT" ] || { echo "sem $CERT — rode: cloudflared tunnel login"; exit 1; }
    [ -n "$ORIG" ] || { echo "informe o IP LAN da máquina do TTS (ex.: 192.168.15.34)"; exit 1; }
    eval "$(_cert_json | python3 -c 'import sys,json;d=json.load(sys.stdin);print("CF_TOKEN=%s CF_ACCT=%s"%(d["apiToken"],d["accountID"]))')"

    if "$BIN" tunnel list 2>/dev/null | grep -q " $NAME "; then
      echo "tunnel $NAME já existe — reaproveitando"
    else
      "$BIN" tunnel create "$NAME" || exit 1
    fi
    TID="$("$BIN" tunnel list --output json 2>/dev/null | python3 -c "import sys,json;print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$NAME'))")"
    "$BIN" tunnel route dns "$NAME" "$HOST" 2>&1 | grep -v WRN || true

    # ingress remoto: só o hostname do TTS + 404 para o resto
    _api PUT "/accounts/$CF_ACCT/cfd_tunnel/$TID/configurations" \
      "{\"config\":{\"ingress\":[{\"hostname\":\"$HOST\",\"service\":\"http://$ORIG:7860\",\"originRequest\":{\"connectTimeout\":15}},{\"service\":\"http_status:404\"}]}}" >/dev/null

    echo
    echo "Pronto. Na máquina do TTS (IP $ORIG) rode:"
    echo "  ./cloudflare.sh install $("$BIN" tunnel token "$NAME" 2>/dev/null)"
    echo "Depois teste: https://$HOST/health  →  {\"ok\":true}"
    exit 0 ;;

  install)
    TOKEN="${2:-}"; [ -n "$TOKEN" ] || { echo "uso: $0 install <token> [label]"; exit 1; }
    command -v cloudflared >/dev/null || brew install cloudflared || exit 1
    mkdir -p "$HOME/Library/Logs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/cloudflared</string>
    <string>tunnel</string><string>--no-autoupdate</string>
    <string>--metrics</string><string>127.0.0.1:20242</string>
    <string>--loglevel</string><string>info</string>
    <string>run</string><string>--token</string><string>$TOKEN</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
    echo "Conector instalado (sobe no login, reconecta sozinho). Log: $LOG"
    exit 0 ;;

  status)
    launchctl print "gui/$UID/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit" || echo "LaunchAgent não carregado"
    tail -3 "$LOG" 2>/dev/null
    exit 0 ;;

  uninstall)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Conector removido ($LABEL). O tunnel e o DNS seguem no Cloudflare."
    exit 0 ;;
esac

echo "uso: $0 provision [nome] [hostname] [ip-destino] | install <token> | status | uninstall"
exit 1