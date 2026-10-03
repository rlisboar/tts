"""#126: fala recebida com o pipeline ocupado não é mais descartada.

Antes: `_live_abre_turno` devolvia `turno_em_curso` e PERDIA o áudio — qualquer
`speech_end` durante uma resposta longa (dezenas de segundos) sumia, e o dono
lia como "parou de captar" (irmão do #117). Agora os trechos se ACUMULAM num
único pendente (ajuste do PM: a pessoa completa a frase em dois pedaços) e
abrem quando o pipeline liberar, precedidos do evento aditivo `turno_pendente`
("anotei, respondo já"). Teto de duração descarta o trecho mais antigo; `cancel`
do cliente limpa o pendente; barge-in mantém; erro do turno anterior não herda.
"""

import queue
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app
import live_pipeline as lp


# ---------------------------------------------------------------------------
# Dublês
# ---------------------------------------------------------------------------

class CanalFalso:
    """Pipeline mínimo: `ocupado` é sinalizado à mão; guarda o que foi aceito."""

    def __init__(self):
        self._trava = threading.Lock()
        self._ocupado = False
        self._buf = bytearray()
        self.aceitos = []                    # (pcm, barge) na ordem

    @property
    def ocupado(self):
        with self._trava:
            return self._ocupado

    def push_pcm(self, pcm, substituir=False):
        with self._trava:
            if substituir:
                self._buf = bytearray()
            self._buf.extend(pcm)

    def end_of_speech(self, inicia_thread=True, barge=False, barge_in=None):
        with self._trava:
            if self._ocupado:
                return False
            self._ocupado = True
            self.aceitos.append((bytes(self._buf),
                                 bool(barge if barge_in is None else barge_in)))
            self._buf = bytearray()
            return True

    def cancel(self):
        """O turno real derruba geração/TTS — no dublê não há o que derrubar."""

    def libera(self):
        """Simula o fim do turno em curso (o teste controla o momento)."""
        with self._trava:
            self._ocupado = False


def _sessao(pipe):
    """Sessão mínima para os caminhos do handler (o resto é `.get()`)."""
    return {"id": "t126", "buffer": bytearray(), "fila": queue.Queue(),
            "fechar": False, "turno": 0, "pipe": pipe,
            "cancelado": threading.Event(),
            "turno_pendente": None, "turno_pendente_barge": False,
            "pend_thread": None, "pendentes_descartados": 0,
            "buffer_consumido": 0}


def _eventos(sess):
    saida = []
    while True:
        try:
            canal, ev = sess["fila"].get_nowait()
        except queue.Empty:
            return saida
        if canal == "json":
            saida.append(ev)


def _espera(cond, timeout=5.0):
    prazo = time.monotonic() + timeout
    while time.monotonic() < prazo:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def sem_historico(monkeypatch):
    """`_live_enriquece` persiste o histórico no turn_complete — fora do escopo."""
    monkeypatch.setattr(app, "_live_hist_pos_turno", lambda sess: None)


def test_rearm_nao_apaga_trecho_que_chegou_entre_o_pop_e_o_rearm():
    """#208: o guarda acumula entre o `pop` do observador e o re-arm. O re-arm
    antigo regravava o valor que ele tinha lido, então o trecho NOVO do usuário
    (falado enquanto o pipeline voltava a ficar ocupado) era apagado."""
    pipe = CanalFalso()
    pipe.end_of_speech()                     # pipeline ocupado: observador espera
    sess = _sessao(pipe)
    app._live_guarda_pendente(sess, b"\x01" * 10, False)
    pendente = sess.pop("turno_pendente")    # o observador "levou" o pendente
    app._live_guarda_pendente(sess, b"\x02" * 10, True)   # chegou NA janela
    app._live_pend_rearma(sess, pendente, False)
    assert sess.get("turno_pendente") == b"\x01" * 10 + b"\x02" * 10, \
        "o trecho que chegou na janela foi apagado pelo re-arm"
    assert sess.get("turno_pendente_barge") is True, "barge é pegajoso no re-arm"


def test_guarda_espera_o_lock_do_pendente():
    """O read-modify-write do guarda é serializado com o do observador."""
    pipe = CanalFalso()
    pipe.end_of_speech()                     # ocupado: o observador não consome já
    sess = _sessao(pipe)
    lock = app._live_pend_lock(sess)
    lock.acquire()
    th = threading.Thread(target=app._live_guarda_pendente,
                          args=(sess, b"\x03" * 8, False))
    th.start()
    time.sleep(0.15)
    assert sess.get("turno_pendente") is None, "escreveu com o lock preso"
    lock.release()
    assert _espera(lambda: sess.get("turno_pendente") is not None)
    assert sess["turno_pendente"] == b"\x03" * 8
    th.join(5)


# ---------------------------------------------------------------------------
# Unidade: a fila de 1 pendente (com acúmulo)
# ---------------------------------------------------------------------------

def test_fala_com_turno_em_curso_vira_pendente_e_abre_so_depois():
    pipe = CanalFalso()
    pipe.end_of_speech()                     # turno 1 "em curso"
    sess = _sessao(pipe)

    app._live_abre_turno(sess, b"\x01\x02" * 100)

    eventos = _eventos(sess)
    assert [e["type"] for e in eventos] == ["turno_pendente"], \
        "a fala não é rejeitada nem vira error"
    assert eventos[0]["buffer_bytes"] == 200
    assert eventos[0]["truncado"] is False
    assert sess.get("turno_pendente") == b"\x01\x02" * 100

    pipe.libera()                            # turno 1 fecha
    assert _espera(lambda: len(pipe.aceitos) == 2), "pendente abre quando libera"
    assert pipe.aceitos[1] == (b"\x01\x02" * 100, False)
    assert sess.get("turno_pendente") is None


def test_dois_pedacos_viram_um_pendente_so():
    """Ajuste do PM: a pessoa completa a frase em dois pedaços — os trechos se
    CONCATENAM e saem como UM turno quando o pipeline liberar."""
    pipe = CanalFalso()
    pipe.end_of_speech()
    sess = _sessao(pipe)

    app._live_abre_turno(sess, b"\x01" * 10)
    app._live_abre_turno(sess, b"\x02" * 20)

    pendentes = [e for e in _eventos(sess) if e["type"] == "turno_pendente"]
    assert len(pendentes) == 2
    assert pendentes[0]["buffer_bytes"] == 10 and pendentes[0]["truncado"] is False
    assert pendentes[1]["buffer_bytes"] == 30, "o 2º pedaço ACUMULA no pendente"
    # o antigo booleano `substituido` virou contador: o cliente vê QUANTOS trechos
    # formam o pendente (e nada saiu no teto ainda)
    assert [e["trechos"] for e in pendentes] == [1, 2]
    assert [e["descartados_ms"] for e in pendentes] == [0, 0]
    assert sess.get("turno_pendente") == b"\x01" * 10 + b"\x02" * 20

    pipe.libera()
    assert _espera(lambda: len(pipe.aceitos) == 2)
    assert len(pipe.aceitos) == 2, "um único turno para os dois pedaços"
    assert pipe.aceitos[1][0] == b"\x01" * 10 + b"\x02" * 20


def test_teto_descarta_o_mais_antigo_e_avisa(monkeypatch):
    """Acima do teto de duração, sai o TRECHO MAIS ANTIGO (o fim é o que completa
    a frase) e o evento vem com `truncado: true` + QUANTO saiu (`descartados_ms`)
    — o antigo booleano `substituido` não dizia se o corte foi de ms ou de s."""
    monkeypatch.setattr(app, "_LIVE_PENDENTE_MAX_BYTES", 6400)   # 200 ms
    pipe = CanalFalso()
    pipe.end_of_speech()
    sess = _sessao(pipe)

    app._live_abre_turno(sess, b"\x01" * 3200)   # antigo: 100 ms
    app._live_abre_turno(sess, b"\x02" * 9600)   # estoura: 12800 > 6400 (-200 ms)

    pendentes = [e for e in _eventos(sess) if e["type"] == "turno_pendente"]
    assert pendentes[-1]["truncado"] is True
    assert pendentes[-1]["descartados_ms"] == 200, "o evento diz QUANTO saiu"
    assert pendentes[-1]["trechos"] == 2
    assert sess.get("turno_pendente") == (b"\x01" * 3200 + b"\x02" * 9600)[-6400:], \
        "mantém a CAUDA (mais novo)"
    assert sess["pendentes_descartados"] == 1

    pipe.libera()
    assert _espera(lambda: len(pipe.aceitos) == 2)
    assert pipe.aceitos[1][0] == (b"\x01" * 3200 + b"\x02" * 9600)[-6400:]


def test_contadores_zeram_por_pendente():
    """`trechos`/`descartados_ms` descrevem o pendente ATUAL: aberto ele, o
    próximo `turno_pendente` não herda o acúmulo do anterior."""
    pipe = CanalFalso()
    pipe.end_of_speech()
    sess = _sessao(pipe)

    app._live_abre_turno(sess, b"\x01" * 10)
    app._live_abre_turno(sess, b"\x02" * 10)     # pendente com 2 trechos
    pipe.libera()
    assert _espera(lambda: len(pipe.aceitos) == 2)   # pendente abre → zera
    assert sess["pendentes_trechos"] == 0

    pipe.end_of_speech()                          # 2º turno preso
    app._live_abre_turno(sess, b"\x03" * 10)
    novo = [e for e in _eventos(sess) if e["type"] == "turno_pendente"][-1]
    assert novo["trechos"] == 1 and novo["descartados_ms"] == 0


class CanalPortao(CanalFalso):
    """`ocupado` responde False na 1ª leitura de CADA thread (sai do laço) e True
    nas seguintes (o `pop` já aconteceu → o caminho é o RE-ARM). O portão
    sincroniza dois observadores para que cheguem JUNTOS ao `pop` — é a corrida
    que o `_live_acorda_pendente` cria na janela entre o `is_alive` e a escrita
    de `pend_thread`."""

    def __init__(self):
        super().__init__()
        self.portao = threading.Event()
        self._vistos = set()
        self._trava_vistos = threading.Lock()
        self._ocupado = True

    @property
    def ocupado(self):
        tid = threading.get_ident()
        with self._trava_vistos:
            primeiro = tid not in self._vistos
            self._vistos.add(tid)
        if primeiro:
            self.portao.wait(5)
            return False
        return self._ocupado


def test_observador_perdedor_nao_zera_os_contadores(monkeypatch):
    """#221: dois observadores acordados pelo MESMO fechamento é o caso NORMAL (o
    `pop` decide: um processa, o outro leva None). O perdedor zerava os
    contadores FORA do lock — entre o `pop` do vencedor e o re-arm dele — e o
    pendente que voltava a existir ficava com `trechos = 0`: o evento seguinte
    SUBCONTAVA (2 trechos acumulados, evento dizendo 1).

    A janela é montada de propósito: o re-arm do vencedor é atrasado, e o
    perdedor só entra quando o pendente já está EM VOO (`turno_pendente` = None
    após o `pop`) — assim o `pop` dele devolve None SEM depender de corrida."""
    pipe = CanalPortao()
    sess = _sessao(pipe)
    sess["pendentes_trechos"] = 0
    sess["pendentes_descartados_ms"] = 0
    _eventos(sess)

    lento = app._live_pend_rearma

    def rearma_lento(s, p, b, *a, **k):           # é a janela do perdedor
        time.sleep(1.0)
        try:
            return lento(s, p, b, *a, **k)        # assinatura COM os contadores
        except TypeError:                         # app.py de antes do #221
            return lento(s, p, b)
    monkeypatch.setattr(app, "_live_pend_rearma", rearma_lento)

    app._live_guarda_pendente(sess, b"\x01" * 10, False)   # arma O1 (preso no portão)
    assert _espera(lambda: sess.get("pend_thread") is not None)
    pipe.portao.set()                             # O1 sai do laço e faz o `pop`
    assert _espera(lambda: sess.get("turno_pendente") is None), "O1 não levou o pendente"

    # o PERDEDOR: entra com o pendente em voo (pop feito, re-arm a caminho) — o
    # `if not pendente` dele é o ramo que zerava os contadores
    perdedor = threading.Thread(target=app._live_descarrega_pendente, args=(sess,),
                                daemon=True)
    perdedor.start()
    perdedor.join(3)
    assert _espera(lambda: sess.get("turno_pendente") is not None), "o pendente não voltou"
    assert sess["pendentes_trechos"] == 1, "o observador perdedor zerou os contadores"

    app._live_guarda_pendente(sess, b"\x02" * 10, False)   # 2º trecho: o evento conta 2
    novo = [e for e in _eventos(sess) if e["type"] == "turno_pendente"][-1]
    assert novo["trechos"] == 2, "o evento `turno_pendente` subconta os trechos"

    pipe.libera()                                 # o pendente abre com o conteúdo INTEIRO
    assert _espera(lambda: bool(pipe.aceitos)), "o pendente não abriu"
    assert pipe.aceitos[-1][0] == b"\x01" * 10 + b"\x02" * 10, \
        "o conteúdo se perdeu (é o #208 de novo)"


def test_pipe_livre_abre_o_turno_na_hora_sem_enfileirar():
    pipe = CanalFalso()
    sess = _sessao(pipe)

    app._live_abre_turno(sess, b"\x03" * 10)

    assert pipe.aceitos == [(b"\x03" * 10, False)]
    assert sess.get("turno_pendente") is None
    assert _eventos(sess) == [], "caminho normal não emite turno_pendente"


def test_pendente_preserva_o_flag_de_barge():
    """Fala nascida no playback mantém a checagem de eco no turno pendente — e o
    flag é PEGAJOSO: qualquer pedaço com barge marca o pendente inteiro."""
    pipe = CanalFalso()
    pipe.end_of_speech()
    sess = _sessao(pipe)

    app._live_abre_turno(sess, b"\x01" * 10)                 # sem barge
    app._live_abre_turno(sess, b"\x02" * 10, barge_in=True)  # nasceu no playback

    assert sess.get("turno_pendente_barge") is True
    pipe.libera()
    assert _espera(lambda: len(pipe.aceitos) == 2)
    assert pipe.aceitos[1] == (b"\x01" * 10 + b"\x02" * 10, True)


def test_ocupado_sem_audio_mantem_o_aviso_antigo():
    """Sem áudio não há o que enfileirar: protocolo antigo segue de pé."""
    pipe = CanalFalso()
    pipe.end_of_speech()
    sess = _sessao(pipe)

    app._live_abre_turno(sess)               # sem pcm e sem buffer

    eventos = _eventos(sess)
    assert len(eventos) == 1 and eventos[0]["type"] == "error"
    assert eventos[0]["code"] == "turno_em_curso"
    assert sess.get("turno_pendente") is None


def test_perdeu_a_corrida_vira_pendente_em_vez_de_sumir():
    """`end_of_speech` que retorna False (turno abriu no intervalo) não perde a fala."""
    pipe = CanalFalso()
    sess = _sessao(pipe)
    pipe.push_pcm(b"\x05" * 10)
    pipe.end_of_speech()                     # abre POR OUTRO lado (corrida)

    app._live_abre_turno(sess, b"\x06" * 20)

    eventos = _eventos(sess)
    assert [e["type"] for e in eventos] == ["turno_pendente"]
    pipe.libera()
    assert _espera(lambda: len(pipe.aceitos) == 2)
    assert pipe.aceitos[1][0] == b"\x06" * 20


def test_fechamento_de_turno_acorda_o_pendente_sem_observador():
    """O turn_complete é a janela de abertura mesmo sem observador vivo."""
    pipe = CanalFalso()
    sess = _sessao(pipe)
    sess["turno_pendente"] = b"\x04" * 10    # pendente órfão (observador morreu)

    app._live_envia_json(sess, {"type": "turn_complete", "turn": 1})

    assert _espera(lambda: len(pipe.aceitos) == 1)
    assert pipe.aceitos[0][0] == b"\x04" * 10


def test_turno_que_termina_em_error_libera_o_pendente():
    """Ajuste do PM: o pendente NÃO herda o erro — `error` do turno em curso
    também abre a janela e a fala guardada é respondida na sequência."""
    pipe = CanalFalso()
    sess = _sessao(pipe)
    sess["turno_pendente"] = b"\x07" * 10    # órfão: só o evento do erro acorda

    app._live_envia_json(sess, {"type": "error", "code": "pipeline",
                                "message": "boom"})

    assert _espera(lambda: len(pipe.aceitos) == 1)
    assert pipe.aceitos[0][0] == b"\x07" * 10


def test_cancel_do_cliente_limpa_o_pendente():
    """`cancel` é abortar TUDO: o pendente sai junto (e conta como descartado)."""
    pipe = CanalFalso()
    pipe.end_of_speech()
    sess = _sessao(pipe)
    app._live_abre_turno(sess, b"\x08" * 10)
    _eventos(sess)                           # drena o turno_pendente

    app._live_cancela(sess, do_cliente=True)

    assert sess.get("turno_pendente") is None
    assert sess["pendentes_descartados"] == 1
    pipe.libera()
    time.sleep(0.2)
    assert len(pipe.aceitos) == 1, "o pendente cancelado não vira turno"


def test_barge_in_mantem_o_pendente():
    """Barge-in NÃO limpa: a fala que interrompeu abre turno próprio (ou já está
    no pendente) e é respondida depois."""
    pipe = CanalFalso()
    pipe.end_of_speech()
    sess = _sessao(pipe)
    app._live_abre_turno(sess, b"\x09" * 10)     # fala do barge já enfileirada
    _eventos(sess)

    app._live_cancela(sess)                      # barge-in (não é do cliente)

    assert sess.get("turno_pendente") == b"\x09" * 10, "barge-in preserva"
    pipe.libera()
    assert _espera(lambda: len(pipe.aceitos) == 2)
    assert pipe.aceitos[1][0] == b"\x09" * 10


def test_fala_na_janela_de_morte_do_observador_nao_fica_orfa():
    """Gate #126/#127 (achado 2): a guarda que correu enquanto o observador
    ainda constava vivo não arma sucessor — se o pendente dela nascer na janela
    entre o trabalho do observador e a morte da thread, só o re-arm no fim do
    observador o salva (sem isso, órfão para sempre com o pipeline livre)."""
    pipe = CanalFalso()                      # livre: o observador abre na hora
    sess = _sessao(pipe)
    sess["turno_pendente"] = b"\x0a" * 10

    original_end = pipe.end_of_speech

    def end_com_guarda_na_janela(*a, **k):
        r = original_end(*a, **k)
        # guarda que correu no MEIO da abertura: viu a thread viva, não armou
        sess["turno_pendente"] = b"\x0b" * 20
        return r

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipe, "end_of_speech", end_com_guarda_na_janela)
        # observador registrado como o `pend_thread` da sessão (como o acorda faz)
        t = threading.Thread(target=app._live_descarrega_pendente, args=(sess,),
                             daemon=True)
        sess["pend_thread"] = t
        t.start()
        t.join(5)

    assert pipe.aceitos and pipe.aceitos[0][0] == b"\x0a" * 10, "1º pendente abriu"
    assert sess.get("pend_thread") is not t, "o moribundo armou o sucessor"
    pipe.libera()                            # turno do 1º pendente fecha
    assert _espera(lambda: len(pipe.aceitos) == 2), \
        "o pendente salvo na janela de morte abre pelo re-arm"
    assert pipe.aceitos[1][0] == b"\x0b" * 20


def test_fechar_nao_gera_observadores_em_cadeia():
    """O re-arm no fim do observador não pode rodar com a sessão fechando:
    pendente + `fechar` morre com a sessão (sem nascedouro de threads)."""
    pipe = CanalFalso()
    pipe.end_of_speech()                     # ocupado
    sess = _sessao(pipe)
    sess["turno_pendente"] = b"\x0c" * 10
    sess["fechar"] = True

    app._live_descarrega_pendente(sess)      # sai no `fechar`, sem re-arm

    assert sess.get("turno_pendente") is None, "pendente morre com a sessão"
    assert sess.get("pend_thread") is None, "nenhum observador armado no fechar"


# ---------------------------------------------------------------------------
# Integração no WS: turno preso + fala simultânea + fechamento na ordem
# ---------------------------------------------------------------------------

@pytest.fixture()
def ws_limpo(monkeypatch):
    """WS sem FSM (o buffer da sessão acumula os frames) e sessões zeradas."""
    monkeypatch.setattr(app, "_live_turns_mod", None)
    monkeypatch.setattr(app, "_LIVE_STATS_MS", 0)
    with app._live_lock:
        app._live_sessions.clear()
    try:
        yield
    finally:
        with app._live_lock:
            app._live_sessions.clear()


def _ate(ws, tipos, maximo=20):
    """Lê até um dos tipos (ignora `stats`/`prewarm` e frames de áudio)."""
    for _ in range(maximo):
        m = ws.receive()
        if m.get("bytes"):
            continue
        ev = __import__("json").loads(m["text"])
        if ev["type"] in ("stats", "prewarm"):
            continue
        if ev["type"] in tipos:
            return ev
    raise AssertionError(f"não veio nenhum de {tipos}")


def test_ws_turno_ocupado_com_fala_simultanea_responde_na_ordem(ws_limpo, monkeypatch):
    """Cenário do dono (#126): resposta longa segura o pipeline; o usuário fala
    em DOIS pedaços — ambos são ANOTADOS (`turno_pendente`) e respondidos num
    ÚNICO turno na sequência, sem repetir e sem error `turno_em_curso`."""
    liberar_stt = threading.Event()
    stt_vistos = []

    def stt_falso(pcm, language=None):
        if len(pcm) == 3200 and not liberar_stt.is_set():   # 1º turno: preso
            liberar_stt.wait(5)
        stt_vistos.append(len(pcm))
        return "oi"

    def llm_falso(msgs):
        yield "ok "

    def tts_falso(texto, omni):
        return np.zeros(240, dtype="float32")

    def pipe_novo(sess):
        hist = [{"role": h["role"], "content": h["text"]} for h in sess["history"]]
        return lp.LivePipeline(
            lambda obj: app._live_envia_json(sess, obj),
            lambda pcm: app._live_envia_audio(sess, pcm),
            voice_id=sess["voice_id"], system=sess["system"] or None,
            history=hist, stt=stt_falso, llm=llm_falso, tts=tts_falso,
            prewarm=lambda **k: None)

    monkeypatch.setattr(app, "_live_pipe_novo", pipe_novo)

    cliente = TestClient(app.app, raise_server_exceptions=False,
                         client=("127.0.0.1", 50000))
    with cliente.websocket_connect("/api/live/ws") as ws:
        ws.send_json({"type": "setup"})
        assert ws.receive_json()["type"] == "ready"

        ws.send_bytes(b"\x00\x01" * 1600)    # turno 1 (3200 bytes)
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, {"speech_start"})["type"] == "speech_start"

        ws.send_bytes(b"\x00\x02" * 800)     # fala simultânea, pedaço A (1600 NOVOS)
        ws.send_json({"type": "end_of_speech"})
        pend_a = _ate(ws, {"turno_pendente"})
        assert pend_a["buffer_bytes"] == 1600, "só a fala nova (sem repetir o turno)"
        ws.send_bytes(b"\x00\x03" * 800)     # pedaço B: completa a frase
        ws.send_json({"type": "end_of_speech"})
        pend_b = _ate(ws, {"turno_pendente"})
        assert pend_b["buffer_bytes"] == 3200, "B ACUMULA no pendente"
        assert stt_vistos == [], "o turno 1 segue em curso (STT preso)"

        liberar_stt.set()                    # turno 1 fecha…
        assert _ate(ws, {"turn_complete"})["type"] == "turn_complete"
        assert _ate(ws, {"speech_start"})["type"] == "speech_start"   # …e o pendente abre
        assert _ate(ws, {"turn_complete"})["type"] == "turn_complete"

        assert stt_vistos == [3200, 3200], \
            "ordem: turno 1 primeiro; os dois pedaços viram UM turno de 3200 bytes"


def test_ws_turno_que_termina_em_error_pipeline_acorda_o_pendente(ws_limpo, monkeypatch):
    """Ajuste do PM, PONTA-A-PONTA: o turno em curso ESTOURA (`error{pipeline}`,
    sem `turn_complete`) e o pendente tem de abrir mesmo assim — o erro do turno
    anterior não é herdado. Prova também que o pipeline fica LIVRE no `finally`
    (sem isso o observador esperaria para sempre) e que o erro chega ao cliente."""
    preso = threading.Event()
    stt_vistos = []

    def stt_falso(pcm, language=None):
        stt_vistos.append(len(pcm))
        if len(stt_vistos) == 1:        # 1º turno: espera o pendente chegar…
            preso.wait(5)
            raise RuntimeError("stt morreu")   # …e MORRE antes do turn_complete
        return "de novo"

    def llm_falso(msgs):
        yield "ok "

    def tts_falso(texto, omni):
        return np.zeros(240, dtype="float32")

    def pipe_novo(sess):
        hist = [{"role": h["role"], "content": h["text"]} for h in sess["history"]]
        return lp.LivePipeline(
            lambda obj: app._live_envia_json(sess, obj),
            lambda pcm: app._live_envia_audio(sess, pcm),
            voice_id=sess["voice_id"], system=sess["system"] or None,
            history=hist, stt=stt_falso, llm=llm_falso, tts=tts_falso,
            prewarm=lambda **k: None)

    monkeypatch.setattr(app, "_live_pipe_novo", pipe_novo)

    cliente = TestClient(app.app, raise_server_exceptions=False,
                         client=("127.0.0.1", 50000))
    with cliente.websocket_connect("/api/live/ws") as ws:
        ws.send_json({"type": "setup"})
        assert ws.receive_json()["type"] == "ready"

        ws.send_bytes(b"\x00\x01" * 1600)             # turno 1 (3200 bytes)
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, {"speech_start"})

        ws.send_bytes(b"\x00\x02" * 800)              # fala nova: vira pendente
        ws.send_json({"type": "end_of_speech"})
        assert _ate(ws, {"turno_pendente"})["buffer_bytes"] == 1600

        preso.set()                                   # turno 1 estoura
        erro = _ate(ws, {"error"})
        assert erro["code"] == "pipeline"
        assert _ate(ws, {"speech_start"}), "o pendente abre apesar do error"
        assert _ate(ws, {"turn_complete"})
        assert stt_vistos == [3200, 1600], "o pendente abriu com a fala NOVA"
