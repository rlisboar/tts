#!/usr/bin/env python3
"""Mede o classificador de intenção (#507): acerto vs regras, latência e memória.

Uso (é o passo do gate #510 — sai !=0 quando reprova):

    ./.venv-mlx/bin/python tests/mede_intencao.py                 # completo
    ./.venv-mlx/bin/python tests/mede_intencao.py --sem-tts       # sem a parte do TTS
    ./.venv-mlx/bin/python tests/mede_intencao.py --minimo 0.9    # barra mais dura

`--sem-tts` pula a medição de latência com o TTS gerando ao mesmo tempo e o
efeito no 1º trecho: nesse modo o veredito usa só o acerto (mais rápido, menos
fiel). Sem ele a barra é a do card: ≥85 % de acerto, modelo > regra, p95 ≤400 ms.

O corpus é versionado (`tests/fixtures/intencao_corpus.jsonl`, 70 frases ambíguas
pt/en rotuladas à mão). A comparação é com a replica LOCAL da regra do #501
(as listas de comando/backchannel daqui) — este arquivo NÃO importa a
`classifica_barge` real; o número sai ao lado da origem, para não fingir
comparação.

Latência: 40 decisões com o modelo quente, sem e COM TTS gerando ao mesmo tempo
(um job real em thread). Memória: PICO de RSS do filho (`ps -o rss=`, amostrado
durante a rodada) — é o que a máquina paga; o RSS medido depois da carga cai por
devolução de página do macOS e não serve de medida.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from _estado_tmp import isola  # noqa: E402

isola()            # #732: estado em tmp ANTES de qualquer `import app`

import live_intencao  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "fixtures" / "intencao_corpus.jsonl"

# Cadeia SÓ de regras do #501, replicada aqui só como BASELINE quando o módulo
# real não estiver no tree. `interrupcao` para comando/pergunta/frase longa.
COMANDOS = {"para", "pare", "espera", "peraí", "pera", "calma", "chega", "não",
            "stop", "wait", "hold", "cancel", "volta", "corrige", "deixa"}
BACKCHANNEL = {"uhum", "aham", "hum", "sim", "sei", "ok", "tá", "certo", "entendi",
               "isso", "claro", "beleza", "legal", "nossa", "mhm", "yeah", "right",
               "okay", "sure", "mhm", "mm-hmm"}
INTERROGATIVAS = {"qual", "quais", "quando", "quanto", "quantos", "como", "quem",
                  "onde", "porque", "what", "when", "how", "who", "where", "why"}


def regra_decisao(texto: str) -> str:
    """Baseline de regras: corta com comando/pergunta/frase longa; senão continua."""
    import re

    palavras = [p for p in re.findall(r"[\wÀ-ÿ'-]+", (texto or "").lower())]
    if not palavras:
        return "continuar"
    if any(p in COMANDOS for p in palavras):
        return "cortar"
    if "?" in texto or any(p in INTERROGATIVAS for p in palavras):
        return "cortar"
    if len(palavras) >= 3 and not all(p in BACKCHANNEL for p in palavras):
        return "cortar"
    return "continuar"


def _rss_mb(pid: int) -> float:
    r = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return round(int((r.stdout or "0").strip() or 0) / 1024.0, 1)


def _args():
    ap = argparse.ArgumentParser(description="acerto, latência e memória do classificador de intenção")
    ap.add_argument("--minimo", type=float, default=0.85,
                    help="acerto mínimo para o veredito ser OK (padrão 0,85, o do card)")
    ap.add_argument("--max-p95", type=int, default=400,
                    help="p95 de latência máximo em ms (padrão 400, o do card)")
    ap.add_argument("--sem-tts", dest="sem_tts", action="store_true",
                    help="pula a parte do TTS concorrente (veredito só pelo acerto)")
    ap.add_argument("--prazo-ms", type=int, default=400,
                    help="prazo do filtro modelo (o timeout_ms do settings). O dsh "
                         "paga o barramento ACP: com 400 ms ~1/4 dos trechos estoura")
    ap.add_argument("--provedor", choices=("local", "dsh"), default="local",
                    help="local = classificador no worker; dsh = o PROMPT do dsh pelo "
                         "chat (#596). O número do dsh é o que decide se ele segue "
                         "na oferta — a acurácia do local não vale para ele")
    return ap.parse_args()


def _mede_dsh(casos: list, a) -> int:
    """Acerto/latência pelo `provedor: dsh` (o prompt do dsh, com prazo real).

    `_live_modelo_dsh` devolve (tipo, p) — `interrupcao` = cortar. Timeout e erro
    contam como NÃO decidido (a cadeia cai no próximo filtro), e por isso entram
    no relatório separados do erro de classificação."""
    import app

    acertos_regra = sum(1 for c in casos if regra_decisao(c["fala"]) == c["esperado"])
    print(f"regras (baseline local): {acertos_regra}/{len(casos)} = "
          f"{100 * acertos_regra / len(casos):.1f}%")
    app._settings["live_barge_filtros"] = {"modelo": {
        "on": True, "provedor": "dsh", "limiar": 0.5, "timeout_ms": a.prazo_ms}}
    sess = {"id": "mede", "barge_stats": {"tipos": {}, "decisoes": [], "modelo": []}}
    acertos, sem_decisao, errados, ms = 0, 0, [], []
    for c in casos:
        t0 = time.monotonic()
        tipo, _p = app._live_modelo_dsh(sess, c["fala"], c["ref"], {"timeout_ms": a.prazo_ms})
        ms.append(int((time.monotonic() - t0) * 1000))
        if tipo is None:
            sem_decisao += 1
            continue
        if (tipo == "interrupcao") == (c["esperado"] == "cortar"):
            acertos += 1
        else:
            errados.append(f"{c['fala']!r} -> {tipo} (esperado {c['esperado']})")
    decididos = len(casos) - sem_decisao
    pct = 100.0 * acertos / len(casos)
    ms.sort()
    print(f"dsh: {acertos}/{len(casos)} = {pct:.1f}% · sem decisão (timeout/erro): "
          f"{sem_decisao} · erro de classificação: {len(errados)} de {decididos} decididos")
    for e in errados[:8]:
        print("   ✘", e)
    if ms:
        print(f"latência: p50 {statistics.median(ms):.0f} ms | "
              f"p95 {ms[int(len(ms) * 0.95) - 1]} ms | máx {ms[-1]} ms "
              f"(prazo {a.prazo_ms} ms; estouro não segura o turno)")
    print(f"timeouts contados: {sess['barge_stats'].get('modelo_timeouts', 0)}")
    ok = pct >= 100 * a.minimo
    print("\nVEREDITO:", "OK" if ok else "REPROVADO",
          f"(piso {100 * a.minimo:.0f}% de acerto — o mesmo do local)")
    return 0 if ok else 1


def main() -> int:
    a = _args()
    casos = [json.loads(l) for l in CORPUS.read_text().splitlines() if l.strip()]
    print(f"corpus: {len(casos)} frases "
          f"({sum(1 for c in casos if c['esperado'] == 'cortar')} cortar / "
          f"{sum(1 for c in casos if c['esperado'] == 'continuar')} continuar)\n")

    acertos_regra = sum(1 for c in casos if regra_decisao(c["fala"]) == c["esperado"])
    print(f"regras (baseline local): {acertos_regra}/{len(casos)} = "
          f"{100 * acertos_regra / len(casos):.1f}%")

    if a.provedor == "dsh":
        return _mede_dsh(casos, a)

    inc = live_intencao.Intencao()
    if not inc.aquece():
        print("worker não subiu:", inc.erro)
        return 1
    pid = inc._proc.pid
    # PICO (#595): amostra em thread, porque o RSS medido depois da carga já caiu
    # por devolução de página e o texto antigo chamava esse valor de "vazio".
    rss_pico = {"mb": 0.0}
    _para_rss = threading.Event()

    def _amostra_rss():
        while not _para_rss.is_set():
            rss_pico["mb"] = max(rss_pico["mb"], _rss_mb(pid))
            _para_rss.wait(0.2)

    threading.Thread(target=_amostra_rss, daemon=True).start()
    try:
        acertos, erros = 0, []
        for c in casos:
            r = inc.decide(c["ref"], c["fala"], c["idioma"])
            if r is None:
                erros.append(c["fala"])
                continue
            if r["decisao"] == c["esperado"]:
                acertos += 1
        print(f"modelo: {acertos}/{len(casos)} = {100 * acertos / len(casos):.1f}%"
              f" (falhas de decisão: {len(erros)})")

        ms = []
        for c in casos[:40]:
            r = inc.decide(c["ref"], c["fala"], c["idioma"])
            if r:
                ms.append(r["ms"])
        ms.sort()
        print(f"\nlatência sem TTS: p50 {statistics.median(ms):.0f} ms | "
              f"p95 {ms[int(len(ms) * 0.95) - 1]} ms (n={len(ms)})")

        # com TTS GERANDO ao mesmo tempo (modelo local de verdade, outra thread)
        import app

        parar = threading.Event()
        gerou = {"n": 0}
        erros_tts = []

        try:
            modelo_tts = app._get_model()
        except Exception as exc:  # noqa: BLE001
            modelo_tts = None
            erros_tts.append(f"{type(exc).__name__}: {exc}")
        sr = 24000

        def _tts():
            if modelo_tts is None:
                return
            while not parar.is_set():
                try:
                    app._generate_chunk(modelo_tts, "uma frase de teste para ocupar o Metal",
                                        "pt", None, None, {"num_steps": 6}, sr=sr)
                    gerou["n"] += 1
                except Exception as exc:  # noqa: BLE001
                    erros_tts.append(f"{type(exc).__name__}: {exc}")
                    break

        t = threading.Thread(target=_tts, daemon=True)
        t.start()
        for _ in range(300):
            if gerou["n"] or erros_tts:
                break
            time.sleep(0.1)
        ms2 = []
        for c in casos[:40]:
            r = inc.decide(c["ref"], c["fala"], c["idioma"])
            if r:
                ms2.append(r["ms"])
        parar.set()
        t.join(30)
        print(f"(gerações concorrentes durante a medição: {gerou['n']}"
              + (f", erros: {erros_tts[:1]}" if erros_tts else "") + ")")
        ms2.sort()
        if ms2:
            print(f"latência com TTS:  p50 {statistics.median(ms2):.0f} ms | "
                  f"p95 {ms2[int(len(ms2) * 0.95) - 1]} ms (n={len(ms2)})")
        else:
            print("latência com TTS: nenhuma decisão voltou (todas estouraram o prazo)")
        # efeito no TTS do turno: o 1º trecho é o que o usuário espera
        def _primeiro_trecho_ms(n=5) -> float:
            import statistics as st

            amostras = []
            for _ in range(n):
                t0 = time.perf_counter()
                app._generate_chunk(modelo_tts, "primeiro trecho curto", "pt", None, None,
                                    {"num_steps": 6}, sr=sr)
                amostras.append((time.perf_counter() - t0) * 1000)
            return st.median(amostras)

        _para_rss.set()
        rss_quente = rss_pico["mb"] or _rss_mb(pid)
        com_worker = _primeiro_trecho_ms()
        inc.fecha()
        time.sleep(0.5)
        sem_worker = _primeiro_trecho_ms()
        delta = 100 * (com_worker - sem_worker) / sem_worker
        print(f"\n1º trecho do TTS: {com_worker:.0f} ms com o worker | "
              f"{sem_worker:.0f} ms sem | {delta:+.1f}%")
        print(f"RSS de PICO do worker: {rss_quente} MB — ~2 GB de modelo, o preço "
              f"de rodar LLM local (o valor pós-carga cai por devolução de página)")
        print(f"acerto modelo {100 * acertos / len(casos):.1f}% x regra "
              f"{100 * acertos_regra / len(casos):.1f}%")
        ok = (acertos / len(casos) >= a.minimo and acertos > acertos_regra
              and (a.sem_tts or (ms2 and ms2[int(len(ms2) * 0.95) - 1] <= a.max_p95)))
        print("\nVEREDITO:", "OK" if ok else "REPROVADO")
        return 0 if ok else 1
    finally:
        inc.fecha()


if __name__ == "__main__":
    sys.exit(main())
