#!/bin/zsh
# Inicia o TTS-STUDIO em http://127.0.0.1:7860 (backend MLX)
# Reinicia sozinho se o processo morrer com segfault/OOM (exit ≠ 0,130,143).
cd "$(dirname "$0")"

if [ ! -d .venv-mlx ]; then
  echo "Criando ambiente virtual (Python 3.12)…"
  python3.12 -m venv .venv-mlx
  ./.venv-mlx/bin/pip install --upgrade pip
  ./.venv-mlx/bin/pip install -r requirements.txt
fi

# YouTube quebra clients antigos do yt-dlp (HTTP 403). Garante o piso do
# requirements sem reresolver o resto do venv a cada subida.
if ! ./.venv-mlx/bin/python -c "import yt_dlp; v=tuple(int(x) for x in yt_dlp.version.__version__.split('.')[:2]); raise SystemExit(0 if v>=(2026,8) else 1)"; then
  echo "Atualizando yt-dlp (YouTube 403 em versões antigas)…"
  ./.venv-mlx/bin/pip install -q 'yt-dlp>=2026.08.19'
fi

# Chaves de API: geridas na UI (Configurações → Acesso) e em .apikeys.json.
# .apikey / TTS_ROD_API_KEY ainda bootstrapam na 1ª subida e valem como chave extra.
if [ -z "$TTS_ROD_API_KEY" ] && [ -f .apikey ]; then
  export TTS_ROD_API_KEY="$(tr -d '[:space:]' < .apikey)"
fi
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
echo "UI local:  http://127.0.0.1:7860"
if [ -n "$LAN_IP" ]; then
  echo "UI rede:   http://$LAN_IP:7860  (outros dispositivos precisam da chave)"
fi
if [ -n "$TTS_ROD_API_KEY" ]; then
  echo "Chave da API (cole no outro dispositivo): $TTS_ROD_API_KEY"
else
  echo "Chaves da API: gerencie em Configurações → Acesso (UI)"
fi

# 0.0.0.0 = acessível na rede local (outros dispositivos usam o IP/hostname do Mac)
# loop: Metal/MLX às vezes segfaulta (exit 139) ao trocar modelos pesados —
# sobe de novo sozinho, a menos que o usuário encerre (Ctrl+C = 130) ou kill (143/SIGTERM).
while true; do
  ./.venv-mlx/bin/uvicorn app:app --host 0.0.0.0 --port 7860
  code=$?
  # parada limpa: Ctrl+C (130) / SIGTERM (143) / exit 0
  if [ $code -eq 0 ] || [ $code -eq 130 ] || [ $code -eq 143 ]; then
    exit $code
  fi
  echo ""
  echo "⚠ servidor caiu (exit $code) — reiniciando em 2s… (Ctrl+C para parar)"
  sleep 2
done
