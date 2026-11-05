#!/usr/bin/env bash
# Fluxo de UI da política admin x uso (tasks #34/#42, política de #21 no app.py).
#
# Exercita o app REAL pelas duas pontas, com chave de verdade:
#   1. cria (por loopback, que é admin) uma chave `role: "use"` e uma `admin`
#   2. confere o contrato no servidor: chave de uso NÃO lê segredo (vem mascarado)
#      e NÃO altera campo administrativo (POST devolve `admin_ignored`)
#   3. abre a UI na LAN (loopback é admin por definição, então só pela LAN a UI
#      vê uma chave de uso) e cobra: selo, campos travados, segredo mascarado,
#      save que NOMEIA o que ficou de fora e 403 que explica onde resolver
#   4. contraprova: com a chave admin, nada travado e o campo aplica
#   5. papel da chave pela própria UI: cria com papel pelo form, confere o selo da
#      linha, troca o papel e apaga (POST/PATCH/DELETE reais)
#   6. remove as chaves de teste
#
#   ./tests/admin_ui_flow.sh                    # SOBE instância própria (padrão)
#   BASE=http://127.0.0.1:53203 ./tests/admin_ui_flow.sh   # usa instância existente
#   ADMIN_UI_NO_DONO=1 ./tests/admin_ui_flow.sh # assume o :7860 do dono de propósito
#
# POR QUE INSTÂNCIA PRÓPRIA POR PADRÃO (#437): a suíte cria chaves de teste, e no
# :7860 elas caem no `.apikeys.json` do DONO — durante a rodada, e para sempre se
# a rodada morrer por SIGKILL. Com instância própria o arquivo de chaves e o de
# settings vão para /tmp (mesmo idioma do `ultima_chave_ui.sh`), e o do dono fica
# byte a byte igual.
#
# ⚠ A INSTÂNCIA DEVE ESCUTAR EM 0.0.0.0 (não só 127.0.0.1): metade da suíte passa
# pela URL de LAN DE PROPÓSITO — loopback é admin por definição, então só por um
# IP não-loopback a UI enxerga uma chave de uso. Com bind só no loopback o
# sintoma é `Connection refused` em `http://<LAN>:<porta>` e NÃO é regressão.
set -euo pipefail

BASE_ORIGINAL="${BASE:-}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

LAN="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
if [ -z "$LAN" ]; then
  echo "✖ sem IP de LAN (en0/en1) — a UI só vê chave de uso por um IP não-loopback" >&2
  exit 1
fi

# Serializa com as outras que carregam modelo (#134): esta suíte gera por TTS.
source "$(dirname "${BASH_SOURCE[0]}")/serial.sh"; serial_pega || exit 1

# A UI SÓ vê chave de uso por um IP não-loopback, então metade do teste roda em
# http://<LAN>:<porta>. A porta tem de sair do PRÓPRIO BASE (#140): com :7860
# fixo, uma instância própria (BASE=…:53203) criava as chaves nela e as usava
# contra o app do DONO — 401 e, pior, validando build velho.
# sem BASE de fora, quem resolve a porta é o bloco da instância própria (abaixo)
PORTA=""
if [ -n "$BASE_ORIGINAL" ]; then
  PORTA="${BASE_ORIGINAL##*:}"; PORTA="${PORTA%%/*}"
fi
case "$PORTA" in
  ''|*[!0-9]*)
    # BASE sem porta: se o host não é loopback, o default 7860 aponta para o app
    # do DONO — a suíte então mede o build errado e cria chaves lá. Exige a porta.
    HOST_BASE="${BASE_ORIGINAL#*://}"; HOST_BASE="${HOST_BASE%%/*}"
    case "$HOST_BASE" in
      ''|127.0.0.1|localhost|'[::1]') PORTA=7860 ;;
      *)
        echo "✖ BASE sem porta ($BASE_ORIGINAL): informe a porta da instância de teste," >&2
        echo "  ex.: BASE=http://$HOST_BASE:53203 $0" >&2
        exit 1 ;;
    esac ;;
esac
LANURL=""   # definido depois (no modo próprio a porta só existe mais adiante)

# ── instância própria (padrão) ──────────────────────────────────────────────
# Chaves e settings em /tmp: a rodada não deixa rastro no estado do dono.
SERVIDOR_PID=""
if [ -z "$BASE_ORIGINAL" ]; then
  PORTA="$(command -v python3 >/dev/null && python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
  APK="$(mktemp -t apikeys-adminui.XXXXXX 2>/dev/null || echo "/tmp/apikeys-adminui.$$")"
  SET="$(mktemp -t settings-adminui.XXXXXX 2>/dev/null || echo "/tmp/settings-adminui.$$")"
  rm -f "$APK" "$SET"      # o mktemp cria vazio; o app gera a chave padrão sozinho
  LOG="$(mktemp -t adminui-servidor.XXXXXX 2>/dev/null || echo "/tmp/adminui-servidor.$$")"
  # `.apikey` (legado) também é espelho de `_save_apikeys`: sem isto a rodada
  # reescrevia o arquivo do DONO mesmo com `APIKEYS_PATH` em /tmp (#535).
  LEG="$(mktemp -t apikey-adminui.XXXXXX 2>/dev/null || echo "/tmp/apikey-adminui.$$")"
  # VOZ PRÓPRIA (#786): sem `voices/` a lista nasce vazia e a checagem da troca
  # de chave morre em "as listas nem renderizaram". Worktree não tem `voices/` (não
  # é versionado), então a instância da rodada ganha a fixture — nada do dono.
  VOZES="$(mktemp -d -t voices-adminui.XXXXXX 2>/dev/null || echo "/tmp/voices-adminui.$$")"
  mkdir -p "$VOZES"
  cp "$(dirname "${BASH_SOURCE[0]}")/fixtures/voz_fala_5s_16k.wav" "$VOZES/fala.wav"
  printf '{"id":"fala","name":"fala","duration":5.0,"created_at":"2026-01-01 00:00:00"}' \
    > "$VOZES/fala.json"
  # ENV **ANTES** do import (#591): `import app` já roda `_save_settings()`
  # (materializa chave default) e `_load_apikeys()`. Patchear o ATRIBUTO depois do
  # import deixava essas duas escritas no arquivo do DONO — o caminho tem de estar
  # no ambiente quando o módulo resolve `TTS_*_PATH` (app.py:188-191).
  TTS_SETTINGS_PATH="$SET" TTS_APIKEYS_PATH="$APK" TTS_LEGACY_APIKEY_PATH="$LEG" \
  TTS_VOICES_DIR="$VOZES" \
  "$PY" - "$PORTA" > "$LOG" 2>&1 <<'PY' &
import sys, uvicorn, app
app._load_apikeys()
uvicorn.run(app.app, host='0.0.0.0', port=int(sys.argv[1]), log_level='warning')
PY
  SERVIDOR_PID=$!
  BASE="http://127.0.0.1:$PORTA"
  limpa_servidor() {
    [ -n "$SERVIDOR_PID" ] && kill "$SERVIDOR_PID" 2>/dev/null || true
    rm -rf "$APK" "$SET" "$LEG" "$VOZES"
  }
  # o trap ÚNICO entra mais abaixo (`trap encerra`): dois traps se sobrescrevem
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    curl -sf -m 2 -o /dev/null "$BASE/" && break
    sleep 0.5
  done
fi

if [ "${BASE_ORIGINAL##*:}" = "7860" ] && [ "${ADMIN_UI_NO_DONO:-}" != "1" ]; then
  echo "✖ BASE aponta para o :7860 (app do dono): a suíte cria chaves no .apikeys.json dele." >&2
  echo "  Sem BASE ela sobe instância própria; para usar a do dono mesmo assim:" >&2
  echo "  ADMIN_UI_NO_DONO=1 BASE=$BASE_ORIGINAL $0" >&2
  exit 1
fi

# Instância que NÃO é loopback exige chave em todo request (loopback é admin por
# definição). Sem um cabeçalho nos `curl` daqui, o `cria_chave` toma 401 e o
# `json.load` estoura com JSONDecodeError — sem dizer que era auth.
# A chave NUNCA é impressa: só o comprimento/máscara.
CHAVE_TESTE="${TESTE_API_KEY:-${TTS_ROD_ADMIN_KEY:-}}"
CABECALHO_CHAVE=()
[ -n "$CHAVE_TESTE" ] && CABECALHO_CHAVE=(-H "X-API-Key: $CHAVE_TESTE")

if ! curl -sf -m 5 -o /dev/null "$BASE/"; then
  echo "✖ app não respondeu em $BASE — rode ./run.sh primeiro" >&2
  exit 1
fi

# Origem não-loopback exige credencial em todo request. Sem a chave, o 401 do
# `cria_chave` virava JSONDecodeError três linhas depois — sem dizer o motivo.
if [ -n "$BASE_ORIGINAL" ]; then
  HOST_ALVO="${BASE#*://}"; HOST_ALVO="${HOST_ALVO%%/*}"; HOST_ALVO="${HOST_ALVO%%:*}"
  case "$HOST_ALVO" in
    127.0.0.1|localhost|'[::1]') ;;
    *)
      if [ -z "$CHAVE_TESTE" ]; then
        echo "✖ BASE de LAN precisa de chave: exporte TESTE_API_KEY=<chave admin> (ou TTS_ROD_ADMIN_KEY)" >&2
        echo "  a chave vai só no cabeçalho X-API-Key e não é impressa" >&2
        exit 1
      fi ;;
  esac
fi
LANURL="http://$LAN:$PORTA"


# ids de chaves de teste por prefixo de nome (pega sujeira de rodada que falhou)
SUF="$$"                       # sufixo por execução (ver comentário no topo)
if [ "${1:-}" = "--limpar-velhos" ]; then LIMPAR_VELHOS=1; fi
# $1 = "sufixo" (padrão): só as MINHAS chaves — não apaga as de uma rodada
# paralela. $1 = "prefixo": varre tudo (limpa resto de rodada que morreu).
ids_de_teste() {
  # -c (não heredoc!): com heredoc o stdin vira o PROGRAMA e o json.load da
  # saída do curl falha em silêncio — foi assim que a limpeza parou de limpar.
  curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} "$BASE/api/apikeys" 2>/dev/null | "$PY" -c '
import sys, json
suf, modo = sys.argv[1], sys.argv[2]
try:
    dados = json.load(sys.stdin)
except Exception:
    dados = {}
for k in dados.get("keys", []):
    nome = k.get("name", "")
    if modo == "prefixo":
        bate = nome.startswith(("teste-ui-", "probe-papel", "probe-adota"))
    elif modo == "velhos":
        import time as _t
        velho = False
        try:
            _t.strptime(k.get("created_at") or "", "%Y-%m-%d %H:%M:%S")
            velho = (_t.time() - _t.mktime(_t.strptime(k.get("created_at"), "%Y-%m-%d %H:%M:%S"))) > 120
        except Exception:
            velho = True
        bate = (nome.startswith(("teste-ui-", "probe-papel", "probe-adota"))
                and not nome.endswith("-" + suf) and velho)
    else:
        bate = nome.endswith("-" + suf)
    if bate:
        print(k["id"])
' "$SUF" "${1:-sufixo}" || true
}

cria_chave() {  # $1 = nome, $2 = role -> "id:secret"
  curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} -X POST -H "Content-Type: application/json" \
    -d "{\"name\":\"$1\",\"role\":\"$2\"}" "$BASE/api/apikeys" \
    | "$PY" -c "import sys,json;k=json.load(sys.stdin)['key'];print(k['id']+':'+k['secret'])"
}

# limpa resto de rodada morta (nome de outro sufixo E antigo) — rodada viva tem
# segundos de idade, então não é tocada
for id in $(ids_de_teste velhos); do curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} -X DELETE "$BASE/api/apikeys/$id" >/dev/null 2>&1 || true; done
USE="$(cria_chave "teste-ui-use-$SUF" use)"
USE_ID="${USE%%:*}"; USE_KEY="${USE#*:}"; CRIADAS+=("$USE_ID")
ADM="$(cria_chave "teste-ui-admin-$SUF" admin)"
ADM_ID="${ADM%%:*}"; ADM_KEY="${ADM#*:}"; CRIADAS+=("$ADM_ID")
limpar() {
  local id
  for id in ${CRIADAS[@]+"${CRIADAS[@]}"}; do
    curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} -X DELETE "$BASE/api/apikeys/$id" >/dev/null 2>&1 || true
  done
  for id in $(ids_de_teste); do curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} -X DELETE "$BASE/api/apikeys/$id" >/dev/null 2>&1 || true; done
  return 0
}
# Ids criados por ESTA rodada, em lista: a limpeza não depende de as variáveis
# USE_ID/ADM_ID já estarem atribuídas, e apaga só o que é nosso.
CRIADAS=()

# ORDEM do encerramento: chaves ANTES do servidor — depois de matá-lo não há API e
# nada mais é apagado. Tudo com `|| true`: sob `set -e` um comando de trap que
# falha aborta o resto do trap (mesma classe do kill do cfg_sujo).
encerra() {
  limpar
  # `limpa_servidor` derruba o servidor E apaga as chaves/settings/vozes da rodada
  # — tem de valer na rodada VERMELHA também, senão sobra `/tmp/voices-adminui.*`
  # a cada falha. Só existe no modo de instância própria.
  if declare -F limpa_servidor >/dev/null; then
    limpa_servidor || true
  fi
  [ -n "${SERVIDOR_PID:-}" ] && { kill "$SERVIDOR_PID" 2>/dev/null || true; }
  [ -n "${APK:-}" ] && rm -f "$APK" "$SET"
  # Este trap SOBRESCREVE o do `serial.sh`: sem estas duas o vigia do teto
  # sobrevive ao shell (e manda ALRM num PID que pode ter sido reusado) e a trava
  # de modelo fica presa até alguém notar o pid morto (#592).
  serial_solta
  _teto_matar
  return 0
}
# EXIT cobre saída normal E falha (`set -e`); INT/TERM cobrem Ctrl-C e o harness
# matando a rodada. `encerra` é idempotente. SIGKILL não roda trap nenhum: para
# esse caso existe a varredura de "velhos" lá em cima (outro sufixo E >2 min).
trap encerra EXIT INT TERM
echo "chaves de teste: use=${USE_KEY:0:8}… admin=${ADM_KEY:0:8}…"

# ── 1. contrato no servidor ───────────────────────────────────────────────
echo
echo "→ contrato (chave de uso fala com $LANURL)"
"$PY" - "$LANURL" "$USE_KEY" <<'PYEOF'
import json, sys, urllib.request
base, use = sys.argv[1], sys.argv[2]

def req(metodo, caminho, chave, corpo=None):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    r = urllib.request.Request(base + caminho, data=dados, method=metodo,
                              headers={"X-API-Key": chave, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r))

s = req("GET", "/api/settings", use)
assert s["is_admin"] is False, f"chave de uso não devia ser admin: {s['is_admin']}"
assert "admin_fields" in s and s["admin_fields"], "servidor não mandou admin_fields"
mascarados = [k for k in ("remote_api_key", "remote_stt_key", "chat_api_key")
              if str(s.get(k, "")).startswith("••••")]
print(f"  is_admin=False · {len(s['admin_fields'])} campos admin · segredos mascarados: {mascarados or '(vazios)'}")

# O payload leva um campo NÃO-admin junto de propósito: `admin_ignored` só prova
# que o admin ficou de fora, e sozinho ele não distingue "ignorou o admin" de
# "ignorou tudo". O não-admin tem de APLICAR.
orig_speed = float(s.get("speed") or 1.0)
alvo = 1.25 if abs(orig_speed - 1.25) > 1e-9 else 1.5
r = req("POST", "/api/settings", use, {"remote_base_url": "http://ignorado.teste/v1", "speed": alvo})
assert "remote_base_url" in r.get("admin_ignored", []), r.get("admin_ignored")
assert abs(float(r.get("speed") or 0) - alvo) < 1e-9, f"campo não-admin não foi aplicado: {r.get('speed')!r}"
print(f"  POST com campo admin -> admin_ignored={r['admin_ignored']} (sem 403)"
      f" · não-admin aplicou: speed {orig_speed} -> {alvo}")
# …e devolve: o settings é o DO DONO e não é versionado. Sem isto a suíte deixava
# `speed` no valor de teste em toda rodada (1.77 do dono virava 1.0).
volta = req("POST", "/api/settings", use, {"speed": orig_speed})
assert abs(float(volta.get("speed") or 0) - orig_speed) < 1e-9, \
    f"speed não voltou ao original: {volta.get('speed')!r} != {orig_speed!r}"
PYEOF

# ── 2. UI: chave de uso e chave admin ────────────────────────────────────
echo
echo "→ UI em $LANURL"
"$PY" - "$LANURL" "$USE_KEY" "$ADM_KEY" "$SUF" <<'PYEOF'
import json, os, sys, time, urllib.request
from playwright.sync_api import sync_playwright

SUF = sys.argv[4]                     # mesmo sufixo do shell ($SUF), não o pid do python:
NOME = f"probe-papel-{SUF}"            # senão o trap não reconhece as chaves como suas
NOME2 = f"probe-adota-{SUF}"
base, use, adm = sys.argv[1], sys.argv[2], sys.argv[3]
falhas = []

def cobrar(cond, msg):
    if not cond:
        falhas.append(msg)

def ir(pg, url, **kw):
    """`goto` com retry curto: se o app reiniciar no meio da rodada, o goto pode
    devolver ERR_CONNECTION_REFUSED e derrubar tudo com falso vermelho.
    3 tentativas com espera crescente (~6 s no total) porque um `run.sh`
    reiniciando fica fora por alguns segundos — 1 retry de 1,5 s não cobria."""
    for tentativa, espera in ((1, 2.0), (2, 4.0), (3, None)):
        try:
            return pg.goto(url, **kw)
        except Exception:
            if espera is None:
                raise
            time.sleep(espera)

with sync_playwright() as p:
    b = p.chromium.launch()

    def abrir(chave, adotar_nova=False):
        ctx = b.new_context()
        ctx.add_init_script(f"localStorage.setItem('ttsStudioKey', {chave!r})")
        pg = ctx.new_page()
        erros = []
        # stack junto: o sintoma "Cannot set properties of null" não diz ONDE
        pg.on("pageerror", lambda e: erros.append(
            str(e) + ("\n" + e.stack.splitlines()[1] if getattr(e, "stack", None) else "")))

        def decide(d):
            # "Usar esta chave neste navegador?" decide se a SESSÃO passa a ser a
            # chave nova (e com ela o privilégio). Nos testes de papel a gente
            # recusa, para seguir admin; no teste de adoção, aceita.
            if "Usar esta chave" in d.message and not adotar_nova:
                d.dismiss()
            else:
                d.accept()
        pg.on("dialog", decide)
        pg._console = []          # as fases leem daqui (não muda a assinatura)
        pg.on("console", lambda m: pg._console.append(m.text))
        # A tela pode nascer SEM ESTADO se o servidor piscar no meio (outro agente
        # reiniciando): aí loadSettings() aborta, o selo fica vazio, nenhum campo
        # trava e nenhuma linha de chave aparece — sintoma que parece regressão e
        # não é. Espera o ESTADO e recarrega uma vez em vez de seguir vazio.
        # O `Cannot set properties of null (setting 'onclick')` de 1 em 4 rodadas
        # na LAN só faz sentido com o documento INCOMPLETO (o script roda no fim do
        # body; se a resposta chega truncada, um `$("id")` posterior é null e o
        # boot inteiro morre). Exige o parse FECHADO antes de qualquer fase.
        for tentativa in (1, 2):
            ir(pg, base, wait_until="networkidle")
            try:
                pg.wait_for_function(
                    "() => document.readyState === 'complete'"
                    " && !!document.getElementById('shutdownBtn')"
                    " && (document.getElementById('cfgAdminBadge')?.textContent || '').length > 0",
                    timeout=9000)
                break
            except Exception:
                if tentativa == 2:
                    break
                pg.wait_for_timeout(1500)     # dá tempo do servidor voltar antes de recarregar
        pg.wait_for_timeout(300)
        return ctx, pg, erros

    # --- chave de uso ---
    ctx, pg, erros = abrir(use)
    selo = pg.inner_text("#cfgAdminBadge")
    travados = pg.eval_on_selector_all("[data-admin-setting]:disabled", "els => els.map(e => e.id)")
    cfg_speed_travado = pg.eval_on_selector("#cfgSpeed", "el => el.disabled")
    segredo = pg.input_value("#chatApiKey")
    ph = pg.get_attribute("#chatApiKey", "placeholder") or ""
    criar = pg.eval_on_selector("#apiKeyCreate", "el => el.disabled")
    print(f"  selo: {selo!r} · travados: {len(travados)} · cfgSpeed travado: {cfg_speed_travado}")
    cobrar("uso" in selo.lower(), f"selo deveria dizer chave de uso, veio {selo!r}")
    cobrar(len(travados) >= 10, f"poucos campos travados: {travados}")
    cobrar(not cfg_speed_travado, "cfgSpeed (não-admin) não devia estar travado")

    # #791: os três campos de `use` que mexem no que a IA responde/apaga. Os dois
    # de prompt ficam abertos (é o propósito), mas AVISAM o alcance; o TTL apaga
    # histórico, então é admin — quando o servidor o proteger, tem de vir travado.
    avisos = pg.eval_on_selector_all(
        ".cfg-alcance", "els => els.map(e => e.textContent.replace(/\\s+/g, ' ').trim())")
    print(f"  avisos de alcance: {len(avisos)}")
    cobrar(len(avisos) >= 2, f"faltou aviso de alcance nos campos de uso ({avisos})")
    cobrar(any("uso" in a and "admin" in a for a in avisos),
           f"o aviso não diz que é campo de uso nem que contorna o admin ({avisos})")
    # #786/#791: os campos de identidade também são admin — a tela não pode
    # oferecer controle que o servidor dropa em silêncio
    ident = pg.eval_on_selector_all(
        "#speakerGate, #spThr, #spFarAlvo, #spAntiClone",
        "els => els.map(e => [e.id, e.dataset.adminSetting, e.disabled])")
    print(f"  campos de identidade: {ident}")
    protegidos = set(pg.evaluate("() => [...ADMIN.fields]") or [])
    for cid, campo, desab in ident:
        cobrar(bool(campo), f"#{cid} sem data-admin-setting (seria dropado em silêncio)")
        if not campo or campo not in protegidos:
            continue          # servidor ainda não lista o campo (#786 sem merge)
        # o anti-clone nasce inerte de propósito ("em breve"): travado nos dois papéis
        cobrar(desab is True, f"#{cid} devia estar travado com chave de uso")
        if cid != "spAntiClone":
            titulo = pg.eval_on_selector(f"#{cid}", "el => el.title")
            cobrar("administrativa" in (titulo or "").lower(),
                   f"#{cid} travado sem dizer o motivo ({titulo!r})")
    marcado = pg.eval_on_selector("#metricsTtl", "el => el.dataset.adminSetting")
    cobrar(marcado == "metrics_ttl_dias",
           f"o TTL do histórico não está marcado como admin ({marcado!r})")
    if "metrics_ttl_dias" in (pg.evaluate("() => [...ADMIN.fields]") or []):
        cobrar(pg.eval_on_selector("#metricsTtl", "el => el.disabled") is True,
               "o TTL é admin no servidor mas não veio travado")
    else:
        print("  (servidor ainda não lista metrics_ttl_dias em admin_fields)")
    cobrar(criar is True, "Nova chave devia estar desabilitada com chave de uso")
    cobrar(not (segredo.startswith("••••") and "mascarado" not in ph),
           "segredo mascarado sem aviso no placeholder")
    # --- AÇÕES elevadas com chave de uso (task #412) ---
    # O servidor responde 403 `Chave administrativa necessária (papel admin)`.
    # A tela trava ANTES do clique e diz o motivo no title; sem isso a pessoa só
    # descobria depois — e no desligar a tela ainda parecia ter funcionado.
    acoes = ["shutdownBtn", "proxyTunnelOn", "proxyTunnelRestart",
             "unloadModelsBtn", "importVoicesBtn", "exportVoicesBtn", "clearAll"]
    estado_acoes = pg.evaluate("""ids => ids.map(id => {
        const el = document.getElementById(id);
        return el ? { id, desab: el.disabled, titulo: el.title || "" } : { id, falta: true };
    })""", acoes)
    for a in estado_acoes:
        cobrar(not a.get("falta"), f"ação elevada #{a['id']} não existe na tela")
        cobrar(a.get("desab") is True, f"#{a['id']} devia estar desabilitado com chave de uso")
        cobrar("administrativa" in a.get("titulo", "").lower(),
               f"#{a['id']} desabilitado sem dizer o motivo (title: {a.get('titulo')!r})")
    # #508: o teto de áudios é CAMPO admin (cbcd540): travado e explicado como
    # os demais — e preenchido com o valor do servidor, não vazio.
    teto = pg.evaluate("""() => { const el = document.getElementById('autoCleanMax');
        return el ? { desab: el.disabled, titulo: el.title || '', valor: el.value } : null; }""")
    cobrar(teto is not None, "o campo do teto de áudios (#508) não existe no Histórico")
    if teto:
        print(f"  teto de áudios com chave use -> desab={teto['desab']} · valor={teto['valor']!r}")
        cobrar(teto["desab"], "o teto de áudios devia estar travado com chave de uso")
        cobrar("administrativa" in teto["titulo"].lower(),
               f"o teto travado não diz o motivo ({teto['titulo']!r})")
        cobrar(teto["valor"] not in ("", None), "o teto não veio preenchido do servidor")

    # #586: os dois campos que viviam na lista de exceções agora travam de verdade
    campos_novos = pg.evaluate("""() => ["remoteTtsModel", "translateModel"].map(id => {
        const el = document.getElementById(id);
        return el ? { id, desab: el.disabled, titulo: el.title || "" } : { id, falta: true };
    })""")
    for c in campos_novos:
        cobrar(not c.get("falta"), f"o campo #{c['id']} não existe na tela")
        cobrar(c.get("desab") is True, f"#{c['id']} devia estar travado com chave de uso")
        cobrar("administrativa" in c.get("titulo", "").lower(),
               f"#{c['id']} travado sem dizer o motivo ({c.get('titulo')!r})")

    # o 🗑 do perfil de locutor é ação elevada (DELETE leva _exige_admin): com
    # chave de uso tem de estar travado. A instância de teste nasce sem perfis,
    # então a lista é servida por rota stubada — o alvo é a régua do botão.
    pg.route("**/api/speaker/profiles",
             lambda r: r.fulfill(status=200, content_type="application/json",
                                 body='[{"name":"probe","samples":2}]'))
    pg.evaluate("refreshSpProfiles()")
    pg.wait_for_timeout(500)
    perfil_del = pg.evaluate("""() => { const b = document.querySelector('#spProfiles [data-del]');
        return b ? { desab: b.disabled, titulo: b.title || '' } : null; }""")
    cobrar(perfil_del is not None, "a lista de perfis não renderizou o botão de apagar")
    if perfil_del:
        cobrar(perfil_del["desab"], "o 🗑 do perfil devia estar desabilitado com chave de uso")
        cobrar("administrativa" in perfil_del["titulo"].lower(), "o 🗑 do perfil não diz o motivo")
    pg.unroute("**/api/speaker/profiles")


    # e o 403 do servidor continua explicado no cliente quando ele chega.
    # Alvo: "Apagar todos" — responde 403 e não apaga nada; o Desligar derrubaria
    # a instância no meio da rodada. `confirm` stubado para não parar o fluxo.
    pg.evaluate("""() => {
        const orig = window.confirm; window.confirm = () => true;
        const b = document.getElementById('clearAll');
        b.disabled = false; b.click();
        setTimeout(() => { window.confirm = orig; }, 100);
    }""")
    pg.wait_for_timeout(900)
    aviso = pg.text_content("#keyBarText") or ""
    cobrar("administrativa" in aviso.lower(),
           f"o 403 do servidor não foi explicado na faixa ({aviso[:80]!r})")
    pg.evaluate("document.getElementById('keyBar').style.display = 'none'")  # não contaminar a fase seguinte

    # --- #511: o Desligar (forçado) com chave de uso NÃO pode fingir sucesso ---
    # Antes: o `catch {}` engolia o 403, o corpo era trocado por "Servidor
    # desligado" (apagando a própria faixa do 403) e o servidor seguia vivo.
    pg.evaluate("""() => {
        const orig = window.confirm; window.confirm = () => true;
        const b = document.getElementById('shutdownBtn');
        b.disabled = false; b.click();
        setTimeout(() => { window.confirm = orig; }, 100);
    }""")
    pg.wait_for_timeout(1200)
    corpo_off = pg.evaluate("() => document.body.innerText")
    aviso_off = pg.text_content("#keyBarText") or ""
    falha_off = pg.text_content("#shutdownAviso") or ""
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as rr:
            saude = rr.status
    except Exception:
        saude = None
    print(f"  desligar com chave use -> faixa={aviso_off[:40]!r} · aviso={falha_off[:40]!r}"
          f" · /health={saude} · corpo diz desligado={'Servidor desligado' in corpo_off}")
    cobrar("administrativa" in aviso_off.lower(),
           f"o 403 do desligar não ficou explicado na faixa ({aviso_off[:80]!r})")
    cobrar("Servidor desligado" not in corpo_off,
           "a tela disse 'Servidor desligado' com o servidor vivo (#511)")
    cobrar("não desligou" in falha_off.lower(),
           f"a tela não disse que NÃO desligou ({falha_off[:80]!r})")
    cobrar(saude == 200, f"o /health devia seguir 200 depois da recusa (veio {saude})")
    pg.evaluate("document.getElementById('keyBar').style.display = 'none'")
    pg.evaluate("() => travarAcoesElevadas()")   # volta o estado travado da chave de uso
    # --- CSP pelo header (task #36): sem <meta> e com a policy APLICADA ---
    # Reintroduzir um <meta> traz de volta o erro de frame-ancestors (ignorado em
    # meta) e a aplicação tardia; e com o meta fora, se o header cair a policy
    # desaparece em silêncio (os bundles de CDN carregam do mesmo jeito).
    metas = pg.eval_on_selector_all('meta[http-equiv="Content-Security-Policy"]', "e => e.length")
    erro_frame = [t for t in pg._console if "frame-ancestors" in t]
    cobrar(metas == 0, f"a CSP não deve voltar para <meta> (achou {metas})")
    cobrar(not erro_frame, f"console com erro de frame-ancestors: {erro_frame[:1]}")

    antes = len([t for t in pg._console if "Content Security Policy" in t])
    pg.evaluate("""() => new Promise(r => {
        const i = new Image(); i.onerror = i.onload = () => r();
        i.src = "https://example.com/fora-da-allowlist.png";
        document.body.appendChild(i); setTimeout(r, 1500);
    })""")
    depois = len([t for t in pg._console if "Content Security Policy" in t])
    print(f"  CSP: {metas} meta(s) · erro frame-ancestors: {bool(erro_frame)} · violação forçada acusada: {depois > antes}")
    cobrar(depois > antes, "sem violação acusada, a policy pode ter sumido junto com o meta")

    # save com chave de uso: tem que NOMEAR o que ficou de fora (item 3 do spec)
    pg.eval_on_selector(".nav-item[data-view='config']", "el => el.click()")
    pg.eval_on_selector("#cfgSave", "el => el.click()")
    pg.wait_for_timeout(1500)
    status = pg.inner_text("#cfgStatus")
    toast = pg.inner_text("#toast")
    keybar = pg.eval_on_selector("#keyBar", "el => getComputedStyle(el).display")
    print(f"  salvar com chave de uso -> {status!r}")
    cobrar("precisam de chave admin" in status, f"status não avisou o que ficou de fora: {status!r}")
    cobrar("precisam de chave admin" in toast, f"toast sem o aviso: {toast!r}")
    cobrar(keybar == "none", "save normal não devia abrir a faixa de chave")

    # #799: o aviso de campo ignorado NÃO pode ser apagado pelo sucesso — o toast é
    # um só, então quem escreve por último manda. O TTL é campo ADMIN (o `api()`
    # avisa e o handler não pode escrever "salvo" por cima); `chat_system` é de
    # USO e aplica, então ali o sucesso é a resposta certa.
    pg.evaluate("() => { document.getElementById('toast').textContent = ''; }")
    pg.eval_on_selector("#metricsTtl",
                        "el => { el.value = 7; el.dispatchEvent(new Event('change')); }")
    pg.wait_for_timeout(1200)
    t = pg.inner_text("#toast")
    print(f"  toast do TTL (admin) com chave de uso: {t[:60]!r}")
    cobrar("precisam de chave admin" in t, f"o aviso do TTL foi sobrescrito pelo sucesso: {t!r}")
    cobrar("Retenção do histórico" not in t, f"o TTL disse 'salvo' com o campo dropado: {t!r}")

    pg.evaluate("() => { document.getElementById('toast').textContent = ''; }")
    pg.eval_on_selector("#cvSystemSave", "el => el.click()")
    pg.wait_for_timeout(1200)
    t = pg.inner_text("#toast")
    print(f"  toast das instruções (uso) com chave de uso: {t[:60]!r}")
    cobrar("Instruções salvas" in t, f"o `chat_system` é de uso e devia aplicar: {t!r}")

    # Os dois handlers de instruções mandam campo de USO, então o servidor real
    # nunca devolve `admin_ignored` para eles — a fiação do guard fica sem prova.
    # Aqui o POST é stubbado devolvendo o ignorado: o aviso (escrito pelo `api()`)
    # NÃO pode ser apagado pelo sucesso. O helper em si é provado pelo TTL acima,
    # com o servidor de verdade.
    def com_ignorado(seletor, rotulo, confirma):
        pg.route("**/api/settings", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "is_admin": False,
                             "admin_ignored": ["chat_system"]})))
        pg.evaluate("() => { document.getElementById('toast').textContent = ''; }")
        pg.eval_on_selector(seletor, "el => el.click()")
        pg.wait_for_timeout(1200)
        t = pg.inner_text("#toast")
        pg.unroute("**/api/settings")
        print(f"  toast do {rotulo} (admin_ignored forçado): {t[:60]!r}")
        cobrar("precisam de chave admin" in t,
               f"o aviso do {rotulo} foi sobrescrito pelo sucesso: {t!r}")
        cobrar(confirma not in t, f"o {rotulo} confirmou com o campo dropado: {t!r}")

    com_ignorado("#cvSystemSave", "salvar instruções", "Instruções salvas")
    com_ignorado("#cvSystemReset", "restaurar padrão", "restauradas")

    # ação administrativa -> 403 com instrução
    r = pg.evaluate("""async () => {
        try { await api('/api/apikeys', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); return {}; }
        catch (e) { return {msg: e.message, status: e.status,
                            bar: document.getElementById('keyBar').style.display,
                            txt: document.getElementById('keyBarText').textContent}; }
    }""")
    print(f"  403 -> status={r.get('status')} bar={r.get('bar')}")
    cobrar(r.get("status") == 403, f"esperava 403, veio {r}")
    cobrar(r.get("bar") == "flex", "403 não abriu a faixa de chave")
    cobrar("administrativa" in (r.get("txt") or ""), "403 sem instrução útil")
    cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
    ctx.close()

    # --- chave admin: contraprova + papel da chave pela UI ---
    ctx, pg, erros = abrir(adm)
    selo = pg.inner_text("#cfgAdminBadge")
    # `[data-sempre-travado]` (anti-clone "em breve") fica desabilitado nos DOIS
    # papéis por desenho: não conta como trava de admin
    travados = pg.eval_on_selector_all(
        "[data-admin-setting]:not([data-sempre-travado]):disabled", "els => els.length")
    segredo = pg.input_value("#chatApiKey")
    print(f"  contraprova admin: selo={selo!r} travados={travados} segredo_revelado={not segredo.startswith('••••')}")
    cobrar("admin" in selo.lower(), f"selo de admin errado: {selo!r}")
    cobrar(travados == 0, f"admin não devia ter campo travado ({travados})")
    # …e o `data-sempre-travado` (anti-clone "em breve") tem de segurar o
    # `disabled` também na fase ADMIN: sem o atributo o JS destrava o controle e a
    # pessoa liga algo que ainda não existe (#791)
    anti = pg.eval_on_selector("#spAntiClone", "el => el.disabled")
    print(f"  anti-clone com chave admin: disabled={anti}")
    cobrar(anti is True, "o anti-clone ('em breve') foi destravado pela chave admin")
    cobrar(not segredo.startswith("••••"), "admin deveria ver o segredo, não a máscara")
    cobrar(not pg.evaluate("() => document.getElementById('autoCleanMax').disabled"),
           "com chave admin o teto de áudios devia estar destravado")
    # `0` é valor (sem teto), não "campo vazio": o salvamento tem de mandar zero
    pg.evaluate("""() => { const c = document.getElementById('autoCleanMax');
                            c.value = '0'; c.onchange(); }""")
    pg.wait_for_timeout(900)
    with urllib.request.urlopen(urllib.request.Request(
            base + "/api/settings", headers={"X-API-Key": adm}), timeout=5) as rr:
        teto_srv = json.load(rr).get("auto_cleanup_max_outputs")
    print(f"  teto zerado pelo admin -> servidor: {teto_srv!r}")
    cobrar(teto_srv == 0, f"o 0 do teto não chegou ao servidor (veio {teto_srv!r})")
    pg.evaluate("""() => { const c = document.getElementById('autoCleanMax');
                            c.value = '500'; c.onchange(); }""")
    pg.wait_for_timeout(700)

    # trocar a chave na mão: o papel NOVO tem de chegar antes das listas
    # dinâmicas. Sem isso os botões são renderizados com o papel velho (admin) e
    # o clique vai a 403. O atraso no /api/settings abre a janela de propósito.
    # marca os itens que JÁ estão na tela: o que interessa é o RE-RENDER
    pg.evaluate("() => document.querySelectorAll('#voiceList .item').forEach(e => e.dataset.velho = '1')")
    pg.evaluate("""async (chave) => {
        const orig = window.api;
        window.api = (caminho, opts) => caminho.includes('/api/settings')
            ? new Promise(res => setTimeout(res, 600)).then(() => orig(caminho, opts))
            : orig(caminho, opts);
        window.__restauraApi = () => { window.api = orig; };
        window.__prontoTroca = trocarChave(chave);
    }""", use)
    # espera o botão APARECER e lê na hora: é a PRIMEIRA renderização que vale.
    # Renderizado antes do /api/settings (atrasado de propósito), ele nasce com o
    # papel velho; depois do conserto ele só nasce quando o papel novo já chegou.
    try:
        pg.wait_for_function("() => !!document.querySelector('#voiceList .item:not([data-velho])')",
                             timeout=6000)
        apagar_voz = pg.evaluate(
            "document.querySelector('#voiceList .item:not([data-velho]) .danger').disabled")
        cobrar(apagar_voz is True,
               "o apagar voz nasceu com o papel VELHO depois da troca de chave (render antes do is_admin)")
        # o aceite é TODOS os elevados, não só os de render
        cobrar(pg.evaluate("document.getElementById('shutdownBtn').disabled") is True,
               "o desligar (ação estática) não travou com a chave de uso")
    except Exception:
        cobrar(False, "as listas nem renderizaram depois da troca de chave")
    pg.evaluate("window.__restauraApi && window.__restauraApi()")
    # a fase seguinte cria chaves, e com a chave de USO isso não dá: volta ao admin
    pg.evaluate("async (chave) => { await trocarChave(chave); }", adm)
    pg.wait_for_timeout(1200)
    cobrar(pg.evaluate("document.querySelector('#apiKeyCreate')?.disabled") is False,
           "a sessão não voltou a ser admin depois do ensaio de troca")
    # o inverso tem de DESTRAVAR os elevados também
    cobrar(pg.evaluate("document.getElementById('shutdownBtn').disabled") is False,
           "voltar para admin não destravou o desligar")

    # papel: cria pelo form (default "uso"), confere o selo da linha, troca e apaga
    pg.eval_on_selector(".nav-item[data-view='config']", "el => el.click()")
    pg.eval_on_selector(".settings-tabs .stab[data-stab='acesso']", "el => el.click()")
    pg.wait_for_timeout(400)
    cobrar(pg.eval_on_selector("#apiKeyNewRole", "el => el.value") == "use",
           "o form de nova chave deveria nascer em papel 'uso'")
    # Se OUTRO agente reiniciar o servidor no meio, o POST de criar pode falhar e
    # a linha nunca aparece — o sintoma é um timeout de 30 s que parece regressão.
    # Tenta de novo UMA vez, distinguindo "o POST falhou" de "o render atrasou".
    def cria_e_acha(ms):
        pg.fill("#apiKeyNewName", NOME)
        pg.select_option("#apiKeyNewRole", "use")
        pg.click("#apiKeyCreate")
        try:
            pg.wait_for_selector(f".api-key-row:has-text('{NOME}')", timeout=ms)
            return True
        except Exception:
            return False

    if not cria_e_acha(8000):
        pg.click("#apiKeysRefresh")          # o POST pode ter passado e só o render atrasado
        try:
            pg.wait_for_selector(f".api-key-row:has-text('{NOME}')", timeout=6000)
        except Exception:
            if not cria_e_acha(8000):        # aí sim o POST falhou: cria de verdade
                raise
    linha = pg.locator(".api-key-row", has_text=NOME).last
    chip = linha.locator(".ak-role").inner_text()
    print(f"  chave criada pela UI com papel -> selo da linha: {chip!r}")
    cobrar("uso" in chip, f"linha deveria mostrar papel 'uso', veio {chip!r}")

    linha.locator("button", has_text="virar admin").click()
    pg.wait_for_timeout(900)
    chip = pg.locator(".api-key-row", has_text=NOME).locator(".ak-role").inner_text()
    print(f"  depois de trocar o papel -> selo da linha: {chip!r}")
    cobrar("admin" in chip, f"PATCH do papel não refletiu na linha: {chip!r}")

    pg.locator(".api-key-row", has_text=NOME).locator("button", has_text="Apagar").click()
    pg.wait_for_timeout(900)
    cobrar(pg.locator(".api-key-row", has_text=NOME).count() == 0,
           "a chave de teste não foi apagada pela UI")
    cobrar(not erros, "erros de JS (admin): " + "; ".join(erros[:3]))

    # --- resguardo: servidor protegendo campo SEM marcador no HTML ---
    # #586: agora é REPROVAÇÃO, não aviso. Os dois que viviam na lista de exceções
    # (`translate_model`, `remote_tts_model`) ganharam input com marcador; qualquer
    # campo admin novo sem `data-admin-setting`/`data-setting` (e sem estar em
    # `ADMIN_SEM_UI`) derruba a suíte em vez de virar aviso eterno.
    avisos = []
    pg.on("console", lambda m: avisos.append(m.text) if m.type == "warning" else None)
    pg.reload()
    pg.wait_for_selector("#cfgAdminBadge", state="attached", timeout=15000)
    pg.wait_for_timeout(400)
    do_load = [a for a in avisos if "[admin]" in a]
    print(f"  aviso no load normal: {do_load or '(nenhum, correto)'}")
    cobrar(not do_load,
           f"todo campo admin precisa de marcador (ou de exceção em ADMIN_SEM_UI): {do_load}")

    sim = pg.evaluate("""() => {
        const vistos = [], orig = console.warn;
        console.warn = (...a) => vistos.push(a.join(" "));
        try {
            ADMIN.fields = new Set([...ADMIN.fields, "campo_novo_sem_marcador"]);
            aplicarPoliticaAdmin();                       // deve avisar 1×
            const comNovo = vistos.length;
            ADMIN.fields = new Set(["translate_model", "remote_tts_model"]);
            aplicarPoliticaAdmin();     // os dois do #586: agora TÊM marcador, sem aviso
            const comSemUi = vistos.length;
            // a exceção explícita continua valendo (campo que só existe por API)
            ADMIN_SEM_UI.add("campo_so_api");
            ADMIN.fields = new Set(["campo_so_api"]);
            aplicarPoliticaAdmin();
            const comExcecao = vistos.length;
            ADMIN_SEM_UI.delete("campo_so_api");
            // controle negativo: controle dinâmico TEM data-setting -> não pode acusar
            const dinamicos = [...new Set([...document.querySelectorAll("[data-setting]")]
                .map(e => e.dataset.setting))];
            ADMIN.fields = new Set(dinamicos.slice(0, 5));
            aplicarPoliticaAdmin();
            return { comNovo, comSemUi, comExcecao, dinamicos: dinamicos.length,
                     total: vistos.length, vistos: vistos.join(" | ") };
        } finally { console.warn = orig; }
    }""")
    print(f"  campo protegido sem marcador -> {sim['comNovo']} aviso(s): {(sim['vistos'] or '')[:60]}")
    cobrar(sim["comNovo"] == 1, f"o resguardo deveria avisar o campo sem marcador: {sim}")
    cobrar("campo_novo_sem_marcador" in sim["vistos"], f"o aviso precisa nomear o campo: {sim['vistos']}")
    cobrar(sim["comSemUi"] == 1,
           f"campo com marcador (`translate_model`/`remote_tts_model`) não pode avisar: {sim['vistos']}")
    cobrar(sim["comExcecao"] == sim["comSemUi"],
           f"a exceção explícita (ADMIN_SEM_UI) precisa calar o aviso: {sim['vistos']}")
    if sim["dinamicos"]:
        cobrar(sim["total"] == sim["comSemUi"],
               f"controle dinâmico (com data-setting) não pode acusar: {sim['vistos']}")
        print(f"  controle negativo: {sim['dinamicos']} controles com data-setting, nenhum acusado")
    else:
        print("  (sem controle dinâmico no DOM — checagem negativa pulada)")
    ctx.close()

    # --- adotar a chave nova (aceitar "usar esta chave") muda o PRIVILÉGIO da
    #     sessão: selo e botões têm que acompanhar na hora, não ficar mentindo ---
    ctx, pg, erros = abrir(adm, adotar_nova=True)
    pg.eval_on_selector(".nav-item[data-view='config']", "el => el.click()")
    pg.eval_on_selector(".settings-tabs .stab[data-stab='acesso']", "el => el.click()")
    pg.wait_for_timeout(300)
    pg.fill("#apiKeyNewName", NOME2)
    pg.select_option("#apiKeyNewRole", "use")
    pg.click("#apiKeyCreate")
    # trocarChave() -> loadSettings() é assíncrono: esperar o ESTADO, não um tempo
    # fixo (1800 ms falhava de vez em quando sob carga)
    try:
        pg.wait_for_function(
            "() => (document.getElementById('cfgAdminBadge')?.textContent || '')"
            ".toLowerCase().includes('uso')", timeout=8000)
    except Exception:
        pass
    selo = pg.inner_text("#cfgAdminBadge")
    criar_depois = pg.eval_on_selector("#apiKeyCreate", "el => el.disabled")
    print(f"  adotando uma chave de uso -> selo: {selo!r} · criar chave travado: {criar_depois}")
    cobrar("uso" in selo.lower(), f"selo devia virar 'chave de uso' ao adotar a chave nova, veio {selo!r}")
    cobrar(criar_depois is True, "adotando chave de uso, criar chave devia ficar desabilitado")
    cobrar(not erros, "erros de JS (adoção): " + "; ".join(erros[:3]))
    ctx.close()

    # --- blip do servidor no load: a tela tem de se recuperar SOZINHA ---
    # Sem este caso o script não distingue "o app se recuperou pela retentativa" de
    # "o abrir() ficou vazio e recarregou" — a proteção do próprio teste mascara.
    ctx = b.new_context()
    ctx.add_init_script(f"localStorage.setItem('ttsStudioKey', {adm!r})")
    pg = ctx.new_page()
    marco = {"t": None}

    def blip(route):
        if "/api/settings" in route.request.url and route.request.method == "GET":
            if marco["t"] is None:
                marco["t"] = time.time()
            if time.time() - marco["t"] < 0.4:      # 400 ms de indisponibilidade
                return route.fulfill(status=503, headers={"Content-Type": "application/json"},
                                     body='{"detail":"blip simulado"}')
        route.continue_()

    pg.route("**/api/settings", blip)
    ir(pg, base, wait_until="domcontentloaded")
    try:
        pg.wait_for_function(
            "() => (document.getElementById('cfgAdminBadge')?.textContent || '').length > 0",
            timeout=9000)
    except Exception:
        pass
    selo_blip = pg.inner_text("#cfgAdminBadge")
    print(f"  blip de 400 ms no load -> tela montou: {bool(selo_blip)}")
    if os.environ.get("FORCA_FALHA"):
        # fixture do #474: falha de ASSERÇÃO (não sinal) no fim, para exercitar a
        # limpeza de chaves no trap de um caminho vermelho real
        cobrar(False, "falha forçada (FORCA_FALHA=1)")
    cobrar("admin" in selo_blip.lower(),
           f"com blip no load a tela devia montar sozinha (retentativa), veio {selo_blip!r}")
    ctx.close()
    b.close()

print()
if falhas:
    print("✖ FALHOU")
    for f in falhas:
        print("  ·", f)
    sys.exit(1)
print("✔ OK — chave de uso vê o que não pode, salva nomeando o que ficou de fora, 403 explica o caminho, "
      "admin destrava tudo, o papel se cria/troca/apaga pela UI e adotar uma chave de uso rebaixa a sessão na hora")
PYEOF