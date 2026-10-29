#!/usr/bin/env bash
# Worktree por agente (decisão do dono, 2026-10-28) — a árvore principal passa a
# ser só INTEGRAÇÃO; cada agente trabalha no próprio worktree, numa branch `wt/<agente>`.
#
# Por quê: a árvore compartilhada foi a causa de vários incidentes (arquivo de um
# agente sobrescrito pelo outro, código que "sumiu" — #573, e a suíte que mutava o
# `app.py` dos outros — #621). O worktree separa o diretório de trabalho; o que
# continua compartilhado é só o que não pode ser copiado: `.venv-mlx` e os PESOS de
# `models/` (symlink, nunca cópia — são 750 MB).
#
# NUNCA são compartilhados: `voices/`, `settings.json`, `.apikey*`, `outputs/` e
# `state/`. Cada worktree tem os seus, então rodada e app não mexem no estado do dono.
#
# Os arquivos de ESTADO do dono (`settings.json`, `.apikey`, `.apikeys.json`,
# `.speaker-profiles.json`) NÃO são copiados nem linkados: a suíte tem de funcionar
# com estado SINTÉTICO (o `conftest` exporta os `TTS_*_PATH` para tmp). Cópia
# multiplicaria os segredos do dono por N diretórios e esconderia célula que lê o
# arquivo real por fora do isolamento — `tests/test_worktree_sem_estado.py` guarda.
# Nada disso é copiado nem linkado — nem `voices/` (são vozes clonadas,
# insubstituíveis: seis worktrees seriam seis cópias). A suíte tem de passar com
# estado SINTÉTICO; a voz de fixture é `tests/fixtures/voz_fala_5s_16k.wav`,
# versionada, e `tests/test_worktree_sem_estado.py` guarda. Os pesos (750 MB) são a
# única coisa ligada, por symlink.
#
# Uso:
#   tools/worktrees.sh cria <agente>     # ../tts-rod-wt/<agente> na branch wt/<agente>
#   tools/worktrees.sh lista
#   tools/worktrees.sh remove <agente> [--forca]
#
# Integração do trabalho de volta na `main`: `tools/integra.sh <agente>`.
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASTA="${TTS_WT_DIR:-$(dirname "$RAIZ")/$(basename "$RAIZ")-wt}"
SUMS="$RAIZ/models/sha256sums.txt"

_uso() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

_nome_ok() {
    case "${1:-}" in
        ''|*[!a-z0-9-]*) echo "nome de agente inválido: '${1:-}' (use [a-z0-9-])" >&2; return 1 ;;
    esac
}

_destino() { printf '%s/%s' "$PASTA" "$1"; }

# Os pesos e o venv entram por symlink. Os pesos saem do MANIFESTO (não de uma
# lista fixa): artefato novo entra sozinho, e o que não é peso (README, NOTICE,
# sha256sums) já é arquivo rastreado do worktree, não pode virar symlink.
_liga_o_que_nao_pode_copiar() {
    local destino="$1" rel
    if [ -d "$RAIZ/.venv-mlx" ]; then
        ln -s "$RAIZ/.venv-mlx" "$destino/.venv-mlx"
    else
        echo "  ⚠ sem .venv-mlx na raiz — o worktree vai precisar de um venv" >&2
    fi
    [ -f "$SUMS" ] || return 0
    while read -r _sha rel; do
        [ -n "${rel:-}" ] || continue
        if [ ! -f "$RAIZ/$rel" ]; then
            echo "  ⚠ peso do manifesto ausente na raiz: $rel (rode tools/baixa_modelos.sh)" >&2
            continue
        fi
        [ -e "$destino/$rel" ] && continue
        mkdir -p "$(dirname "$destino/$rel")"
        ln -s "$RAIZ/$rel" "$destino/$rel"
    done < "$SUMS"
}

cria() {
    local agente="${1:-}" destino
    _nome_ok "$agente" || exit 2
    destino="$(_destino "$agente")"
    if git -C "$RAIZ" rev-parse --verify -q "refs/heads/wt/$agente" >/dev/null; then
        echo "✖ a branch wt/$agente já existe (remova com: $0 remove $agente)" >&2; exit 1
    fi
    if [ -e "$destino" ]; then
        echo "✖ $destino já existe" >&2; exit 1
    fi
    mkdir -p "$PASTA"
    git -C "$RAIZ" worktree add -q -b "wt/$agente" "$destino" main || exit 1
    _liga_o_que_nao_pode_copiar "$destino"
    echo "✔ worktree de $agente em $destino (branch wt/$agente, a partir de main)"
    echo "  compartilhados: .venv-mlx e os pesos de models/ (symlink)"
    echo "  próprios (nascem vazios): voices/, settings.json, .apikey*, outputs/, state/"
    echo "  integre com: tools/integra.sh $agente"
}

lista() {
    git -C "$RAIZ" worktree list
    echo
    echo "branches de trabalho:"
    git -C "$RAIZ" for-each-ref --format='  %(refname:short)' 'refs/heads/wt/*'
}

remove() {
    local agente="${1:-}" forca="${2:-}" destino
    _nome_ok "$agente" || exit 2
    destino="$(_destino "$agente")"
    if [ -d "$destino" ] && [ -n "$(git -C "$destino" status --porcelain 2>/dev/null)" ] && [ "$forca" != "--forca" ]; then
        echo "✖ o worktree tem trabalho não commitado — commite (ou use --forca para perder):" >&2
        git -C "$destino" status --short >&2
        exit 1
    fi
    git -C "$RAIZ" worktree remove --force "$destino" 2>/dev/null \
        || rm -rf "$destino"
    git -C "$RAIZ" branch -D "wt/$agente" >/dev/null 2>&1
    echo "✔ worktree e branch de $agente removidos"
}

case "${1:-}" in
    cria)   shift; cria "$@" ;;
    lista)  lista ;;
    remove) shift; remove "$@" ;;
    *)      _uso; exit 2 ;;
esac
