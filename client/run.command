#!/bin/zsh
# Roteador de microfone do TTS-Rod. Dois cliques no Finder abrem a janela.
# (1ª vez: clique direito > Abrir, para autorizar no Gatekeeper)
# Cria um venv local, instala sounddevice/numpy e roda a janela.
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -d "$VENV" ]; then
  echo "Criando ambiente (1ª vez)…"
  python3 -m venv "$VENV" || { echo "Falha ao criar venv"; sleep 3; exit 1; }
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet sounddevice numpy || { echo "Falha ao instalar deps"; sleep 3; exit 1; }
fi

# avisa se o BlackHole não estiver instalado
if ! "$VENV/bin/python" - <<'PY'
import sounddevice as sd, sys
ok = any("blackhole" in d["name"].lower() for d in sd.query_devices())
sys.exit(0 if ok else 1)
PY
then
  echo "BlackHole não encontrado. Instale com:  brew install blackhole-2ch"
  osascript -e 'display notification "Instale o BlackHole: brew install blackhole-2ch" with title "TTS-Rod · mic"' 2>/dev/null
fi

exec "$VENV/bin/python" mic_router.py
