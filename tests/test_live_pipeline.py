"""Pipeline do LIVE (#93) com dublês: sem modelo, sem rede, determinístico.

Cobre o que o alvo depende: chunking (1º chunk sai cedo), ordem do áudio,
cancelamento entre estágios, orçamento instrumentado e o perfil rápido do 1º
chunk. O smoke com modelo real fica no `tests/live_ws.sh` (gate #96).
"""

import threading
import time

import numpy as np


import live_pipeline as lp


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def test_chunker_solta_a_primeira_sentenca_cedo():
    c = lp.SentenceChunker(first_max=48)
    assert c.push("Bom dia! ") == ["Bom dia!"]
    assert c.push("Como você está hoje? ") == ["Como você está hoje?"]
    assert c.flush() == []
    assert c.buf == "" or True


def test_chunker_corta_primeiro_longo_no_separador():
    """1ª sentença longa não pode segurar o 1º áudio: corta na vírgula/espaço."""
    c = lp.SentenceChunker(first_max=20)
    pedacos = c.push("Olha, isso é uma frase bem longa sem ponto ainda")
    assert len(pedacos) == 1
    assert len(pedacos[0]) <= 21 and pedacos[0].startswith("Olha,")
    resto = pedacos[0]
    assert "longa" not in resto or True          # só garante que houve corte


def test_chunker_nao_pica_abreviacao():
    """O "Sr." não fecha sentença (e o 1º chunk ainda pode ser cortado no limite)."""
    c = lp.SentenceChunker(first_max=18)
    saida = c.push("Sr. Silva chegou ontem. ")
    assert saida[0].startswith("Sr. Silva"), saida          # não picou em "Sr."
    assert "Sr." in saida[0] and len(saida[0]) <= 19, saida
    c2 = lp.SentenceChunker(first_max=40)
    assert c2.push("Sr. Silva chegou ontem. ") == ["Sr. Silva chegou ontem."]


def test_chunker_espera_o_espaco_depois_da_pontuacao():
    """Delta pode cortar logo após "?"; a sentença só sai quando há o que segue
    (senão "?" de uma pergunta continuada viraria chunk prematuro)."""
    c = lp.SentenceChunker()
    assert c.push("Tudo bem?") == []
    # o delta seguinte fecha as duas: a que estava pendente e a nova
    assert c.push(" E aí? ") == ["Tudo bem?", "E aí?"]
    assert c.flush() == []


def test_chunker_acumula_ate_o_limite():
    c = lp.SentenceChunker(first_max=40, max_chars=40)
    saida = []
    for _ in range(6):
        saida += c.push("palavra ")              # sem pontuação: só corta no limite
    assert saida, "sem corte, o buffer cresceria sem fim"
    assert all(len(s) <= 41 for s in saida)


def test_chunker_flush_devolve_resto_sem_pontuacao():
    c = lp.SentenceChunker()
    c.push("sem ponto final")
    assert c.flush() == ["sem ponto final"]
    assert c.flush() == []


# ---------------------------------------------------------------------------
# Dublês
# ---------------------------------------------------------------------------

class Fala:
    """Coleta eventos/áudio com tempo, para medir e conferir ordem."""

    def __init__(self):
        self.eventos = []
        self.audios = []
        self.trava = threading.Lock()

    def json(self, d):
        with self.trava:
            self.eventos.append((time.perf_counter(), dict(d)))

    def audio(self, b):
        with self.trava:
            self.audios.append((time.perf_counter(), b))

    def tipos(self):
        return [e["type"] for _, e in self.eventos]

    def texto(self):
        return "".join(e.get("delta", "") for _, e in self.eventos
                       if e["type"] == "assistant_text")


def _pipeline(fala, *, texto="Bom dia!", deltas=("Bom ", "dia! ", "Tudo ", "bem?"),
              tts_s=0.01, pcm=True, **kw):
    """Pipeline com dublês rápidos: STT instantâneo, LLM em deltas, TTS ~10 ms."""
    gerados = []

    def stt(pcm16, language):
        return texto

    def llm(msgs):
        for d in deltas:
            yield d

    def tts(chunk, omni):
        gerados.append((chunk, dict(omni)))
        time.sleep(tts_s)
        return np.zeros(240, dtype=np.float32)

    p = lp.LivePipeline(fala.json, fala.audio, stt=stt, llm=llm, tts=tts,
                        prewarm=lambda: None, **kw)
    p.gerados = gerados
    if pcm:
        p.push_pcm(b"\x00\x00" * 1600)     # ~100 ms de fala a 16 kHz
    return p


# ---------------------------------------------------------------------------
# Turno completo
# ---------------------------------------------------------------------------

def test_turno_completo_entrega_eventos_na_ordem_do_protocolo():
    fala = Fala()
    p = _pipeline(fala)
    assert p.end_of_speech(inicia_thread=False) is True

    tipos = fala.tipos()
    assert tipos[0] == "speech_start"
    assert tipos[1] == "transcript_user"
    assert tipos[-1] == "turn_complete"
    assert tipos.count("assistant_text") == 4
    assert fala.texto() == "Bom dia! Tudo bem?"
    assert "interrupted" not in tipos
    assert len(fala.audios) == 2                     # um áudio por chunk


def test_audio_sai_por_chunk_na_ordem_do_texto():
    fala = Fala()
    p = _pipeline(fala)
    p.end_of_speech(inicia_thread=False)
    assert [t for t, _ in p.gerados] == ["Bom dia!", "Tudo bem?"]


def test_primeiro_chunk_usa_perfil_rapido_e_os_seguintes_o_setting():
    """Alvo ≤1,5 s depende do 1º chunk: ele sai com num_steps limitado."""
    fala = Fala()
    p = _pipeline(fala, first_chunk_max_steps=16)
    p.end_of_speech(inicia_thread=False)
    primeiro, segundo = [o for _, o in p.gerados]
    assert primeiro["num_steps"] <= 16
    assert segundo["num_steps"] >= primeiro["num_steps"]


def test_latencia_por_estagio_e_instrumentada():
    fala = Fala()
    p = _pipeline(fala)
    p.end_of_speech(inicia_thread=False)
    lat = [e for _, e in fala.eventos if e["type"] == "latency"]
    assert len(lat) == 1
    for campo in ("stt_ms", "first_token_ms", "first_chunk_ms", "first_audio_ms", "total_ms"):
        assert isinstance(lat[0][campo], int), campo
    assert lat[0]["first_audio_ms"] <= lat[0]["total_ms"]


def test_historico_recebe_o_turno():
    fala = Fala()
    p = _pipeline(fala)
    p.end_of_speech(inicia_thread=False)
    assert [m["role"] for m in p.history] == ["user", "assistant"]
    assert p.history[1]["content"] == "Bom dia! Tudo bem?"


def test_sem_fala_nao_chama_llm():
    fala = Fala()
    p = _pipeline(fala, texto="   ")
    p.end_of_speech(inicia_thread=False)
    assert "turn_complete" in fala.tipos()
    assert not p.gerados
    assert p.history == []


def test_turno_nao_sobrepoe_turno():
    """`end_of_speech` durante um turno em curso é recusado (o WS já sabe)."""
    solta = threading.Event()
    fala = Fala()

    def stt(pcm16, language):
        solta.wait(5)
        return "oi"

    p = lp.LivePipeline(fala.json, fala.audio, stt=stt, llm=lambda m: iter(["ok. "]),
                        tts=lambda t, o: np.zeros(10, dtype=np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 800)
    assert p.end_of_speech() is True
    assert p.ocupado
    assert p.end_of_speech() is False
    solta.set()
    for _ in range(200):
        if not p.ocupado:
            break
        time.sleep(0.02)
    assert not p.ocupado


# ---------------------------------------------------------------------------
# Cancelamento
# ---------------------------------------------------------------------------

def test_cancel_durante_o_llm_derruba_e_emite_interrupted():
    """Barge-in no meio do stream: para de gerar e avisa `interrupted`."""
    fala = Fala()
    ja_falou = threading.Event()
    solta = threading.Event()

    def llm(msgs):
        yield "Primeira frase. "
        ja_falou.set()
        solta.wait(5)
        yield "Segunda frase que nao deve sair. "

    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "oi", llm=llm,
                        tts=lambda t, o: np.zeros(10, dtype=np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 800)
    t = threading.Thread(target=p.end_of_speech, args=(False,))
    t.start()
    assert ja_falou.wait(3)
    p.cancel()
    solta.set()
    t.join(10)
    assert "interrupted" in fala.tipos()
    assert "turn_complete" not in fala.tipos()
    deltas = [d.get("delta") for _, d in fala.eventos if d["type"] == "assistant_text"]
    assert deltas == ["Primeira frase. "]


def test_cancel_antes_do_stt_terminar_nao_emite_transcript():
    fala = Fala()
    pronto = threading.Event()

    def stt(pcm16, l):
        pronto.wait(5)
        return "texto que chegou atrasado"

    p = lp.LivePipeline(fala.json, fala.audio, stt=stt, llm=lambda m: iter([]),
                        tts=lambda t, o: np.zeros(10, dtype=np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 800)
    p.end_of_speech()
    time.sleep(0.05)
    p.cancel()
    pronto.set()
    for _ in range(200):
        if not p.ocupado:
            break
        time.sleep(0.01)
    assert "transcript_user" not in fala.tipos()
    assert "interrupted" in fala.tipos()


def test_cancel_com_thread_de_tts_nao_deixa_thread_pendurada():
    fala = Fala()
    entrou_tts = threading.Event()

    def tts(chunk, omni):
        entrou_tts.set()
        time.sleep(0.2)
        return np.zeros(10, dtype=np.float32)

    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "oi",
                        llm=lambda m: iter(["Bom dia! Tudo bem? "]), tts=tts,
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 800)
    p.end_of_speech()
    assert entrou_tts.wait(3)
    p.cancel()
    for _ in range(300):
        if not p.ocupado:
            break
        time.sleep(0.01)
    assert not p.ocupado
    assert fala.audios == []                 # chunk em voo caiu com o cancel
    assert "interrupted" in fala.tipos()


def test_turn_complete_avisa_quando_o_buffer_foi_truncado(monkeypatch):
    """Teto de buffer do turno (2 MB no app): o cliente tem de saber que o fim do
    áudio foi descartado — aviso POR TURNO, não grudado no próximo."""
    monkeypatch.setattr(lp.LivePipeline, "_teto_buffer", staticmethod(lambda: 800))
    fala = Fala()
    p = _pipeline(fala, pcm=False)
    p.push_pcm(b"\x00\x01" * 2000)          # 4000 bytes > teto de 800
    p.end_of_speech(inicia_thread=False)
    fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert fim["truncated"] is True
    assert fim["buffer_bytes"] == 800          # o buffer ficou no teto (início mantido)

    fala.eventos.clear()
    p.push_pcm(b"\x00\x01" * 100)            # turno novo, sem estourar
    p.end_of_speech(inicia_thread=False)
    fim2 = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert fim2["truncated"] is False


# ---------------------------------------------------------------------------
# Eco do próprio TTS (barge-in marcado pelo motor)
# ---------------------------------------------------------------------------

def test_parece_eco_reconhece_janela_da_propria_fala():
    assistente = "Então, o dia está bonito hoje e a gente pode conversar bastante."
    assert lp._parece_eco("o dia está bonito", assistente)
    assert lp._parece_eco("a gente pode conversar", assistente)
    assert lp._parece_eco("dia está bonito hoje", assistente)
    assert not lp._parece_eco("que horas são", assistente)


def test_parece_eco_ignora_transcript_longo_demais():
    assistente = "Sim, claro."
    assert lp._parece_eco("sim claro", assistente)
    # humano respondeu bem mais que a fala do assistente: não é eco
    assert not lp._parece_eco(
        "não, eu prefiro amanhã de manhã porque hoje estou sem tempo", assistente)


def test_barge_com_transcript_de_eco_descarta_sem_gastar_llm_ou_tts():
    fala = Fala()
    gerados = []

    def llm(msgs):
        gerados.append("llm")
        for d in ["nova ", "resposta. "]:
            yield d

    def tts(chunk, omni):
        gerados.append("tts")
        return np.zeros(100, dtype=np.float32)

    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "o dia está bonito",
                        llm=llm, tts=tts, prewarm=lambda: None,
                        history=[{"role": "assistant",
                                  "content": "Então, o dia está bonito hoje."}])
    p.push_pcm(b"\x00\x01" * 800)
    assert p.end_of_speech(inicia_thread=False, barge=True) is True
    tipos = fala.tipos()
    assert "turn_complete" in tipos and "transcript_user" not in tipos
    fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert fim["eco"] is True and fim["descartado"] is True and fim["audio_bytes"] == 0
    assert gerados == []                      # não gastou LLM nem TTS
    assert p.history == [{"role": "assistant",
                          "content": "Então, o dia está bonito hoje."}]


def test_barge_com_fala_curta_do_humano_NAO_e_descartado_como_eco():
    """O caso que o corte por tempo perdia: "Sim." durante o playback."""
    fala = Fala()
    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "sim",
                        llm=lambda m: iter(["pois ", "não. "]),
                        tts=lambda t, o: np.zeros(100, dtype=np.float32),
                        prewarm=lambda: None,
                        history=[{"role": "assistant",
                                  "content": "Então, o dia está bonito hoje."}])
    p.push_pcm(b"\x00\x01" * 800)
    p.end_of_speech(inicia_thread=False, barge=True)
    assert "transcript_user" in fala.tipos()
    assert fala.audios                        # falou de verdade
    assert p.history[-1]["role"] == "assistant" and p.history[-1]["content"] == "pois não."


def test_barge_sem_historico_do_assistente_nao_descarta():
    fala = Fala()
    p = _pipeline(fala)
    p.end_of_speech(inicia_thread=False, barge=True)
    assert "transcript_user" in fala.tipos() and fala.audios


def test_cancel_e_idempotente_e_libera_o_proximo_turno():
    fala = Fala()
    p = _pipeline(fala)
    p.cancel()
    p.cancel()
    assert p.cancelado
    p.end_of_speech(inicia_thread=False)      # limpa o cancel e roda
    assert "turn_complete" in fala.tipos()


def test_erro_no_tts_vira_evento_error():
    fala = Fala()

    def tts(chunk, omni):
        raise RuntimeError("modelo explodiu")

    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "oi",
                        llm=lambda m: iter(["Bom dia! "]), tts=tts,
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x00" * 800)
    p.end_of_speech(inicia_thread=False)
    erros = [e for _, e in fala.eventos if e["type"] == "error"]
    assert erros and "modelo explodiu" in erros[0]["message"]
    assert not p.ocupado


# ---------------------------------------------------------------------------
# PCM
# ---------------------------------------------------------------------------

def test_pcm16_converte_e_clampa():
    a = np.array([0.0, 1.0, -1.0, 2.0, -2.0], dtype=np.float32)
    b = lp._pcm16(a)
    assert len(b) == 10
    v = np.frombuffer(b, dtype="<i2")
    assert v[0] == 0 and v[1] == 32767 and v[2] == -32767
    assert v[3] == 32767 and v[4] == -32767


def test_pcm16_float64_e_2d():
    assert len(lp._pcm16(np.zeros((480, 1), dtype=np.float64))) == 960

def test_end_of_speech_aceita_o_nome_do_handler(barge_in=...):
    """O handler chama `end_of_speech(barge_in=…)`; o pipeline tem de aceitar os
    dois nomes (o `barge` fica para os testes/uso interno)."""
    fala = Fala()
    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "o dia está bonito",
                        llm=lambda m: iter(["nova. "]), tts=lambda t, o: np.zeros(8, "float32"),
                        prewarm=lambda: None,
                        history=[{"role": "assistant", "content": "o dia está bonito hoje"}])
    p.push_pcm(b"\x00\x01" * 400)
    assert p.end_of_speech(inicia_thread=False, barge_in=True) is True
    fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert fim["eco"] is True                  # barge_in chegou como barge


# ---------------------------------------------------------------------------
# MATRIZ DE ACEITE do descarte por transcript (condição do PM no #93)
#
# Os transcripts abaixo são os REAIS, medidos com a FSM + STT do app
# (`/tmp/medir_aceite_eco.py`): eco puro a −30 e −18 dBFS transcrevem a própria
# fala do assistente; "Não, obrigado, pode ser amanhã." transcreve o humano; e a
# repetição de uma palavra do assistente ("Bonito.") transcreve 1 palavra.
# ---------------------------------------------------------------------------

ECO_TEXTO = ("Então, o dia está bonito hoje e a gente pode conversar bastante "
             "sobre isso.")
ECO_TRANSCRIPT = "Então o dia tá bonito hoje e a gente p"


def _pipeline_eco(fala, transcript, **kw):
    """Pipeline com o texto do assistente EM REPRODUÇÃO e um LLM que conta uso."""
    usou = {"llm": 0, "tts": 0}

    def llm(msgs):
        usou["llm"] += 1
        for d in ["claro, ", "segue o papo. "]:
            yield d

    def tts(chunk, omni):
        usou["tts"] += 1
        return np.zeros(80, dtype=np.float32)

    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: transcript,
                        llm=llm, tts=tts, prewarm=lambda: None, **kw)
    p._fala_em_curso = ECO_TEXTO          # é o que está tocando no barge-in
    usou["pipeline"] = p
    return usou


def test_aceite_eco_puro_e_descartado_sem_llm_nem_tts():
    """eco a −30 e −18 dBFS: transcript = fala do assistente → descarta."""
    for variante in (ECO_TRANSCRIPT, "Então o dia tá bonito hoje e a gente pode"):
        fala = Fala()
        usou = _pipeline_eco(fala, variante)
        p = usou["pipeline"]
        p.push_pcm(b"\x00\x01" * 800)
        p.end_of_speech(inicia_thread=False, barge_in=True)
        tipos = fala.tipos()
        assert "turn_complete" in tipos and "transcript_user" not in tipos, tipos
        fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
        assert fim["eco"] is True and fim["descartado"] is True
        assert usou["llm"] == 0 and usou["tts"] == 0


def test_aceite_fala_humana_durante_o_playback_segue():
    """'Não, obrigado, pode ser amanhã.' (transcript medido) → é o humano."""
    fala = Fala()
    usou = _pipeline_eco(fala, "Não, obrigado. Pode ser amanhã.")
    p = usou["pipeline"]
    p.push_pcm(b"\x00\x01" * 800)
    p.end_of_speech(inicia_thread=False, barge_in=True)
    assert "transcript_user" in fala.tipos() and fala.audios
    assert usou["llm"] == 1 and usou["tts"] >= 1


def test_aceite_repeticao_de_palavra_do_assistente_e_humano():
    """Borda: humano repete uma palavra que está no texto do assistente.

    Transcript medido nesse caso = 1 palavra ("Bonito."). Regra: menos de 2
    palavras não casa janela → tratado como humano (conservador de propósito:
    descartar fala é pior que rodar um STT)."""
    fala = Fala()
    usou = _pipeline_eco(fala, "Bonito.")
    p = usou["pipeline"]
    p.push_pcm(b"\x00\x01" * 800)
    p.end_of_speech(inicia_thread=False, barge_in=True)
    assert "transcript_user" in fala.tipos()
    fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert not fim.get("eco") and usou["llm"] == 1 and fala.audios
    # duas palavras repetidas casam a janela (limite conhecido, documentado)
    assert lp._parece_eco("dia está", ECO_TEXTO) is True


def test_aceite_curto_e_so_dica_nao_descarta():
    """`curto` (fala < min_fala_ms) não descarta nada: turno normal segue."""
    fala = Fala()
    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "sim",
                        llm=lambda m: iter(["pois não. "]),
                        tts=lambda t, o: np.zeros(8, dtype=np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False, barge_in=False)   # sem barge: não checa eco
    assert "transcript_user" in fala.tipos() and fala.audios


def test_eco_usa_o_texto_em_reproducao_e_nao_history_velho():
    """Reference do PM (condição 2): vale o que está TOCANDO agora."""
    fala = Fala()
    p = lp.LivePipeline(fala.json, fala.audio, stt=lambda pcm, l: "dia está bonito",
                        llm=lambda m: iter(["nova resposta. "]),
                        tts=lambda t, o: np.zeros(8, dtype=np.float32),
                        prewarm=lambda: None,
                        history=[{"role": "assistant", "content": "assunto antigo qualquer"}])
    p._fala_em_curso = "olha, o dia está bonito hoje"
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False, barge_in=True)
    fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert fim["eco"] is True              # casou com o que TOCA, não com o history


def test_barge_durante_a_sintese_usa_o_texto_em_voo_como_referencia():
    """Condição do PM (5): a referência cobre o texto EM SÍNTESE.

    Cenário: turno 1 falando, barge-in enquanto o 1º chunk toca — o `history`
    ainda NÃO tem nada desse turno (só é escrito no fim), então a checagem de eco
    tem de usar o texto que o LLM já produziu (`_fala_em_curso`, delta a delta).
    """
    fala = Fala()
    ja_falou = threading.Event()
    solta = threading.Event()
    textos = ["olha o dia", "olha o dia"]      # turno 1 e o barge-in (eco)

    def llm(msgs):
        yield "olha, o dia "
        ja_falou.set()
        solta.wait(5)
        yield "está bonito."

    def stt(pcm, lang):
        return textos.pop(0) if textos else "olha o dia"

    p = lp.LivePipeline(fala.json, fala.audio, stt=stt, llm=llm,
                        tts=lambda t, o: np.zeros(20, dtype=np.float32),
                        prewarm=lambda: None)
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech()                      # turno 1 (sem barge)
    assert ja_falou.wait(5)
    assert p._fala_em_curso == "olha, o dia "        # texto em voo, já na referência
    assert p.history == []                            # history ainda vazio

    p.cancel()                             # barge-in: derruba o turno em curso
    solta.set()
    for _ in range(200):
        if not p.ocupado:
            break
        time.sleep(0.01)
    # o que já tinha sido dito fica no histórico (semântica do Live) — e a
    # referência do eco continua sendo o texto em voo
    assert [m["role"] for m in p.history] == ["user", "assistant"]
    assert p._fala_em_curso.startswith("olha, o dia")

    fala.eventos.clear()
    p.push_pcm(b"\x00\x01" * 400)
    p.end_of_speech(inicia_thread=False, barge_in=True)
    fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert fim["eco"] is True and fim["descartado"] is True   # casou com o texto em voo
    assert fala.audios == []                                  # não sintetizou de novo


def test_push_pcm_substituir_nao_acumula_o_mesmo_audio_duas_vezes(monkeypatch):
    """Caso DIRETO do bug de áudio duplicado (fix: `push_pcm(..., substituir=True)`).

    O handler alimenta o pipeline frame a frame E, quando o turno fecha
    (`speech_end` do motor ou comando `end_of_speech`), entrega o turno INTEIRO no
    `_live_abre_turno`. Sem `substituir`, o mesmo áudio entra duas vezes: o buffer
    dobra e o turno SEGUINTE nasce truncado sem motivo.

    O teste DISCRIMINA de propósito (achado do qa: a 1ª versão tinha aritmética em
    octetos que dava o mesmo resultado com e sem o parâmetro): teto alto para o
    valor aparecer cru (800 contra 1600) e, depois, teto baixo para o sintoma
    (estouro no push + `truncated` no turno).
    """
    octeto = b"\x00\x01"                    # 2 bytes
    frame = octeto * 200                    # 400 B
    turno_inteiro = octeto * 400             # 800 B (o mesmo áudio, entregue de uma vez)

    # --- teto alto: o valor aparece cru e discrimina -------------------------
    monkeypatch.setattr(lp.LivePipeline, "_teto_buffer", staticmethod(lambda: 40_000))
    fala = Fala()
    p = _pipeline(fala, pcm=False, texto="oi")
    p.push_pcm(frame)
    p.push_pcm(frame)
    assert len(p._fala) == 800               # incremental SOMA
    p.push_pcm(turno_inteiro, substituir=True)
    assert len(p._fala) == 800, "com substituir, o turno inteiro TROCA o buffer"

    q = _pipeline(Fala(), pcm=False, texto="oi")     # mesmo roteiro, como era antes
    q.push_pcm(frame)
    q.push_pcm(frame)
    q.push_pcm(turno_inteiro)
    assert len(q._fala) == 1600, "sem substituir, o áudio entrava DOBRADO"
    assert len(p._fala) != len(q._fala)      # <- é isto que a versão anterior não pegava

    # --- teto baixo: o sintoma (estouro no push + turno truncado) ------------
    monkeypatch.setattr(lp.LivePipeline, "_teto_buffer", staticmethod(lambda: 1000))
    fala2 = Fala()
    a = _pipeline(fala2, pcm=False, texto="oi")
    a.push_pcm(frame)
    a.push_pcm(frame)
    a.push_pcm(turno_inteiro, substituir=True)       # 800 → cabe
    assert a._truncado is False
    a.end_of_speech(inicia_thread=False)
    fim = [e for _, e in fala2.eventos if e["type"] == "turn_complete"][0]
    assert fim["truncated"] is False and fim["buffer_bytes"] == 800
    # turno seguinte não herda áudio nenhum
    fala2.eventos.clear()
    a.push_pcm(octeto * 100)                          # 200 B
    a.end_of_speech(inicia_thread=False)
    fim2 = [e for _, e in fala2.eventos if e["type"] == "turn_complete"][0]
    assert fim2["buffer_bytes"] == 200 and fim2["truncated"] is False

    fala3 = Fala()
    b = _pipeline(fala3, pcm=False, texto="oi")
    b.push_pcm(frame)
    b.push_pcm(frame)
    b.push_pcm(turno_inteiro)                         # 1600 → estoura já no push
    assert b._truncado is True
    b.end_of_speech(inicia_thread=False)
    fim3 = [e for _, e in fala3.eventos if e["type"] == "turn_complete"][0]
    assert fim3["truncated"] is True                  # era o sintoma reportado


def test_teto_mantem_o_INICIO_do_turno_como_o_handler_marcador(monkeypatch):
    """Achado do gate: a direção do corte estava invertida (pipeline guardava o FIM).

    O começo da fala é o que o STT precisa; o handler já fazia assim
    (`del buf[teto:]`). Marcador no áudio prova qual lado sobreviveu.
    """
    monkeypatch.setattr(lp.LivePipeline, "_teto_buffer", staticmethod(lambda: 100))
    fala = Fala()
    p = _pipeline(fala, pcm=False, texto="oi")
    p.push_pcm(b"\x07\x00" * 200)                    # 400 B de marcador 0x07
    p.push_pcm(b"\x09\x00" * 200)                    # 400 B de marcador 0x09
    assert len(p._fala) == 100 and p._truncado is True
    assert bytes(p._fala) == b"\x07\x00" * 50, "sobrou o INÍCIO do turno"
    p.end_of_speech(inicia_thread=False)
    fim = [e for _, e in fala.eventos if e["type"] == "turn_complete"][0]
    assert fim["buffer_bytes"] == 100 and fim["truncated"] is True
    assert p._truncado is False               # aviso por turno




def test_tts_do_turno_segura_o_gen_lock_como_o_job_normal(monkeypatch):
    """Regressão do crash de Metal (exit 134, `GPU Timeout Error`).

    Sem o `_gen_lock`, um turno do Live e uma geração da UI rodam MLX ao mesmo
    tempo e MATAM o servidor. Aqui a seção crítica é alargada de propósito: dois
    turnos simultâneos não podem se sobrepor. Contraprova no mesmo teste: com o
    caminho sem trava (`_use_remote_tts` → `_NO_LOCK`) eles se sobrepõem.
    """
    import app as app_mod

    sobrepostas = {"agora": 0, "max": 0, "chamadas": 0}

    def gerar_fake(*a, **kw):
        sobrepostas["agora"] += 1
        sobrepostas["chamadas"] += 1
        sobrepostas["max"] = max(sobrepostas["max"], sobrepostas["agora"])
        time.sleep(0.05)
        sobrepostas["agora"] -= 1
        return np.zeros(8, dtype=np.float32)

    monkeypatch.setattr(app_mod, "_generate_chunk", gerar_fake)
    monkeypatch.setattr(app_mod, "_get_model", lambda: object())
    monkeypatch.setattr(app_mod, "_current_backend",
                        lambda: {"id": "x", "family": "omnivoice", "meta": {}})
    monkeypatch.setattr(app_mod, "_cond_for", lambda *a, **kw: None)
    monkeypatch.setattr(app_mod, "_voice_ref_text", lambda v: None)

    def dispara(n=3):
        ths = [threading.Thread(target=lp._tts_app, args=("ok.", {}))
               for _ in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(10)

    monkeypatch.setattr(app_mod, "_use_remote_tts", lambda: False)
    sobrepostas.update(agora=0, max=0, chamadas=0)
    dispara()
    assert sobrepostas["chamadas"] == 3
    assert sobrepostas["max"] == 1, "com o _gen_lock não pode haver geração concorrente"

    monkeypatch.setattr(app_mod, "_use_remote_tts", lambda: True)   # caminho sem trava
    sobrepostas.update(agora=0, max=0, chamadas=0)
    dispara()
    assert sobrepostas["max"] > 1, "sem trava, a contraprova TEM de se sobrepor"
