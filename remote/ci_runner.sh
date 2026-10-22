#!/bin/zsh
# Runner SELF-HOSTED do GitHub Actions no Mac mini de PRODUÇÃO (192.168.15.34).
#
# Por que existe: o CI da #293 tem de rodar o gate hermético sem depender da
# máquina de quem commita. A suíte é macOS/arm64 na prática (é onde ela nasceu e
# onde o venv é real) e o runner hospedado paga download grande e minutos por
# isso. Custo da escolha, medido: no mini o runner DISPUTA CPU com o :7860, que é
# produção — daí o `ProcessType=Background` + `Nice` do agente (item de aceite:
# com a suíte rodando, `curl -m 5 http://192.168.15.34:7860/health` continua 200).
#
# O que o runner NUNCA toca: o checkout de produção (`~/Documents/tts-rod`). O
# `actions/checkout` escreve em `~/actions-runner/_work`, que é outro diretório —
# um CI verde não pode deixar a produção no meio de um switch de branch.
#
# Uso (NA MÁQUINA DO MINI, não por ssh):
#   ./remote/ci_runner.sh install      # baixa, registra e instala o agente
#   ./remote/ci_runner.sh status | uninstall
#
# Pré-requisitos: `gh` autenticado como dono do repo (só para o token de
# registro, válido ~1 h — nunca versionado) e rede. O runner é registrado com as
# labels `self-hosted,macOS,ARM64,tts-mini`; o workflow da #293 aponta para
# `runs-on: [self-hosted, tts-mini]`.
#
# Análise estática: `zsh -n remote/ci_runner.sh` e `shellcheck -s bash`.
set -u
umask 077
LABEL="com.local.actions-runner-tts"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/actions-runner-tts.log"
DIR="$HOME/actions-runner"
REPO="rlisboar/tts"          # dono/repo do GitHub — derivado do remote do clone
ARQ="actions-runner-osx-arm64.tar.gz"
API="https://api.github.com/repos/$REPO/actions/runners"

_dep() { # dependência dura, com mensagem que diz o que fazer
  command -v "$1" >/dev/null || { print -r -- "falta '$1' — $2" >&2; exit 1; }
}

_registration_token() {
  gh api --method POST "$API/registration-token" --jq .token
}

_versao() { # última versão do runner no release oficial (só o número)
  gh api repos/actions/runner/releases/latest --jq .tag_name | sed 's/^v//'
}

case "${1:-}" in
  install)
    _dep gh "instale com: brew install gh && gh auth login"
    _dep tar "vem com o macOS"
    [ "$(uname -m)" = "arm64" ] || { print -r -- "runner aqui é arm64 (uname -m = $(uname -m))" >&2; exit 1; }
    # O serviço de produção tem de estar de pé ANTES: se o installment derrubar
    # o app, o sintoma é confundido com o próprio CI.
    if ! curl -s -m 5 -o /dev/null http://127.0.0.1:7860/health; then
      print -r -- "AVISO: :7860 não responde — instalar o runner aqui pode confundir o diagnóstico" >&2
    fi
    V="$(_versao)"
    [ -n "$V" ] || { print -r -- "não achei a versão do runner (gh autenticado?)" >&2; exit 1; }
    mkdir -p "$DIR"
    print -r -- "baixando runner v$V em $DIR …"
    curl -fsSL -o "$DIR/$ARQ" \
      "https://github.com/actions/runner/releases/download/v$V/actions-runner-osx-arm64-$V.tar.gz" \
      || { print -r -- "download falhou (versão v$V)" >&2; exit 1; }
    tar -xzf "$DIR/$ARQ" -C "$DIR" && rm -f "$DIR/$ARQ"
    TOKEN="$(_registration_token)"
    [ -n "$TOKEN" ] || { print -r -- "sem token de registro (precisa de admin no repo)" >&2; exit 1; }
    "$DIR/config.sh" --unattended --replace \
      --url "https://github.com/$REPO" --token "$TOKEN" \
      --labels "self-hosted,macOS,ARM64,tts-mini" --work "$DIR/_work" \
      || { print -r -- "config.sh falhou (token expirado? repo errado?)" >&2; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/run.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- produção (:7860) tem prioridade: o CI cede CPU e IO -->
  <key>ProcessType</key><string>Background</string>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
    chmod 600 "$PLIST"   # o plist cita caminho de working dir; nada de segredo, mas o padrão do repo é 0600
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      print -r -- "Runner instalado (label $LABEL, work em $DIR). Log: $LOG"
      print -r -- "Confira: ./remote/ci_runner.sh status · gh api /repos/$REPO/actions/runners --jq '.runners[].name'"
      print -r -- "O workflow usa runs-on: [self-hosted, tts-mini] — sem label, job hospedado."
    else
      print -r -- "AVISO: $LABEL NÃO ficou carregado no launchd — confira: launchctl print gui/$UID/$LABEL" >&2
      exit 1
    fi
    exit 0 ;;

  status)
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      launchctl print "gui/$UID/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit"
      tail -3 "$LOG" 2>/dev/null
      exit 0
    fi
    print -r -- "Runner não carregado ($LABEL)."
    exit 1 ;;

  uninstall)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    [ -x "$DIR/config.sh" ] && "$DIR/config.sh" remove --unattended 2>/dev/null || true
    print -r -- "Runner desregistrado e agente removido. $DIR fica (apague à mão se quiser)."
    exit 0 ;;
esac

print -r -- "uso: $0 install | status | uninstall"
exit 1
