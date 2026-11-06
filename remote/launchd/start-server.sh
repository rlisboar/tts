#!/bin/zsh
# Sobe o servidor no mini — chamado pelo agente `studio.tts.server` (ver
# `studio.tts.server.plist`); cópia literal do que roda em produção (#348).
#
# A limpeza antes do `exec` não é zelo: se o launchd mata o ssh do agente, o
# `run.sh` fica ÓRFÃO segurando a 7860 e o próximo morre com "address already in
# use" — o ciclo vira KeepAlive falhando em silêncio. O laço de restart do
# `run.sh` (segfault do Metal) fica no `run.sh`, não aqui: este script é o boot.
#
# Instalar (no mini): mkdir -p ~/.tts-studio && cp remote/launchd/start-server.sh
# ~/.tts-studio/ && chmod +x ~/.tts-studio/start-server.sh
pkill -f "zsh \./run\.sh" 2>/dev/null || true
pkill -f "uvicorn app:app" 2>/dev/null || true
sleep 1
cd /Users/lisboa/Documents/tts-rod || exit 1
# #837: o `EnvironmentVariables` do plist não atravessa o `ssh` do agente (ssh só
# encaminha com SendEnv/AcceptEnv), então a allowlist de `Host` do #779 mora
# AQUI. Sem ela o túnel leva 421 e a página pública sai do ar.
export TTS_ALLOWED_HOSTS="${TTS_ALLOWED_HOSTS:-tts.the-dudes.com}"
# #813/#789: origens de PÁGINA autorizadas a ler a API. Sem a origem pública o
# preflight (`X-API-Key` é header não-simples) volta 400 "Disallowed CORS origin"
# e o navegador reporta "Failed to fetch" — a página sobe e nada carrega. A LAN
# entra junto porque a UI também é aberta pelo IP.
export TTS_ORIGENS_PERMITIDAS="${TTS_ORIGENS_PERMITIDAS:-https://tts.the-dudes.com,http://192.168.15.34:7860,http://192.168.15.48:7860}"

exec /bin/zsh ./run.sh
