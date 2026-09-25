#!/usr/bin/env bash
# Conversa: retry curto no 429 de admissão do TTS (task #49).
#
# O servidor recusa geração acima de TTS_JOBS_ACTIVE_MAX com 429 + Retry-After.
# A Conversa tem que repetir AQUELE bloco uma vez, esperando o tempo pedido, sem
# atropelar barge-in. Duas fases, no app real:
#   A. a função de retry, determinística: repete 1× em 429, não repete em outro
#      erro, respeita Retry-After, e aborta a espera no barge-in (não segura o
#      turno pelos 5 s) — inclusive já abortado antes de tentar.
#   B. o encanamento: `cvSintetiza` (o caminho da fala) realmente repete quando a
#      rota responde 429, e o status avisa que está esperando.
#
#   ./tests/conversa_429.sh          # app precisa estar de pé (./run.sh)
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:7860}"
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv-mlx/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

if ! curl -sf -m 5 -o /dev/null "$BASE/"; then
  echo "✖ app não respondeu em $BASE — rode ./run.sh primeiro" >&2
  exit 1
fi

"$PY" - "$BASE" <<'PYEOF'
import sys
import time
from playwright.sync_api import sync_playwright

base = sys.argv[1]
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
    pg = b.new_page()
    erros = []
    pg.on("pageerror", lambda e: erros.append(str(e)))
    ir(pg, base, wait_until="networkidle")
    pg.wait_for_function("() => typeof cvTtsComRetry === 'function'", timeout=15000)

    # ── A. a lógica de retry, isolada ────────────────────────────────────
    r = pg.evaluate("""async () => {
      const falha = (status, retryAfter) => {
        const e = new Error("Limite de jobs em andamento atingido (20/20)");
        e.status = status;
        if (retryAfter !== undefined) e.retryAfter = retryAfter;
        return e;
      };
      const out = {};

      // 1) 429 na 1ª, sucesso na 2ª → repete exatamente 1×
      let n = 0, t0 = performance.now();
      out.um429 = await (async () => {
        const t = Math.round(performance.now() - t0);
        try {
          const v = await cvTtsComRetry(async () => { if (++n === 1) throw falha(429, 1); return "blob"; });
          return { valor: v, chamadas: n, ms: Math.round(performance.now() - t0) };
        } catch (e) { return { valor: "ERRO: " + e.message, chamadas: n, ms: Math.round(performance.now() - t0), t }; }
      })();

      // 2) outro erro (500) → não repete
      n = 0;
      try { await cvTtsComRetry(async () => { n++; throw falha(500); }); } catch (e) { out.erro500 = { chamadas: n, status: e.status }; }

      // 3) 429 duas vezes → desiste depois de 2 tentativas, erro legível
      n = 0;
      try { await cvTtsComRetry(async () => { n++; throw falha(429, 1); }); }
      catch (e) { out.dois429 = { chamadas: n, msg: e.message, status: e.status }; }

      // 4) barge-in DURANTE a espera: aborta, não chama de novo e não espera os 5 s
      n = 0; t0 = performance.now();
      const ac = new AbortController();
      const p = cvTtsComRetry(async () => { n++; throw falha(429, 5); }, ac.signal);
      setTimeout(() => ac.abort(), 200);
      try { await p; out.abortNoMeio = { chamadas: n, erro: "resolveu" }; }
      catch (e) { out.abortNoMeio = { chamadas: n, erro: e.name, ms: Math.round(performance.now() - t0) }; }

      // 5) signal já abortado antes de tentar → nem tenta
      const ac2 = new AbortController(); ac2.abort();
      n = 0;
      try { await cvTtsComRetry(async () => { n++; throw falha(429, 5); }, ac2.signal); }
      catch (e) { out.jaAbortado = { chamadas: n, erro: e.name }; }

      return out;
    }""")

    cobrar(r["um429"]["valor"] == "blob" and r["um429"]["chamadas"] == 2,
           f"429 deveria repetir 1× e resolver, veio {r['um429']}")
    cobrar(r["um429"]["ms"] >= 900, f"deveria esperar o Retry-After (1 s), esperou {r['um429']['ms']}ms")
    cobrar(r.get("erro500", {}).get("chamadas") == 1,
           f"erro 500 não deveria repetir: {r.get('erro500')}")
    cobrar(r.get("dois429", {}).get("chamadas") == 2, f"429 duplo: 2 tentativas, veio {r.get('dois429')}")
    cobrar("Limite de jobs" in (r.get("dois429", {}).get("msg") or ""),
           f"a mensagem do servidor tem que sobreviver: {r.get('dois429')}")
    cobrar(r["abortNoMeio"]["chamadas"] == 1 and r["abortNoMeio"]["erro"] == "AbortError",
           f"barge-in na espera deveria abortar sem nova tentativa: {r['abortNoMeio']}")
    cobrar(r["abortNoMeio"]["ms"] < 3000,
           f"abortar não pode esperar os 5 s inteiros: {r['abortNoMeio']['ms']}ms")
    # signal já abortado: não vale gastar uma segunda síntese num turno morto.
    # (o erro que sobe é o original do 429, não AbortError — o cvSpeak classifica
    #  como "cortado" pelo !vivo() de qualquer forma)
    cobrar(r["jaAbortado"]["chamadas"] == 1,
           f"signal já abortado não pode repetir: {r['jaAbortado']}")

    print(f"  A. retry: 429→1 repetição e resolve ({r['um429']['ms']}ms) · 500 não repete · 429 duplo desiste · "
          f"barge-in aborta em {r['abortNoMeio']['ms']}ms · já-abortado não tenta")

    # ── B. o encanamento em cvSintetiza ─────────────────────────────────
    pg.evaluate("""() => {
      window.__pedidos = 0;
      window.__wav = new Blob([new Uint8Array([82,73,70,70])], { type: "audio/wav" });
    }""")
    chamadas = []

    def rota(route):
        chamadas.append(1)
        if len(chamadas) == 1:
            route.fulfill(status=429, headers={"Content-Type": "application/json", "Retry-After": "1"},
                          body='{"detail":"Limite de jobs em andamento atingido (20/20) — aguarde um job terminar e tente de novo"}')
        else:
            route.fulfill(status=200, headers={"Content-Type": "audio/wav"}, body="RIFF")

    pg.route("**/v1/audio/speech", rota)
    res = pg.evaluate("""async () => {
      const st = [];
      const orig = cvStatus;
      cvStatus = t => { st.push(t || ""); };
      try {
        const blob = await cvSintetiza("Frase de teste.", null, () => true);
        return { ok: true, tamanho: blob.size, status: st };
      } catch (e) { return { ok: false, erro: e.message, status: st }; }
      finally { cvStatus = orig; }
    }""")

    cobrar(res.get("ok"), f"cvSintetiza deveria ter sucesso após o retry: {res}")
    cobrar(len(chamadas) == 2, f"esperava 2 requisições (429 + repetição), veio {len(chamadas)}")
    cobrar(any("ocupado" in t for t in res.get("status", [])),
           f"o status deveria avisar que está esperando: {res.get('status')}")
    print(f"  B. encanamento: {len(chamadas)} requisições (429 + retry) · status avisou · blob {res.get('tamanho')}b")

    cobrar(not erros, "erros de JS: " + "; ".join(erros[:3]))
    b.close()

print()
if falhas:
    print("✖ FALHOU")
    for f in falhas:
        print("  ·", f)
    sys.exit(1)
print("✔ OK — 429 de admissão repete uma vez com o Retry-After do servidor, sem atropelar barge-in")
PYEOF