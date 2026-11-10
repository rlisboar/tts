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
# estado SINTÉTICO; a voz de fixture é `tests/fixtures/voz_sintetica/`
# (19 KB, sintética), versionada, e `tests/test_worktree_sem_estado.py` guarda. Os pesos (750 MB) são a
# única coisa ligada, por symlink.
#
# Uso:
#   tools/worktrees.sh cria <agente>     # ../tts-rod-wt/<agente> na branch wt/<agente>
#   tools/worktrees.sh lista
#   tools/worktrees.sh remove <agente> [--forca]
#   tools/worktrees.sh verifica [<agente>|--aqui]   # cópia do dado do dono?
#   tools/worktrees.sh liga [<agente>|--aqui]       # adota worktree feito por fora
#                       (`git worktree add` cru): liga .venv-mlx e os pesos (#1113)
#
# Integração do trabalho de volta na `main`: `tools/integra.sh <agente>`.
set -uo pipefail

# A RAIZ sai do `.git` comum, não do diretório do script: chamado de dentro de um
# worktree, `dirname/..` apontava para o próprio worktree e a pasta virava
# `…-wt/<agente>-wt` (#690).
comum="$(git rev-parse --git-common-dir 2>/dev/null)" || { echo "✖ não é repo git" >&2; exit 2; }
case "$comum" in /*) ;; *) comum="$(cd "$comum" && pwd)" ;; esac
RAIZ="$(cd "$(dirname "$comum")" && pwd)"
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
    # `-n`: alvo que JÁ é link não pode ser seguido (senão nasce `x/x` dentro do
    # dado do dono — `voices/voices`, duas vezes no #703).
    if [ -d "$RAIZ/.venv-mlx" ]; then
        [ -e "$destino/.venv-mlx" ] || [ -L "$destino/.venv-mlx" ] || \
            ln -sfn "$RAIZ/.venv-mlx" "$destino/.venv-mlx"
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
        [ -e "$destino/$rel" ] || [ -L "$destino/$rel" ] && continue
        mkdir -p "$(dirname "$destino/$rel")"
        ln -sfn "$RAIZ/$rel" "$destino/$rel"
    done < "$SUMS"
}

# ── guarda contra CÓPIA do dado do dono (#727) ────────────────────────────────
# A regra escrita ("state e voices são próprios de cada worktree") não bastou:
# agentes copiaram as 21 vozes e o estado para "validar suíte". Aqui a comparação é
# mecânica e SÓ LÊ: arquivo com o MESMO sha256 do original é cópia.
_sha_de() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" 2>/dev/null | cut -c1-64
    else shasum -a 256 "$1" 2>/dev/null | cut -c1-64; fi
}

# relatórios relativos à raiz: `voices/*`, `outputs/*` e os quatro do estado
_areas() { printf '%s\n' voices outputs settings.json .apikey .apikeys.json .speaker-profiles.json; }

#: sha256 de tudo o que é dado do dono — comparar por CONTEÚDO, não por nome: voz
#: copiada com outro nome (`fixture.wav`) é cópia igual
_hashes_do_dono() {
    local area
    for area in $(_areas); do
        [ -e "$RAIZ/$area" ] || continue
        find "$RAIZ/$area" -type f -print0 2>/dev/null \
            | xargs -0 -r -n 50 sh -c 'for f do sha256sum "$f" 2>/dev/null || shasum -a 256 "$f"; done' _ \
            | awk '{print $1}'
    done | sort -u
}

_verifica_um() {
    local wt="$1" area rel achados=0 hashes
    hashes="$(_hashes_do_dono)"
    [ -n "$hashes" ] || return 0
    for area in $(_areas); do
        [ -e "$wt/$area" ] || continue
        while IFS= read -r -d '' arq; do
            [ -L "$arq" ] && continue               # symlink não é cópia
            rel="${arq#$wt/}"
            if printf '%s\n' "$hashes" | grep -qx "$(_sha_de "$arq")"; then
                echo "✖ $rel é CÓPIA do dado do dono ($(basename "$wt"))" >&2
                achados=$((achados + 1))
            fi
        done < <(find "$wt/$area" -type f -print0 2>/dev/null)
    done
    return $((achados > 0 ? 1 : 0))
}

verifica() {
    local alvo="${1:-}" wt achados=0
    local -a alvos=()
    if [ "$alvo" = "--aqui" ]; then
        wt="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 2
        case "$wt" in "$RAIZ") echo "· raiz é a árvore principal — nada a comparar"; return 0 ;; esac
        alvos=("$wt")
    elif [ -n "$alvo" ]; then
        _nome_ok "$alvo" || exit 2
        wt="$(_destino "$alvo")"
        [ -d "$wt" ] || { echo "✖ não achei o worktree de $alvo em $wt" >&2; exit 2; }
        alvos=("$wt")
    else
        for wt in "$PASTA"/*; do [ -d "$wt" ] && alvos+=("$wt"); done
        [ "${#alvos[@]}" -gt 0 ] || { echo "· nenhum worktree em $PASTA"; return 0; }
    fi
    for wt in "${alvos[@]}"; do
        _verifica_um "$wt" || achados=$((achados + 1))
    done
    if [ "$achados" -gt 0 ]; then
        echo "→ $achados worktree(s) com cópia. Copie só o que precisar, ou apague: a" >&2
        echo "  guarda nunca apaga sozinha." >&2
        return 1
    fi
    echo "✔ nenhuma cópia do dado do dono nos worktrees"
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
    # o `.venv-mlx` ligado aqui tem de estar IGNORADO: sem isso o symlink entra no
    # commit apontando para o venv da raiz (#685). O `integra.sh` também recusa,
    # mas falhar já na criação é mais barato.
    if ! git -C "$RAIZ" check-ignore -q .venv-mlx; then
        echo "✖ \`.venv-mlx\` não está no .gitignore — o symlink entrará no commit" >&2
        echo "  ponha \`/.venv-mlx\` no .gitignore (sem barra no fim: symlink não é diretório)" >&2
        exit 1
    fi
    echo "✔ worktree de $agente em $destino (branch wt/$agente, a partir de main)"
    echo "  compartilhados: .venv-mlx e os pesos de models/ (symlink)"
    echo "  próprios (nascem vazios): voices/, settings.json, .apikey*, outputs/, state/"
    echo "  integre com: tools/integra.sh $agente"
}

# #1113: worktree criado com `git worktree add` cru (sem o `cria`) nasce sem o
# `.venv-mlx` e sem os pesos; o hook então para (e antes caía no python3 do
# sistema: `No module named ruff`, sem dizer a causa). `liga` adota o worktree:
# faz só a parte do `cria` que é symlink — nunca copia, nunca toca dado do dono.
liga() {
    local alvo="${1:-}" wt
    if [ "$alvo" = "--aqui" ]; then
        wt="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 2
    else
        _nome_ok "$alvo" || exit 2
        wt="$(_destino "$alvo")"
    fi
    [ -d "$wt" ] || { echo "✖ não achei o worktree em $wt" >&2; exit 2; }
    case "$(cd "$wt" && pwd -P)" in
        "$(cd "$RAIZ" && pwd -P)") echo "✖ $wt é a árvore principal — o venv dela é o de verdade, não um link" >&2; exit 2 ;;
    esac
    if ! git -C "$RAIZ" check-ignore -q .venv-mlx; then
        echo "✖ \`.venv-mlx\` não está no .gitignore — o symlink entraria no commit" >&2
        exit 1
    fi
    _liga_o_que_nao_pode_copiar "$wt"
    if [ -x "$wt/.venv-mlx/bin/python" ]; then
        echo "✔ $wt ligado: .venv-mlx → $(readlink "$wt/.venv-mlx") e os pesos do manifesto"
    else
        echo "✖ $wt continua sem .venv-mlx/bin/python (a raiz tem venv?)" >&2
        exit 1
    fi
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
        || /bin/rm -rf "$destino"
    git -C "$RAIZ" branch -D "wt/$agente" >/dev/null 2>&1
    echo "✔ worktree e branch de $agente removidos"
}

case "${1:-}" in
    cria)   shift; cria "$@" ;;
    lista)  lista ;;
    remove) shift; remove "$@" ;;
    verifica) shift; verifica "$@" ;;
    liga)   shift; liga "$@" ;;
    *)      _uso; exit 2 ;;
esac
