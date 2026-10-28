#!/usr/bin/env bash
# Sonda do `tests/test_run_sh.py`: registra como BASE e os escapes do dono
# chegaram. O runner só imprime a saída da suíte quando ela falha, então a sonda
# grava em `SONDA_SAIDA` (arquivo) além do stdout.
set -uo pipefail
{ echo "BASE=[${BASE-<vazio>}]"
  echo "ADMIN_UI_NO_DONO=[${ADMIN_UI_NO_DONO-<vazio>}]"
  echo "NO_DONO=[${NO_DONO-<vazio>}]"; } | tee "${SONDA_SAIDA:-/dev/stdout}"
