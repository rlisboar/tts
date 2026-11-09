#!/usr/bin/env bash
# Última chave de API pela UI (task #298, contrato do 409 da #292).
#
# A rota de apagar devolve 409 quando é a ÚLTIMA chave e nada mais sustenta a
# auth: sem ela a LAN inteira passa a entrar sem credencial. A tela trata isso
# como PERGUNTA — avisa o preço e oferece forçar ou cancelar; forçar repete com
# `?forcar=1`. Também cobra o aviso permanente de "LAN aberta" e o selo de papel.
#
# Servidor PRÓPRIO em porta livre e arquivo de chaves PRÓPRIO (`TTS_APIKEYS_PATH`
# em /tmp, lido no import — #461): o `.apikeys.json` do dono não é tocado e a instância dele não precisa
# escutar em 0.0.0.0 (o roteiro é todo por loopback, que é admin por definição).
#
#   ./tests/ultima_chave_ui.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

cd "$RAIZ"
"$PY" - "$RAIZ" <<'PYEOF'
import json, os, pathlib, signal, socket, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright

RAIZ = pathlib.Path(sys.argv[1])
SUF = str(os.getpid())
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

# Fora da faixa de controle do mic-router (7861-7865, #898): se o servidor do
# teste cair ali, a sonda do painel OBS fala com ele no lugar do roteador e
# leva CORS — vermelho que não existe.
def porta_livre():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]
        if not 7861 <= porta <= 7865:
            return porta

porta = porta_livre()
apikeys = pathlib.Path(f"/tmp/apikeys_teste-{SUF}.json")
log = pathlib.Path(f"/tmp/ultima_chave-{SUF}.log")   # único por execução (#1017)
# isolamento por AMBIENTE (#461): `TTS_APIKEYS_PATH` é lido no import do app, então
# não precisa de launcher com monkeypatch — o arquivo do dono nem é aberto
# o `.apikey` legado também: `_save_apikeys` espelha nele e, sem a variável, o
# espelho caía no arquivo do dono (#535)
_SETTINGS = pathlib.Path(f"/tmp/settings_teste-{SUF}.json")
_SETTINGS.write_text("{}")          # defaults do app; nada do dono
env_proc = {**os.environ, "TTS_APIKEYS_PATH": str(apikeys),
            "TTS_LEGACY_APIKEY_PATH": str(pathlib.Path(f"/tmp/apikey_teste-{SUF}")),
            "TTS_SETTINGS_PATH": str(_SETTINGS)}
# #998: sem isto a suíte herda o `TTS_ROD_ADMIN_KEY` do runner e a linha readonly de
# ambiente entra na lista — o roteiro (contagens e o clique em Apagar) não a previu.
env_proc.pop("TTS_ROD_ADMIN_KEY", None)
env_proc.pop("TTS_ROD_API_KEY", None)   # é esta que vira linha readonly na lista
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                         "--host", "127.0.0.1", "--port", str(porta),
                         "--log-level", "warning"],
                        cwd=str(RAIZ), env=env_proc, stdout=log.open("w"),
                        stderr=subprocess.STDOUT, start_new_session=True)
base = f"http://127.0.0.1:{porta}"

def espera_servidor():
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/", timeout=2).read(1)
            return True
        except Exception:
            time.sleep(0.25)
    return False

def cria(nome, papel="use"):
    req = urllib.request.Request(base + "/api/apikeys", method="POST",
                                 data=json.dumps({"name": nome, "role": papel}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["key"]

def chaves():
    return json.loads(urllib.request.urlopen(base + "/api/apikeys").read())["keys"]

def limpa():
    """`_load_apikeys` gera uma chave "padrão" no arquivo novo: o roteiro precisa
    de contagem determinística, então zera tudo com `forcar=1` (o 409 barraria)."""
    for k in chaves():
        urllib.request.urlopen(urllib.request.Request(
            base + f"/api/apikeys/{k['id']}?forcar=1", method="DELETE")).read()

falhas_setup = []
try:
    if not espera_servidor():
        print("✖ servidor de teste não subiu"); sys.exit(1)

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context()
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.accept())      # o confirm de um passo só
        limpa()                                   # 0 chaves: o roteiro conta sozinho
        pg.goto(base, wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)
        pg.evaluate("showView('config', {tab: 'acesso'})")
        pg.wait_for_timeout(600)

        def apagar_primeiro(pagina=None):
            # #998: a linha da chave de AMBIENTE é readonly e NÃO tem o botão —
            # escolher a primeira linha que TENHA o botão (antes, um `.find`
            # devolvia undefined e o `.click()` estourava TypeError).
            return (pagina or pg).evaluate("""() => {
                for (const row of document.querySelectorAll('#apiKeysList .api-key-row')) {
                    const b = [...row.querySelectorAll('button')]
                        .find(x => x.textContent.trim() === 'Apagar');
                    if (b) { b.click(); return true; }
                }
                return false;
            }""")

        # ── caso comum: duas chaves, apagar é UM passo e não pergunta preço ──
        cria("uma", "use"); cria("outra", "use")
        pg.evaluate("loadApiKeys()")
        pg.wait_for_timeout(500)
        cobrar(pg.evaluate("document.querySelectorAll('#apiKeysList .api-key-row').length") == 2,
               "a lista não mostrou as duas chaves de teste")
        cobrar(pg.evaluate("!!document.querySelector('#apiKeysList .ak-role')"), "selo de papel ausente na linha")
        apagar_primeiro()
        pg.wait_for_timeout(700)
        cobrar(not pg.evaluate("!!document.querySelector('.ak-confirma')"),
               "apareceu pergunta de ÚLTIMA chave apagando uma de duas")
        cobrar(len(chaves()) == 1, "a chave não foi apagada no caso comum")

        # ── última chave: 409 vira pergunta com o preço dito ────────────────
        apagar_primeiro()
        pg.wait_for_timeout(800)
        cobrar(len(chaves()) == 1, "o 409 apagou a chave mesmo sem forçar")
        caixa = pg.evaluate("""() => { const c = document.querySelector('.ak-confirma');
            return c ? { texto: c.textContent, botoes: [...c.querySelectorAll('button')].map(b => b.textContent.trim()) } : null; }""")
        cobrar(caixa is not None, "a última chave não abriu a confirmação inline")
        if caixa:
            cobrar("última" in caixa["texto"].lower(),
                   f"a confirmação não diz que é a última ({caixa['texto'][:60]!r})")
            # `consequencia` do servidor entra no texto: se a regra do servidor
            # mudar, a tela acompanha sem alguém reescrever a frase no front
            cobrar("qualquer máquina da rede" in caixa["texto"],
                   f"a confirmação não usa a `consequencia` do servidor ({caixa['texto'][:80]!r})")
            cobrar(any("LAN" in x for x in caixa["botoes"]), f"sem botão de forçar ({caixa['botoes']})")
            cobrar(any("Cancelar" in x for x in caixa["botoes"]), f"sem botão de cancelar ({caixa['botoes']})")
            # foco vai para a ação, não para o vazio
            cobrar(pg.evaluate("!!document.activeElement && document.activeElement.textContent || ''.includes('LAN')"),
                   "o foco não foi para o botão de forçar")

        # cancelar não muda nada
        pg.evaluate("""() => { const c = document.querySelector('.ak-confirma');
            const b = c && [...c.querySelectorAll('button')].find(x => x.textContent.includes('Cancelar'));
            if (b) b.click(); }""")
        pg.wait_for_timeout(250)
        cobrar(not pg.evaluate("!!document.querySelector('.ak-confirma')"), "Cancelar não fechou a pergunta")
        cobrar(len(chaves()) == 1, "Cancelar apagou a chave")

        # forçar apaga e o aviso de LAN aberta acende
        apagar_primeiro()
        pg.wait_for_timeout(700)
        pg.evaluate("""() => { const c = document.querySelector('.ak-confirma');
            const b = c && [...c.querySelectorAll('button')].find(x => x.textContent.includes('LAN'));
            if (b) b.click(); }""")
        pg.wait_for_timeout(900)
        cobrar(len(chaves()) == 0, f"o forçar não apagou (restam {len(chaves())})")
        # o `avisos` do 200 não pode morrer: é a explicação que o servidor mandou
        pg.wait_for_timeout(200)
        cobrar("Autenticação DESLIGADA" in (pg.text_content("#toast") or ""),
               f"o aviso do servidor não foi mostrado ({pg.text_content('#toast')!r})")
        cobrar(pg.evaluate("!document.getElementById('apiLanAberta').hidden"),
               "o aviso permanente de LAN aberta não apareceu")
        # ── toggle: desligar a exigência avisa pelo `avisos` ────────────────
        pg.evaluate("loadApiKeys()")
        pg.wait_for_timeout(400)
        pg.evaluate("""() => { const t = document.getElementById('apiAuthEnabled');
            t.checked = true; t.dispatchEvent(new Event('change', {bubbles:true})); }""")
        pg.wait_for_timeout(700)
        pg.evaluate("""() => { const t = document.getElementById('apiAuthEnabled');
            t.checked = false; t.dispatchEvent(new Event('change', {bubbles:true})); }""")
        pg.wait_for_timeout(700)
        cobrar("DESLIGADA" in (pg.text_content("#toast") or ""),
               f"desligar a exigência pelo toggle não mostrou o aviso ({pg.text_content('#toast')!r})")
        # ── #998: com `TTS_ROD_ADMIN_KEY` no ambiente, a linha readonly aparece e o
        #    "Apagar" tem de achar a linha que TEM o botão (não a de ambiente).
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        apikeys.unlink(missing_ok=True)
        porta2 = porta_livre()
        env2 = {**env_proc, "TTS_ROD_API_KEY": "chave-de-ambiente-teste-998"}
        log2 = pathlib.Path(f"/tmp/ultima_chave_env-{SUF}-{porta2}.log")   # SUF: porta pode repetir
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                                 "--host", "127.0.0.1", "--port", str(porta2),
                                 "--log-level", "warning"],
                                cwd=str(RAIZ), env=env2, stdout=log2.open("w"),
                                stderr=subprocess.STDOUT, start_new_session=True)
        base = f"http://127.0.0.1:{porta2}"
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/", timeout=2).read(1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise SystemExit("servidor com chave de ambiente não subiu")
        pg2 = ctx.new_page()
        pg2.on("dialog", lambda d: d.accept())   # o confirm do apagar
        pg2.goto(base, wait_until="domcontentloaded")
        pg2.wait_for_timeout(1200)
        pg2.evaluate("showView('config', {tab: 'acesso'})")
        pg2.wait_for_timeout(600)
        limpa()                              # 0 chaves de arquivo: só a de ambiente
        pg2.evaluate("loadApiKeys()")
        pg2.wait_for_timeout(600)
        cobrar(pg2.evaluate("document.querySelectorAll('#apiKeysList .api-key-row').length") == 1,
               "a chave de ambiente não entrou na lista (esperava 1 linha readonly)")
        # 1) só a linha de ambiente: o clique não pode estourar (o `find` devolvia
        #    undefined e o `.click()` lançava TypeError) e devolve False
        sem_botao = apagar_primeiro(pg2)
        cobrar(sem_botao is False, f"a linha de ambiente sem botão devia devolver False (veio {sem_botao!r})")
        # 2) com chaves de teste, acha a linha que TEM o botão e apaga
        cria("uma", "use"); cria("outra", "use")
        pg2.evaluate("loadApiKeys()")
        pg2.wait_for_timeout(600)
        cobrar(pg2.evaluate("document.querySelectorAll('#apiKeysList .api-key-row').length") == 3,
               "as chaves de teste não entraram na lista (esperava 3 linhas)")
        apagou = apagar_primeiro(pg2)
        pg2.wait_for_timeout(800)
        cobrar(apagou is True, "o Apagar não achou nenhuma linha com botão")
        # com a chave de ambiente a rede segue autenticada; se a tela perguntar, força
        pg2.evaluate("""() => { const c = document.querySelector('.ak-confirma');
            const b = c && [...c.querySelectorAll('button')]
                .find(x => x.textContent.includes('LAN') || x.textContent.includes('forçar'));
            if (b) b.click(); }""")
        pg2.wait_for_timeout(800)
        cobrar(len(chaves()) == 1, f"apagar com chave de ambiente não apagou uma chave de teste (restam {len(chaves())})")
        pg2.close()
        b.close()
finally:
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try: proc.terminate()
        except Exception: pass
    try: proc.wait(timeout=10)
    except Exception:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass
    for sobra in (apikeys,):
        try: sobra.unlink()
        except Exception: pass

if falhas:
    print("✖ falhas:")
    for f in falhas: print("  -", f)
    print("· log do servidor em", log)
    sys.exit(1)
log.unlink(missing_ok=True)
print("✔ última chave: comum não pergunta, 409 vira pergunta com o preço dito, Cancelar preserva, "
      "forçar apaga e acende o aviso de LAN aberta")
PYEOF
