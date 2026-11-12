#!/usr/bin/env bash
# Flag de BANCADA no mini (#1132): `~/.tts-studio/bancada.lock`.
# Quem roda medida no mini (H6, bakeoff, mede_pos_llm.sh…) cria o lock ANTES e o solta DEPOIS;
# enquanto ele existir, o job de CI no mini (tools/ci_mini_ocioso.sh) espera e não larga a suíte.
#
#   tools/bancada_lock.sh pega <agente> [pid]   # cria ATÔMICO (ln); ESPERA dono vivo (<4 h), teto TTS_BANCADA_ESPERA (s), exit 1 se esgotar
#                                               (pid = dono; padrão o PID DESTE shell pai)
#   tools/bancada_lock.sh solta                 # remove
#   tools/bancada_lock.sh status
#   # numa bancada: pega no início e `trap 'tools/bancada_lock.sh solta' EXIT`
#
# Lock de dono morto (pid inexistente) ou com mais de 4 h é tratado como órfão pelo CI, então
# uma bancada que morreu não trava o CI para sempre.
set -u
umask 077
LOCK="${TTS_BANCADA_LOCK:-$HOME/.tts-studio/bancada.lock}"
TTL_H="${TTS_BANCADA_TTL_H:-4}"
ESPERA="${TTS_BANCADA_ESPERA:-3600}"     # teto (s) que o `pega` espera um dono vivo; 0 = não espera
PASSO="${TTS_BANCADA_PASSO:-2}"

_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || date +%s; }
_campo() { sed -n "s/^$1=//p" "$LOCK" 2>/dev/null | head -1; }
# 0 = o lock atual tem dono vivo e novo (segura a bancada); 1 = ausente ou órfão
_vivo() {
  [ -e "$LOCK" ] || return 1
  local pid; pid="$(_campo pid)"
  [ $(( $(date +%s) - $(_mtime "$LOCK") )) -gt $((TTL_H * 3600)) ] && return 1
  [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null && return 1
  return 0
}

case "${1:-}" in
  pega)
    mkdir -p "$(dirname "$LOCK")"
    agente="${2:-?}"; dono="${3:-$PPID}"
    tmp="$LOCK.$$.tmp"
    printf 'agente=%s\npid=%s\ndesde=%s\n' "$agente" "$dono" "$(date +%s)" > "$tmp"
    trap '/bin/rm -f "$tmp"' EXIT
    limite=$(( $(date +%s) + ESPERA )); avisou=0
    while :; do
      # `ln` falha se o destino existe: é o ÚNICO ponto que cria o lock, e o conteúdo já está inteiro
      if ln "$tmp" "$LOCK" 2>/dev/null; then
        echo "bancada.lock: $agente pid=$dono"; exit 0
      fi
      if [ "$(_campo pid)" = "$dono" ] && _vivo; then   # mesmo dono pedindo de novo: idempotente
        echo "bancada.lock: $agente pid=$dono (já era seu)"; exit 0
      fi
      if ! _vivo; then
        # órfão: toma o lugar sob um guarda atômico, para dois `pega` não removerem o lock um do outro
        if mkdir "$LOCK.guarda" 2>/dev/null; then
          _vivo || /bin/rm -f "$LOCK"
          rmdir "$LOCK.guarda" 2>/dev/null
        elif [ -d "$LOCK.guarda" ] && [ $(( $(date +%s) - $(_mtime "$LOCK.guarda") )) -gt 30 ]; then
          rmdir "$LOCK.guarda" 2>/dev/null   # guarda de um `pega` que morreu no meio
        fi
        continue
      fi
      if [ "$(date +%s)" -ge "$limite" ]; then
        echo "bancada.lock: OCUPADA por $(_campo agente) pid=$(_campo pid); desisti após ${ESPERA}s" >&2
        exit 1
      fi
      [ "$avisou" = 1 ] || { echo "bancada.lock: ocupada por $(_campo agente) pid=$(_campo pid); esperando (teto ${ESPERA}s)" >&2; avisou=1; }
      sleep "$PASSO"
    done ;;
  solta) /bin/rm -f "$LOCK"; echo "bancada.lock: removido" ;;
  status)
    if [ -e "$LOCK" ]; then
      if _vivo; then echo "bancada.lock PRESENTE: $(tr '\n' ' ' < "$LOCK")"
      else echo "bancada.lock ÓRFÃO (dono morto ou > ${TTL_H}h; o próximo pega toma): $(tr '\n' ' ' < "$LOCK")"; fi
    else echo "bancada.lock: livre"; fi ;;
  *) echo "uso: $0 pega <agente> [pid] | solta | status   (pega ESPERA dono vivo, teto TTS_BANCADA_ESPERA=${ESPERA}s)" >&2; exit 2 ;;
esac
