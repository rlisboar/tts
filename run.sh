#!/bin/zsh
# Inicia o TTS-STUDIO em http://127.0.0.1:7860 (backend MLX)
# Reinicia sozinho se o processo morrer com segfault/OOM (exit ≠ 0,130,143).
cd "$(dirname "$0")"

# O dsh scandira ~/.dsh/sessions ao subir; com o teto duro herdado baixo
# (zsh/launchd variam: 4096-256) o handshake morre com EMFILE. O kernel aceita
# 184320 por processo (kern.maxfilesperproc); subir o SOFT até o HARD herdado
# é permitido sem privilégio e resolve. O _raise_nofile do dsh_client cobre o
# filho; isto aqui cobre o próprio processo do app (imports/load abrem fds tb).
_soft=$(ulimit -Sn); _hard=$(ulimit -Hn)
if [ "$_hard" != "unlimited" ] && [ "$_soft" -lt "$_hard" ]; then
  ulimit -Sn "$_hard" 2>/dev/null || true
fi
if [ "$_hard" = "unlimited" ] || [ "$(ulimit -Sn)" -lt 65536 ]; then
  ulimit -Sn 65536 2>/dev/null || true   # best-effort: hard unlimited aceita subir
fi

# onnxruntime tenta gravar o ID de telemetria em $HOME; com HOME não-gravável
# (sandbox/CI) ele cai num arquivo ":memory:.ses" no CWD, que é a raiz do repo.
# O app é local/offline: desligar a telemetria mata o artefato e o warning.
export ORT_DISABLE_TELEMETRY="${ORT_DISABLE_TELEMETRY:-1}"

# DSH_HOME dedicado ao TTS: o dsh scandira ~/.dsh/sessions a cada handshake e o
# HOME compartilhado acumula milhares de sessões de outros usos — EMFILE no
# handshake do Live (#259). Home próprio, raso e sob nosso controle; credentials
# e profiles ficam em ~/$DSH_HOME (symlinks abaixo).
if [ -z "$DSH_HOME" ]; then
  export DSH_HOME="$HOME/.dsh-tts"
  mkdir -p "$DSH_HOME"
  # profiles/settings precisam ser COPIA (o dsh resolve o path canônico do home;
  # symlink para ~/.dsh/profiles não é encontrado). Sessions NÃO copia: cada
  # processo dsh cria as suas aqui dentro, raso e sob controle do sweeper.
  [ -d "$HOME/.dsh/profiles" ] && cp -R "$HOME/.dsh/profiles" "$DSH_HOME/" 2>/dev/null
  [ -f "$HOME/.dsh/settings.yaml" ] && cp "$HOME/.dsh/settings.yaml" "$DSH_HOME/" 2>/dev/null
  # .credentials.yaml (GLM_API_KEY e rotas) É LIDO DO HOME DEDICADO — sem ele o
  # handshake sobe mas o TURNO falha ("no credential for provider route glm").
  [ -f "$HOME/.dsh/.credentials.yaml" ] && cp "$HOME/.dsh/.credentials.yaml" "$DSH_HOME/" 2>/dev/null
fi

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
  KEY_PREFIX="${TTS_ROD_API_KEY:0:4}"
  KEY_SUFFIX="${TTS_ROD_API_KEY: -4}"
  echo "Chave da API carregada: ${KEY_PREFIX}…${KEY_SUFFIX} (use Configurações → Acesso → Revelar)"
else
  echo "Chaves da API: gerencie em Configurações → Acesso (UI)"
fi

# 0.0.0.0 = acessível na rede local (outros dispositivos usam o IP/hostname do Mac)
# loop: Metal/MLX às vezes segfaulta (exit 139) ao trocar modelos pesados —
# sobe de novo sozinho, a menos que o usuário encerre (Ctrl+C = 130) ou kill (143/SIGTERM).
#
# Encerrar aqui é LIMPO (#358): SIGINT/SIGTERM chegam ao uvicorn, que roda o
# `lifespan` do app — fecha sessões Live, workers de job e dsh quente e salva o
# histórico de jobs. Por isso 130/143 saem do laço em vez de virar restart, e por
# isso o `/api/shutdown` faz o mesmo teardown antes de sair.
# Sweeper: sessões dsh do TTS >48h viram lixo (handshakes antigos) e o scandir
# delas é o que estoura EMFILE — manter o diretório raso em cada boot.
find "$DSH_HOME/sessions" -mindepth 2 -maxdepth 2 -type d -mtime +2 -exec rm -rf {} + 2>/dev/null &

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
