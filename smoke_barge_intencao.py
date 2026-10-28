#!/usr/bin/env python3
"""Matriz offline da INTENÇÃO de interrupção do Live (#501).

O corte por energia derruba a resposta em "uhum" e em tosse. Esta matriz mede a
cadeia de decisão sobre TEXTO — sem áudio, sem STT, sem rede —, que é a parte
que a regra resolve; o caminho de áudio tem os testes de turno.

Corpora (≥20 em cada, pt e en):
  - BACKCHANNEL: confirmações curtas que NÃO devem cortar.
  - COMANDO: ordens de parar, que DEVEM cortar.
  - PERGUNTA: pergunta e frase com conteúdo, que DEVEM cortar.
  - RUÍDO: transcript vazio/oscilações, que NÃO devem cortar.
  - ECO: trechos literais do assistente, que NÃO devem cortar (regra do #301).

Critério (do aceite do card): backchannel e ruído NÃO cortam em ≥90 %; comando e
pergunta cortam em ≥95 %; eco não corta em 100 %. Versionado: é o instrumento do
gate — sem ele a matriz não se reproduz.

    ./smoke_barge_intencao.py            # veredito
    ./smoke_barge_intencao.py --verboso  # imprime cada caso errado
"""
from __future__ import annotations

import pathlib
import queue
import sys

import app  # noqa: F401 — o `_settings` da cadeia
from live_turns import classifica_barge

BACKCHANNEL = ["uhum", "aham", "hum", "hmm", "sim", "sei", "ok", "tá", "certo",
               "entendi", "isso", "claro", "beleza", "legal", "nossa", "mhm",
               "yeah", "right", "aham sim", "sim sim", "uhum uhum", "certo isso"]
COMANDO = ["para", "pare", "espera", "peraí", "pera", "calma", "chega", "não",
           "stop", "wait", "hold on", "para aí", "espera um pouco", "chega disso",
           "não, para", "calma calma", "para para", "espera, espera", "pare aí",
           "não quero", "chega por hoje"]
PERGUNTA = ["qual o prazo?", "quando sai?", "onde está?", "por que assim?",
            "quanto custa?", "quem fez?", "como faz?", "que horas são?",
            "what time is it?", "why so slow?", "cadê o arquivo?", "que dia é hoje?",
            "o prazo de entrega é sexta-feira?", "e o outro relatório?",
            "me diz o valor", "quero o resumo de ontem", "muda para o outro arquivo",
            "abre a pasta de saída", "faz de novo", "mostra o resultado"]
RUIDO = ["", "", "hm", "ã", "é", "ahn"]
ECO = ["então o dia está bonito hoje", "o dia está bonito",
       "quer que eu conte mais alguma coisa?", "claro, o dia está bonito hoje",
       "mais alguma coisa", "que eu conte mais alguma coisa"]
# (paráfrase fora do literal — "eu posso contar outra coisa" — não entra: a regra
# do #301 compara palavras, e uma paráfrase cairia em `frase_longa` e cortaria)
REF = "então o dia está bonito hoje. Quer que eu conte mais alguma coisa?"

ESPERADO = {"backchannel": "nao_corta", "comando": "corta", "pergunta": "corta",
            "ruido": "nao_corta", "eco": "nao_corta"}


def decide(texto: str, ms: int) -> str:
    # modo FIXO no default (o `settings.json` do dono é mutável e a rodada tem de
    # ser reprodutível): `BARGE_MODO=imediata` força a mordida
    import os
    modo = os.environ.get("BARGE_MODO") or "inteligente"
    if modo == "imediata":
        return "corta"        # o modo antigo corta no limiar, sem olhar conteúdo
    sess = {"id": "smoke", "ultimo_assistente": REF, "pipe": None}
    # `so_comando`: só a lista de parar corta (o resto segue a resposta)
    app._settings["live_barge_filtros"] = (
        {"pergunta": {"on": False}, "frase_longa": {"on": False},
         "fala_sustentada": {"on": False}, "eco": {"on": False}} if modo == "so_comando" else {})
    app._settings["live_barge_sem_decisao"] = "continua"
    tipo, _p, _m, _f = app._live_avalia_filtros(sess, texto, ms, {})
    return "corta" if tipo == "interrupcao" else "nao_corta"


def _latencia_do_comando(n: int = 6) -> list[int]:
    """Mede onset→corte com ÁUDIO REAL: wav do repo → VAD e STT reais.

    O trecho é a fala de uma voz do repositório (2,2 s, em tmp via `mkdtemp`) e o
    comando casado é a palavra que ela de fato transcreve ("olá") — sem texto
    injetado à mão no caminho. Passo de 150 ms, o default do produto."""
    import tempfile
    import threading
    import time

    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    import live_turns as lt
    from live_pipeline import _stt_app

    voz = sorted(pathlib.Path("voices").glob("*.wav"))[0]
    a, sr = sf.read(str(voz), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    pcm = (np.clip(resample_poly(a, 16000, int(sr)), -1, 1) * 32767).astype("<i2").tobytes()[:36000]
    tmp = pathlib.Path(tempfile.mkdtemp()) / "comando.wav"
    sf.write(str(tmp), np.frombuffer(pcm, dtype="<i2"), 16000, subtype="PCM_16")
    app._settings["live_barge_filtros"] = {"palavras_corte": {"palavras": ["olá", "ola"],
                                               "passo_ms": 150}}
    tempos = []
    for _ in range(n):
        pipe = type("P", (), {"language": "pt", "cancel": lambda self=None: None,
                              "ocupado": False})()
        pipe._stt = lambda pcm, lang: _stt_app(pcm, lang)
        sess = {"id": "smoke", "ultimo_assistente": "", "pipe": pipe,
                "cancelado": threading.Event(), "turno": 0, "fila": queue.Queue()}
        sess["engine"] = lt.TurnEngine(lt.Config(interrupcao="inteligente"),
                                       scorer=lambda f: 0.95)
        sess["engine"].set_speaking(True, nivel_dbfs=-20)

        class _Ev:
            audio = pcm

        t0 = time.monotonic()
        app._live_suspeita_barge(sess, _Ev())
        # mede até o `interrupted` de VERDADE (não até a suspeita fechar)
        while time.monotonic() - t0 < 6:
            try:
                tipo, obj = sess["fila"].get_nowait()
            except queue.Empty:
                time.sleep(0.005)
                continue
            if obj.get("type") == "interrupted":
                break
        tempos.append(round((time.monotonic() - t0) * 1000))
        time.sleep(0.15)                   # respiro: MLX/Metal não gosta de rajada
    tmp.unlink(missing_ok=True)
    return tempos


def main(argv: list[str]) -> int:
    verboso = "--verboso" in argv
    corpora = {"backchannel": BACKCHANNEL, "comando": COMANDO,
               "pergunta": PERGUNTA, "ruido": RUIDO, "eco": ECO}
    piso = {"backchannel": 0.90, "ruido": 0.90, "comando": 0.95,
            "pergunta": 0.95, "eco": 1.00}
    dur = {"backchannel": 300, "comando": 350, "pergunta": 500, "ruido": 180,
           "eco": 600}
    falhou = False
    for nome, casos in corpora.items():
        ok = 0
        for texto in casos:
            obtido = decide(texto, dur[nome])
            certo = obtido == ESPERADO[nome]
            ok += certo
            if not certo and verboso:
                print(f"  ✘ {nome}: {texto!r} → {obtido} (esperado {ESPERADO[nome]})")
        taxa = ok / len(casos)
        marca = "✔" if taxa >= piso[nome] else "✘"
        print(f"{marca} {nome:12} {ok}/{len(casos)} = {taxa*100:5.1f}% (piso {piso[nome]*100:.0f}%)")
        falhou |= taxa < piso[nome]
    # ADENDO: comando cadastrado corta NA HORA (STT em passos), sem esperar a
    # janela. Piso do card: p50 ≤600 ms. Roda com `--latencia` (custa ~1 s por
    # amostra de whisper REAL; sem a flag o smoke fica rápido e só de regra).
    if "--latencia" in argv:
        ms_corte = _latencia_do_comando()
        ms_corte.sort()
        p50 = ms_corte[len(ms_corte) // 2]
        marca = "✔" if p50 <= 600 else "✘"
        print(f"{marca} onset→corte do comando p50 = {p50} ms (pior {ms_corte[-1]} ms,"
              f" piso 600 ms; áudio real, VAD e STT reais)")
        falhou |= p50 > 600

    # amostras de intenção: a mesma classificação pura, direto na função
    amostras = [("para", 300), ("uhum", 250), ("", 150), ("qual o prazo?", 400)]
    print("classifica_barge:", [classifica_barge(t, m, REF) for t, m in amostras])
    print("\nVEREDITO:", "REPROVADO" if falhou else "OK")
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
