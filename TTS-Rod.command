#!/bin/zsh
# Botão liga/desliga do TTS-Rod. Dê dois cliques no Finder:
#   - servidor parado  -> inicia (em segundo plano) e abre o navegador
#   - servidor rodando -> para
# (na 1ª vez: clique direito > Abrir, para autorizar no Gatekeeper)
cd "$(dirname "$0")"

if pgrep -f "uvicorn app:app" >/dev/null 2>&1; then
  pkill -f "uvicorn app:app"
  osascript -e 'display notification "Servidor parado" with title "TTS-Rod"' 2>/dev/null
  echo "TTS-Rod parado."
else
  nohup ./run.sh >/tmp/tts-rod.log 2>&1 &
  # espera a porta subir (até ~15s) antes de abrir o navegador
  for i in {1..50}; do
    curl -s -o /dev/null http://127.0.0.1:7860/api/status && break
    sleep 0.3
  done
  open http://127.0.0.1:7860
  osascript -e 'display notification "Servidor iniciado em http://127.0.0.1:7860" with title "TTS-Rod"' 2>/dev/null
  echo "TTS-Rod iniciado. Log em /tmp/tts-rod.log"
fi
sleep 1
