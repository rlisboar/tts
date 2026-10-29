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
# Uso: tools/integra.sh <agente>        (da raiz ou de qualquer lugar)
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
[ "$atual" = "main" ] || _aborta "a raiz está em '$atual' — a integração é sempre na main"
wip_raiz="$(git -C "$RAIZ" status --porcelain)"
[ -z "$wip_raiz" ] || { echo "$wip_raiz" >&2; _aborta "a raiz tem trabalho não commitado (commite/stashe antes de integrar)"; }
wip_wt="$(git -C "$WT" status --porcelain)"
[ -z "$wip_wt" ] || { echo "$wip_wt" >&2; _aborta "o worktree de $agente tem trabalho não commitado (commite lá antes)"; }

echo "→ rebase de $branch em cima da main"
if ! git -C "$WT" rebase main; then
    git -C "$WT" rebase --abort 2>/dev/null
    _aborta "rebase conflitou — nada foi integrado (resolva no worktree e rode de novo)"
fi

echo "→ hook no worktree (gate)"
# Executa o ARQUIVO, não `git hook run`: aquele devolve o mesmo código para "hook
# não existe" e para "hook reprovou", e a queda para execução direta acabava rodando
# a suíte DUAS vezes em toda reprovação (medido: 9 min a mais por vermelho).
hook="$WT/.githooks/pre-commit"
[ -x "$hook" ] || _aborta "não achei o hook executável em $hook"
( cd "$WT" && "$hook" ) || _aborta "o hook reprovou — nada foi integrado"

echo "→ merge --ff-only de $branch na main"
git -C "$RAIZ" merge --ff-only "$branch" || _aborta "merge não foi fast-forward — nada integrado"
echo "✔ $branch integrado na main em $(git -C "$RAIZ" rev-parse --short HEAD)"
echo "  a branch e o worktree continuam (remova com: tools/worktrees.sh remove $agente)"
