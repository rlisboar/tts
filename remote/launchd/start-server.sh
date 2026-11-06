#!/bin/zsh
# Sobe o servidor no mini — chamado pelo agente `studio.tts.server` (ver
# `studio.tts.server.plist`); cópia literal do que roda em produção (#348).
#
# A limpeza antes do `exec` não é zelo: se o launchd mata o ssh do agente, o
# `run.sh` fica ÓRFÃO segurando a 7860 e o próximo morre com "address already in
# use" — o ciclo vira KeepAlive falhando em silêncio. O laço de restart do
# `run.sh` (segfault do Metal) fica no `run.sh`, não aqui: este script é o boot.
#
# A limpeza mata SÓ o que é da produção: o `run.sh` anterior (pidfile) e quem
# estiver NA PORTA de produção. O `pkill -f "uvicorn app:app"` anterior levava
# junto instância de teste/bench em 7861-7865 — e o laço do `run.sh` ressuscitava
# o uvicorn no meio do restart, disputando a porta com o processo novo.
#
# Instalar (no mini): mkdir -p ~/.tts-studio && cp remote/launchd/start-server.sh
# ~/.tts-studio/ && chmod +x ~/.tts-studio/start-server.sh
PORTA="${TTS_SERVER_PORTA:-7860}"
REPO="${TTS_SERVER_REPO:-/Users/lisboa/Documents/tts-rod}"
PIDFILE="$HOME/.tts-studio/server.pid"

# Derruba a ÁRVORE a partir do pid (folha antes do pai). Filho de uvicorn não
# escuta porta nenhuma: `intencao_worker` (3,8 GB), `marca_worker`,
# `separa_worker`, `tts_worker`. Matar só o listener deixa todos órfãos com
# PPID 1 segurando GPU/RAM — e o `run.sh` ainda ressuscita o uvicorn em 2 s.
_mata_arvore() {
  local raiz="$1" filho
  for filho in $(pgrep -P "$raiz" 2>/dev/null); do
    _mata_arvore "$filho"
  done
  kill "$raiz" 2>/dev/null || true
}

# 1) o `run.sh` anterior e a árvore dele: sem isto o laço ressuscita o uvicorn
if [ -f "$PIDFILE" ]; then
  antigo="$(cat "$PIDFILE" 2>/dev/null)"
  case "$antigo" in
    ''|*[!0-9]*) ;;
    *) if ps -o command= -p "$antigo" 2>/dev/null | grep -q "run\.sh"; then
         _mata_arvore "$antigo"
       fi ;;
  esac
fi
# 2) quem ainda estiver na porta de produção (uvicorn órfão de um ssh morto) —
# a árvore dele primeiro, o `run.sh` pai depois
for pid in $(lsof -ti ":$PORTA" -sTCP:LISTEN 2>/dev/null); do
  [ "$pid" = "$$" ] && continue
  _mata_arvore "$pid"
  pai="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
  [ -n "$pai" ] && ps -o command= -p "$pai" 2>/dev/null | grep -q "run\.sh" \
    && _mata_arvore "$pai"
done
# 3) rede de segurança para worker que já perdeu o pai (crash anterior): aí não
# há árvore para descer, só o caminho do REPO — worktree é `…/tts-rod-wt/…` e
# não casa com a barra final de `…/tts-rod/`
for _w in tts_worker marca_worker separa_worker intencao_worker; do
  pkill -f "$REPO/${_w}\.py" 2>/dev/null || true
done
sleep 1
cd "$REPO" || exit 1
mkdir -p "$(dirname "$PIDFILE")"
echo $$ > "$PIDFILE"          # `exec` mantém o pid: o arquivo aponta para o run.sh
# #837: o `EnvironmentVariables` do plist não atravessa o `ssh` do agente (ssh só
# encaminha com SendEnv/AcceptEnv), então a allowlist de `Host` do #779 mora
# AQUI. Sem ela o túnel leva 421 e a página pública sai do ar.
export TTS_ALLOWED_HOSTS="${TTS_ALLOWED_HOSTS:-tts.the-dudes.com}"
# #813/#789: origens de PÁGINA autorizadas a ler a API. Sem a origem pública o
# preflight (`X-API-Key` é header não-simples) volta 400 "Disallowed CORS origin"
# e o navegador reporta "Failed to fetch" — a página sobe e nada carrega. A LAN
# entra junto porque a UI também é aberta pelo IP.
export TTS_ORIGENS_PERMITIDAS="${TTS_ORIGENS_PERMITIDAS:-https://tts.the-dudes.com,https://the-dudes.com,https://www.the-dudes.com,http://192.168.15.34:7860,http://192.168.15.48:7860}"

exec /bin/zsh ./run.sh
