#!/usr/bin/env bash
# Integra o trabalho de um agente na `main` da árvore principal (decisão do dono,
# 2026-10-28). O agente commita no PRÓPRIO worktree; este script:
#
#   1. exige a raiz em `main` e SEM trabalho não commitado (o merge é `--ff-only`,
#      e WIP na raiz seria atropelado);
#   2. exige o worktree do agente limpo (commit primeiro, nada de stash invisível);
#   3. faz `rebase main` no worktree — conflito aborta e volta tudo;
#   4. roda o HOOK no worktree (é o gate: análise estática + testes);
#   5. só então `merge --ff-only wt/<agente>` na raiz.
#
# Ordem importa: o hook roda no worktree, onde o código ainda não entrou na `main`.
# O merge é `--ff-only` de propósito — rebase primeiro garante histórico linear e
# nenhum commit de merge; se não for fast-forward, algo fugiu do fluxo e é melhor
# parar do que inventar.
#
# Uso: tools/integra.sh <agente>        (da raiz ou de QUALQUER worktree)
set -uo pipefail

# A RAIZ sai do `.git` comum, não do diretório do script: chamado de dentro de um
# worktree, `dirname/..` dava `…-wt/<agente>-wt` e o script recusava (#690).
comum="$(git rev-parse --git-common-dir 2>/dev/null)" || { echo "✖ não é repo git" >&2; exit 2; }
case "$comum" in /*) ;; *) comum="$(cd "$comum" && pwd)" ;; esac
RAIZ="$(cd "$(dirname "$comum")" && pwd)"
PASTA="${TTS_WT_DIR:-$(dirname "$RAIZ")/$(basename "$RAIZ")-wt}"

_aborta() { echo "✖ $*" >&2; exit 1; }

agente="${1:-}"
case "$agente" in
    ''|*[!a-z0-9-]*) _aborta "uso: tools/integra.sh <agente> (nome [a-z0-9-])" ;;
esac
WT="$PASTA/$agente"
branch="wt/$agente"

[ -d "$WT" ] || _aborta "não achei o worktree de $agente em $WT (crie com tools/worktrees.sh cria $agente)"
git -C "$RAIZ" rev-parse --verify -q "refs/heads/$branch" >/dev/null || _aborta "a branch $branch não existe"

atual="$(git -C "$RAIZ" symbolic-ref --short -q HEAD)"
[ "$atual" = "main" ] || _aborta "a raiz está em '${atual:-detached}' — a integração é sempre na main"
wip_raiz="$(git -C "$RAIZ" status --porcelain)"
[ -z "$wip_raiz" ] || { echo "$wip_raiz" >&2; _aborta "a raiz tem trabalho não commitado (commite/stashe antes de integrar)"; }
wip_wt="$(git -C "$WT" status --porcelain)"
[ -z "$wip_wt" ] || { echo "$wip_wt" >&2; _aborta "o worktree de $agente tem trabalho não commitado (commite lá antes)"; }

# Estado do dono NÃO entra no commit: nem arquivo, nem symlink (modo 120000 — é o
# que acontecia com `voices`/`.venv-mlx`, que num worktree são link e iam para o
# índice apontando para o dado real). Recusa ANTES de rebase/hook: barato e claro.
_estado_proibido() {
    local p="$1"
    case "$p" in
        .venv-mlx|.venv-mlx/*|voices|voices/*|outputs|outputs/*|state|state/*|\
        settings.json|.apikey|.apikeys.json|.speaker-profiles.json) return 0 ;;
    esac
    return 1
}
_links=""
_novos=""
while read -r modo _sha _resto caminho; do
    [ "$modo" = "120000" ] || continue        # symlink: é o que entrava calado
    [ -n "${caminho:-}" ] || continue
    _estado_proibido "$caminho" && _links="$_links $caminho"
done < <(git -C "$RAIZ" ls-tree -r "$branch")
while read -r caminho; do
    [ -n "${caminho:-}" ] || continue
    _estado_proibido "$caminho" && _novos="$_novos $caminho"
done < <(git -C "$RAIZ" diff --name-only --diff-filter=A "main...$branch")
if [ -n "$_links$_novos" ]; then
    echo "✖ a branch $branch leva estado do dono para o commit:" >&2
    [ -n "$_links" ] && echo "   symlink rastreado:$_links" >&2
    [ -n "$_novos" ] && echo "   arquivo adicionado:$_novos" >&2
    echo "  tire do índice e commite no worktree: git -C \"$WT\" rm --cached <caminho>" >&2
    exit 1
fi

base_inicial="$(git -C "$RAIZ" rev-parse main)"
echo "→ rebase de $branch em cima da main"
if ! git -C "$WT" rebase main; then
    git -C "$WT" rebase --abort 2>/dev/null
    _aborta "rebase conflitou — nada foi integrado (resolva no worktree e rode de novo)"
fi

# `TTS_INTEGRA_SEM_HOOK=1` pula o gate — mas SÓ em árvore descartável (sob
# `$TMPDIR`/`/tmp`). No repo de verdade o atalho engana: foi por ele que um teste
# integrou um agente FALSO na main real sem gate (#705).
pular_hook=0
if [ "${TTS_INTEGRA_SEM_HOOK:-0}" = 1 ]; then
    case "$RAIZ/" in
        "${TMPDIR:-/tmp}"/*|/tmp/*) pular_hook=1 ;;
        *) echo "  (atalho ignorado: $RAIZ não é árvore descartável — o hook roda)" ;;
    esac
fi
if [ "$pular_hook" = 1 ]; then
    echo "→ hook no worktree (gate) — PULADO (TTS_INTEGRA_SEM_HOOK=1, árvore em tmp)"
else
echo "→ hook no worktree (gate)"
# Executa o ARQUIVO, não `git hook run`: aquele devolve o mesmo código para "hook
# não existe" e para "hook reprovou", e a queda para execução direta acabava rodando
# a suíte DUAS vezes em toda reprovação (medido: 9 min a mais por vermelho).
hook="$WT/.githooks/pre-commit"
[ -x "$hook" ] || _aborta "não achei o hook executável em $hook"
( cd "$WT" && "$hook" ) || _aborta "o hook reprovou — nada foi integrado"
fi

# a `main` pode andar entre o rebase e o merge (outro agente integrando): o merge
# `--ff-only` falharia com "not possible to fast-forward" sem dizer por quê
base_agora="$(git -C "$RAIZ" rev-parse main)"
[ "$base_agora" = "$base_inicial" ] || _aborta \
    "a main andou durante a rodada (${base_inicial:0:7} → ${base_agora:0:7}) — rode de novo"

echo "→ merge --ff-only de $branch na main"
git -C "$RAIZ" merge --ff-only "$branch" || _aborta "merge não foi fast-forward — nada integrado"
echo "✔ $branch integrado na main em $(git -C "$RAIZ" rev-parse --short HEAD)"
echo "  a branch e o worktree continuam (remova com: tools/worktrees.sh remove $agente)"
