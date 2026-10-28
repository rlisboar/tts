#!/usr/bin/env bash
# Sonda do #591: fica rodando sem escrever nada, para a escrita de OUTRA suíte
# cair dentro da janela dela (`-j 3`) — é o cenário que fazia a acusação por
# janela culpar a suíte errada.
# O nome começa com `z` de propósito: no empate de idade (as suítes largam no
# mesmo instante) a ordem alfabética põe o escritor primeiro.
set -uo pipefail
sleep "${SONDA_SONO:-2}"
echo "parada, sem escrever"
