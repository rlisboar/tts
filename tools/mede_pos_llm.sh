#!/bin/sh
# Rodada LOCAL do pos_llm (#1022): instância isolada + LLM falso de ritmo fixo +
# bancada. Estado sintético em tmp (#728): nada de settings/chaves/vozes do dono.
#
#   tools/mede_pos_llm.sh <rotulo> [ciclos]
#
# Env: PORTA (7895), PORTA_LLM (7899), TTFT_MS (300), TPS (40),
#      FLEURS (~/tts-bench/fleurs/fala_pt, montado por tools/bakeoff/wer_pt_real.py --monta;
#      fora do /tmp: o tmp_cleaner do macOS o apaga)
# Corpus e voz da sessão saem do FLEURS pt_br (CC BY 4.0) — NUNCA de voices/ (#728).
# Saída: /tmp/pos-llm-<rotulo>-<pid>.json e .log (sufixo por execução: rodada
# repetida com o mesmo rótulo não sobrescreve a anterior).
set -eu
ROTULO="${1:?rótulo}"
CICLOS="${2:-6}"
PORTA="${PORTA:-7895}"
PORTA_LLM="${PORTA_LLM:-7899}"
FLEURS="${FLEURS:-$HOME/tts-bench/fleurs/fala_pt}"
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
EST="$(mktemp -d /tmp/pos-llm-estado.XXXXXX)"
# sufixo por execução: rodada repetida com o mesmo rótulo não sobrescreve a
# anterior (mesma classe do #12/#240/#998 — caminho fixo em /tmp compartilhado).
SUF="$$"
LOG="/tmp/pos-llm-$ROTULO-$SUF.log"
SAIDA="/tmp/pos-llm-$ROTULO-$SUF.json"

limpa() {
  [ -n "${PID_APP:-}" ] && kill "$PID_APP" 2>/dev/null || true
  [ -n "${PID_LLM:-}" ] && kill "$PID_LLM" 2>/dev/null || true
  /bin/rm -rf "$EST"
}
trap limpa EXIT INT TERM

# corpus público no formato da bancada (wav + json com ref_text) e UMA voz de
# clone para a sessão, as duas em tmp
# corpus e voz da sessão do FLEURS (#728), pela mesma ferramenta da rodada
"$PY" "$RAIZ/tools/corpus_bancada.py" --fleurs "$FLEURS" --destino "$EST" >/dev/null
AUDIO="$EST/corpus"; VOZ=flvoz

"$PY" "$RAIZ/tools/llm_fake_sse.py" --porta "$PORTA_LLM" \
  --ttft-ms "${TTFT_MS:-300}" --tps "${TPS:-40}" >>"$LOG" 2>&1 &
PID_LLM=$!

cd "$RAIZ"
TTS_STATE_DIR="$EST" TTS_SETTINGS_PATH="$EST/settings.json" \
TTS_APIKEYS_PATH="$EST/apikeys.json" TTS_LEGACY_APIKEY_PATH="$EST/apikey" \
TTS_METRICS_DIR="$EST/metrics" TTS_SPEAKER_PATH="$EST/speaker-profiles.json" \
TTS_SPEAKER_AUDIO_DIR="$EST/speaker-audio" TTS_VOICES_DIR="$EST/voices" \
TTS_CHAT_BASE_URL="http://127.0.0.1:$PORTA_LLM/v1" TTS_CHAT_MODEL=fake \
TTS_CHAT_BACKEND=openai TTS_CHAT_BACKEND_LIVE=openai \
TTS_LIVE_TTL_S=1800 TTS_MARCA_PREWARM_BOOT=1 \
  "$RAIZ/.venv-mlx/bin/uvicorn" app:app --host 127.0.0.1 --port "$PORTA" >>"$LOG" 2>&1 &
PID_APP=$!

i=0
until curl -fs "http://127.0.0.1:$PORTA/health" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -gt 240 ] && { echo "app não subiu" >&2; exit 1; }
  sleep 1
done

"$PY" -u "$RAIZ/tools/live_bench.py" --url "ws://127.0.0.1:$PORTA/api/live/ws" \
  --audio "$AUDIO" --voice "$VOZ" --ciclos "$CICLOS" --seed 875 --teto-turno-s 120 \
  --out "$SAIDA" --rotulo "$ROTULO" >>"$LOG" 2>&1 || true

"$PY" - "$SAIDA" <<'EOF'
import json, sys
s2 = json.load(open(sys.argv[1]))["resumo"]["S2"]
print(f"S2 p50 {s2['p50_ms']}  pos_llm p50 {s2['pos_llm_p50_ms']} p95 {s2['pos_llm_p95_ms']}")
for k, v in s2["pos_llm_estagios"].items():
    print(f"  {k:10s} n={v['n']:3d}  p50={v['p50_ms']}  p95={v['p95_ms']}")
EOF
