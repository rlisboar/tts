#!/usr/bin/env bash
# Trava ATÔMICA de máquina para a suíte cheia (#1130).
#
# O problema: cada espera checava "máquina vazia" (`pgrep`) e largava depois — checar e
# largar não são atômicos, então duas esperas passavam juntas (hook do #1126 às 01:43:06
# e `integra728` às 01:43:41), e o `pgrep` não ancorado casava com o argv do `claude` e
# nunca liberava. Aqui quem larga é quem TEM a trava: `mkdir` é atômico no mesmo volume.
#
#   tools/fila_suite.sh roda -- <comando…>   # espera a vez, roda o comando, solta a trava
#   tools/fila_suite.sh status               # quem está com a trava e há quanto tempo
#
# Receita do time: suíte cheia SÓ por aqui, dentro do `screen`:
#   screen -dmS suite tools/fila_suite.sh roda -- ./.venv-mlx/bin/python -m pytest tests/ -q
# O hook de pre-commit (que roda a suíte cheia) já usa a trava. Rodada curta (um arquivo)
# NÃO precisa dela.
#
# Contrato:
#   • trava = diretório `$TTS_FILA_DIR/suite` (padrão `~/.tts-rod-locks`, FORA do /tmp que
#     o macOS varre às 00:00), com `pid` (do dono), `agente`, `worktree`, `inicio` e `cmd`;
#   • o dono é ESTE script (fica vivo enquanto o comando roda); libera no trap
#     EXIT/INT/TERM/HUP, em qualquer saída;
#   • trava de dono morto (SIGKILL) é recuperada por `mv` (atômico: só UM recuperador
#     vence) — sem esperar TTL;
#   • reentrância: o comando roda com `TTS_FILA_SEGURA=1`; um `roda` aninhado (o hook
#     chamado de dentro de uma suíte já travada) só executa, sem pegar a trava de novo;
#   • espera até TTS_FILA_ESPERA segundos (padrão 10800 = 3 h); depois sai 75.
#   • TTS_FILA=0 desliga (executa direto) — escape para máquina sem concorrência.
set -u
umask 077
RAIZ="${TTS_FILA_DIR:-$HOME/.tts-rod-locks}"
TRAVA="$RAIZ/suite"
ESPERA="${TTS_FILA_ESPERA:-10800}"
case "$ESPERA" in ''|*[!0-9]*) ESPERA=10800 ;; esac

_agora() { date +%s; }
_le() { cat "$TRAVA/$1" 2>/dev/null || true; }
_dono_vivo() { local p; p="$(_le pid)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

_status() {
  if [ ! -d "$TRAVA" ]; then echo "fila: livre"; return 0; fi
  local p; p="$(_le pid)"
  if _dono_vivo; then
    echo "fila: OCUPADA há $(( $(_agora) - $(_le inicio_s) ))s — agente=$(_le agente) pid=$p worktree=$(_le worktree)"
    echo "      cmd: $(_le cmd)"
  else
    echo "fila: trava ÓRFÃ (dono pid=${p:-?} morto; agente=$(_le agente)) — o próximo 'roda' a recupera"
  fi
}

_recupera_orfa() {
  # `mv` de diretório é atômico: com N recuperadores só um move; os outros falham e
  # voltam ao laço. O cadáver tem nome único, então ninguém apaga a trava NOVA.
  local morto="$RAIZ/.orfa.$$.$RANDOM"
  mv "$TRAVA" "$morto" 2>/dev/null && /bin/rm -rf "$morto" && echo "  [fila] trava órfã (dono morto) recuperada"
}

_solta() {
  [ "$(_le pid)" = "$$" ] || return 0
  local morto="$RAIZ/.solta.$$.$RANDOM"
  mv "$TRAVA" "$morto" 2>/dev/null && /bin/rm -rf "$morto"
}

_pega() {
  local t0 avisou=""
  t0="$(_agora)"
  mkdir -p "$RAIZ" && chmod 700 "$RAIZ"
  while :; do
    if mkdir "$TRAVA" 2>/dev/null; then
      printf '%s\n' "$$" > "$TRAVA/pid"
      printf '%s\n' "${TTS_AGENTE:-$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")}" > "$TRAVA/agente"
      printf '%s\n' "$PWD" > "$TRAVA/worktree"
      printf '%s\n' "$(_agora)" > "$TRAVA/inicio_s"
      printf '%s\n' "$*" > "$TRAVA/cmd"
      return 0
    fi
    if [ -d "$TRAVA" ] && ! _dono_vivo; then
      # `mkdir` feito e `pid` ainda não escrito é uma janela de ms; com idade, é dono
      # morto entre os dois (SIGKILL).
      if [ -n "$(_le pid)" ] || [ "$(( $(_agora) - $(stat -f %m "$TRAVA" 2>/dev/null || stat -c %Y "$TRAVA" 2>/dev/null || _agora) ))" -gt 5 ]; then
        _recupera_orfa; continue
      fi
    fi
    if [ $(( $(_agora) - t0 )) -gt "$ESPERA" ]; then
      echo "  [fila] não liberou em ${ESPERA}s — $(_status | head -1)" >&2; return 75
    fi
    if [ -z "$avisou" ]; then echo "  [fila] esperando a vez — $(_status | head -1)"; avisou=1; fi
    sleep "0.$(( (RANDOM % 7) + 3 ))"
  done
}

case "${1:-}" in
  status) _status ;;
  roda)
    shift; [ "${1:-}" = "--" ] && shift
    [ "$#" -gt 0 ] || { echo "uso: $0 roda -- <comando…>" >&2; exit 2; }
    if [ "${TTS_FILA:-1}" = 0 ] || [ "${TTS_FILA_SEGURA:-0}" = 1 ]; then exec "$@"; fi
    _pega "$@" || exit $?
    FILHO=""
    # sinal: derruba o FILHO (senão a suíte fica órfã sem trava) e só então solta
    _acaba() { [ -n "$FILHO" ] && kill -TERM "$FILHO" 2>/dev/null && wait "$FILHO" 2>/dev/null; _solta; exit "$1"; }
    trap '_solta' EXIT
    trap '_acaba 143' TERM
    trap '_acaba 130' INT
    trap '_acaba 129' HUP
    echo "  [fila] com a trava (pid $$): $*"
    TTS_FILA_SEGURA=1 "$@" &
    FILHO=$!
    wait "$FILHO"; rc=$?
    FILHO=""
    exit "$rc" ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
