#!/usr/bin/env python3
"""Mede o classificador de intenção (#507): acerto vs regras, latência e memória.

Uso: `./.venv-mlx/bin/python tests/mede_intencao.py`

O corpus é versionado (`tests/fixtures/intencao_corpus.jsonl`, 70 frases ambíguas
pt/en rotuladas à mão). As regras de comparação são a cadeia SÓ de regras do
#501 (`classifica_barge`) quando ela existir; sem ela, este arquivo traz a
mesma lista de comandos/backchannels e mede a regra equivalente — o número sai
com a origem escrita ao lado, para não fingir comparação.

Latência: 40 decisões com o modelo quente, sem e COM TTS gerando ao mesmo tempo
(um job real em thread). Memória: RSS do filho, do `/proc`-equivalente do macOS
(`ps -o rss=`).
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

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


def main() -> int:
    casos = [json.loads(l) for l in CORPUS.read_text().splitlines() if l.strip()]
    print(f"corpus: {len(casos)} frases "
          f"({sum(1 for c in casos if c['esperado'] == 'cortar')} cortar / "
          f"{sum(1 for c in casos if c['esperado'] == 'continuar')} continuar)\n")

    acertos_regra = sum(1 for c in casos if regra_decisao(c["fala"]) == c["esperado"])
    print(f"regras (baseline local): {acertos_regra}/{len(casos)} = "
          f"{100 * acertos_regra / len(casos):.1f}%")

    inc = live_intencao.Intencao()
    if not inc.aquece():
        print("worker não subiu:", inc.erro)
        return 1
    pid = inc._proc.pid
    rss_vazio = _rss_mb(pid)
    try:
        acertos, erros, lat = 0, [], []
        for c in casos:
            r = inc.decide(c["ref"], c["fala"], c["idioma"])
            if r is None:
                erros.append(c["fala"])
                continue
            if r["decisao"] == c["esperado"]:
                acertos += 1
            else:
                lat.append(r["ms"])
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

        rss_quente = _rss_mb(pid)
        com_worker = _primeiro_trecho_ms()
        inc.fecha()
        time.sleep(0.5)
        sem_worker = _primeiro_trecho_ms()
        delta = 100 * (com_worker - sem_worker) / sem_worker
        print(f"\n1º trecho do TTS: {com_worker:.0f} ms com o worker | "
              f"{sem_worker:.0f} ms sem | {delta:+.1f}%")
        print(f"RSS do worker: {rss_quente} MB (vazio {rss_vazio} MB) "
              f"— ~2 GB de modelo, o preço de rodar LLM local")
        print(f"acerto modelo {100 * acertos / len(casos):.1f}% x regra "
              f"{100 * acertos_regra / len(casos):.1f}%")
        ok = (acertos / len(casos) >= 0.85 and acertos > acertos_regra
              and ms2 and ms2[int(len(ms2) * 0.95) - 1] <= 400)
        print("\nVEREDITO:", "OK" if ok else "REPROVADO")
        return 0 if ok else 1
    finally:
        inc.fecha()


if __name__ == "__main__":
    sys.exit(main())
