#!/usr/bin/env bash
# Regressão do sink `innerHTML` da lista de vozes (static/index.html).
#
# Prova, contra o app REAL (sem stub), que um agente hostil guardado no backup
# de vozes não executa script na UI:
#   1. monta 2 .json de voz com HTML no campo `id` (e no `created_at`)
#   2. importa pelo próprio endpoint /api/voices/import (feature do produto)
#   3. além dos dois payloads do import, escreve À MÃO um terceiro meta com id
#      hostil: o #37 alinha o id importado ao nome do arquivo, então sem ele o
#      sink `esc(v.id)` ficava latente (o teste passava mesmo revertendo)
#   4. abre a UI no Chromium headless e cobra o DOM
#
# Modelo pós-#76: payload injetado vira ELEMENTO mas não executa (handler em
# atributo exige 'unsafe-inline'; o script-src virou nonce por resposta).
#
# Os DOIS ramos da lista são cobrados, porque o bug original existia só em um
# deles (o ramo preset/virtual já escapava antes) — reverter o `esc()` de um
# ramo sozinho tem de derrubar este script:
#   · ramo de voz real: .json + .wav  (id/created_at com HTML)
#   · ramo preset/virtual: .json sem .wav e `preset: true` (id com HTML)
# O `created_at` também é cobrado: `<b>2024-01-01</b>` tem de continuar sendo
# texto, não virar elemento.
#
# A execução do conteúdo injetado depende da POLICY SERVIDA (o teste a lê e
# deriva): com nonce, handler em atributo é barrado (espera 0 + violação); sem
# nonce e com unsafe-inline, ele roda (espera 1). Fora da CSP, sempre executa.
#
# Falha se algum `window.__xss*` != 0 fora do esperado (payload executou), se o <code class="vid">
# ainda for um elemento em vez de texto, se o card de um ramo sumir da lista ou
# se o `created_at` virar elemento. Também reproduz o sink ANTIGO inline
# (`innerHTML` cru) para provar que os payloads são mesmo XSS — sem isso, "não
# executou" poderia ser só um payload fraco.
#
#   ./tests/xss_frontend_repro.sh            # app precisa estar de pé em :7860
#   BASE=http://127.0.0.1:7860 ./tests/xss_frontend_repro.sh
#   ./tests/xss_frontend_repro.sh --cleanup  # remove as vozes de teste no fim
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:7860}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
# sufixo por execução: duas rodadas simultâneas usavam os MESMOS nomes e a
# limpeza de uma apagava os arquivos da outra (falso vermelho, não regressão).
SUF="$$"
# Restos de rodadas que não limparam acumulariam para sempre (agora que a limpeza
# é por sufixo). Varre o que é ANTIGO por mtime: uma rodada viva criou há
# segundos, então não é tocada — a distinção é o tempo, não o nome.
# `|| true` porque a suíte roda com `set -e`: qualquer falha do find (permissão,
# sandbox) derrubaria a rodada inteira antes de medir. Com `-delete` e nenhum
# match o find já sai 0 — o `|| true` é rede, não remendo de exit code.
find "$RAIZ/voices" -maxdepth 1 \( -name 'xssprobe*' -o -name 'xssvirt*' -o -name 'xssmanual*' \) \
  -mmin +2 -delete 2>/dev/null || true
STEM="xssprobe-$SUF"
STEM_V="xssvirt-$SUF"
STEM_M="xssmanual-$SUF"   # meta escrito À MÃO: id hostil que não passa pelo import
NOME_M="probe meta html-$SUF"
LIMPAR=0
[ "${1:-}" = "--cleanup" ] && LIMPAR=1

command -v curl >/dev/null || { echo "curl não encontrado"; exit 1; }
if [ ! -x "$PY" ]; then PY="$(command -v python3)"; fi

if ! curl -sf -m 5 -o /dev/null "$BASE/"; then
  echo "✖ app não respondeu em $BASE — rode ./run.sh primeiro" >&2
  exit 1
fi

# mktemp em /var/folders pode falhar sob sandbox (workspace-write) — nesse caso
# o diretório temporário vai para dentro do próprio repo.
TMP="$(mktemp -d -t ttsxss.XXXXXX 2>/dev/null || true)"
if [ -z "$TMP" ] || [ ! -d "$TMP" ]; then
  TMP="$RAIZ/.xss-repro-$$"
  mkdir -p "$TMP"
fi
trap 'rm -rf "$TMP"' EXIT

# ── 1. payload ────────────────────────────────────────────────────────────
"$PY" - "$TMP" "$STEM" "$STEM_V" "$STEM_M" "$RAIZ" "$NOME_M" <<'PYEOF'
import json, sys, wave, zipfile
from pathlib import Path

tmp, stem, stem_v, stem_m, raiz = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], Path(sys.argv[5])
with wave.open(str(tmp / f"{stem}.wav"), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 8000)          # 0.5 s — o import não checa duração

payload = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
(tmp / f"{stem}.json").write_text(json.dumps({
    "id": payload,                       # o import ALINHA o id ao nome do arquivo (#37)
    "name": payload,                     # ...por isso o payload vive também no nome
    "created_at": "<b>2024-01-01</b>",
    "duration": 0.5,
}))
# segundo alvo: ramo preset/virtual (entra pelo mesmo import; json órfão sem wav
# + preset:true = a UI renderiza o card "virtual", que é outro sink)
payload_v = '<img src=x onerror="window.__xssV=(window.__xssV||0)+1">'
(tmp / f"{stem_v}.json").write_text(json.dumps({
    "id": payload_v,
    "name": payload_v,
    "preset": True,
    "created_at": "<b>2024-02-02</b>",
    "duration": 0,
}))
with zipfile.ZipFile(tmp / "payload.zip", "w") as z:
    z.write(tmp / f"{stem}.json", f"{stem}.json")
    z.write(tmp / f"{stem}.wav", f"{stem}.wav")
    z.write(tmp / f"{stem_v}.json", f"{stem_v}.json")
# terceiro alvo: id hostil que SOBREVIVE, porque não passa pelo import. O #37
#
# EFEITO ESPERADO (não é bug): `list_voices()` devolve este id hostil — é o que
# chega ao card e o que `esc(v.id)` tem de tratar —, a GERAÇÃO funciona
# (`_voice_path` tolera id exótico dentro de voices/), mas `/audio` e `/peaks`
# respondem 404 porque o `_safe_id` da rota é estrito. Ou seja: aqui é só ESCAPE
# na UI; os dois 404 no console são esperados. (Para o meta manual também
# funcionar nas rotas seria preciso normalizar na LEITURA, aí o id divergiria do
# nome do arquivo — decisão de outra linha.)
# alinha o id importado ao nome do arquivo, o que deixava `esc(v.id)` latente
# (o teste passava mesmo revertendo o escape). Meta escrito à mão = caso real
# (voz antiga/feita na mão, que o #33 tolera).
payload_m = '<img src=x onerror="window.__xssM=(window.__xssM||0)+1">'
(raiz / f"voices/{stem_m}.json").write_text(json.dumps({
    "id": payload_m,
    "name": sys.argv[6],
    "created_at": "2024-03-03 03:03:03",
    "duration": 0.5,
}))
print("payload (ramo real)   :", payload)
print("payload (ramo virtual):", payload_v)
print("payload (meta à mão)  :", payload_m)
PYEOF

# ── 2. importa pelo endpoint do produto ───────────────────────────────────
echo
echo "→ POST $BASE/api/voices/import"
curl -sf -X POST -F "zip_file=@$TMP/payload.zip" "$BASE/api/voices/import"
echo

# ── 3. cobra o DOM no navegador real ──────────────────────────────────────
echo
"$PY" - "$BASE" "$STEM" "$STEM_V" "$STEM_M" "$NOME_M" <<'PYEOF'
import sys
import time
from playwright.sync_api import sync_playwright

BASE, STEM, STEM_V, STEM_M, NOME_M = sys.argv[1:6]
PAYLOAD = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
PAYLOAD_V = '<img src=x onerror="window.__xssV=(window.__xssV||0)+1">'
# mesma forma, contador próprio: prova que o payload é XSS sem poluir __xss/__xssV
NAIVE = '<img src=x onerror="window.__xssNaive=(window.__xssNaive||0)+1">'
NAIVE_V = '<img src=x onerror="window.__xssNaiveV=(window.__xssNaiveV||0)+1">'
payload_m = '<img src=x onerror="window.__xssM=(window.__xssM||0)+1">'

# A expectativa do handler inline é DERIVADA da policy servida, não escrita no
# teste: se a policy mudar de forma por motivo legítimo (tirar o nonce, voltar
# unsafe-inline, entrar unsafe-hashes), o script acompanha em vez de acusar
# "payload fraco". Regra do CSP: com nonce/hash de ELEMENTO na lista,
# 'unsafe-inline' é IGNORADO — então handler em atributo só roda quando NÃO há
# nonce e HÁ unsafe-inline.
#
# NÃO coberto de propósito: 'unsafe-hashes' com um hash que case ESTE handler —
# aí ele rodaria sem unsafe-inline e a expectativa (barrado) ficaria errada.
# Se alguém adotar, o teste precisa saber o hash do texto de cada handler; até
# hoje nenhuma policy do projeto usou.
import urllib.request
_pol = urllib.request.urlopen(BASE).headers.get("Content-Security-Policy", "")
_ss = next((d.strip() for d in _pol.split(";") if d.strip().startswith("script-src")), "")
handler_barrado = ("nonce-" in _ss) or ("'unsafe-inline'" not in _ss)
print(f"script-src servido: {_ss[:70]}…")
print(f"  -> handler inline {'BARRADO (espero 0 execuções + violação)' if handler_barrado else 'PERMITIDO (espero 1 execução, sem violação)'}")

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
    pg = b.new_page()
    erros, consolo = [], []
    pg.on("pageerror", lambda e: erros.append(str(e)))
    pg.on("console", lambda m: consolo.append(m.text))
    ir(pg, BASE, wait_until="networkidle")
    # a lista vive na tela "vozes", que nasce escondida (display:none) — attached
    pg.wait_for_selector("#voiceList .vid", state="attached", timeout=15000)

    # os dois cards do payload têm de existir: cada ramo é um sink diferente.
    # O card é achado pelo NOME (não pelo .vid) — com o bug o `.vid` vira um
    # <img> com textContent vazio, e aí um matcher pelo texto não acharia nada.
    def card(stem, payload):
        return pg.evaluate("""([stem, payload]) => {
            const item = [...document.querySelectorAll('#voiceList .item')]
                           .find(it => { const v = it.querySelector('.vid');
                                         return v && v.textContent === stem; });
            if (!item) return null;
            const el = item.querySelector('.vid');
            return { tem_vid: !!el,
                     id_igual_ao_stem: el ? el.textContent === stem : null,
                     payload_no_texto: item.textContent.includes(payload),
                     virou_elemento: !!item.querySelector('img'),
                     b_no_item: item.querySelectorAll('b').length,
                     texto_do_item: item.textContent };
        }""", [stem, payload])

    achado = card(STEM, PAYLOAD)
    achado_v = card(STEM_V, PAYLOAD_V)

    # repro do sink ANTIGO (innerHTML cru) para provar que os payloads são XSS
    pg.evaluate("""([a, b]) => {
        const d = document.createElement('div');
        d.className = 'naive-sink';
        d.innerHTML = '<code class="vid">' + a + '</code><code class="vid">' + b + '</code>';
        document.body.appendChild(d);
    }""", [NAIVE, NAIVE_V])
    pg.wait_for_timeout(600)
    # pós-#76 o handler em ATRIBUTO não executa (unsafe-inline saiu, virou nonce):
    # o que discrimina é o DOM — escapado = texto, não escapado = elemento.
    naive_img = pg.evaluate("() => document.querySelectorAll('.naive-sink img').length")
    xss = pg.evaluate("() => window.__xss || 0")
    # id hostil que SOBREVIVEU ao import: escrito à mão em voices/, então o #37
    # (que alinha id importado ao nome do arquivo) não o normaliza. É este card
    # que mantém `esc(v.id)` exercitado — sem ele o teste passava mesmo revertendo.
    manual = pg.evaluate("""([payload, nome]) => {
        const item = [...document.querySelectorAll('#voiceList .item')]
                       .find(it => it.textContent.includes(nome));
        if (!item) return null;
        const el = item.querySelector('.vid');
        return { tem_vid: !!el, texto: el ? el.textContent : null,
                 virou_elemento: !!item.querySelector('img') };
    }""", [payload_m, NOME_M])
    xss_m = pg.evaluate("() => window.__xssM || 0")
    xss_v = pg.evaluate("() => window.__xssV || 0")
    naive = pg.evaluate("() => window.__xssNaive || 0")
    naive_v = pg.evaluate("() => window.__xssNaiveV || 0")
    # a violação tem de ser REPORTADA (não só "0 execuções"): é o que distingue
    # "a CSP bloqueou" de "o handler estava quebrado".
    viol_inline = [t for t in consolo if "inline event handler" in t]

    # PROVA DE VALIDADE do payload, fora da página com CSP: em about:blank não há
    # header, então o mesmo markup TEM de executar. Sem isto o teste diria só
    # "injeta elemento" e perderia o "é XSS de verdade" (propósito do controle).
    ir(pg, "about:blank")
    pg.evaluate("""([a, b]) => {
        const d = document.createElement('div');
        d.innerHTML = '<code class="vid">' + a + '</code><code class="vid">' + b + '</code>';
        document.body.appendChild(d);
    }""", [NAIVE, NAIVE_V])
    pg.wait_for_timeout(600)
    naive_livre = pg.evaluate("() => window.__xssNaive || 0")
    b.close()

falhas = []
if naive_img != 2:
    falhas.append(f"o sink antigo não produziu elemento (img={naive_img}) — payload fraco")
if naive_livre != 1:
    falhas.append(f"fora da CSP o payload TEM de executar (é a prova de que é XSS): {naive_livre}")
if handler_barrado:
    if not viol_inline:
        falhas.append("CSP com nonce: o bloqueio do handler TEM de ser reportado (silencioso não serve)")
    if naive != 0 or naive_v != 0:
        falhas.append(f"CSP com nonce: handler não devia executar (naive={naive}/v={naive_v})")
else:
    if viol_inline:
        falhas.append(f"CSP sem nonce e com unsafe-inline: não devia haver violação ({viol_inline[:1]})")
    if naive != 1 or naive_v != 1:
        falhas.append(f"CSP sem nonce: handler DEVIA executar (naive={naive}/v={naive_v})")
if achado is None:
    falhas.append("card do ramo de voz REAL não está na lista (sink não exercitado)")
if achado_v is None:
    falhas.append("card do ramo PRESET/VIRTUAL não está na lista (sink não exercitado)")
if xss != 0:
    falhas.append(f"XSS vivo no ramo de voz real: __xss={xss}")
if xss_v != 0:
    falhas.append(f"XSS vivo no ramo preset/virtual: __xssV={xss_v}")
for nome, achado_ramo, esperado in (("real", achado, PAYLOAD), ("virtual", achado_v, PAYLOAD_V)):
    if not achado_ramo:
        continue
    if not achado_ramo["tem_vid"]:
        falhas.append(f"ramo {nome}: o .vid sumiu do card")
    elif not achado_ramo["id_igual_ao_stem"]:
        falhas.append(f"ramo {nome}: o import devia alinhar o id ao nome do arquivo (#37)")
    if not achado_ramo["payload_no_texto"]:
        falhas.append(f"ramo {nome}: `name` com HTML não aparece como TEXTO no card")
    if achado_ramo["virou_elemento"]:
        falhas.append(f"ramo {nome}: payload executou (virou elemento <img>, não texto)")
if manual is None:
    falhas.append("card do meta à mão não está na lista (sink `esc(v.id)` não exercitado)")
else:
    if not manual["tem_vid"] or manual["texto"] != payload_m:
        falhas.append(f"meta à mão: id hostil não saiu como TEXTO ({manual['texto']!r})")
    if manual["virou_elemento"]:
        falhas.append("meta à mão: id hostil virou elemento — `esc(v.id)` caiu")
if xss_m != 0:
    falhas.append("meta à mão: payload do id EXECUTOU (sink de id aberto)")

if achado and achado["b_no_item"] != 0:
    falhas.append("ramo real: `created_at` virou elemento <b> (escape sumiu do sink)")
if achado and "<b>2024-01-01</b>" not in achado["texto_do_item"]:
    falhas.append("ramo real: `created_at` não aparece como texto literal na ficha")
if erros:
    falhas.append("erros de JS: " + "; ".join(erros[:3]))

print(f"naive: {naive_img} elemento(s) no app | execucao no app {naive}/{naive_v} " +
      f"({0 if handler_barrado else 1} esperado) | fora da CSP {naive_livre} (1 = payload válido) | " +
      f"violacao reportada: {bool(viol_inline)} (esperado: {handler_barrado})")
print(f"ramo real    : {achado}")
print(f"ramo virtual : {achado_v}")
print(f"__xss (real)={xss}  __xssV (virtual)={xss_v}  "
      f"__xssNaive={naive}  __xssNaiveV={naive_v}")
print()
if falhas:
    print("✖ FALHOU")
    for f in falhas:
        print("  ·", f)
    sys.exit(1)
_ramo = ("sem executar no app (script-src com nonce)" if handler_barrado
         else "e EXECUTA no app (script-src com unsafe-inline)")
print("✔ OK — os três payloads saem como TEXTO (id importado, id de meta à mão, name, created_at); "
      f"o sink antigo produz ELEMENTO {_ramo} e EXECUTA fora da CSP (payload é XSS)")
PYEOF

if [ "$LIMPAR" = "1" ]; then
  rm -f "$RAIZ/voices/$STEM.json" "$RAIZ/voices/$STEM.wav" "$RAIZ/voices/$STEM_V.json" "$RAIZ/voices/$STEM_M.json"
  echo "· vozes de teste removidas de voices/"
else
  echo "· deixando voices/$STEM.{json,wav}, voices/$STEM_V.json e voices/$STEM_M.json (remova com --cleanup)"
fi