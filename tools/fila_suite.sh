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
#     chamado de dentro de uma suíte já travada) só executa, sem pegar a trava de novo.
#     ATENÇÃO: isso vale para QUALQUER descendente que herde `TTS_FILA_SEGURA` — um filho
#     que a exporte (ou a herde de um processo que a tenha) FURA a trava e roda junto
#     com a suíte dona. Por desenho: confie em quem é seu descendente, não exporte-a à mão;
#   • sinal (TERM/INT/HUP): derruba o GRUPO inteiro do comando (ele roda em job próprio,
#     `set -m`), não só o pid — netos também saem — e só então solta a trava;
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

# Quem ESPERA a trava também fica registrado (`$RAIZ/espera/<pid>`: agente, worktree, desde
# quando, comando). Sem isso uma espera perdida (a do `commit1127`, por pgrep, sem trava)
# não aparecia em lugar nenhum. O registro sai na saída; o de pid morto (SIGKILL) é varrido
# pelo `status`.
ESPERA_DIR="$RAIZ/espera"
_registra_espera() {
  mkdir -p "$ESPERA_DIR" && printf '%s|%s|%s|%s\n' \
    "${TTS_AGENTE:-$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")}" \
    "$PWD" "$(_agora)" "$*" > "$ESPERA_DIR/$$"
}
_tira_espera() { /bin/rm -f "$ESPERA_DIR/$$" 2>/dev/null; }

_lista_espera() {
  [ -d "$ESPERA_DIR" ] || return 0
  local f p ag wt desde cmd n=0
  for f in "$ESPERA_DIR"/*; do
    [ -e "$f" ] || continue
    p="$(basename "$f")"
    if ! kill -0 "$p" 2>/dev/null; then /bin/rm -f "$f"; continue; fi   # esperador morto
    IFS='|' read -r ag wt desde cmd < "$f"
    n=$((n + 1))
    echo "  espera #$n: há $(( $(_agora) - desde ))s — agente=$ag pid=$p worktree=$wt"
    echo "              cmd: $cmd"
  done
  [ "$n" -eq 0 ] || return 0
}

_status() {
  if [ ! -d "$TRAVA" ]; then echo "fila: livre"
  else
    local p; p="$(_le pid)"
    if _dono_vivo; then
      echo "fila: OCUPADA há $(( $(_agora) - $(_le inicio_s) ))s — agente=$(_le agente) pid=$p worktree=$(_le worktree)"
      echo "      cmd: $(_le cmd)"
    else
      echo "fila: trava ÓRFÃ (dono pid=${p:-?} morto; agente=$(_le agente)) — o próximo 'roda' a recupera"
    fi
  fi
  _lista_espera
}

_recupera_orfa() {
  # Check-then-act no NOME da trava seria uma corrida: o recuperador A remove a órfã e
  # cria a trava NOVA; o recuperador B, que leu o pid morto ANTES, move a trava nova de A
  # (as duas suítes passam a rodar juntas — o teste de 4 recuperadores acusou). Por isso a
  # recuperação é serializada por um mutex próprio (`mkdir`) e RECONFERE a órfã dentro
  # dele: com a órfã presente ninguém consegue criar a trava, então entre a reconferência e
  # o `mv` ela não muda. Mutex de recuperador morto expira em 10 s.
  local rec="$RAIZ/.recupera"
  if ! mkdir "$rec" 2>/dev/null; then
    local idade=$(( $(_agora) - $(stat -f %m "$rec" 2>/dev/null || stat -c %Y "$rec" 2>/dev/null || _agora) ))
    [ "$idade" -gt 10 ] && /bin/rm -rf "$rec"
    return 1
  fi
  local morto="$RAIZ/.orfa.$$.$RANDOM" rc=1
  if [ -d "$TRAVA" ] && ! _dono_vivo \
     && { [ -n "$(_le pid)" ] || [ "$(( $(_agora) - $(stat -f %m "$TRAVA" 2>/dev/null || stat -c %Y "$TRAVA" 2>/dev/null || _agora) ))" -gt 5 ]; }; then
    mv "$TRAVA" "$morto" 2>/dev/null && /bin/rm -rf "$morto" && echo "  [fila] trava órfã (dono morto) recuperada" && rc=0
  fi
  /bin/rm -rf "$rec"
  return $rc
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
        # só volta ao topo se RECUPEROU; mutex de outro recuperador (rc 1) cai no teto de
        # espera e no sleep abaixo, em vez de girar sem parar
        _recupera_orfa && continue
      fi
    fi
    if [ $(( $(_agora) - t0 )) -gt "$ESPERA" ]; then
      echo "  [fila] não liberou em ${ESPERA}s — $(_status | head -1)" >&2; return 75
    fi
    if [ -z "$avisou" ]; then
      echo "  [fila] esperando a vez — $(_status | head -1)"; avisou=1
      _registra_espera "$@"
    fi
    sleep "0.$(( (RANDOM % 7) + 3 ))"
  done
}

case "${1:-}" in
  status) _status ;;
  roda)
    shift; [ "${1:-}" = "--" ] && shift
    [ "$#" -gt 0 ] || { echo "uso: $0 roda -- <comando…>" >&2; exit 2; }
    if [ "${TTS_FILA:-1}" = 0 ] || [ "${TTS_FILA_SEGURA:-0}" = 1 ]; then exec "$@"; fi
    trap '_tira_espera' EXIT
    trap '_tira_espera; exit 143' TERM
    trap '_tira_espera; exit 130' INT
    trap '_tira_espera; exit 129' HUP
    _pega "$@"; rc=$?
    _tira_espera
    [ "$rc" -eq 0 ] || exit "$rc"
    FILHO=""
    # `set -m`: o filho nasce no seu PRÓPRIO grupo de processos (pgid = pid dele), e o
    # sinal vai para o GRUPO (`kill -- -pgid`). Matar só o pid deixava o NETO vivo
    # (`sh -c 'sleep 60 & wait'`: a suíte e o que ela dispara ficavam órfãos, sem trava).
    # Sem job control (`set -m` pode falhar sem tty em alguns ambientes) o filho NÃO ganha
    # grupo próprio, e `kill -- -pgid` acertaria o NOSSO grupo (ou nada). Detecta e cai para o
    # pid + FILHOS DIRETOS (`pkill -P`), avisando — pega `sh -c 'sleep & wait'`, mas não
    # netos de netos. `TTS_FILA_SEM_JOBCTRL=1` força esse caminho (teste).
    JOBCTRL=1
    if [ "${TTS_FILA_SEM_JOBCTRL:-0}" = 1 ]; then JOBCTRL=0; else set -m 2>/dev/null; fi
    case "$-" in *m*) ;; *) JOBCTRL=0 ;; esac
    [ "$JOBCTRL" = 1 ] || echo "  [fila] AVISO: sem job control — o sinal só alcança o filho e os filhos diretos" >&2
    _mata_filho() { # $1 = sinal
      if [ "$JOBCTRL" = 1 ]; then kill -"$1" -- "-$FILHO" 2>/dev/null
      else pkill -"$1" -P "$FILHO" 2>/dev/null; kill -"$1" "$FILHO" 2>/dev/null; fi
    }
    _vivo_filho() {
      if [ "$JOBCTRL" = 1 ]; then kill -0 -- "-$FILHO" 2>/dev/null; else kill -0 "$FILHO" 2>/dev/null; fi
    }
    # sinal: derruba o GRUPO do filho (TERM, depois KILL se não sair em ~5 s) e só então
    # solta a trava
    _acaba() {
      if [ -n "$FILHO" ]; then
        _mata_filho TERM
        local i=0
        while [ "$i" -lt 50 ] && _vivo_filho; do sleep 0.1; i=$((i + 1)); done
        _mata_filho KILL
        wait "$FILHO" 2>/dev/null
      fi
      _solta; exit "$1"
    }
    trap '_solta; _tira_espera' EXIT
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
