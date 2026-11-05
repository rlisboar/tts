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

Critério (do aceite do card): backchannel e ruído NÃO cortam em ≥90 %; comando,
pergunta e conteúdo próprio cortam em ≥95 %; eco não corta em 100 %. Versionado:
é o instrumento do gate — sem ele a matriz não se reproduz.

Duas MORDIDAS cobradas (#547, receita em `tests/MORDIDAS.md`): trocar o
`return "interrupcao"` final por `backchannel` reprova o grupo CONTEUDO, e trocar
o `return "backchannel"` por `interrupcao` reprova o grupo BACKCHANNEL. Uma
direção sem a outra passaria com metade da regra morta.

    ./smoke_barge_intencao.py            # veredito
    ./smoke_barge_intencao.py --verboso  # imprime cada caso errado
"""
from __future__ import annotations

import os
import pathlib
import queue
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tests"))
from _estado_tmp import isola  # noqa: E402

isola()          # #732: estado em tmp, nunca no worktree

import app  # noqa: E402,F401 — o `_settings` da cadeia
from live_turns import classifica_barge

BACKCHANNEL = ["uhum", "aham", "hum", "hmm", "sim", "sei", "ok", "tá", "certo",
               "entendi", "isso", "claro", "beleza", "legal", "nossa", "mhm",
               "yeah", "right", "ã", "é", "ahn", "aham sim", "sim sim",
               "uhum uhum", "certo isso"]
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
RUIDO = ["", "", "pff", "", "tss", ""]   # `hm`/`ã`/`é`/`ahn` são backchannel
# CONTEÚDO (#547, mordida do PM): fala com conteúdo PRÓPRIO que não é comando nem
# pergunta e fica abaixo de 1200 ms, então só casa a última regra da
# `classifica_barge` (3+ palavras fora da lista de backchannel). Concordância
# longa é o caso típico: o usuário está junto do assistente, mas está FALANDO e
# corta. Sem este grupo, trocar o `return "interrupcao"` final por `ruido`/
# `backchannel` passava em todo o resto da matriz.
CONTEUDO = ["sim, sim, faz sentido isso que você falou", "concordo com você nisso",
            "é exatamente isso mesmo", "eu já sabia dessa história",
            "entendi, mas não é bem assim", "pode continuar daí então",
            "isso me lembra uma coisa", "já vi esse filme antes",
            "faz sentido o que você disse", "acho que vale a pena",
            "yeah that makes sense to me", "i already knew about it",
            "that is exactly the point", "sure, but not quite like that",
            "go on from there then", "it reminds me of something",
            "i have seen this before", "that makes sense, keep going",
            "agreed, that is exactly it", "well, it is not that simple"]
# CURTO (#547): 1–2 palavras com conteúdo, fora da lista de backchannel — só a
# ÚLTIMA linha da `classifica_barge` as pega. Mordem o `return "interrupcao"`
# final, que o grupo CONTEUDO (3+ palavras) não alcança.
CURTO = ["tchau", "obrigado", "beleza mano", "vamos lá", "combinado então",
         "isso aí", "pode ser", "claro que sim", "até logo", "já era",
         "bye", "thanks", "not really", "of course", "see you", "all right",
         "no way", "for sure", "let us go", "right now"]
ECO = ["então o dia está bonito hoje", "o dia está bonito",
       "quer que eu conte mais alguma coisa?", "claro, o dia está bonito hoje",
       "mais alguma coisa", "que eu conte mais alguma coisa"]
# AMBÍGUO (#509): o filtro `modelo` existe para decidir o que a LISTA de
# backchannel engole por parecer concordância. Com o modelo ligado e ele dizendo
# "corta" (p alta), estes viram interrupção; sem o filtro, ficam em backchannel —
# é a comparação que a mordida do card cobra.
AMBIGUO = ["sim, sim", "uhum", "sei", "tá", "certo", "entendi", "isso", "claro",
           "beleza", "nossa", "aham", "yeah", "right", "sim sim", "uhum uhum",
           "certo isso", "mhm", "ok", "hum", "hmm"]
# (paráfrase fora do literal — "eu posso contar outra coisa" — não entra: a regra
# do #301 compara palavras, e uma paráfrase cairia em `frase_longa` e cortaria)
REF = "então o dia está bonito hoje. Quer que eu conte mais alguma coisa?"

ESPERADO = {"ambíguo": "corta", "backchannel": "backchannel", "comando": "corta",
            "pergunta": "corta",
            "ruido": "ruido", "eco": "eco", "conteudo": "corta",
            "curto": "corta"}


def decide(texto: str, ms: int, modelo: bool = False) -> str:
    # modo FIXO no default (o `settings.json` do dono é mutável e a rodada tem de
    # ser reprodutível): `BARGE_MODO=imediata` força a mordida
    import os
    modo = os.environ.get("BARGE_MODO") or "inteligente"
    if modo == "imediata":
        return "corta"        # o modo antigo corta no limiar, sem olhar conteúdo
    # MODELO (#509): `BARGE_MODELO=<p>` liga o filtro com um dublê que devolve
    # esse p. O dublê é o certo aqui: a matriz mede a CADEIA, e o classificador
    # real tem card e mordidas próprios — com ele a rodada deixaria de ser
    # reproduzível e passaria a depender de Metal.
    if os.environ.get("BARGE_MODELO"):
        p_modelo = float(os.environ["BARGE_MODELO"])

        def _stub(*_a, **_k):
            return p_modelo

        app.__dict__["_live_classificador_barge"] = _stub
    # SESSÃO pelo caminho de produção (#801): o pipeline é que carrega a
    # referência de eco. Antes o smoke injetava `ultimo_assistente` na mão e
    # media um cenário que a produção não tinha — a chave nunca era gravada.
    sess = _sessao_do_eco()
    # `so_comando`: só a lista de parar corta (o resto segue a resposta)
    base_f = {}
    if modo == "so_comando":
        base_f = {"pergunta": {"on": False}, "frase_longa": {"on": False},
                  "fala_sustentada": {"on": False}, "eco": {"on": False}}
    if modelo:
        # o modelo decide EXATAMENTE o que a lista de backchannel engoliria; nas
        # OUTRAS famílias ele fica fora para o dublê constante não mascará-las
        base_f = {**base_f, "backchannel": {"on": False},
                  "modelo": {"on": True, "limiar": 0.5, "timeout_ms": 400}}
    app._settings["live_barge_filtros"] = base_f
    app._settings["live_barge_sem_decisao"] = "continua"
    tipo, _p, _m, _f = app._live_avalia_filtros(sess, texto, ms, base_f)
    # devolve o TIPO (não só corta/não corta): com a regra sabotada para "sempre
    # interrupção", o backchannel vira `sem_decisao` e o grupo tem de REPROVAR
    return "corta" if tipo == "interrupcao" else tipo


_SESSAO_ECO = {}


def _sessao_do_eco() -> dict:
    """Sessão com um pipeline REAL que está falando `REF` (uma por processo).

    Só a JANELA é armada (`_play_fim` + `_marcar_render`): é o estado em que o
    filtro `eco` tem de reconhecer o próprio TTS. A referência não é injetada —
    quem a expõe é o `texto_em_reproducao` do pipeline."""
    if _SESSAO_ECO:
        return _SESSAO_ECO
    sess = {"id": "smoke", "pipe": None, "fila": queue.Queue(), "voice_id": "",
            "system": "", "history": [], "cancelado": threading.Event(), "turno": 0,
            "st_stage": "idle", "st_stage_ini": None, "st_erro": None,
            "visto": time.monotonic()}
    pipe = app._live_pipe_novo(sess)
    pipe._play_fim = time.monotonic() + 30.0
    pipe._marcar_render(REF)
    sess["pipe"] = pipe
    _SESSAO_ECO.update(sess)
    return _SESSAO_ECO


def _latencia_do_comando(n: int = 6) -> list[int]:
    """Mede onset→corte com ÁUDIO REAL: wav do repo → VAD e STT reais.

    O trecho é a fala de uma voz do repositório (2,2 s, em tmp via `mkdtemp`) e o
    comando casado é a palavra que ela de fato transcreve ("olá") — sem texto
    injetado à mão no caminho. Passo de 150 ms, o default do produto."""
    import tempfile
    import threading
    import time

    import subprocess

    import numpy as np
    import soundfile as sf

    import live_turns as lt
    from live_pipeline import _stt_app

    # VOZ: a do `voices/` quando existe, senão um COMANDO sintetizado (#732) — num
    # worktree `voices/` não existe e o primeiro wav do dono transcreve uma frase
    # qualquer ("olá"), que não casa `palavras_corte`: a medição dava 6 s (o teto
    # da espera) em toda amostra, sem nunca cortar.
    # O comando é SINTETIZADO sempre: a voz de `voices/` muda por árvore (o dono
    # tem as dele, o worktree tem a fixture) e o que ela transcreve não é comando
    # — "Esta é uma amostra" não casa `palavras_corte` e a amostra expirava no teto
    # de 6 s sem corte. Com `say` o áudio é o mesmo em qualquer máquina.
    dir_tmp = pathlib.Path(tempfile.mkdtemp())
    aiff = dir_tmp / "cmd.aiff"
    wav = dir_tmp / "cmd.wav"
    subprocess.run(["say", "-o", str(aiff), "Para, para, espera um pouco."],
                   check=True, timeout=30, capture_output=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                    str(aiff), str(wav)], check=True, timeout=30, capture_output=True)
    a, sr = sf.read(str(wav), dtype="float32")
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
    palavras = ["para", "espera"]
    tmp = dir_tmp / "comando.wav"
    sf.write(str(tmp), np.frombuffer(pcm, dtype="<i2"), 16000, subtype="PCM_16")
    app._settings["live_barge_filtros"] = {"palavras_corte": {"palavras": palavras,
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
        # BACKLOG no engine: sem ele `playback_restante_ms()==0` e o
        # `fim_de_resposta` ganha do comando (anota em vez de cortar) — a amostra
        # mediria o último segundo, não o corte
        sess["engine"].set_speaking(True, nivel_dbfs=-20, duracao_ms=3000)

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
               "pergunta": PERGUNTA, "ruido": RUIDO, "eco": ECO,
               "conteudo": CONTEUDO, "curto": CURTO}
    piso = {"backchannel": 0.90, "ruido": 0.90, "comando": 0.95,
            "pergunta": 0.95, "eco": 1.00, "conteudo": 0.95, "curto": 0.95}
    falhou = False
    if os.environ.get("BARGE_MODELO"):
        # grupo PRÓPRIO, com o modelo ligado só nele (a leitura direta do card);
        # as outras famílias seguem sem ele, senão um dublê constante as derruba
        corpora["ambíguo"] = AMBIGUO
        piso["ambíguo"] = 0.90
        # MORDIDA do card, medida na própria rodada: SEM o filtro estes casos NÃO
        # podem cortar (é o que os torna ambíguos) e COM ele têm de cortar. Se os
        # dois números empatarem, o grupo não mede nada.
        sem_modelo = sum(1 for t in AMBIGUO if decide(t, 300, modelo=False) == "corta")
        marca_b = "✔" if sem_modelo <= 2 else "✘"
        print(f"{marca_b} ambíguo sem o filtro: {sem_modelo}/{len(AMBIGUO)} cortam "
              f"(esperado ≤2 — é o que os torna ambíguos)")
        falhou |= sem_modelo > 2
    # `ruido` abaixo de 200 ms de propósito: acima disso o `stt_vazio` (falha
    # segura) corta, e o grupo de ruído deixaria de medir o que mede
    # `conteudo` abaixo de 1200 ms de propósito: acima disso o `fala_sustentada`
    # atalha o STT e o grupo deixaria de medir a regra de conteúdo próprio.
    dur = {"ambíguo": 300, "backchannel": 300, "comando": 350, "pergunta": 500,
           "ruido": 150, "eco": 600, "conteudo": 900, "curto": 700}
    for nome, casos in corpora.items():
        ok = 0
        for texto in casos:
            obtido = decide(texto, dur[nome], modelo=(nome == "ambíguo"))
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
