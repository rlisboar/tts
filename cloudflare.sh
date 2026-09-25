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
# O TOKEN não vai para o stdout: o `provision` grava ele em
# `~/.cloudflared/<nome>.token` (0600, criado com umask 077) e imprime um
# `install --token-file <arquivo>` para colar — fora do scrollback e do
# history do shell. `./cloudflare.sh token` mostra o token quando você
# realmente precisar dele.
#
# Uso (na máquina com `cloudflared tunnel login` feito — normalmente o Macbook):
#   ./cloudflare.sh provision [nome] [hostname] [ip-destino]
#     ex.: ./cloudflare.sh provision tts-mac-mini tts.the-dudes.com 192.168.15.34
#
# Uso (na máquina que roda o TTS):
#   ./cloudflare.sh install --token-file ~/.cloudflared/tts-mac-mini.token [label]
#   ./cloudflare.sh install <token> [label]     # token cru colado no shell
#   ./cloudflare.sh install - [label]           # token pelo stdin
#   ./cloudflare.sh status | uninstall | token [arquivo]
#
# Análise estática: `zsh -n cloudflare.sh` e `shellcheck -s bash cloudflare.sh`
# (o shellcheck não tem dialeto zsh; o que ele estranha — `print`, `(N)` de
# glob, `${var//x/y}` — é zsh de propósito).
set -u
# Tudo que este script cria carrega segredo (o token vai dentro do plist e do
# arquivo do token): nasce 0600, sem janela legível entre escrever e chmodar.
umask 077
LABEL="${CLOUDFLARE_LABEL:-com.local.cloudflared-tts}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/cloudflared-tts.log"
BIN="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"
TOKEN_DIR="$HOME/.cloudflared"
CERT="$TOKEN_DIR/cert.pem"
API="https://api.cloudflare.com/client/v4"

# O cert.pem do `tunnel login` é um JSON em base64 com zoneID/accountID/apiToken.
_cert_json() {
  python3 - "$CERT" <<'PY'
import base64, json, re, sys
t = open(sys.argv[1]).read()
print(json.dumps(json.loads(base64.b64decode(re.sub(r"-----[^-]+-----", "", t).replace("\n", "")))))
PY
}

# Campo do cert.pem SEM eval: o valor sai cru. Com o eval antigo o valor era
# reinterpretado pelo shell (espaço quebrava em comando, `$x` expandia,
# `*` virava glob) — token com caractere especial mentia em silêncio.
_cert_field() { # $1 = campo do JSON (apiToken | accountID)
  _cert_json | python3 -c 'import json,sys;print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

# O que entra em <string> do plist precisa de entidade para & < >.
_xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"
  print -rn -- "$s"
}

_api() { # _api METHOD PATH [BODY] → rc≠0 quando a API responde success:false
  local out
  if [ -n "${3:-}" ]; then
    out="$(curl -s -X "$1" -H "Authorization: Bearer $CF_TOKEN" \
          -H "Content-Type: application/json" --data "$3" "$API$2")"
  else
    out="$(curl -s -X "$1" -H "Authorization: Bearer $CF_TOKEN" "$API$2")"
  fi
  print -r -- "$out"
  # o rc tem de ser o do python (antes terminava no `print` acima = sempre 0 e um
  # ingress não configurado passava batido)
  print -r -- "$out" | python3 -c 'import sys,json
d = json.load(sys.stdin)
print("ok" if d.get("success") else "erro: " + json.dumps(d.get("errors")))
sys.exit(0 if d.get("success") else 1)' >&2
}

case "${1:-}" in
  provision)
    NAME="${2:-tts-mac-mini}"; HOST="${3:-tts.the-dudes.com}"; ORIG="${4:-}"
    [ -f "$CERT" ] || { echo "sem $CERT — rode: cloudflared tunnel login"; exit 1; }
    [ -n "$ORIG" ] || { echo "informe o IP LAN da máquina do TTS (ex.: 192.168.15.34)"; exit 1; }
    CF_TOKEN="$(_cert_field apiToken)"
    CF_ACCT="$(_cert_field accountID)"
    [ -n "$CF_TOKEN" ] && [ -n "$CF_ACCT" ] || { echo "cert.pem sem apiToken/accountID — rode: cloudflared tunnel login"; exit 1; }

    if "$BIN" tunnel list 2>/dev/null | grep -q " $NAME "; then
      echo "tunnel $NAME já existe — reaproveitando"
    else
      "$BIN" tunnel create "$NAME" || exit 1
    fi
    TID="$("$BIN" tunnel list --output json 2>/dev/null | python3 -c "import sys,json;print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$NAME'))")"
    [ -n "$TID" ] || { echo "não achei o id do tunnel $NAME"; exit 1; }
    "$BIN" tunnel route dns "$NAME" "$HOST" 2>&1 | grep -v WRN || true

    # ingress remoto: só o hostname do TTS + 404 para o resto
    _api PUT "/accounts/$CF_ACCT/cfd_tunnel/$TID/configurations" \
      "{\"config\":{\"ingress\":[{\"hostname\":\"$HOST\",\"service\":\"http://$ORIG:7860\",\"originRequest\":{\"connectTimeout\":15}},{\"service\":\"http_status:404\"}]}}" >/dev/null \
      || { echo "o ingress remoto NÃO foi configurado (erro da API acima)"; exit 1; }

    # O token sai do cloudflared direto para o arquivo (nunca pelo stdout do
    # terminal) e já fica 0600 — o install seguinte pode lê-lo do arquivo.
    TOKEN_FILE="$TOKEN_DIR/${NAME}.token"
    if ! "$BIN" tunnel token "$NAME" > "$TOKEN_FILE" 2>/dev/null; then
      echo "não consegui obter o token do tunnel $NAME"; exit 1
    fi
    [ -s "$TOKEN_FILE" ] || { echo "token vazio em $TOKEN_FILE"; exit 1; }
    chmod 600 "$TOKEN_FILE"   # umask já cria 0600; garante em arquivo pré-existente

    echo
    echo "Pronto. Token em ${TOKEN_FILE/#$HOME/~} (0600) — fora do stdout e do history."
    echo "Na máquina do TTS (IP $ORIG), com o arquivo em mãos:"
    echo "  ./cloudflare.sh install --token-file ${TOKEN_FILE/#$HOME/~}"
    echo "  (outra máquina? copie o arquivo antes — ex.: scp — ou passe o token cru)"
    echo "Ver o token na mão depois: ./cloudflare.sh token"
    echo "Depois teste: https://$HOST/health  →  {\"ok\":true}"
    exit 0 ;;

  install)
    shift
    TOKEN=""; FILE=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -f|--token-file)
          FILE="${2:-}"; [ -n "$FILE" ] || { echo "uso: $0 install --token-file <arquivo> [label]"; exit 1; }
          shift 2 ;;
        -) TOKEN="$(<&0)"; shift ;;   # token pelo stdin (cola do provision)
        *) if [ -z "$TOKEN" ] && [ -z "$FILE" ]; then
             TOKEN="$1"
           else
             LABEL="$1"; PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
           fi
           shift ;;
      esac
    done
    if [ -n "$FILE" ]; then
      [ -r "$FILE" ] || { echo "não achei o arquivo do token: $FILE"; exit 1; }
      # launchd roda o agente com cwd `/`: caminho relativo no plist não resolve,
      # e o sintoma é o conector parado sem erro no install. `:A` absolutiza
      # (o arquivo existe — acabamos de conferir), resolvendo `..` e symlink.
      FILE="${FILE:A}"
      TOKEN="$(<"$FILE")"
    fi
    [ -n "$TOKEN" ] || { echo "uso: $0 install <token> | install --token-file <arquivo> | install -"; exit 1; }
    if [ -z "$FILE" ]; then
      # token cru/stdin: normaliza para arquivo 0600 — o plist passa a usar
      # --token-file e o segredo deixa de aparecer no `ps` (e no próprio plist).
      FILE="$TOKEN_DIR/${LABEL}.token"
      mkdir -p "$TOKEN_DIR"
      print -rn -- "$TOKEN" > "$FILE"
      chmod 600 "$FILE"
      FILE="${FILE:A}"   # idem: o plist nunca guarda caminho relativo
    fi
    command -v cloudflared >/dev/null || brew install cloudflared || exit 1
    BIN="$(command -v cloudflared || echo "$BIN")"   # re-resolve se o brew acabou de instalar
    mkdir -p "$HOME/Library/Logs" "$PLIST:h"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$(_xml_escape "$LABEL")</string>
  <key>ProgramArguments</key><array>
    <string>$(_xml_escape "$BIN")</string>
    <string>tunnel</string><string>--no-autoupdate</string>
    <string>--metrics</string><string>127.0.0.1:20242</string>
    <string>--loglevel</string><string>info</string>
    <string>run</string><string>--token-file</string><string>$(_xml_escape "$FILE")</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$(_xml_escape "$LOG")</string>
  <key>StandardErrorPath</key><string>$(_xml_escape "$LOG")</string>
</dict></plist>
EOF
    chmod 600 "$PLIST"   # defensivo: o plist pode citar caminho de token; antes de carregar
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
    # F1: bootstrap/load têm stderr suprimido — sem esta conferência o install
    # dizia "instalado" e saía 0 mesmo com o launchd recusando o agente.
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      echo "Conector instalado (sobe no login, reconecta sozinho)."
      echo "plist: $PLIST · token em $FILE (0600, fora do stdout e do ps) · log: $LOG"
    else
      echo "AVISO: $LABEL NÃO ficou carregado no launchd (bootstrap/load falharam)" >&2
      echo "  confira: launchctl print gui/$UID/$LABEL" >&2
      exit 1
    fi
    exit 0 ;;

  status)
    # `launchctl print` traz os argumentos (inclusive o token); o grep fica
    # só no estado para não despejar segredo no terminal.
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      launchctl print "gui/$UID/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit"
      tail -3 "$LOG" 2>/dev/null
      exit 0
    fi
    echo "LaunchAgent não carregado ($LABEL)"
    tail -3 "$LOG" 2>/dev/null
    exit 1 ;;

  token)
    # Único ponto que imprime o token — quando você precisa copiá-lo na mão.
    FILE="${2:-}"
    if [ -z "$FILE" ]; then
      # shellcheck disable=SC1036  # (N) é qualificador de glob do zsh
      found=("$TOKEN_DIR"/*.token(N))
      case ${#found} in
        1) FILE="${found[1]}" ;;
        0) echo "nenhum token em $TOKEN_DIR — rode: $0 provision"; exit 1 ;;
        *) echo "mais de um token em $TOKEN_DIR — indique: $0 token <arquivo>"; exit 1 ;;
      esac
    fi
    [ -r "$FILE" ] || { echo "não achei o arquivo do token: $FILE"; exit 1; }
    print -r -- "$(<"$FILE")"
    exit 0 ;;

  uninstall)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Conector removido ($LABEL). O tunnel e o DNS seguem no Cloudflare."
    exit 0 ;;
esac

echo "uso: $0 provision [nome] [hostname] [ip-destino]"
echo "     $0 install --token-file <arquivo> [label] | install <token> | install -"
echo "     $0 status | uninstall | token [arquivo]"
exit 1