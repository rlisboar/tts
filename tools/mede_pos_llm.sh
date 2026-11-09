#!/bin/sh
# Rodada LOCAL do pos_llm (#1022): instância isolada + LLM falso de ritmo fixo +
# bancada. Estado sintético em tmp (#728): nada de settings/chaves/vozes do dono.
#
#   tools/mede_pos_llm.sh <rotulo> [ciclos]
#
# Env: PORTA (7895), PORTA_LLM (7899), TTFT_MS (300), TPS (40),
#      FLEURS (/tmp/tts-bakeoff/fala_pt, montado por tools/bakeoff/wer_pt_real.py --monta)
# Corpus e voz da sessão saem do FLEURS pt_br (CC BY 4.0) — NUNCA de voices/ (#728).
# Saída: /tmp/pos-llm-<rotulo>.json e .log
set -eu
ROTULO="${1:?rótulo}"
CICLOS="${2:-6}"
PORTA="${PORTA:-7895}"
PORTA_LLM="${PORTA_LLM:-7899}"
FLEURS="${FLEURS:-/tmp/tts-bakeoff/fala_pt}"
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
EST="$(mktemp -d /tmp/pos-llm-estado.XXXXXX)"
LOG="/tmp/pos-llm-$ROTULO.log"

limpa() {
  [ -n "${PID_APP:-}" ] && kill "$PID_APP" 2>/dev/null || true
  [ -n "${PID_LLM:-}" ] && kill "$PID_LLM" 2>/dev/null || true
  /bin/rm -rf "$EST"
}
trap limpa EXIT INT TERM

# corpus público no formato da bancada (wav + json com ref_text) e UMA voz de
# clone para a sessão, as duas em tmp
AUDIO="$EST/corpus"; mkdir -p "$AUDIO" "$EST/voices"
VOZ="$("$PY" - "$FLEURS" "$AUDIO" "$EST/voices" <<'PY'
import json, pathlib, shutil, sys
src, dst, voz = map(pathlib.Path, sys.argv[1:])
meta = json.loads((src / "meta.json").read_text())
nomes = sorted(meta["refs"])
for i, nome in enumerate(nomes):
    vid = f"fl{i:02d}"
    shutil.copy(src / nome, dst / f"{vid}.wav")
    (dst / f"{vid}.json").write_text(json.dumps(
        {"id": vid, "name": vid, "ref_text": meta["refs"][nome]}, ensure_ascii=False))
# a voz da sessão é a última fala, que fica FORA do corpus do mic
alvo, ult = "flvoz", f"fl{len(nomes)-1:02d}"
(dst / f"{ult}.wav").rename(voz / f"{alvo}.wav")
d = json.loads((dst / f"{ult}.json").read_text()); d["id"] = d["name"] = alvo
(voz / f"{alvo}.json").write_text(json.dumps(d, ensure_ascii=False))
(dst / f"{ult}.json").unlink()
print(alvo)
PY
)"

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
  --out "/tmp/pos-llm-$ROTULO.json" --rotulo "$ROTULO" >>"$LOG" 2>&1 || true

"$PY" - "/tmp/pos-llm-$ROTULO.json" <<'EOF'
import json, sys
s2 = json.load(open(sys.argv[1]))["resumo"]["S2"]
print(f"S2 p50 {s2['p50_ms']}  pos_llm p50 {s2['pos_llm_p50_ms']} p95 {s2['pos_llm_p95_ms']}")
for k, v in s2["pos_llm_estagios"].items():
    print(f"  {k:10s} n={v['n']:3d}  p50={v['p50_ms']}  p95={v['p95_ms']}")
EOF
