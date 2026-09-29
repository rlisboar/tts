#!/usr/bin/env bash
# E2E da escolha de backend de IA (task_bde89b9a / DSH-3): endpoint+chave OU
# dsh (harness ACP local), com modelo descoberto pelo próprio dsh e effort.
# Cobre também o backend PRÓPRIO do Live (task_d148d385 / #178): o seletor
# "Backend do Live" com herdar/openai/dsh, o hint do backend EFETIVO e o campo
# entrando no payload (inclusive `""` = herdar).
#
# Usa o `dsh` REAL do host (a descoberta leva 1–4 s e fica em cache no servidor);
# se o binário não existir, o teste cai no caminho de erro — que é justamente o
# que precisa funcionar sem mentir.
#
#   ./tests/dsh_ui.sh
#
# Servidor PRÓPRIO (porta livre) e stub de chat por env, mas o settings.json é o
# do DONO (não é versionado): a suíte normaliza o que precisa para o cenário e
# devolve TUDO que tocou no fim, campo a campo — senão sobra de suíte vira
# "escolha do dono" para quem ler depois. Já aconteceu: `chat_dsh_bin` regrediu de
# caminho absoluto para o literal `dsh` (que depende de PATH no env do filho).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# O dsh COMPÕE o perfil no boot (escreve cordis.yml) e morre com EPERM se
# ~/.dsh não for gravável — acontece sob sandbox (medido: `touch` no perfil
# falha aqui). Cópia em local gravável + DSH_HOME resolve sem tocar no original.
export DSH_PROFILE="${DSH_PROFILE:-tts-studio}"   # o python abaixo usa o mesmo
DSH_HOME_TMP=""
if [ -f "$HOME/.dsh/profiles/$DSH_PROFILE/cordis.yml" ] \
   && ! touch "$HOME/.dsh/profiles/$DSH_PROFILE/cordis.yml" 2>/dev/null; then
  DSH_HOME_TMP="$(mktemp -d "/tmp/tts-dsh-home-$$.XXXXXX")"
  cp -R "$HOME/.dsh/." "$DSH_HOME_TMP/"
  chmod -R u+w "$DSH_HOME_TMP"
  grep -rl "$HOME/.dsh" "$DSH_HOME_TMP/profiles" 2>/dev/null \
    | while read -r f; do sed -i '' "s|$HOME/.dsh|$DSH_HOME_TMP|g" "$f"; done || true
  export DSH_HOME="$DSH_HOME_TMP"
  echo "  ~/.dsh não gravável nesta sessão — usando cópia em $DSH_HOME_TMP"
fi
cleanup_dsh() { rm -rf "${DSH_HOME_TMP:-/nonexistent-dsh-home}"; }
trap cleanup_dsh EXIT

# Só a descoberta (não carrega modelo de áudio) — não precisa da trava, mas o
# binário do dsh é resolvido no PATH do servidor.
cd "$RAIZ"
"$PY" - "$RAIZ" <<'PYEOF'
import json, os, pathlib, shutil, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

RAIZ = pathlib.Path(sys.argv[1])
SUF = str(os.getpid())
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

stub_porta = porta_livre()
threading.Thread(target=HTTPServer(("127.0.0.1", stub_porta), Stub).serve_forever, daemon=True).start()

DSH_BIN = os.environ.get("DSH_BIN") or ""
if not DSH_BIN:
    for p in ("/opt/homebrew/bin/dsh", "/usr/local/bin/dsh"):
        if os.path.exists(p): DSH_BIN = p; break
DSH_BIN = DSH_BIN or "dsh"
PROFILE = os.environ.get("DSH_PROFILE") or "tts-studio"

porta = porta_livre()
# SEM TTS_CHAT_DSH_*: o env TEM PRECEDÊNCIA sobre o settings (por desenho, #112) e
# com ele o campo `chat_dsh_bin` da tela vira decorativo — foi o que escondeu o
# caminho de erro na 1ª versão deste teste.
env = {**os.environ, "TTS_CHAT_BASE_URL": f"http://127.0.0.1:{stub_porta}/v1",
       "TTS_CHAT_MODEL": "stub-dsh-ui", "DSH_UI_PORT": str(porta),
       "ORT_DISABLE_TELEMETRY": "1"}
log = pathlib.Path("/tmp") / f"dsh_ui_servidor_{SUF}.log"
_launcher = '''
import os, uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["DSH_UI_PORT"]))
'''
proc = subprocess.Popen([str(RAIZ / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                        start_new_session=True)
base = f"http://127.0.0.1:{porta}"
try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu na subida (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor de teste em {base} · dsh={DSH_BIN}")

    def post_settings(d):
        r = urllib.request.Request(base + "/api/settings", method="POST",
                                   data=json.dumps(d).encode(),
                                   headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(r, timeout=20).read())

    # ─── cenário: normaliza o que NÃO serve e guarda o ORIGINAL para o fim ────
    # O settings é o do dono: tudo que a suíte toca (normalização + fluxo da tela)
    # volta no fim pelo valor original — inclusive quando o original é vazio ou um
    # caminho que só funciona no PATH dele. `chat_dsh_bin` e `chat_dsh_profile`
    # ficaram de fora do restauro antigo e a suíte deixava o valor NORMALIZADO no
    # arquivo (absoluto → `dsh`), que é o que este bloco conserta.
    orig = json.loads(urllib.request.urlopen(base + "/api/settings", timeout=10).read())
    CAMPOS_TOCADOS = ("chat_backend", "chat_backend_live", "chat_dsh_bin",
                      "chat_dsh_profile", "chat_dsh_model", "chat_dsh_effort")
    orig_tocado = {k: orig.get(k, "") for k in CAMPOS_TOCADOS}
    orig_effort = orig_tocado["chat_dsh_effort"] or "off"
    orig_live = orig_tocado["chat_backend_live"] or ""

    def _bin_serve(v):
        v = (v or "").strip()
        if not v: return False
        return os.path.exists(v) if os.path.sep in v else bool(shutil.which(v))

    # Só reescreve o que NÃO serve: valor bom do dono não é trocado pelo normalizado.
    normalizar = {}
    if not _bin_serve(orig_tocado["chat_dsh_bin"]): normalizar["chat_dsh_bin"] = DSH_BIN
    if not (orig_tocado["chat_dsh_profile"] or "").strip(): normalizar["chat_dsh_profile"] = PROFILE
    if normalizar: post_settings(normalizar)
    print(f"  cenário: bin={orig_tocado['chat_dsh_bin'] or DSH_BIN!r} "
          f"profile={orig_tocado['chat_dsh_profile'] or PROFILE!r} · "
          f"normalizado={normalizar or 'nada'} (backend original: {orig_tocado['chat_backend']!r})")

    # pré-checa o endpoint fora do browser (o teste da UI não deve confundir
    # "endpoint quebrado" com "tela quebrada")
    req = urllib.request.Request(base + "/api/chat/dsh/models")
    try:
        dados = json.loads(urllib.request.urlopen(req, timeout=90).read())
        tem_dsh = True
        catalogo = len(dados.get("models", []))
        print(f"  endpoint: {catalogo} modelo(s) · node {dados.get('node')}")
        cobrar(len(dados.get("models", [])) > 0, "endpoint devolveu lista vazia de modelos")
        cobrar(all(k in dados for k in ("models", "efforts", "current", "default_model")),
               f"contrato do endpoint incompleto: {sorted(dados)}")
    except Exception as exc:
        tem_dsh = False
        print(f"  endpoint indisponível ({exc}) — só o caminho de erro será cobrado")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(viewport={"width": 1500, "height": 1000}).new_page()
        erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
        cons = []; pg.on("console", lambda m: cons.append(m.text)
                         if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        corpo_save = {}
        pg.on("request", lambda r: corpo_save.update(json.loads(r.post_data or "{}"))
               if r.url.endswith("/api/settings") and r.method == "POST" else None)
        pg.goto(base, wait_until="networkidle")
        pg.evaluate("() => showView('config', { tab: 'rede' })")
        pg.wait_for_timeout(300)

        # ─── 1) a tela REFLETE o backend salvo (não um palpite) ────────────────
        salvo = json.loads(urllib.request.urlopen(base + "/api/settings", timeout=10).read())
        backend_salvo = salvo.get("chat_backend") or "openai"
        ini = pg.evaluate("""() => ({ back: document.getElementById('chatBackend').value,
            dshEscondido: document.getElementById('chatDshBox').hidden,
            openaiVisivel: !document.getElementById('chatOpenaiBox').hidden,
            hint: document.getElementById('chatBackendHint').textContent })""")
        print(f"  salvo={backend_salvo!r} · form={ini['back']!r} · dshEscondido={ini['dshEscondido']}")
        cobrar(ini["back"] == backend_salvo, f"form não reflete o backend salvo ({ini['back']!r} != {backend_salvo!r})")
        # visibilidade segue o backend EFETIVO dos dois caminhos (o Live pode ter
        # backend próprio, #178): o bloco some só quando NENHUM caminho o usa
        efetivo_live = (orig.get("chat_backend_live") or "") or backend_salvo
        cobrar(ini["dshEscondido"] == (backend_salvo != "dsh" and efetivo_live != "dsh"),
               f"bloco do dsh visível/escondido fora do esperado (conversa={backend_salvo!r}"
               f" live={efetivo_live!r})")
        cobrar(ini["openaiVisivel"] == (backend_salvo != "dsh" or efetivo_live != "dsh"),
               "bloco do endpoint visível/escondido fora do esperado")
        cobrar("Base URL" in ini["hint"] or "rota do dsh" in ini["hint"],
               f"hint do backend não diz o destino: {ini['hint']!r}")

        # ─── 2) escolher dsh: aparecem os campos DELE e some o endpoint+chave ─
        pg.select_option("#chatBackend", "dsh")
        pg.wait_for_timeout(300)
        est = pg.evaluate("""() => ({ dshVisivel: !document.getElementById('chatDshBox').hidden,
            openaiEscondido: document.getElementById('chatOpenaiBox').hidden,
            hint: document.getElementById('chatBackendHint').textContent,
            texto: document.getElementById('chatDshBox').textContent })""")
        print("  dsh selecionado → dshVisivel:", est["dshVisivel"], "· openai escondido:", est["openaiEscondido"])
        cobrar(est["dshVisivel"] and est["openaiEscondido"], "trocar para dsh não alternou os campos")
        cobrar("rota do dsh" in est["hint"], f"hint do dsh não diz o destino: {est['hint']!r}")
        cobrar("1,5 s" in est["texto"] or "latência" in est["texto"],
               "aviso de que effort ≠ off estoura a latência do Live não aparece")
        cobrar("processo local" in est["texto"], "não declara que o dsh é processo local/rota dele")
        # o alvo de 1,5 s é do cenário local/stub — com provedor remoto o 1º token
        # dele domina; sem essa qualificação o dono lê 31 s e acha que quebrou (#174)
        cobrar("local/stub" in est["texto"] and "remoto" in est["texto"],
               "hint do effort não qualifica que o alvo de 1,5 s é do cenário local/stub")
        # aviso do spike (sem streaming incremental): a tela tem de dizer ANTES que
        # no Live a fala só sai com a resposta inteira, por causa da latência
        # o aviso tem de valer NOS DOIS estados do bridge (o patch é local e pode
        # sumir num `npm install -g`): com patch = deltas em ~0,4 s; sem = no fim
        cobrar("deltas são projetados" in est["texto"] and "0,4 s" in est["texto"]
               and "sem o patch" in est["texto"] and "só chega no fim" in est["texto"],
               "aviso do streaming do dsh (condicionado ao patch do bridge) não está completo")
        cobrar(pg.evaluate("() => !document.getElementById('chatDshStreaming').hidden"),
               "aviso de streaming escondido com o backend dsh selecionado")

        # ─── 2b) "Backend do Live" (#178): o campo tem 3 estados e a tela diz qual
        # backend o Live usa DE FATO — sem isso "igual ao da Conversa" lê como "não
        # faz nada" e o dono fica no escuro sobre o que está valendo.
        live = pg.evaluate("""() => { const s = document.getElementById('chatBackendLive');
            return { existe: !!s, admin: !!document.querySelector('[data-admin-setting="chat_backend_live"]'),
                     opcoes: s ? [...s.options].map(o => o.value) : [],
                     valor: s ? s.value : null,
                     hint: (document.getElementById('chatBackendLiveHint')||{}).textContent || '' }; }""")
        print("  backend do Live:", {k: live[k] for k in ("existe", "admin", "opcoes", "valor")},
              "· hint:", live["hint"][:70])
        cobrar(live["existe"], "select 'Backend do Live' não existe")
        cobrar(live["admin"], "campo do Live sem data-admin-setting (não travaria p/ chave de uso)")
        cobrar(live["opcoes"] == ["", "openai", "dsh"],
               f"opções do backend do Live não são herdar/openai/dsh: {live['opcoes']}")
        cobrar("mesmo backend da Conversa" in live["hint"] and "dsh" in live["hint"],
               f"com o Live herdando, o hint não diz o backend EFETIVO: {live['hint']!r}")
        pg.select_option("#chatBackendLive", value="openai")
        pg.wait_for_timeout(200)
        pro = pg.evaluate("() => document.getElementById('chatBackendLiveHint').textContent")
        print("  Live com backend próprio:", pro[:70])
        cobrar("próprio" in pro and "Endpoint" in pro, f"backend próprio não aparece no hint: {pro!r}")
        pg.select_option("#chatBackendLive", value="")
        pg.wait_for_timeout(200)
        cobrar("mesmo backend da Conversa" in pg.evaluate(
                   "() => document.getElementById('chatBackendLiveHint').textContent"),
               "voltar para 'igual ao da Conversa' não devolve o hint de herança")
        pg.locator("#chatBackendLive").scroll_into_view_if_needed()
        pg.screenshot(path=str(RAIZ / "evidence" / "178-backend-live-heranca.png"))
        print("  evidência: evidence/178-backend-live-heranca.png")

        # ─── 2c) caminhos MISTOS: a config do dsh é COMPARTILHADA, então o bloco
        # não pode sumir quando só o Live quer o dsh (senão o dono não alcança o
        # que o Live vai usar) — e o mesmo vale para o bloco do endpoint.
        pg.select_option("#chatBackendLive", value="dsh")
        pg.select_option("#chatBackend", "openai")
        pg.wait_for_timeout(400)
        misto = pg.evaluate("""() => ({ dshVisivel: !document.getElementById('chatDshBox').hidden,
            nota: !document.getElementById('chatOpenaiNota').hidden })""")
        print("  Conversa=endpoint + Live=dsh →", misto)
        cobrar(misto["dshVisivel"], "bloco do dsh escondido com o Live no dsh (config compartilhada)")
        cobrar(not misto["nota"], "nota do endpoint apareceu sem o Live estar no endpoint")
        pg.select_option("#chatBackend", "dsh")
        pg.select_option("#chatBackendLive", value="openai")
        pg.wait_for_timeout(400)
        inv = pg.evaluate("""() => ({ dshVisivel: !document.getElementById('chatDshBox').hidden,
            openaiVisivel: !document.getElementById('chatOpenaiBox').hidden,
            nota: !document.getElementById('chatOpenaiNota').hidden })""")
        print("  Conversa=dsh + Live=endpoint →", inv)
        cobrar(inv["dshVisivel"] and inv["openaiVisivel"], "um dos blocos sumiu no caso misto")
        cobrar(inv["nota"], "nota do endpoint não avisa que os campos valem para o Live")
        pg.select_option("#chatBackendLive", value="")
        pg.wait_for_timeout(200)
        # caminho RESOLVIDO visível (pedido do PM na #124): vai no placeholder, sem
        # sujar o valor salvo
        ph = pg.evaluate("() => document.getElementById('chatDshBin').placeholder")
        print("  placeholder do binário:", ph)
        cobrar("resolvido:" in ph and DSH_BIN in ph, f"placeholder não mostra o binário resolvido: {ph!r}")
        cobrar(pg.evaluate("() => document.getElementById('chatDshBin').value") == DSH_BIN,
               "mostrar o resolvido mexeu no valor salvo")

        # ─── 3) catálogo do modelo: carrega, mostra progresso, não fica vazio ──
        if tem_dsh:
            try:
                pg.wait_for_function(
                    "() => document.getElementById('chatDshStatus').textContent.includes('falei com o dsh em')",
                    timeout=90000)
            except Exception:
                pass
            cat = pg.evaluate("""() => { const sel = document.getElementById('chatDshModel');
                const grupos = [...sel.querySelectorAll('optgroup')].map(g => g.label);
                const op = sel.options[sel.selectedIndex];
                const daOpcao = (op && op.dataset.efforts || '').split(',').filter(Boolean);
                return { n: sel.options.length, valor: sel.value, grupos,
                         efforts: [...document.getElementById('chatDshEffort').options].map(o => o.value),
                         effortsDoModelo: daOpcao,
                         status: document.getElementById('chatDshStatus').textContent }; }""")
            print("  catálogo:", {k: cat[k] for k in ("n", "valor", "efforts")}, "· status:", cat["status"][:70])
            # o catálogo inteiro, não "alguma coisa": `n > 0` tolerava o select
            # reduzido a uma opção de consolo (#156)
            cobrar(cat["n"] >= max(5, catalogo),
                   f"select de modelo não trouxe o catálogo ({cat['n']}/{catalogo})")
            cobrar(cat["valor"], "nenhum modelo selecionado após a descoberta")
            cobrar(cat["grupos"] and all(g for g in cat["grupos"]),
               f"select de modelo sem agrupamento por provedor: {cat['grupos']}")
        cobrar(cat["efforts"] and cat["efforts"] == cat["effortsDoModelo"],
               f"effort não derivou do modelo: opção={cat['effortsDoModelo']} select={cat['efforts']}")
        cobrar("falei com o dsh em" in cat["status"] and " ms" in cat["status"],
               f"Testar não reporta a latência do próprio clique: {cat['status'][:80]!r}")

        # ─── 3b) effort SALVO sobrevive ao load ───────────────────────────────
        # O select de effort nasce VAZIO (as opções são criadas no liga()): se o
        # valor salvo fosse escrito antes de popular, ele se perderia e a tela
        # mostraria "off" para quem escolheu outro. Vale um check próprio.
        post_settings({"chat_backend": "dsh", "chat_dsh_effort": "low"})
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(600)
        cobrar(pg.evaluate("() => document.getElementById('chatBackend').value") == "dsh",
               "backend salvo não voltou no reload")
        cobrar(pg.evaluate("() => document.getElementById('chatDshEffort').value") == "low",
               f"effort salvo perdido no load (veio "
               f"{pg.evaluate('() => document.getElementById("chatDshEffort").value')!r})")

        # ─── 4) erro do dsh é MOSTRADO e não apaga o modelo guardado ──────────
        # A descoberta usa o binário SALVO (o endpoint lê settings): por isso
        # salva primeiro — é o que a própria tela orienta.
        antes = pg.evaluate("() => document.getElementById('chatDshModel').value")
        pg.fill("#chatDshBin", "/nao/existe/dsh")
        pg.evaluate("() => document.getElementById('cfgSave').click()")
        pg.wait_for_timeout(1200)
        pg.click("#chatDshTestar")
        pg.wait_for_function(
            "() => document.getElementById('chatDshStatus').textContent.startsWith('✖')", timeout=90000)
        erro = pg.evaluate("""() => ({ status: document.getElementById('chatDshStatus').textContent,
            valor: document.getElementById('chatDshModel').value,
            n: document.getElementById('chatDshModel').options.length })""")
        print("  erro exibido:", erro["status"][:110])
        cobrar("✖ não consegui falar com o dsh em" in erro["status"],
               "erro do dsh não foi exibido no status (com a latência do teste)")
        cobrar("Testar" in erro["status"], "erro não diz como resolver")
        # no ERRO também: manter o catálogo é o esperado; sobrar só a consolação de
        # 1 opção é pior e tem de ser acusado (#156) — o piso de 5 vem do host, mas
        # fica explícito para o caso de não haver catálogo medido
        cobrar(erro["n"] >= min(5, max(1, catalogo)),
               f"caminho de erro perdeu o catálogo (n={erro['n']}, esperado >= {min(5, max(1, catalogo))})"
               " — só a opção de consolo sobrou")
        cobrar(erro["n"] > 0, "erro deixou o select de modelo VAZIO (o save apagaria o guardado)")
        cobrar(erro["valor"], "erro deixou o select SEM valor selecionado")
        if antes:
            cobrar(erro["valor"] == antes, f"erro trocou o modelo selecionado ({antes!r} → {erro['valor']!r})")
        else:
            cobrar(erro["valor"] == ((pg.evaluate("() => (LAST_SETTINGS||{}).chat_dsh_model || ''"))),
                   f"erro não preservou o modelo guardado ({erro['valor']!r})")

        # volta o binário bom (o select tem de voltar a carregar depois de salvar)
        pg.fill("#chatDshBin", DSH_BIN)
        pg.evaluate("() => document.getElementById('cfgSave').click()")
        pg.wait_for_timeout(1200)
        corpo_save.clear()
        if tem_dsh:
            pg.click("#chatDshTestar")
            pg.wait_for_function(
                "() => document.getElementById('chatDshStatus').textContent.includes('falei com o dsh em')",
                timeout=90000)
            cobrar(True, "recuperou depois do erro")

        # ─── 5) os 5 campos são administrativos (travam p/ chave de uso) ─────
        adm = pg.evaluate("""() => Object.fromEntries(
            ['chat_backend','chat_backend_live','chat_dsh_bin','chat_dsh_profile','chat_dsh_model','chat_dsh_effort']
            .map(k => [k, !!document.querySelector(`[data-admin-setting="${k}"]`)]))""")
        print("  marcados como admin:", adm)
        cobrar(all(adm.values()), f"campo sem data-admin-setting: {[k for k, v in adm.items() if not v]}")
        # o marcador no DOM só vale se o servidor de fato tratar o campo como admin
        # (é ele quem trava a chave de uso) — checado no mesmo GET que a tela usa
        campos_admin = set(salvo.get("admin_fields") or [])
        cobrar("chat_backend_live" in campos_admin,
               f"servidor não lista chat_backend_live como admin: {sorted(campos_admin)}")

        # ─── 6) o save leva os 5 campos (payload real do POST) ───────────────
        pg.evaluate("() => showView('config')")
        pg.click("#cfgSave")
        pg.wait_for_timeout(1500)
        enviados = {k: corpo_save.get(k) for k in
                    ("chat_backend", "chat_backend_live", "chat_dsh_bin", "chat_dsh_profile",
                     "chat_dsh_model", "chat_dsh_effort")}
        print("  payload do save:", enviados)
        cobrar(corpo_save, "nenhum POST /api/settings capturado")
        cobrar(enviados["chat_backend"] == "dsh", f"chat_backend não foi enviado: {enviados['chat_backend']!r}")
        cobrar(enviados["chat_dsh_model"], "chat_dsh_model foi enviado vazio")
        cobrar(enviados["chat_dsh_effort"] in ("off", "low", "high", "max"),
               f"chat_dsh_effort inválido no payload: {enviados['chat_dsh_effort']!r}")
        # "" (herdar) precisa IR no payload — se o campo saísse de fora, o save de
        # outro campo apagaria a escolha do Live
        cobrar(enviados["chat_backend_live"] == "",
               f"herdar não foi enviado como \"\": {enviados['chat_backend_live']!r}")

        # ─── 6b) "" é valor VÁLIDO e um backend próprio persiste ─────────────
        conf_live = json.loads(urllib.request.urlopen(base + "/api/settings", timeout=10).read())
        cobrar((conf_live.get("chat_backend_live") or "") == "",
               f"servidor não guardou \"\" (herdar): {conf_live.get('chat_backend_live')!r}")
        pg.select_option("#chatBackendLive", value="dsh")
        corpo_save.clear()
        pg.click("#cfgSave")
        pg.wait_for_timeout(1500)
        cobrar(corpo_save.get("chat_backend_live") == "dsh",
               f"backend próprio do Live não foi enviado: {corpo_save.get('chat_backend_live')!r}")
        cobrar((json.loads(urllib.request.urlopen(base + "/api/settings", timeout=10).read())
                .get("chat_backend_live") or "") == "dsh", "servidor não guardou o backend próprio do Live")

        # ─── 7) devolve ao dono TUDO que a suíte tocou (o settings é dele) ────
        # Primeiro pela TELA, para exercitar o caminho do Salvar nos campos que o
        # form cobre…
        corpo_save.clear()
        pg.evaluate("(p) => { document.getElementById('chatBackend').value = p.backend; "
                    "const e = document.getElementById('chatDshEffort'); if (e) e.value = p.effort; "
                    "document.getElementById('chatBackendLive').value = p.live; "
                    "atualizaBackendChat(); atualizaBackendLive(); "
                    "document.getElementById('cfgSave').click(); }",
                    {"backend": backend_salvo, "effort": orig_effort, "live": orig_live})
        pg.wait_for_timeout(1500)
        print("  restaurado pela tela:", corpo_save.get("chat_backend"), "· Live:", corpo_save.get("chat_backend_live"))
        cobrar(corpo_save.get("chat_backend") == backend_salvo,
               f"a tela não devolveu o backend original ({corpo_save.get('chat_backend')!r})")
        cobrar((corpo_save.get("chat_backend_live") or "") == orig_live,
               f"a tela não devolveu o backend do Live ({corpo_save.get('chat_backend_live')!r} != {orig_live!r})")
        # …e o resto por POST direto: o Salvar manda o FORM, que neste ponto tem os
        # valores NORMALIZADOS (bin/perfil/modelo). Este passo é o que faz o arquivo
        # voltar idêntico — e vale para TODO campo tocado, não só os de hoje.
        post_settings(orig_tocado)
        conf = json.loads(urllib.request.urlopen(base + "/api/settings", timeout=10).read())
        # o servidor canoniza vazio → default nesses três (app.py), então é com o
        # que ele GRAVA que se compara (não com o vazio que estava no arquivo)
        esperado = {**orig_tocado, "chat_dsh_bin": orig_tocado["chat_dsh_bin"] or "dsh",
                    "chat_dsh_effort": orig_tocado["chat_dsh_effort"] or "off",
                    "chat_dsh_profile": orig_tocado["chat_dsh_profile"] or "tts-studio"}
        for k in CAMPOS_TOCADOS:
            atual = conf.get(k)
            cobrar((atual if atual is not None else "") == esperado[k],
                   f"campo {k} ficou {atual!r} (original {orig_tocado[k]!r})")
        print("  devolvido ao dono:", {k: esperado[k] for k in
              ("chat_dsh_bin", "chat_dsh_profile", "chat_dsh_model")})

        print(f"  pageerror: {erros} · console.error de script: {cons}")
        cobrar(not erros, f"pageerror: {erros}")
        cobrar(not cons, f"console.error de script: {cons}")
        b.close()
finally:
    try: os.killpg(os.getpgid(proc.pid), 15)
    except Exception: proc.terminate()

if falhas:
    print("\n✖ FALHAS:")
    for f in falhas: print("  -", f)
    sys.exit(1)
print("\n✔ OK — escolha de backend de IA (endpoint+chave x dsh) com modelo/effort,"
      " backend próprio do Live com herança visível, erro visível e payload completo.")
PYEOF