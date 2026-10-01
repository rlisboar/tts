#!/usr/bin/env bash

# log do servidor desta execução (paralelo com outras suítes)
export HASH_DEEP_LOG="${HASH_DEEP_LOG:-/tmp/hash_deep_link_servidor-$$.log}"
# Regressão do deep-link por hash (#128, task_22184151): a rota por `location.hash`
# não existia — abrir http://127.0.0.1:7860/#live caía na tela padrão, porque o
# hash só era lido depois do load e nada escutava `hashchange`.
#
# Sobe servidor PRÓPRIO em porta livre (settings.json do dono intocado; nenhum
# modelo é carregado — o deep-link é roteamento puro no cliente) e cobra no
# Chromium headless o contrato do fix:
#   1. load fresco com #live            -> aba Live ativa, nav marcada E o hook de
#                                          abertura da aba disparou (lxAoAbrir lista
#                                          as vozes do servidor em #lxVoz)
#   2. load fresco com hash inválido    -> tela padrão
#   3. hashchange válido                -> comuta SEM reload (estado de página vivo)
#   4. hashchange inválido e hash limpo -> tela padrão
#   5. load fresco SEM hash             -> restaura a última tela (ttsStudioView)
#   6. console sem erro novo
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

cd "$RAIZ"
"$PY" - <<'PYEOF'
import os, pathlib, signal, socket, subprocess, sys, time, urllib.request

RAIZ = pathlib.Path.cwd()
falhas = []
def cobrar(c, m):
    if not c: falhas.append(m)

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

porta = porta_livre()
env = {**os.environ, "LIVE_UI_PORT": str(porta)}
log = pathlib.Path(os.environ.get("HASH_DEEP_LOG") or "/tmp/hash_deep_link_servidor.log")
_launcher = '''
import os, uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["LIVE_UI_PORT"]))
'''
proc = subprocess.Popen([str(RAIZ / ".venv-mlx" / "bin" / "python"), "-c", _launcher],
                        cwd=str(RAIZ), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                        start_new_session=True)
base = f"http://127.0.0.1:{porta}"

def ativo(pg):
    """id da view ativa (o `.view.active`), ou None."""
    return pg.evaluate("""() => {
        const a = document.querySelector('.view.active');
        return a ? a.id.replace(/^view-/, '') : null;
    }""")

def nav_ativa(pg):
    return pg.evaluate("""() => {
        const a = document.querySelector('.nav-item.active');
        return a ? a.dataset.view : null;
    }""")

try:
    for _ in range(300):
        if proc.poll() is not None: raise SystemExit(f"servidor morreu na subida (log {log})")
        try: urllib.request.urlopen(base + "/health", timeout=2).read(); break
        except Exception: time.sleep(0.2)
    print(f"servidor de teste em {base}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()

        def nova_aba(hash_local_antes=None):
            ctx = b.new_context()
            if hash_local_antes is not None:
                # roda ANTES de qualquer script da página: simula "última tela" gravada
                ctx.add_init_script(
                    f"localStorage.setItem('ttsStudioView', {hash_local_antes!r});")
            pg = ctx.new_page()
            erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
            return ctx, pg, erros

        # ─── 1. deep-link válido num LOAD FRESCO ────────────────────────────
        ctx, pg, erros = nova_aba()
        pg.goto(base + "/#live", wait_until="networkidle")
        pg.wait_for_selector("#view-live", state="attached", timeout=15000)
        v = ativo(pg)
        print(f"  load fresco com #live -> view ativa: {v!r} · nav: {nav_ativa(pg)!r}")
        cobrar(v == "live", f"#live não abriu a aba Live (view ativa {v!r})")
        cobrar(nav_ativa(pg) == "live", "o item de navegação do Live não ficou marcado")
        # o hook de abertura da aba tem de ter rodado (senão a aba abre "morta")
        try:
            pg.wait_for_function("() => document.querySelectorAll('#lxVoz option').length > 0",
                                 timeout=10000)
            ok_vozes = True
        except Exception:
            ok_vozes = False
        cobrar(ok_vozes, "deep-link abriu a aba Live sem disparar lxAoAbrir (#lxVoz vazio)")
        cobrar(pg.evaluate("() => localStorage.getItem('ttsStudioView')") == "live",
               "a tela do deep-link não foi gravada como última tela")
        cobrar(not erros, "erros de JS no deep-link: " + "; ".join(erros[:3]))
        ctx.close()

        # ─── 2. hash INVÁLIDO num load fresco -> padrão ─────────────────────
        ctx, pg, erros = nova_aba()
        pg.goto(base + "/#nao-existe", wait_until="networkidle")
        pg.wait_for_timeout(300)
        v = ativo(pg)
        print(f"  load fresco com hash inválido -> view ativa: {v!r}")
        cobrar(v == "gerar", f"hash inválido não caiu na tela padrão (view ativa {v!r})")
        cobrar(not erros, "erros de JS com hash inválido: " + "; ".join(erros[:3]))
        ctx.close()

        # ─── 3/4. hashchange SEM reload ─────────────────────────────────────
        ctx, pg, erros = nova_aba()
        pg.goto(base + "/", wait_until="networkidle")
        pg.wait_for_timeout(300)
        # marca de vida: se houvesse reload, o estado de página sumiria
        pg.evaluate("() => { window.__vivo = 'x'; }")
        pg.evaluate("() => { location.hash = '#config'; }")
        pg.wait_for_timeout(400)
        v = ativo(pg)
        print(f"  hashchange #config -> view ativa: {v!r} · marca viva: "
              f"{pg.evaluate('() => window.__vivo')!r}")
        cobrar(v == "config", f"hashchange válido não comutou (view ativa {v!r})")
        cobrar(pg.evaluate("() => window.__vivo") == "x", "hashchange recarregou a página")
        cobrar(pg.evaluate("() => performance.getEntriesByType('navigation').length") == 1,
               "hashchange gerou nova navegação (reload)")

        pg.evaluate("() => { location.hash = '#lixo'; }")
        pg.wait_for_timeout(400)
        v = ativo(pg)
        print(f"  hashchange inválido -> view ativa: {v!r}")
        cobrar(v == "gerar", f"hashchange inválido não caiu na padrão (view ativa {v!r})")

        # hash limpo: history.replaceState não dispara hashchange, então navega
        # pelo próprio endereço sem reload
        pg.evaluate("() => { location.hash = ''; }")
        pg.wait_for_timeout(400)
        v = ativo(pg)
        print(f"  hash limpo -> view ativa: {v!r}")
        cobrar(v == "gerar", f"hash limpo não voltou à padrão (view ativa {v!r})")
        cobrar(not erros, "erros de JS no hashchange: " + "; ".join(erros[:3]))

        # ─── 5. SEM hash: a última tela continua sendo restaurada ───────────
        pg.evaluate("() => { localStorage.setItem('ttsStudioView', 'tradutor'); }")
        ctx2, pg2, erros2 = nova_aba("tradutor")
        pg2.goto(base + "/", wait_until="networkidle")
        pg2.wait_for_selector("#view-tradutor", state="attached", timeout=15000)
        v2 = ativo(pg2)
        print(f"  load fresco SEM hash (última = tradutor) -> view ativa: {v2!r}")
        cobrar(v2 == "tradutor", f"sem hash a última tela não foi restaurada (view ativa {v2!r})")
        cobrar(not erros2, "erros de JS na restauração: " + "; ".join(erros2[:3]))
        ctx2.close(); ctx.close()
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

print()
if falhas:
    print("✖ FALHOU")
    for f in falhas: print("  ·", f)
    sys.exit(1)
print("✔ OK — deep-link por hash: #live abre a aba (com o hook), inválido cai na padrão, "
      "hashchange comuta sem reload e sem hash a última tela é restaurada")
PYEOF