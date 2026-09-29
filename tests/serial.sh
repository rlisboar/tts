#!/usr/bin/env bash
# Trava de MODELO entre suítes — task #134.
#
# POR QUE: as suítes que carregam Whisper/Kokoro disputam Metal/CPU quando rodam
# em paralelo, e o alvo de latência de ponta do live_ws.sh (fim-de-fala → 1º
# áudio ≤ 1500 ms) vira FALSO VERMELHO. Medido pelo QA: 1792 ms em paralelo
# (pre-warm 15,1 s, stt_ms 1118) contra 884 ms sozinho, segundos depois.
# O alvo está certo; o que faltava era as suítes não se atropelarem.
#
# USO (no topo da suíte, antes de subir servidor/navegador):
#     source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1
# A trava é liberada no exit (inclusive falha/sinal) pelo trap armado aqui.
#
# ESCAPES: TTS_SERIAL=0 desliga a trava; TTS_SERIAL_ESPERA=Ns ajusta o timeout de
# espera; TTS_SERIAL_TTL=Ns ajusta quando um lock ÓRFÃO (suíte morta por SIGKILL)
# pode ser roubado — além do TTL, um lock cujo PID já morreu é roubado na hora.
_TRAVA_DIR="${TTS_SERIAL_LOCK:-/tmp/tts-rod-modelo.lock}"

# mtime portátil (BSD/macOS e GNU/Linux)
_serial_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

serial_pega() {
  [ "${TTS_SERIAL:-1}" = "0" ] && return 0
  local espera="${TTS_SERIAL_ESPERA:-1800}" t0=$SECONDS pid idade dono avisou=""
  local eu; eu="$(basename "${1:-$0}")"
  while :; do
    if mkdir "$_TRAVA_DIR" 2>/dev/null; then
      echo "$$" > "$_TRAVA_DIR/pid"
      echo "$eu" > "$_TRAVA_DIR/qual"
      return 0
    fi
    pid="$(cat "$_TRAVA_DIR/pid" 2>/dev/null || true)"
    dono="$(cat "$_TRAVA_DIR/qual" 2>/dev/null || echo "?")"
    idade=$(( $(date +%s) - $(_serial_mtime "$_TRAVA_DIR") ))
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      echo "  [serial] trava órfã (pid $pid morto) — assumindo"
      rm -rf "$_TRAVA_DIR"; continue
    fi
    if [ -z "$pid" ] && [ "$idade" -gt 5 ]; then
      # `mkdir` feito e pid ainda não escrito é uma janela de ms; sem pid com
      # idade é dona morta entre os dois (SIGKILL). Sem este ramo a trava SEM pid
      # só cairia no TTL (40 min) e a suíte pareceria pendurada.
      echo "  [serial] trava sem pid (${idade}s) — assumindo"
      rm -rf "$_TRAVA_DIR"; continue
    fi
    if [ "$idade" -gt "${TTS_SERIAL_TTL:-2400}" ]; then
      echo "  [serial] trava órfã (${idade}s) — assumindo"
      rm -rf "$_TRAVA_DIR"; continue
    fi
    if [ $((SECONDS - t0)) -gt "$espera" ]; then
      echo "  [serial] trava de modelo não liberou em ${espera}s (dona: $dono)"
      return 1
    fi
    if [ -z "$avisou" ]; then
      echo "  [serial] $eu esperando a trava de modelo (dona: $dono)…"; avisou=1
    fi
    sleep 0.5
  done
}

serial_solta() {
  [ "${TTS_SERIAL:-1}" = "0" ] && return 0
  [ "$(cat "$_TRAVA_DIR/pid" 2>/dev/null || true)" = "$$" ] || return 0
  rm -rf "$_TRAVA_DIR"
  return 0
}

trap 'serial_solta; exit 130' INT TERM
trap 'serial_solta' EXIT