#!/bin/zsh
# Inicia o TTS-Rod em http://127.0.0.1:7860 (backend MLX)
cd "$(dirname "$0")"

if [ ! -d .venv-mlx ]; then
  echo "Criando ambiente virtual (Python 3.12)…"
  python3.12 -m venv .venv-mlx
  ./.venv-mlx/bin/pip install --upgrade pip
  ./.venv-mlx/bin/pip install -r requirements.txt
fi

# Chave de API: gerada uma vez e persistida em .apikey (TTS_ROD_API_KEY sobrepõe)
if [ -z "$TTS_ROD_API_KEY" ]; then
  if [ ! -f .apikey ]; then
    openssl rand -hex 24 > .apikey
    chmod 600 .apikey
  fi
  export TTS_ROD_API_KEY="$(cat .apikey)"
fi
echo "Chave da API: $TTS_ROD_API_KEY"

# 0.0.0.0 = acessível na rede local (outros dispositivos usam o IP/hostname do Mac)
exec ./.venv-mlx/bin/uvicorn app:app --host 0.0.0.0 --port 7860
