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
  "$PY" -c "
import pathlib, sys, uvicorn, app
app.APIKEYS_PATH = pathlib.Path(sys.argv[1])
# `_settings` já veio do arquivo do dono no import; o CAMINHO passa a apontar para
# o tmp, então todo save da rodada grava lá (o arquivo dele não é tocado)
app.SETTINGS_PATH = pathlib.Path(sys.argv[2])
app._load_apikeys()
uvicorn.run(app.app, host='0.0.0.0', port=int(sys.argv[3]), log_level='warning')
" "$APK" "$SET" "$PORTA" > "$LOG" 2>&1 &
  SERVIDOR_PID=$!
  BASE="http://127.0.0.1:$PORTA"
  limpa_servidor() {
    [ -n "$SERVIDOR_PID" ] && kill "$SERVIDOR_PID" 2>/dev/null || true
    rm -f "$APK" "$SET"
  }
  trap 'limpa_servidor' EXIT INT TERM
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
ADM="$(cria_chave "teste-ui-admin-$SUF" admin)"
USE_ID="${USE%%:*}"; USE_KEY="${USE#*:}"
ADM_ID="${ADM%%:*}"; ADM_KEY="${ADM#*:}"
limpar() {
  curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} -X DELETE "$BASE/api/apikeys/$USE_ID" >/dev/null 2>&1 || true
  curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} -X DELETE "$BASE/api/apikeys/$ADM_ID" >/dev/null 2>&1 || true
  for id in $(ids_de_teste); do curl -sf ${CABECALHO_CHAVE[@]+"${CABECALHO_CHAVE[@]}"} -X DELETE "$BASE/api/apikeys/$id" >/dev/null 2>&1 || true; done
}
# EXIT cobre saída normal E falha (`set -e`); INT/TERM cobrem Ctrl-C e o harness
# matando a rodada. `limpar` é idempotente, então rodar duas vezes (INT + EXIT)
# não incomoda. SIGKILL não roda trap nenhum: para esse caso existe a varredura de
# "velhos" lá em cima (nome de outro sufixo E com mais de 2 min) — foi ela que
# limpou as sobras da rodada 29093, criadas por uma execução morta à força.
trap limpar EXIT INT TERM
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
import os, sys, time
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
        pg.on("pageerror", lambda e: erros.append(str(e)))

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
        for tentativa in (1, 2):
            ir(pg, base, wait_until="networkidle")
            try:
                pg.wait_for_function(
                    "() => (document.getElementById('cfgAdminBadge')?.textContent || '').length > 0",
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
    travados = pg.eval_on_selector_all("[data-admin-setting]:disabled", "els => els.length")
    segredo = pg.input_value("#chatApiKey")
    print(f"  contraprova admin: selo={selo!r} travados={travados} segredo_revelado={not segredo.startswith('••••')}")
    cobrar("admin" in selo.lower(), f"selo de admin errado: {selo!r}")
    cobrar(travados == 0, f"admin não devia ter campo travado ({travados})")
    cobrar(not segredo.startswith("••••"), "admin deveria ver o segredo, não a máscara")

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
    # O aviso é console-only, então sem isto um refactor que apague o bloco passa
    # despercebido. E os dois administrativos que ainda não têm input na tela
    # (`translate_model`, `remote_tts_model`) NÃO podem virar aviso eterno.
    avisos = []
    pg.on("console", lambda m: avisos.append(m.text) if m.type == "warning" else None)
    pg.reload()
    pg.wait_for_selector("#cfgAdminBadge", state="attached", timeout=15000)
    pg.wait_for_timeout(400)
    do_load = [a for a in avisos if "[admin]" in a]
    print(f"  aviso no load normal: {do_load or '(nenhum, correto)'}")
    cobrar(not do_load, f"load normal não pode avisar (os 2 sem-UI estão suprimidos): {do_load}")

    sim = pg.evaluate("""() => {
        const vistos = [], orig = console.warn;
        console.warn = (...a) => vistos.push(a.join(" "));
        try {
            ADMIN.fields = new Set([...ADMIN.fields, "campo_novo_sem_marcador"]);
            aplicarPoliticaAdmin();                       // deve avisar 1×
            const comNovo = vistos.length;
            ADMIN.fields = new Set(["translate_model", "remote_tts_model"]);
            aplicarPoliticaAdmin();                       // os 2 sem-UI: sem aviso
            const comSemUi = vistos.length;
            // controle negativo: controle dinâmico TEM data-setting -> não pode acusar
            const dinamicos = [...new Set([...document.querySelectorAll("[data-setting]")]
                .map(e => e.dataset.setting))];
            ADMIN.fields = new Set(dinamicos.slice(0, 5));
            aplicarPoliticaAdmin();
            return { comNovo, comSemUi, dinamicos: dinamicos.length,
                     total: vistos.length, vistos: vistos.join(" | ") };
        } finally { console.warn = orig; }
    }""")
    print(f"  campo protegido sem marcador -> {sim['comNovo']} aviso(s): {(sim['vistos'] or '')[:60]}")
    cobrar(sim["comNovo"] == 1, f"o resguardo deveria avisar o campo sem marcador: {sim}")
    cobrar("campo_novo_sem_marcador" in sim["vistos"], f"o aviso precisa nomear o campo: {sim['vistos']}")
    cobrar(sim["comSemUi"] == 1, f"os 2 sem-UI não podem avisar: {sim['vistos']}")
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