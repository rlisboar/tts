#!/usr/bin/env bash
# prova do teto do runner: pendura quando PENDURA=1
set -uo pipefail
[ "${PENDURA:-}" = "1" ] && sleep 300
echo "ok"
