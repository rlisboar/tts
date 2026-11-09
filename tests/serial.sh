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
#
# TETO DEPOIS DA TRAVA (#535): o runner arma um teto de FILA largo e exporta
# `TTS_TETO_POS_TRAVA`; aqui o alarme é trocado pelo teto REAL no instante em que
# a trava é obtida. Sem isso, os 90 s do teto eram gastos esperando na fila com
# `-j 3` e a suíte morria antes de trabalhar (medido: 4 suítes por SIGALRM).
_TRAVA_DIR="${TTS_SERIAL_LOCK:-/tmp/tts-rod-modelo.lock}"

# mtime portátil (BSD/macOS e GNU/Linux). A ORDEM importa e não dá para usar
# `||` entre as duas formas: no GNU o `stat -f` NÃO falha — ele imprime stats do
# FILESYSTEM, então o fallback `stat -c %Y` nunca rodava, a idade saía lixo e
# nenhum lock velho/órfão era roubado no Linux (medido no CI da #293: 7 asserções
# de `serial_lock.sh` vermelhas só por isso).
if stat --version >/dev/null 2>&1; then          # GNU coreutils
  _serial_mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }
else                                             # BSD/macOS
  _serial_mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }
fi

# Watchdog do teto de trabalho: só existe se o runner pediu (TTS_TETO_POS_TRAVA).
#
# O bash ADIA trap enquanto um filho em foreground roda, então o ALRM sozinho não
# basta: suíte que trabalha dentro de UM `python` longo só veria o alarme quando o
# python terminasse — e aí o teto não vale nada. Medido (#592): sob carga o python
# do Playwright pode NÃO morrer no primeiro TERM, o trap nunca roda e a suíte passa
# de 828 s com teto de 90 s, sem UMA linha de log. Daí a escada, e daí a MENSAGEM
# sair do vigia (que é outro processo, não é afetado pelo adiamento): ela existe
# mesmo se o shell nunca mais rodar um trap.
#
# A escada vai até KILL: ALRM → TERM nos filhos → (1 s) KILL nos filhos → (2 s)
# TERM no shell → (2 s) KILL no shell; um teto de 2 s mata em ~6 s. O TERM antes do KILL no shell é o que
# preserva o trap de EXIT (soltar a trava).
#
# `_SERIAL_TRABALHANDO` (#592) é armado aqui e lido no rótulo: adivinhar por "sou
# eu o pid da trava?" invertia com a trava mexida por concorrência e a mensagem
# mentia (suíte de 93 s, dona da trava, acusada de estouro de FILA).
_TETO_MARCA=""
_teto_armar() {
  [ -n "${TTS_TETO_POS_TRAVA:-}" ] || return 0
  _SERIAL_TRABALHANDO=1
  _TETO_MARCA="${TMPDIR:-/tmp}/tts-teto.$$"
  /bin/rm -f "$_TETO_MARCA"
  (
    sleep "$TTS_TETO_POS_TRAVA"
    # o shell pode ter saído sem `_teto_matar` (suíte que sobrescreve o trap EXIT):
    # aí o vigia só existe para não mandar ALRM num PID que já é de outro.
    kill -0 $$ 2>/dev/null || exit 0
    : > "$_TETO_MARCA"          # o trap de ALRM passa a não repetir a mensagem
    echo "  [serial] teto de ${TTS_TETO_POS_TRAVA}s estourado DEPOIS da trava (trabalho, não fila)"
    kill -ALRM $$ 2>/dev/null
    sleep 1
    for filho in $(pgrep -P $$ 2>/dev/null); do kill -TERM "$filho" 2>/dev/null; done
    sleep 1
    for filho in $(pgrep -P $$ 2>/dev/null); do kill -KILL "$filho" 2>/dev/null; done
    sleep 2
    kill -TERM $$ 2>/dev/null
    sleep 2
    kill -KILL $$ 2>/dev/null
  ) &
  _TETO_PID=$!
}
_teto_matar() {
  [ -n "${_TETO_PID:-}" ] && kill "$_TETO_PID" 2>/dev/null
  _TETO_PID=""
  [ -n "${_TETO_MARCA:-}" ] && /bin/rm -f "$_TETO_MARCA"
  # `return 0` OBRIGATÓRIO (#592): sem o vigia armado as duas linhas acima são
  # `[ -n "" ] && …` e voltam 1, então o trap de EXIT devolvia 1 e suíte VERDE saía
  # rc=1 (`./tests/voz_check_ui.sh` imprimia "✔ OK" e o runner marcava FALHA).
  return 0
}

# Remove a trava e só devolve 0 quando ela SUMIU de fato. Com um `rm` de PATH que
# sai 0 sem apagar (guard do harness, #996) o `rm -rf` não tirava nada e o laço
# reencontrava a MESMA trava para sempre, imprimindo "assumindo" a cada 0,5 s
# (~1,5 MB de log por rodada). O teto troca o laço infinito por erro claro, com o
# caminho na mensagem — vale também se o `rm` falhar de verdade (permissão, disco).
_serial_remove_trava() {
  local n=0 teto="${TTS_SERIAL_REMOCAO_TETO:-5}"
  # #1013: teto não numérico (ou 0) fazia o `-gt` errar e o teto ficar INERTE —
  # de volta ao laço infinito. Só inteiro positivo vale; o resto cai no 5.
  case "$teto" in
    ''|*[!0-9]*|0*) teto=5 ;;
  esac
  until [ ! -e "$_TRAVA_DIR" ]; do
    n=$((n + 1))
    if [ "$n" -gt "$teto" ]; then
      echo "  [serial] não consegui remover a trava $_TRAVA_DIR (rm não apaga?) — desisto" >&2
      return 1
    fi
    /bin/rm -rf "$_TRAVA_DIR" 2>/dev/null
    [ -e "$_TRAVA_DIR" ] && sleep 0.1
  done
  return 0
}

serial_pega() {
  if [ "${TTS_SERIAL:-1}" = "0" ]; then _teto_armar; return 0; fi
  local espera="${TTS_SERIAL_ESPERA:-1800}" t0=$SECONDS pid idade dono avisou=""
  local eu; eu="$(basename "${1:-$0}")"
  while :; do
    if mkdir "$_TRAVA_DIR" 2>/dev/null; then
      echo "$$" > "$_TRAVA_DIR/pid"
      echo "$eu" > "$_TRAVA_DIR/qual"
      _teto_armar      # a partir daqui o relógio é o do TRABALHO
      return 0
    fi
    pid="$(cat "$_TRAVA_DIR/pid" 2>/dev/null || true)"
    dono="$(cat "$_TRAVA_DIR/qual" 2>/dev/null || echo "?")"
    idade=$(( $(date +%s) - $(_serial_mtime "$_TRAVA_DIR") ))
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      echo "  [serial] trava órfã (pid $pid morto) — assumindo"
      _serial_remove_trava || return 1; continue
    fi
    if [ -z "$pid" ] && [ "$idade" -gt 5 ]; then
      # `mkdir` feito e pid ainda não escrito é uma janela de ms; sem pid com
      # idade é dona morta entre os dois (SIGKILL). Sem este ramo a trava SEM pid
      # só cairia no TTL (40 min) e a suíte pareceria pendurada.
      echo "  [serial] trava sem pid (${idade}s) — assumindo"
      _serial_remove_trava || return 1; continue
    fi
    if [ "$idade" -gt "${TTS_SERIAL_TTL:-2400}" ]; then
      echo "  [serial] trava órfã (${idade}s) — assumindo"
      _serial_remove_trava || return 1; continue
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
  /bin/rm -rf "$_TRAVA_DIR"
  return 0
}
# O teto estourado depois da trava tem mensagem própria (a suíte está trabalhando,
# não esperando): sem ela o SIGALRM parece pendura da fila — e o texto tem de
# distinguir os dois, senão o operador procura contenção onde não tem.
# Dois caminhos chegam aqui: o vigia (já imprimiu; a marca silencia) e o teto de
# FILA do runner (ALRM do perl, antes da trava — `_SERIAL_TRABALHANDO` ainda 0).
_serial_alarme() {
  if [ -n "${_TETO_MARCA:-}" ] && [ -e "$_TETO_MARCA" ]; then
    exit 1
  fi
  if [ "${_SERIAL_TRABALHANDO:-0}" = 1 ]; then
    echo "  [serial] teto de ${TTS_TETO_POS_TRAVA:-?}s estourado DEPOIS da trava (trabalho, não fila)"
  else
    echo "  [serial] teto de FILA estourado esperando a trava"
  fi
  exit 1
}
trap '_serial_alarme' ALRM
trap 'serial_solta; _teto_matar; exit 130' INT TERM
trap 'serial_solta; _teto_matar' EXIT