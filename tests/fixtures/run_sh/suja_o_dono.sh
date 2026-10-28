#!/usr/bin/env bash
# Sonda do #535/#591: escreve no arquivo de settings do "dono", como as suítes de
# verdade faziam. O runner tem de ver isso no diff da impressão GLOBAL e NOMEAR
# esta suíte. `TTS_DONO_DIR` aponta para uma CÓPIA (o `settings.json` real da
# máquina de trabalho não é tocado); sem ele, o alvo é a raiz do repo.
#
# Grava-se um ARQUIVO NOVO em vez de acrescentar linha: a rodada do `pytest` não
# deixa mais resíduo nenhum no arquivo real (17 linhas em branco sobraram no gate
# do #535).
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '{}\n' > "${TTS_DONO_DIR:-$AQUI/../../..}/settings.json"
# segue VIVA um instante depois de escrever: o amostrador precisa ver o marcador
# dela no instante da mudança para poder nomeá-la (a escrita em si é instantânea)
sleep "${SONDA_ESPERA:-1}"
echo "sujei o settings do dono"
