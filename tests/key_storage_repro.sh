#!/usr/bin/env bash
# Regressão do armazenamento da chave da API no navegador (static/index.html).
#
# A opção "Guardar só nesta sessão" só vale se a chave existir em EXATAMENTE um
# storage: uma cópia esquecida no localStorage anula o modo sessão (máquina
# compartilhada continua com o segredo no disco) e um `ttsRodKey` legado
# ressuscita a chave. Este script cobra a invariante na UI REAL, clicando nos
# controles (Configurações → Acesso) num Chromium headless:
#
#   1. nasce com `ttsRodKey` legado no localStorage e uma chave vinda dele
#   2. salva uma chave nova  → só localStorage, legado apagado
#   3. marca "só nesta sessão" → só sessionStorage, rótulo muda junto
#   4. abre outra ABA no mesmo contexto → não herda a chave (sessionStorage é
#      por aba), mas herda o MODO escolhido
#   5. desmarca → volta para localStorage; "Limpar" → nenhum dos dois
#
# Também cobra que a chave não vá na URL (só no header X-API-Key).
#
#   ./tests/key_storage_repro.sh             # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/key_storage_repro.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:7860}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"

if [ ! -x "$PY" ]; then PY="$(command -v python3)"; fi
if ! curl -sf -m 5 -o /dev/null "$BASE/"; then
  echo "✖ app não respondeu em $BASE — rode ./run.sh primeiro" >&2
  exit 1
fi

"$PY" - "$BASE" <<'PYEOF'
import sys
import time
from playwright.sync_api import sync_playwright

BASE = sys.argv[1]
CHAVE = "chave-de-teste-1234567890"          # >10 chars: exercita a máscara
MASCARA = CHAVE[:4] + "…" + CHAVE[-4:]
# legado no disco ANTES do script do app rodar; a flag fica no localStorage
# para a 2ª aba não semear de novo (sessionStorage é por aba e re-semearia)
SEMENTE = ("if (!localStorage.getItem('__seed')) {"
           "  localStorage.setItem('ttsRodKey', 'chave-legada');"
           "  localStorage.setItem('__seed', '1'); }")
LEGADO = "chave-legada"

falhas = []


def check(cond, msg):
    if not cond:
        falhas.append(msg)


def ler(pg):
    return pg.evaluate("""() => ({
        local: localStorage.getItem('ttsStudioKey'),
        sessao: sessionStorage.getItem('ttsStudioKey'),
        legado: localStorage.getItem('ttsRodKey'),
        modo: localStorage.getItem('ttsStudioKeyMode'),
        rotulo: (document.getElementById('clientApiKeyStatus') || {}).textContent || '',
        marcado: !!document.getElementById('clientApiKeySession')?.checked,
    })""")


def acesso(pg):
    pg.click('.nav-item[data-view="config"]')
    pg.click('.settings-tabs .stab[data-stab="acesso"]')
    pg.wait_for_selector('#clientApiKey', state="visible", timeout=10000)


def invariante(pg, esperado, etapa):
    """esperado: 'nenhum' | 'local' | 'sessao' — onde a chave tem de estar."""
    s = ler(pg)
    locais = [("local", s["local"]), ("sessao", s["sessao"]), ("legado", s["legado"])]
    ocupados = [n for n, v in locais if v]
    if esperado == "nenhum":
        check(not ocupados, f"{etapa}: storages deviam estar vazios, achei {ocupados}")
    else:
        check(ocupados == [esperado],
              f"{etapa}: a chave devia estar SÓ em {esperado}, achei {ocupados}")
    return s


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
    ctx = b.new_context()
    ctx.add_init_script(SEMENTE)             # legado já no disco, como num Mac usado
    erros, reqs = [], []
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: erros.append(str(e)))
    pg.on("request", lambda r: reqs.append((r.url, r.headers.get("x-api-key"))))
    ir(pg, BASE, wait_until="networkidle")
    acesso(pg)

    # 1. nasce com o legado lido da UI (pré-condição do teste)
    s = ler(pg)
    check(s["legado"] == LEGADO, f"pré-condição: ttsRodKey legado sumiu ({s['legado']!r})")
    check(pg.input_value("#clientApiKey") == LEGADO,
          "pré-condição: a UI não leu a chave legada do localStorage")
    check(s["local"] is None and s["sessao"] is None, "pré-condição: storage já tinha ttsStudioKey")
    check(not s["marcado"], "pré-condição: o modo devia nascer desmarcado (padrão = dispositivo)")
    check(s["rotulo"] == "Salva neste navegador: " + LEGADO[:4] + "…" + LEGADO[-4:],
          f"rótulo inicial (chave legada) inesperado: {s['rotulo']!r}")

    # 2. salva no modo padrão: um storage só, e o legado sai de cena
    pg.fill("#clientApiKey", CHAVE)
    pg.click("#clientApiKeySave")
    s = invariante(pg, "local", "após salvar (modo padrão)")
    check(s["legado"] is None, "ttsRodKey legado sobreviveu ao save (vai ressuscitar a chave)")
    check(s["rotulo"] == "Salva neste navegador: " + MASCARA,
          f"rótulo do modo dispositivo errado: {s['rotulo']!r}")

    # 3. marca "só nesta sessão": muda de storage e o rótulo junto
    pg.check("#clientApiKeySession")
    s = invariante(pg, "sessao", "após marcar 'só nesta sessão'")
    check(s["modo"] == "sessao", f"modo não persistiu: {s['modo']!r}")
    check(s["rotulo"] == "Nesta sessão: " + MASCARA,
          f"rótulo do modo sessão errado: {s['rotulo']!r}")

    # 4. outra ABA do mesmo contexto: não herda a chave (sessão é por aba),
    #    mas herda o modo — e com o modo em sessão nasce sem chave nenhuma
    pg2 = ctx.new_page()
    ir(pg2, BASE, wait_until="networkidle")
    s2 = ler(pg2)
    check(s2["sessao"] is None, "aba nova herdou a chave da sessão (sessionStorage é por aba)")
    check(s2["local"] is None, "aba nova viu ttsStudioKey no localStorage (modo sessão furado)")
    acesso(pg2)
    s2 = ler(pg2)
    check(s2["marcado"], "aba nova não herdou o MODO 'só nesta sessão'")
    check("Nenhuma chave" in s2["rotulo"],
          f"aba nova devia abrir sem chave, rótulo: {s2['rotulo']!r}")
    pg2.close()
    pg.bring_to_front()

    # 5. desmarca → volta pro localStorage; "Limpar" → nenhum storage
    pg.uncheck("#clientApiKeySession")
    s = invariante(pg, "local", "após desmarcar")
    check(s["rotulo"] == "Salva neste navegador: " + MASCARA,
          f"rótulo não voltou ao modo dispositivo: {s['rotulo']!r}")
    # legado "tardio": outra aba/versão antiga pode regravar ttsRodKey a qualquer
    # momento — limpar tem de levar o legado junto, senão ele ressuscita a chave
    # no próximo load (e o modo "só nesta sessão" vira mentira)
    pg.evaluate("() => localStorage.setItem('ttsRodKey', 'legado-tardio')")
    pg.click("#clientApiKeyClear")
    s = invariante(pg, "nenhum", "após 'Limpar'")
    check(s["legado"] is None, "ttsRodKey legado ressuscitou depois de limpar")
    check("Nenhuma chave" in s["rotulo"], f"rótulo depois de limpar: {s['rotulo']!r}")
    b.close()

# a chave nunca na URL; ela viaja no header X-API-Key
com_chave = [u for u, h in reqs if CHAVE in u]
check(not com_chave, f"chave apareceu na URL: {com_chave[:3]}")
check(any(h == CHAVE for _, h in reqs), "nenhuma requisição levou a chave no header X-API-Key")
if erros:
    falhas.append("erros de JS: " + "; ".join(erros[:3]))

print(f"chave de teste: {MASCARA}  |  requisições observadas: {len(reqs)}")
print()
if falhas:
    print("✖ FALHOU")
    for f in falhas:
        print("  ·", f)
    sys.exit(1)
print("✔ OK — a chave existe em exatamente um storage em cada modo, o rótulo acompanha, "
      "aba nova não herda a chave e nada vai na URL")
PYEOF