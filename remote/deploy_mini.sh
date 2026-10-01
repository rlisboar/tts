#!/bin/zsh
# Deploy da instância de PRODUÇÃO no Mac mini (tts.the-dudes.com) — reproduzível
# e verificável, no mesmo padrão do remote/deploy.sh.
#
# Por que existe: a produção NÃO é esta máquina. O hostname público é servido pelo
# app do Mac mini (192.168.15.34:7860), exposto pelo Cloudflare Tunnel do próprio
# mini, e não há auto-pull em lugar nenhum (`start-server.sh` só faz
# `cd repo && exec ./run.sh`). Sem este script a produção congela no commit do dia
# em que foi instalada — foi o que aconteceu (achado #231: 66 commits / 8 dias).
#
#   ./remote/deploy_mini.sh recon                # read-only: host, git, serviço, deps, uso
#   ./remote/deploy_mini.sh compare              # HEAD local × o que está no ar (e no git do mini)
#   ./remote/deploy_mini.sh smoke                # no ar: /health, auth, /api/build, WS, index
#   ./remote/deploy_mini.sh deploy  [--apply]    # push → backup → pull → deps → restart → smoke
#   ./remote/deploy_mini.sh rollback [--apply]   # volta o SHA salvo pelo último deploy
#
# Sem `--apply` nada muda no mini (nem no origin). Flags do deploy:
#   --sem-push   não publica no origin (use quando o push for por fora)
#   --sem-deps   não roda `pip install` (subida só de código)
#   --sem-tts    não faz a síntese real do smoke
#
# O alvo é um REV: `TTS_MINI_REV=<sha|corte>` (padrão HEAD) — permite subir um
# corte específico (ex.: o endurecimento antes do épico Live) sem publicar a
# árvore em movimento de hoje. O mini fica numa branch `prod-<sha>` (não detached),
# e o `rollback` usa o mesmo passo com o sha salvo no último deploy.
#
# Regras que o script respeita:
#   • `recon` só atualiza os REFS do mini (`git fetch`, para a distância até o
#     origin não mentir) — árvore e serviço intocados; `compare`/`smoke` não
#     escrevem nada em lugar nenhum;
#   • o que sobe é o COMMIT (HEAD), não a árvore viva: a suíte/evidência desta
#     máquina roda com WIP não commitado e não pode ir para produção por acidente;
#   • o mini tem que estar LIMPO e fast-forwardável — senão o script para ANTES de
#     reiniciar (restart derruba a produção por alguns segundos);
#   • `codigo` do /api/build tem que bater com o hash dos módulos do rev alvo: é o
#     que separa "código novo no disco" de "código novo CARREGADO" (#190/#214);
#   • nenhuma chave é impressa: a chave do mini é lida por ssh e usada só em curl.
#
# Config (env): TTS_MINI_HOST (padrão 192.168.15.34), TTS_MINI_USER (lisboa),
# TTS_MINI_DIR (Documents/tts-rod), TTS_MINI_LABEL (studio.tts.server),
# TTS_MINI_PORT (7860), TTS_PUBLIC_HOST (tts.the-dudes.com), TTS_MINI_REPO
# (repo local de onde sai o push; padrão = a raiz deste repo).
set -u
RAIZ="${0:A:h}"                                   # remote/
LOCAL_REPO="${TTS_MINI_REPO:-${RAIZ:h}}"
SSH=(ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=8)
HOST="${TTS_MINI_HOST:-192.168.15.34}"
USUARIO="${TTS_MINI_USER:-lisboa}"
DIR="${TTS_MINI_DIR:-Documents/tts-rod}"
LABEL="${TTS_MINI_LABEL:-studio.tts.server}"
PORTA="${TTS_MINI_PORT:-7860}"
PUBLICO="${TTS_PUBLIC_HOST:-tts.the-dudes.com}"
ESPERA="${TTS_MINI_ESPERA:-6}"                    # run.sh sobe o uvicorn; smoke espera a porta
BOOT_MS_MAX="${TTS_MINI_BOOT_MS:-120000}"         # pós-deploy: processo novo, não instância velha
TTS_TIMEOUT="${TTS_MINI_TTS_TIMEOUT:-120}"        # teto do job de síntese real no smoke
ESTADO=".deploy-mini-estado"                      # no repo do mini (untracked; só o --apply escreve)
# O preview de deps vai para o /tmp DO MINI, não para a árvore do repo: sem isso o
# dry-run sujava a árvore de produção (F1 do gate #232 — o script promete "nada muda").
# E o nome leva $$: com caminho FIXO, dois deploys/suítes ao mesmo tempo reescrevem e
# apagam o MESMO arquivo e um lê o do outro (falso vermelho intermitente — #240).
REQ_PREVIEW="${TTS_MINI_PREVIEW:-/tmp/deploy-mini-requirements-preview-$$}"
MODULOS=(app.py common.py backends.py tts_worker.py live_pipeline.py live_turns.py dsh_client.py)
ARQ_PARIDADE=(app.py static/index.html)

ACOES=(recon compare smoke deploy rollback)
ACAO="${1:-}"
[ -n "$ACAO" ] && shift
APPLY=0; PUSH=1; DEPS=1; TTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --sem-push) PUSH=0; shift ;;
    --sem-deps) DEPS=0; shift ;;
    --sem-tts) TTS=0; shift ;;
    -h|--help) ACAO=""; shift ;;
    -*) print -r -- "opção desconhecida: $1" >&2; exit 2 ;;
    *) print -r -- "sobrando: $1 (host vem de TTS_MINI_HOST)" >&2; exit 2 ;;
  esac
done
case " ${ACOES[*]} " in *" $ACAO "*) ;; *) ACAO="";; esac
if [ -z "$ACAO" ]; then
  print -r -- "uso: $0 recon|compare|smoke|deploy|rollback [--apply] [--sem-push] [--sem-deps] [--sem-tts]"
  sed -n '/^#   \.\/remote\/deploy_mini\.sh/p' "$0" | sed 's/^#   //'
  exit 2
fi

falhas=0
distante() { "${SSH[@]}" "$USUARIO@$HOST" "$1"; }         # $1 = comando (string única)
# O diretório é expandido NO MINI ($HOME de lá), não aqui.
cd_remoto() { case "$DIR" in /*) print -r -- "$DIR" ;; *) print -r -- "\$HOME/$DIR" ;; esac; }
preview_caminho() { case "$REQ_PREVIEW" in /*) print -r -- "$REQ_PREVIEW" ;; *) print -r -- "$(cd_remoto)/$REQ_PREVIEW" ;; esac; }
sha_blob() { git -C "$LOCAL_REPO" show "$1:$2" 2>/dev/null | shasum -a 256 | awk '{print $1}'; }
conteudo_blob() { git -C "$LOCAL_REPO" show "$1:$2" 2>/dev/null; }
sha_arq()  { distante "shasum -a 256 '$1' 2>/dev/null | awk '{print \$1}'" | head -1; }
# O app injeta `nonce="…"` por request no HTML servido (CSP): tira dos dois lados
# antes de comparar conteúdo.
sem_nonce() { sed -E 's/ nonce="[^"]*"//g'; }
# A rota existe no CÓDIGO DO REV ALVO? (rota ausente ≠ falha do smoke)
tem_rota() { git -C "$LOCAL_REPO" show "${1}:app.py" 2>/dev/null | grep -qF "$2"; }
git_remoto() { distante "cd $(cd_remoto) && git $1"; }

# `codigo` do /api/build: sha256-8 da CONCATENAÇÃO dos módulos, na ordem em que o
# app os lista. Arquivo ausente vira b"<ausente>", como no app.
codigo_esperado() { # $1 = rev (padrão HEAD)
  # shellcheck disable=SC2128  # zsh expande o array inteiro (o bash só o 1º item)
  { for m in $MODULOS; do git -C "$LOCAL_REPO" show "${1:-HEAD}:$m" 2>/dev/null || print -rn -- '<ausente>'; done } \
    | shasum -a 256 | cut -c1-8
}
chave_do_mini() { distante "cat $(cd_remoto)/.apikey 2>/dev/null" | tr -d '\n'; }
# curl sempre com -o /dev/null -w '%{http_code}'; tail -1 porque curl imprime 000 e
# sai != 0 quando a conexão falha (não queremos "000\n000" na comparação).
codigo_http() { curl -s -m 8 "$@" 2>/dev/null | tail -1; }
# `codigo` que a INSTÂNCIA VIVA reporta — "" quando não dá para perguntar.
codigo_no_ar() {
  local chave; chave="$(chave_do_mini)"
  [ -n "$chave" ] || return 0
  curl -s -m 8 -H "X-API-Key: $chave" "http://$HOST:$PORTA/api/build" 2>/dev/null \
    | sed -n 's/.*"codigo": *"\([0-9a-f]*\)".*/\1/p' | head -1
}

alvo_local() { git -C "$LOCAL_REPO" rev-parse HEAD; }
alvo_publicado() { git -C "$LOCAL_REPO" rev-parse origin/main 2>/dev/null || print -r -- "?"; }
alvo_no_ar() { git_remoto "rev-parse HEAD" | head -1; }
# O alvo do deploy é um REV (padrão HEAD, trocável por TTS_MINI_REV) — é o que
# permite subir um corte específico sem publicar a árvore em movimento de hoje.
resolver_alvo() { git -C "$LOCAL_REPO" rev-parse --verify --quiet "${1}^{commit}" 2>/dev/null; }

# ---------------------------------------------------------------- smoke

smoke_url() { # $1 = base, $2 = rótulo, $3 = chave ("" = pula autenticados)
  local base="$1" rotulo="$2" chave="$3" c corpo
  c="$(codigo_http -o /dev/null -w '%{http_code}' "$base/health")"
  if [ "$c" = 200 ] && curl -s -m 8 "$base/health" 2>/dev/null | grep -q '"ok"'; then
    print -r -- "  [ok] $rotulo /health 200"
  else
    print -r -- "  [FALHA] $rotulo /health devolveu $c (esperado 200 com \"ok\")"; falhas=1
  fi
  c="$(codigo_http -o /dev/null -w '%{http_code}' "$base/api/voices")"
  if [ "$c" = 401 ]; then
    print -r -- "  [ok] $rotulo sem chave → 401 (loopback não isenta)"
  else
    print -r -- "  [FALHA] $rotulo sem chave devolveu $c (esperado 401)"; falhas=1
  fi
  if [ -z "$chave" ]; then
    print -r -- "  [aviso] sem a chave do mini (ssh falhou?) — pulando os testes autenticados"
    return 0
  fi
  c="$(codigo_http -o /dev/null -w '%{http_code}' -H "X-API-Key: $chave" "$base/api/voices")"
  if [ "$c" = 200 ]; then
    print -r -- "  [ok] $rotulo com a chave do mini → 200"
  else
    print -r -- "  [FALHA] $rotulo recusou a chave do mini ($c)"; falhas=1
  fi
  # /api/build: prova que a INSTÂNCIA VIVA carregou o rev alvo (não só o disco).
  # A rota só é exigida quando o rev alvo a tem — num corte anterior ao #190 o
  # smoke tem de dizer "não existe neste rev", não "falha".
  if ! tem_rota "$REV_ALVO" '@app.get("/api/build")'; then
    print -r -- "  [ok] $rotulo /api/build ausente NESTE rev (anterior ao #190) — esperado"
  else
  corpo="$(curl -s -m 8 -H "X-API-Key: $chave" "$base/api/build" 2>/dev/null)"
  if print -r -- "$corpo" | grep -q '"codigo"'; then
    local no_ar esperado boot
    no_ar="$(print -r -- "$corpo" | sed -n 's/.*"codigo": *"\([0-9a-f]*\)".*/\1/p' | head -1)"
    esperado="$(codigo_esperado "$REV_ALVO")"
    boot="$(print -r -- "$corpo" | sed -n 's/.*"boot_ms": *\([0-9]*\).*/\1/p' | head -1)"
    if [ "$no_ar" = "$esperado" ]; then
      print -r -- "  [ok] $rotulo /api/build codigo=$no_ar = rev alvo ${REV_ALVO[1,12]}…"
    else
      print -r -- "  [FALHA] $rotulo /api/build codigo=$no_ar ≠ esperado $esperado (instância não é o rev alvo)"
      falhas=1
    fi
    if [ -n "$boot" ] && [ "$boot" -lt "$BOOT_MS_MAX" ]; then
      print -r -- "  [ok] processo novo (boot_ms=$boot)"
    elif [ "$POS_DEPLOY" = 1 ]; then
      print -r -- "  [FALHA] boot_ms=$boot ≥ $BOOT_MS_MAX — reiniciou de verdade?"; falhas=1
    else
      print -r -- "  [aviso] boot_ms=$boot (instância antiga: ok fora do pós-deploy)"
    fi
  else
    print -r -- "  [FALHA] $rotulo /api/build sem \"codigo\" (rota existe no rev alvo — versão errada no ar?)"; falhas=1
  fi
  fi
  # WS: o TestClient passa sem upgrade; no navegador sem `websockets` o /api/live/ws
  # dá 500 — este é o único jeito de pegar isso sem navegador. Como o /api/build,
  # só é exigido quando o rev alvo tem a rota (o corte do passo 1 é anterior ao épico).
  if ! tem_rota "$REV_ALVO" '@app.websocket("/api/live/ws")'; then
    print -r -- "  [ok] $rotulo /api/live/ws ausente NESTE rev (anterior ao épico Live) — esperado"
  else
  c="$(curl -s -m 5 -o /dev/null -w '%{http_code}' \
       -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
       -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
       "$base/api/live/ws?key=$chave" 2>/dev/null | tail -1)"
  case "$c" in
    101) print -r -- "  [ok] $rotulo WebSocket 101 (uvicorn com upgrade)" ;;
    000) print -r -- "  [aviso] $rotulo WS sem resposta em 5s (timeout do curl, não necessariamente erro)" ;;
    *)   print -r -- "  [FALHA] $rotulo /api/live/ws devolveu $c (esperado 101; falta websockets?)"; falhas=1 ;;
  esac
  fi
  # Paridade de conteúdo: o index servido tem que ser o blob do rev alvo. O app
  # injeta `nonce="…"` POR REQUEST no HTML (CSP), então a paridade é do conteúdo —
  # o nonce sai dos dois lados antes do sha (sem isso a checagem nunca fecha).
  local sha_no_ar sha_esperado
  sha_no_ar="$(curl -s -m 10 "$base/" 2>/dev/null | sem_nonce | shasum -a 256 | awk '{print $1}')"
  sha_esperado="$(conteudo_blob "$REV_ALVO" static/index.html | sem_nonce | shasum -a 256 | awk '{print $1}')"
  if [ "$sha_no_ar" = "$sha_esperado" ]; then
    print -r -- "  [ok] $rotulo index.html = blob do rev alvo (nonce do CSP ignorado)"
  else
    print -r -- "  [FALHA] $rotulo index.html (${sha_no_ar[1,12]}…) ≠ rev alvo (${sha_esperado[1,12]}…)"
    falhas=1
  fi
  [ "$TTS" = 1 ] && smoke_tts "$base" "$rotulo" "$chave"
}

# Síntese REAL: /health e /api/voices provam auth e versão, não o caminho que a
# produção usa (texto → job → peça). Roda o job até o fim e confere que saiu áudio.
smoke_tts() { # $1 = base, $2 = rótulo, $3 = chave
  # `status` é read-only no zsh (alias de $?) — daí `estado_job`.
  local base="$1" rotulo="$2" chave="$3" corpo job estado_job t=0 cod_p tam
  corpo="$(curl -s -m 20 -X POST -H "X-API-Key: $chave" -H 'Content-Type: application/json' \
           -d '{"text":"teste do deploy"}' "$base/api/tts" 2>/dev/null)"
  job="$(print -r -- "$corpo" | sed -n 's/.*"job_id": *"\([^"]*\)".*/\1/p' | head -1)"
  if [ -z "$job" ]; then
    print -r -- "  [FALHA] $rotulo POST /api/tts sem job_id: $(print -r -- "$corpo" | head -c 120)"
    falhas=1; return 0
  fi
  while [ "$t" -lt "$TTS_TIMEOUT" ]; do
    estado_job="$(curl -s -m 8 -H "X-API-Key: $chave" "$base/api/tts/jobs/$job" 2>/dev/null \
                 | sed -n 's/.*"status": *"\([a-z]*\)".*/\1/p' | head -1)"
    [ "$estado_job" = "done" ] && break
    [ "$estado_job" = "error" ] && break
    sleep 1; t=$((t + 1))
  done
  if [ "$estado_job" = "done" ]; then
    cod_p="$(codigo_http -o /dev/null -w '%{http_code}' -H "X-API-Key: $chave" "$base/api/tts/jobs/$job/pieces/0")"
    tam="$(curl -s -m 15 -H "X-API-Key: $chave" "$base/api/tts/jobs/$job/pieces/0" 2>/dev/null | wc -c | tr -d ' ')"
    if [ "$cod_p" = 200 ] && [ "${tam:-0}" -gt 0 ]; then
      print -r -- "  [ok] $rotulo POST /api/tts real: job $job em ~${t}s, peça 0 com ${tam} bytes"
    else
      print -r -- "  [FALHA] $rotulo job $job done mas a peça 0 deu $cod_p com ${tam:-0} bytes"
      falhas=1
    fi
  elif [ "$estado_job" = "error" ]; then
    print -r -- "  [FALHA] $rotulo job $job terminou em error (log do mini: /tmp/tts-studio.log)"
    falhas=1
  else
    print -r -- "  [FALHA] $rotulo job $job não concluiu em ${TTS_TIMEOUT}s (status=${estado_job:-sem resposta})"
    falhas=1
  fi
}

smoke() {
  print -r -- "== smoke (rev alvo ${REV_ALVO[1,12]}…)"
  local chave; chave="$(chave_do_mini)"
  smoke_url "http://$HOST:$PORTA" "LAN" "$chave"
  smoke_url "https://$PUBLICO" "público" "$chave"
  if [ -z "$chave" ]; then
    print -r -- "  [aviso] chave do mini vazia: conferir $(cd_remoto)/.apikey"
  fi
}

# ---------------------------------------------------------------- recon

recon() {
  print -r -- "== host"
  distante "hostname; uname -srm; uptime | sed 's/^ *//'" | sed 's/^/  /'
  distante "sysctl -n hw.memsize | awk '{printf \"RAM: %.0f GB\\n\", \$1/1073741824}'; df -h / | tail -1 | awk '{print \"disco: \" \$4 \" livres (\" \$5 \" usado)\"}'" | sed 's/^/  /'
  print -r -- "== git do mini"
  distante "cd $(cd_remoto) && git log -1 --format='HEAD %h %ad %s' --date=iso" | sed 's/^/  /'
  distante "cd $(cd_remoto) && git status --porcelain --untracked-files=no | head -5" | sed 's/^/  sujo: /'
  distante "cd $(cd_remoto) && (git fetch --quiet origin main 2>/dev/null || echo '  [aviso] fetch falhou (sem rede no mini?)') && echo \"distância até origin/main: \$(git rev-list --count origin/main..HEAD 2>/dev/null) local, \$(git rev-list --count HEAD..origin/main 2>/dev/null) atrás\"" | sed 's/^/  /'
  print -r -- "== serviço"
  distante "launchctl list | grep -i '$(print -r -- "$LABEL" | sed 's/\./\\./g')' || echo 'agente ausente'" | sed 's/^/  /'
  distante "lsof -nP -iTCP:$PORTA -sTCP:LISTEN 2>/dev/null | tail -1 || echo 'nada na porta $PORTA'" | sed 's/^/  porta: /'
  print -r -- "== venv e deps do caminho novo"
  distante "cd $(cd_remoto) && ./.venv-mlx/bin/python -V 2>&1" | sed 's/^/  /'
  distante "cd $(cd_remoto) && ./.venv-mlx/bin/python -c 'import importlib.util as u; print(\"websockets\", bool(u.find_spec(\"websockets\"))); print(\"importlib_resources\", bool(u.find_spec(\"importlib_resources\")))' 2>&1" | sed 's/^/  dep: /'
  print -r -- "== uso (log de produção)"
  distante "grep -c 'POST /api/tts' /tmp/tts-studio.log 2>/dev/null || echo 0" | sed 's|^|  POST /api/tts no log: |'
  distante "ls -l /tmp/tts-studio.log 2>/dev/null | awk '{print \$5, \$6, \$7, \$8}'" | sed 's/^/  log: /'
  print -r -- "== estado do último deploy (se houve)"
  distante "cd $(cd_remoto) && cat $ESTADO 2>/dev/null || echo '(sem $ESTADO)'" | sed 's/^/  /'
  print -r -- "  chave do mini: $(chave_do_mini | wc -c | tr -d ' ') bytes"
}

# ---------------------------------------------------------------- compare

compare() {
  local local_sha no_ar_sha
  local_sha="$(alvo_local)"; no_ar_sha="$(alvo_no_ar)"
  print -r -- "== rev"
  print -r -- "  alvo do deploy:    ${ALVO_SHA[1,12]}…${ALVO_REV:+ ($ALVO_REV)}"
  print -r -- "  local (HEAD):      ${local_sha[1,12]}…"
  print -r -- "  origin/main:       $(alvo_publicado | cut -c1-12)…"
  print -r -- "  no ar (mini HEAD): ${no_ar_sha[1,12]}…"
  if [ "$ALVO_SHA" = "$no_ar_sha" ]; then
    print -r -- "  [IGUAL] produção no rev alvo"
  else
    local atras
    atras="$(git -C "$LOCAL_REPO" rev-list --count "$no_ar_sha..$ALVO_SHA" 2>/dev/null || print -r -- '?')"
    print -r -- "  [DIFERENTE] produção $atras commit(s) atrás do rev alvo"
    if git -C "$LOCAL_REPO" merge-base --is-ancestor "$no_ar_sha" "$ALVO_SHA" 2>/dev/null; then
      print -r -- "  alvo é descendente da produção (subida direta)"
    else
      print -r -- "  [aviso] produção NÃO é ancestral do alvo — é rewind/desvio, confira o motivo"
    fi
    falhas=1
  fi
  print -r -- "== árvore do mini"
  local sujo
  sujo="$(git_remoto "status --porcelain --untracked-files=no" | head -5)"
  if [ -z "$sujo" ]; then
    print -r -- "  [ok] limpa (pull não vai conflitar)"
  else
    print -r -- "  [DIFERENTE] rastreados modificados no mini:"; print -r -- "$sujo" | sed 's/^/    /'
    falhas=1
  fi
  print -r -- "== conteúdo (blob do rev alvo × arquivo no ar)"
  local alvo a b
  for alvo in "${ARQ_PARIDADE[@]}"; do
    a="$(sha_blob "$ALVO_SHA" "$alvo")"; b="$(sha_arq "$(distante "cd $(cd_remoto) && pwd")/$alvo")"
    if [ -z "$b" ]; then
      print -r -- "  [DIFERENTE] $alvo: ausente no mini"; falhas=1
    elif [ "$a" = "$b" ]; then
      print -r -- "  [IGUAL]     $alvo ${a[1,12]}…"
    else
      print -r -- "  [DIFERENTE] $alvo: rev alvo ${a[1,12]}… × no ar ${b[1,12]}…"; falhas=1
    fi
  done
}

# ---------------------------------------------------------------- deploy

push_necessario() { # 0 = sim (o rev alvo ainda não está no origin)
  git -C "$LOCAL_REPO" merge-base --is-ancestor "$ALVO_SHA" origin/main 2>/dev/null && return 1
  return 0
}

# Delta de deps contra o requirements DO REV ALVO — e não o do mini, que antes do
# pull ainda é o antigo (era o erro do primeiro dry-run: sem `websockets` na lista
# velha, o preview dizia "nada a instalar" enquanto a produção ia quebrar no WS).
deps_preview() { # $1 = rev alvo; escreve o requirements no mini e devolve o "Would install"
  # ${1} com chaves: `"$1:requirements.txt"` o zsh lê como modificador `:r` e vira
  # "...equirements.txt" (o sha não é ambíguo, o `git show` só não acha o arquivo).
  local prev; prev="$(preview_caminho)"
  git -C "$LOCAL_REPO" show "${1}:requirements.txt" 2>/dev/null \
    | "${SSH[@]}" "$USUARIO@$HOST" "cat > '$prev'" || return 0
  distante "cd $(cd_remoto) && ./.venv-mlx/bin/pip install --dry-run -r '$prev' 2>&1 | grep -E 'Would install|^ERROR' | tail -3"
}

deploy() {
  print -r -- "== deploy (rev alvo ${ALVO_SHA[1,12]}…${ALVO_REV:+ ($ALVO_REV)})"
  local wip
  wip="$(git -C "$LOCAL_REPO" status --porcelain --untracked-files=no | head -5)"
  if [ -n "$wip" ]; then
    print -r -- "  [aviso] árvore local com WIP (não commitado NÃO vai para produção):"
    print -r -- "$wip" | sed 's/^/    /'
  fi
  if push_necessario; then
    local pendentes; pendentes="$(git -C "$LOCAL_REPO" rev-list --count "origin/main..$ALVO_SHA" 2>/dev/null || print -r -- '?')"
    if [ "$PUSH" = 1 ]; then
      print -r -- "  push: publicar o rev alvo em origin/main ($pendentes commit(s) à frente do origin)"
    else
      print -r -- "  push: pulado (--sem-push) — $pendentes commit(s) fora do origin"
      print -r -- "    [aviso] o mini puxa do origin: sem push ele não chega no rev alvo"
    fi
  else
    print -r -- "  push: nada a publicar (origin já contém o rev alvo)"
  fi

  print -r -- "== mini"
  local sujo
  sujo="$(git_remoto "status --porcelain --untracked-files=no" | head -5)"
  if [ -n "$sujo" ]; then
    print -r -- "  [PARA] mini com rastreados modificados (switch não pode atropelar):"
    print -r -- "$sujo" | sed 's/^/    /'
    [ "$APPLY" = 1 ] && return 1 || return 0
  fi
  local deps=""
  if [ "$DEPS" = 1 ]; then
    deps="$(deps_preview "$ALVO_SHA")"
    print -r -- "  deps: pip install -r requirements.txt (do rev alvo)"
    print -r -- "    preview: $(preview_caminho)  (fora da árvore do mini)"
    if [ -n "$deps" ]; then print -r -- "$deps" | sed 's/^/    /'
    else print -r -- "    nada a instalar (o venv já satisfaz o requirements)"; fi
  else
    print -r -- "  deps: pulado (--sem-deps)"
  fi
  if [ "$APPLY" = 0 ]; then
    print -r -- "  (dry-run: rode com --apply para push + backup + switch + deps + restart + smoke)"
    # o arquivo fica (o caminho sai no output): é o que prova que o preview usou o
    # requirements DO REV ALVO, não o do mini. O --apply limpa no fim.
    return 0
  fi

  # --- a partir daqui muda a produção
  if [ "$PUSH" = 1 ] && push_necessario; then
    git -C "$LOCAL_REPO" push origin "${ALVO_SHA}:refs/heads/main" \
      || { print -r -- "  [PARA] push falhou (origin/main à frente do alvo? publique o alvo como branch ou use --sem-push)"; return 1; }
    print -r -- "  push: ok (origin/main = rev alvo)"
  fi
  # Nada a fazer = mini já no rev alvo, sem deps novas e a INSTÂNCIA VIVA já com o
  # código carregado (o codigo do /api/build é o que separa "no disco" de "carregado").
  local sem_deps_novas=1
  print -r -- "$deps" | grep -q 'Would install' && sem_deps_novas=0
  if [ "$(alvo_no_ar)" = "$ALVO_SHA" ] && [ "$sem_deps_novas" = 1 ] \
     && [ -n "$(codigo_no_ar)" ] && [ "$(codigo_no_ar)" = "$(codigo_esperado "$ALVO_SHA")" ]; then
    print -r -- "  nada a fazer (rev alvo no ar, sem deps novas e código já carregado) — restart recarrega modelo, então não reinicia"
    POS_DEPLOY=0 REV_ALVO="$ALVO_SHA" smoke
    return 0
  fi
  print -r -- "  backup do estado atual:"
  # O estado é o alvo do ROLLBACK: num re-deploy do MESMO rev ele não pode ser
  # sobrescrito com o próprio alvo (senão o rollback vira no-op e perde o alvo real).
  if [ "$(alvo_no_ar)" = "$ALVO_SHA" ]; then
    print -r -- "    (mini já no rev alvo — o alvo de rollback anterior é preservado)"
  fi
  distante "cd $(cd_remoto) && { [ \"\$(git rev-parse HEAD)\" = '$ALVO_SHA' ] || printf 'sha=%s\ndata=%s\n' \"\$(git rev-parse HEAD)\" \"\$(date '+%F %T')\" > $ESTADO; } && ./.venv-mlx/bin/pip freeze > .deploy-mini-freeze-\$(date +%F-%H%M%S) && cat $ESTADO" | sed 's/^/    /'
  # `switch -C prod-<sha>` (e não checkout --detach): o mini fica numa branch com
  # nome, o próximo deploy não se perde em detached HEAD e o rollback é o mesmo passo.
  print -r -- "  fetch + switch para o rev alvo:"
  distante "cd $(cd_remoto) && git fetch --quiet origin main" \
    || { print -r -- "  [PARA] fetch falhou no mini — nada foi reiniciado"; return 1; }
  if ! distante "cd $(cd_remoto) && git cat-file -e '${ALVO_SHA}^{commit}' 2>/dev/null"; then
    print -r -- "  [PARA] o rev alvo não existe no mini depois do fetch (--sem-push? origin à frente?)"
    return 1
  fi
  distante "cd $(cd_remoto) && git switch --quiet -C 'prod-${ALVO_SHA[1,12]}' '$ALVO_SHA' && git log -1 --format='    agora: %h %s'" \
    || { print -r -- "  [PARA] switch para o rev alvo falhou — nada foi reiniciado"; return 1; }
  if [ "$DEPS" = 1 ]; then
    distante "cd $(cd_remoto) && ./.venv-mlx/bin/pip install -r requirements.txt 2>&1 | tail -3" | sed 's/^/    /'
  fi
  distante "cd $(cd_remoto) && rm -f '$(preview_caminho)'"
  print -r -- "  reiniciando $LABEL…"
  distante "launchctl kickstart -k gui/\$(id -u)/$LABEL && echo kickstart-ok" | sed 's/^/    /'
  print -r -- "  esperando ${ESPERA}s o run.sh subir a porta…"
  sleep "$ESPERA"
  POS_DEPLOY=1 REV_ALVO="$ALVO_SHA" smoke
}

# ---------------------------------------------------------------- rollback

rollback() {
  local estado sha
  estado="$(distante "cd $(cd_remoto) && cat $ESTADO 2>/dev/null")"
  sha="$(print -r -- "$estado" | sed -n 's/^sha=//p' | head -1)"
  if [ -z "$sha" ]; then
    print -r -- "  [aviso] nenhum $ESTADO no mini — sem rollback automático (o SHA alvo seria manual)"
    return 0
  fi
  print -r -- "== rollback para ${sha[1,12]}… ($(print -r -- "$estado" | sed -n 's/^data=//p' | head -1))"
  if [ "$APPLY" = 0 ]; then
    print -r -- "  (dry-run: rode com --apply para switch + restart + smoke)"
    return 0
  fi
  distante "cd $(cd_remoto) && git switch --quiet -C 'prod-${sha[1,12]}' '$sha' && git log -1 --format='  agora: %h %s'" | sed 's/^/  /'
  print -r -- "  reiniciando $LABEL…"
  distante "launchctl kickstart -k gui/\$(id -u)/$LABEL && echo kickstart-ok" | sed 's/^/  /'
  print -r -- "  [nota] deps NÃO voltam sozinhas: o freeze do deploy está em .deploy-mini-freeze-* no mini"
  sleep "$ESPERA"
  POS_DEPLOY=1 REV_ALVO="$sha" smoke
}

ALVO_REV="${TTS_MINI_REV:-HEAD}"
ALVO_SHA="$(resolver_alvo "$ALVO_REV")" || true
if [ -z "${ALVO_SHA:-}" ]; then
  print -r -- "rev alvo não resolve no repo ($LOCAL_REPO): $ALVO_REV" >&2
  exit 2
fi
REV_ALVO="$ALVO_SHA"
POS_DEPLOY="${TTS_MINI_POS_DEPLOY:-0}"
case "$ACAO" in
  recon)    recon ;;
  compare)  compare ;;
  smoke)    smoke ;;
  deploy)   deploy || falhas=1 ;;
  rollback) rollback || falhas=1 ;;
esac
exit $falhas
