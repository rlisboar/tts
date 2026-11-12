#!/usr/bin/env bash
# Flag de BANCADA no mini (#1132): `~/.tts-studio/bancada.lock`.
# Quem roda medida no mini (H6, bakeoff, mede_pos_llm.sh…) cria o lock ANTES e o solta DEPOIS;
# enquanto ele existir, o job de CI no mini (tools/ci_mini_ocioso.sh) espera e não larga a suíte.
#
#   tools/bancada_lock.sh pega <agente> [pid]   # cria (pid = dono; padrão o PID DESTE shell pai)
#   tools/bancada_lock.sh solta                 # remove
#   tools/bancada_lock.sh status
#   # numa bancada: pega no início e `trap 'tools/bancada_lock.sh solta' EXIT`
#
# Lock de dono morto (pid inexistente) ou com mais de 4 h é tratado como órfão pelo CI, então
# uma bancada que morreu não trava o CI para sempre.
set -u
umask 077
LOCK="${TTS_BANCADA_LOCK:-$HOME/.tts-studio/bancada.lock}"
case "${1:-}" in
  pega)
    mkdir -p "$(dirname "$LOCK")"
    printf 'agente=%s\npid=%s\ndesde=%s\n' "${2:-?}" "${3:-$PPID}" "$(date +%s)" > "$LOCK"
    echo "bancada.lock: $(sed -n 's/^agente=//p' "$LOCK") pid=$(sed -n 's/^pid=//p' "$LOCK")" ;;
  solta) /bin/rm -f "$LOCK"; echo "bancada.lock: removido" ;;
  status)
    if [ -e "$LOCK" ]; then echo "bancada.lock PRESENTE: $(tr '\n' ' ' < "$LOCK")"; else echo "bancada.lock: livre"; fi ;;
  *) echo "uso: $0 pega <agente> [pid] | solta | status" >&2; exit 2 ;;
esac
