#!/usr/bin/env bash
# PROVA do teto por suíte do runner (`run_sh.sh`): pendura de propósito para o
# alarme cortar. NÃO é uma suíte de produto.
#
# · está registrada no estágio `rapido` — o guard do `test_run_sh.py` cobra que
#   toda suíte esteja em um estágio, e deixá-la fora vira arquivo ambíguo;
# · custa 0 s no normal (só pendura com `PENDURA=1`);
# · prova de aceite: `PENDURA=1 ./tests/run_sh.sh rapido -j 1` tem de terminar com
#   `FALHA pendurada_probe 90s (rc=142)` — 142 é SIGALRM, o corte; `TETO_S=5`
#   encurta a prova para 5 s.
set -uo pipefail
[ "${PENDURA:-}" = "1" ] && sleep 300
echo "ok"
