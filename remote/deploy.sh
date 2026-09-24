#!/bin/zsh
# Deploy dos servidores RTX (OmniVoice/Voxtral) — reproduzível e verificável.
#
# Nada aqui toca a máquina remota sem o subcomando pedido, e `deploy`/`rollback`
# só mudam o servidor com `--apply` (o padrão é mostrar o que faria).
#
#   ./remote/deploy.sh recon    [host]                 # read-only: units, portas, venv, arquivos
#   ./remote/deploy.sh compare  [host]                 # sha256 repo × no ar (sem imprimir segredo)
#   ./remote/deploy.sh smoke    [host]                 # pós-deploy: /health 200, 401 sem chave
#   ./remote/deploy.sh deploy   [host] --apply         # backup + scp + ast.parse + restart + smoke
#   ./remote/deploy.sh rollback [host] --apply         # volta o último .bak-* e reinicia
#
# `--service voxtral|omni|all` (padrão: all) limita a um servidor. `host` também
# vem de $TTS_REMOTE_HOST. Portas/units/pastas saem de $*_REMOTE_* (ver DEPLOY.md
# §4): o unit e a porta do Voxtral ainda são ❓ e podem ser corrigidos sem editar
# este arquivo.
#
# Regras que o script respeita (e o DEPLOY.md §5 explica):
#   • ssh com ControlMaster=no/ControlPath=none — o ~/.ssh/config do dono usa
#     socket em ~/.ssh/cm, que quebra quando o HOME não é gravável (sandbox/CI);
#   • `restart` recarrega os modelos na VRAM (dezenas de segundos): sem diff, não
#     reinicia;
#   • nenhuma chave é impressa: `Environment=` do unit sai com o valor redigido
#     para <len N> (e o nome, para conferir presença).
#
# Uso típico (quando o dono liberar): `recon` → `compare` → `deploy --apply` →
# `smoke`; deu ruim, `rollback --apply`.
set -u
RAIZ="${0:A:h}"                 # remote/
SSH=(ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=8)
SCP=(scp -o BatchMode=yes -o ControlMaster=no -o ControlPath=none)

HOST_ARG=""
SERVICO="all"
APPLY=0
DIR_OMNI="${OMNI_REMOTE_DIR:-/root/omnivoice}"
DIR_VOXTRAL="${VOXTRAL_REMOTE_DIR:-/root/voxtral}"
UNIT_OMNI="${OMNI_REMOTE_UNIT:-omnivoice-tts.service}"
UNIT_VOXTRAL="${VOXTRAL_REMOTE_UNIT:-voxtral.service}"
PORT_OMNI="${OMNI_REMOTE_PORT:-8800}"
PORT_VOXTRAL="${VOXTRAL_REMOTE_PORT:-8000}"
COMPARTILHADO="auth_policy.py"  # usado pelos dois servidores: vai junto no deploy
ARQ_REMOTO="server.py"          # nome do arquivo NO SERVIDOR (o repo tem nome descritivo)

ACOES=(recon compare smoke deploy rollback)
ACAO="${1:-}"
if [ -n "$ACAO" ]; then shift; fi
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --service) SERVICO="${2:?--service precisa de valor}"; shift 2 ;;
    --service=*) SERVICO="${1#*=}"; shift ;;
    -h|--help) ACAO=""; shift ;;
    -*) print -r -- "opção desconhecida: $1" >&2; exit 2 ;;
    *) [ -n "$HOST_ARG" ] && { print -r -- "host já definido ($HOST_ARG) — sobrou $1" >&2; exit 2; }
       HOST_ARG="$1"; shift ;;
  esac
done
case " ${ACOES[*]} " in *" $ACAO "*) ;; *) ACAO="";; esac
if [ -z "$ACAO" ]; then
  print -r -- "uso: $0 recon|compare|smoke|deploy|rollback [host] [--apply] [--service voxtral|omni|all]"
  sed -n '/^#   \.\/remote\/deploy\.sh/p' "$0" | sed 's/^#   //'
  exit 2
fi
HOST="${HOST_ARG:-${TTS_REMOTE_HOST:-}}"
[ -n "$HOST" ] || { print -r -- "informe o host (ou exporte TTS_REMOTE_HOST)" >&2; exit 2; }
case "$SERVICO" in
  all) SERVICOS=(voxtral omni) ;;
  voxtral|omni) SERVICOS=("$SERVICO") ;;
  *) print -r -- "serviço desconhecido: $SERVICO (use voxtral|omni|all)" >&2; exit 2 ;;
esac

servico_arquivo() { case "$1" in omni) print -r -- "$RAIZ/omni_server.py" ;; *) print -r -- "$RAIZ/voxtral_server.py" ;; esac; }
servico_dir()     { case "$1" in omni) print -r -- "$DIR_OMNI" ;; *) print -r -- "$DIR_VOXTRAL" ;; esac; }
servico_unit()    { case "$1" in omni) print -r -- "$UNIT_OMNI" ;; *) print -r -- "$UNIT_VOXTRAL" ;; esac; }
servico_porta()   { case "$1" in omni) print -r -- "$PORT_OMNI" ;; *) print -r -- "$PORT_VOXTRAL" ;; esac; }

distante() { "${SSH[@]}" "$HOST" "$1"; }          # $1 = comando (string única)
sha_local() { shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'; }
sha_distante() { distante "sha256sum '$1' 2>/dev/null | awk '{print \$1}'"; }

# `systemctl show -p Environment` traz OMNI_API_KEY=... — redige só o valor.
sem_segredo() {
  python3 -c '
import re, sys
segredo = re.compile(r"(_API_KEY|_TOKEN|_SECRET|_PASSWORD)$")
par = re.compile(r"^((?:Environment=)?[A-Za-z_][A-Za-z0-9_]*)=(.*)$")
for pedaco in sys.stdin.read().split():
    m = par.match(pedaco)
    if m and segredo.search(m.group(1)):
        pedaco = f"{m.group(1)}=<len {len(m.group(2))}>"
    print(pedaco)
'
}

tem_diff() { # $1 = serviço; 0 quando há algo diferente para subir
  local dir local_arq remoto
  dir="$(servico_dir "$1")"
  for alvo in "$ARQ_REMOTO" "$COMPARTILHADO"; do
    local_arq="$(servico_arquivo "$1")"; [ "$alvo" = "$COMPARTILHADO" ] && local_arq="$RAIZ/$COMPARTILHADO"
    # hash (e não diff por stdin): arquivo sem \n no fim dava falso positivo
    remoto="$(sha_distante "$dir/$alvo" | head -1)"
    [ -z "$remoto" ] && return 0
    [ "$(sha_local "$local_arq")" = "$remoto" ] || return 0
  done
  return 1
}

diff_do_servico() { # $1 = serviço; mostra o que o `deploy` mudaria
  local dir arq novo remoto
  dir="$(servico_dir "$1")"; arq="$ARQ_REMOTO"; novo="$(servico_arquivo "$1")"
  for alvo in "$arq" "$COMPARTILHADO"; do
    local local_arq="$novo"; [ "$alvo" = "$COMPARTILHADO" ] && local_arq="$RAIZ/$COMPARTILHADO"
    remoto="$(distante "cat '$dir/$alvo' 2>/dev/null" || true)"
    if [ -z "$remoto" ]; then
      print -r -- "  $alvo (repo $(basename "$local_arq")): não existe no servidor (primeira subida)"
      continue
    fi
    local iguais=0
    print -r -- "$remoto" | diff -q - "$local_arq" >/dev/null 2>&1 && iguais=1
    if [ "$iguais" = 1 ]; then
      print -r -- "  $alvo: igual ao repo"
    else
      print -r -- "  $alvo (repo $(basename "$local_arq")): DIFERENTE do repo:"
      print -r -- "$remoto" | diff -u -L "no-ar/$alvo" -L "repo/$alvo" - "$local_arq" | tail -n +3 | sed 's/^/    /' || true
    fi
  done
}

smoke_do_servico() { # $1 = serviço; 0 = passou
  local porta url corpo codigo falhou=0
  porta="$(servico_porta "$1")"; url="http://$HOST:$porta/health"
  corpo="$(curl -s -m 8 "$url" 2>/dev/null || true)"
  codigo="$(curl -s -o /dev/null -w '%{http_code}' -m 8 "$url" 2>/dev/null || print -r -- 000)"
  if [ "$codigo" = 200 ] && print -r -- "$corpo" | grep -q '"auth"'; then
    print -r -- "  [ok] /health 200 com campo auth"
  else
    print -r -- "  [FALHA] /health devolveu $codigo (esperado 200 com \"auth\")"
    falhou=1
  fi
  local modo; modo="$(print -r -- "$corpo" | sed -n 's/.*"auth": *"\([a-z]*\)".*/\1/p' | head -1)"
  local sem_chave
  sem_chave="$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X POST "http://$HOST:$porta/rota-inexistente" 2>/dev/null || print -r -- 000)"
  case "$modo" in
    required)
      if [ "$sem_chave" = 401 ]; then
        print -r -- "  [ok] sem chave → 401 (chave exigida)"
      else
        print -r -- "  [FALHA] sem chave devolveu $sem_chave (esperado 401 — middleware fora do ar?)"
        falhou=1
      fi ;;
    open)
      case "$1" in omni) escape="OMNI_ALLOW_NO_AUTH=1" ;; *) escape="VOXTRAL_ALLOW_NO_AUTH=1" ;; esac
      print -r -- "  [aviso] modo ABERTO ($escape): use só atrás de firewall/VPN" ;;
    *) print -r -- "  [aviso] /health sem modo de auth legível — republica com 'auth' no corpo" ;;
  esac
  if print -r -- "$corpo" | grep -q '"vad": *"torch-jit"'; then
    print -r -- "  [aviso] VAD no jit do torch (falta onnxruntime no venv do servidor?)"
  fi
  local chave=""
  case "$1" in omni) chave="${OMNI_API_KEY:-}";; voxtral) chave="${VOXTRAL_API_KEY:-}";; esac
  if [ -n "$chave" ]; then
    local com_chave
    com_chave="$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X POST -H "Authorization: Bearer $chave" \
                 "http://$HOST:$porta/rota-inexistente" 2>/dev/null || print -r -- 000)"
    if [ "$com_chave" != 401 ]; then
      print -r -- "  [ok] com a chave do ambiente → $com_chave (não é 401)"
    else
      print -r -- "  [FALHA] a chave de $1 no ambiente foi recusada (401)"
      falhou=1
    fi
  fi
  return $falhou
}

falhas=0
for s in "${SERVICOS[@]}"; do
  case "$ACAO" in
    recon)
      dir="$(servico_dir "$s")"
      print -r -- "== $s ($dir, unit $(servico_unit "$s"), porta $(servico_porta "$s"))"
      distante "hostname; uname -srm" | sed 's/^/  host: /'
      distante "systemctl is-active '$(servico_unit "$s")' 2>/dev/null || echo inativo" | sed 's/^/  unit: /'
      distante "systemctl show -p FragmentPath -p ExecStart -p Environment '$(servico_unit "$s")' 2>/dev/null" | sem_segredo | sed 's/^/  /'
      distante "cd '$dir' 2>/dev/null && ls -l server.py '$COMPARTILHADO' 2>/dev/null | awk '{print \$5, \$9}'" | sed 's/^/  arquivo: /'
      print -r -- "  sha256 no ar:"
      for alvo in "$ARQ_REMOTO" "$COMPARTILHADO"; do
        print -r -- "    $alvo $(sha_distante "$dir/$alvo" | head -1)"
      done
      distante "python3 -V 2>&1" | sed 's/^/  python: /'
      distante "python3 -m pip freeze 2>/dev/null | wc -l" | sed 's/^/  pacotes no venv: /'
      # deps do VAD: ONNX (senão o servidor cai no jit do torch) e o backport do
      # importlib_resources (sem ele o silero usa importlib.resources.path,
      # deprecado desde o py3.11 — task #35). find_spec não importa nada.
      distante "python3 -c 'import importlib.util as u; print(\"importlib_resources\", bool(u.find_spec(\"importlib_resources\"))); print(\"onnxruntime\", bool(u.find_spec(\"onnxruntime\")))' 2>&1" | sed 's/^/  dep: /'
      distante "python3 -W always::DeprecationWarning -c 'import silero_vad; silero_vad.load_silero_vad(onnx=True)' 2>&1 | grep -ci deprecat || true" | sed 's/^/  silero DeprecationWarning: /'
      distante "nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>/dev/null || echo sem nvidia-smi" | sed 's/^/  gpu: /'
      ;;
    compare)
      print -r -- "== $s"
      for alvo in "$ARQ_REMOTO" "$COMPARTILHADO"; do
        local_arq="$(servico_arquivo "$s")"; [ "$alvo" = "$COMPARTILHADO" ] && local_arq="$RAIZ/$COMPARTILHADO"
        a="$(sha_local "$local_arq")"; b="$(sha_distante "$(servico_dir "$s")/$alvo" | head -1)"
        if [ -z "$b" ]; then
          print -r -- "  [DIFERENTE] $alvo (repo $(basename "$local_arq")): não existe no servidor (repo ${a[1,12]}…)"
          falhas=1
        elif [ "$a" = "$b" ]; then
          print -r -- "  [IGUAL]     $alvo (repo $(basename "$local_arq")) ${a[1,12]}…"
        else
          print -r -- "  [DIFERENTE] $alvo (repo $(basename "$local_arq")): repo ${a[1,12]}… × no ar ${b[1,12]}…"
          falhas=1
        fi
      done
      ;;
    smoke)
      print -r -- "== $s (porta $(servico_porta "$s"))"
      smoke_do_servico "$s" || falhas=1
      ;;
    deploy)
      print -r -- "== $s ($(servico_dir "$s"))"
      diff_do_servico "$s"
      if [ "$APPLY" = 0 ]; then
        print -r -- "  (dry-run: rode com --apply para backup + scp + restart)"
        continue
      fi
      if ! tem_diff "$s"; then
        print -r -- "  nada a fazer (sem diff) — restart recarrega modelo, então não reinicia"
        continue
      fi
      distante "cd '$(servico_dir "$s")' && cp -a server.py \"server.py.bak-\$(date +%F-%H%M%S)\" && ls -1t server.py.bak-* | head -1" | sed 's/^/  backup: /'
      "${SCP[@]}" "$(servico_arquivo "$s")" "$HOST:$(servico_dir "$s")/server.py"
      "${SCP[@]}" "$RAIZ/$COMPARTILHADO" "$HOST:$(servico_dir "$s")/$COMPARTILHADO"
      print -r -- "  copiado (server.py + $COMPARTILHADO)"
      distante "cd '$(servico_dir "$s")' && python3 -c 'import ast,pathlib;ast.parse(pathlib.Path(\"server.py\").read_text())' && echo sintaxe-ok" | sed 's/^/  /'
      print -r -- "  reiniciando $(servico_unit "$s") (recarrega modelo na VRAM)…"
      distante "systemctl restart '$(servico_unit "$s")'"
      distante "sleep ${TTS_REMOTE_ESPERA:-5}; systemctl is-active '$(servico_unit "$s")'" | sed 's/^/  unit: /'
      print -r -- "  smoke:"
      smoke_do_servico "$s" || falhas=1
      ;;
    rollback)
      print -r -- "== $s"
      ultimo="$(distante "ls -1t '$(servico_dir "$s")'/server.py.bak-* 2>/dev/null | head -1")"
      if [ -z "$ultimo" ]; then
        print -r -- "  [aviso] nenhum server.py.bak-* em $(servico_dir "$s")"
        continue
      fi
      print -r -- "  volta $ultimo"
      if [ "$APPLY" = 0 ]; then
        print -r -- "  (dry-run: rode com --apply para restaurar e reiniciar)"
        continue
      fi
      distante "cd '$(servico_dir "$s")' && cp -a '$ultimo' server.py && systemctl restart '$(servico_unit "$s")'"
      print -r -- "  restaurado e $(servico_unit "$s") reiniciado — smoke:"
      smoke_do_servico "$s" || falhas=1
      ;;
  esac
done

exit $falhas
