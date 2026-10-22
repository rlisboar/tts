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
exec /bin/zsh ./run.sh
