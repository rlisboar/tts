#!/usr/bin/env bash
# Mordidas da task #244: provar que o live_ui.sh CAI se o ramo `session_substituida`
# for revertido. Duas mordidas, uma por metade do fix:
#   A) o ramo dedicado no `case "error"` (sem ele o texto genérico "sessão viva" volta);
#   B) a precedência no `onclose` (sem ela o close LIMPO sobrescreve o motivo com
#      "sessão encerrada" — o caso que o #244 existe para não deixar passar).
# JS não tem o problema de bytecode do #238; o alvo é o `static/index.html` servido
# a cada request, então basta editar o arquivo entre as rodadas.
#
#   ./evidence/244-mordidas.sh            # ~2 min (2 rodadas da suíte)
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"
ALVO="static/index.html"
BAK="$(mktemp -t index-244.XXXXXX.html)"
cp "$ALVO" "$BAK"
MD5_ORIG="$(md5 -q "$ALVO" 2>/dev/null || md5sum "$ALVO" | cut -d' ' -f1)"
echo "md5 original: $MD5_ORIG"
falhas=()

restaura() {
  cp "$BAK" "$ALVO"
  local agora; agora="$(md5 -q "$ALVO" 2>/dev/null || md5sum "$ALVO" | cut -d' ' -f1)"
  [ "$agora" = "$MD5_ORIG" ] || { echo "  ✖ restauração não bateu o md5 ($agora)"; falhas+=("restauração"); }
}

morde() { # nome, busca, troca, arquivo-de-saida
  local nome="$1" busca="$2" troca="$3" saida="$4"
  "$PY" - "$ALVO" "$busca" "$troca" <<'PYEOF'
import sys, pathlib
arq, busca, troca = sys.argv[1], sys.argv[2], sys.argv[3]
h = pathlib.Path(arq).read_text()
n = h.count(busca)
assert n == 1, f"padrão aparece {n}x (esperado 1): {busca!r}"
pathlib.Path(arq).write_text(h.replace(busca, troca))
PYEOF
  echo "── mordida $nome: '$busca' -> '$troca'"
  ./tests/live_ui.sh > "$saida" 2>&1; local rc=$?
  echo "EXIT=$rc" >> "$saida"
  if [ "$rc" = "0" ]; then
    echo "  ✖ a suíte PASSOU com o fix revertido (falso PASSA)"; falhas+=("$nome")
  else
    echo "  ✔ suíte caiu (rc=$rc):"
    grep -E "^  ·" "$saida" | sed 's/^/    /'
    if ! grep -q "substituída\|substituicao\|session_substituida" "$saida"; then
      echo "  ⚠ caiu, mas nenhuma falha cita a substituição — conferir"; falhas+=("$nome (motivo)")
    fi
  fi
  restaura
}

PY="${PYTHON:-python3}"

morde "A (ramo dedicado)" \
      '} else if (m.code === "session_substituida") {' \
      '} else if (false && m.code === "session_substituida") {' \
      "evidence/244-mordida-A.txt"

morde "B (precedência no onclose)" \
      'if (LX.substituida) lxEstado("desligado", LX_SUBSTITUIDA);' \
      'if (false) lxEstado("desligado", LX_SUBSTITUIDA);' \
      "evidence/244-mordida-B.txt"

rm -f "$BAK"
echo
if [ "${#falhas[@]}" -gt 0 ]; then
  echo "✖ mordidas com problema: ${falhas[*]}"; exit 1
fi
echo "✔ mordidas OK — as duas metades do fix têm teste que morde (evidências 244-mordida-{A,B}.txt)"
