"""Cancel do Live reconhecido NA HORA (#1129, anomalia do barge).

Medido no proxy do #1129 (eco −15): com o pipeline gerando TTS, `_live_cancela` só
ligava a flag e o `interrupted` saía quando o trecho de TTS em geração VOLTAVA — ~2 s
depois do `cancel` (stop de 6640 ms) — e o motor repetia `barge_in` + `cancel` a cada
400 ms enquanto isso. Agora:
  * a sessão emite o `interrupted` na hora (um por turno);
  * o áudio do turno ainda na fila do WS sai dela;
  * o PCM que o trecho em curso terminar de gerar é DESCARTADO, nunca enviado;
  * o `interrupted` atrasado do pipeline para o MESMO turno é engolido — o de um turno
    NOVO, não;
  * o trecho em curso termina COM a `_gen_lock` e o turno seguinte não a pega antes
    (a mesma `_gen_lock` do `_tts_app`).
As células usam o `LivePipeline` real com dublês de STT/LLM/TTS (sem MLX).
"""
from __future__ import annotations

import threading
import time

import numpy as np

import app
import live_pipeline as lp


def _sess(**extra) -> dict:
    sess = {"id": "s-cancel", "t0": time.monotonic(), "criada": time.time(),
            "visto": time.monotonic(), "turno": 1, "fila": app._LiveFila(1 << 22),
            "cancelado": threading.Event(), "engine": None, "pipe": None,
            "falando": False, "audio_pendente": 0, "st_stage": "idle",
            "st_stage_ini": None, "st_erro": None, "st_chunks": 0, "st_audio_bytes": 0,
            "st_proximo": 0.0, "st_bytes": 0, "vencida": False,
            "substituida": False, "fechar": False}
    sess.update(extra)
    return sess


def _drena(fila) -> list:
    itens = []
    while not fila.empty():
        itens.append(fila.get(False))
    return itens


def _envia_pcm(sess, pipe):
    """O MESMO portão do `_envia_pcm` do app (o PCM do turno cancelado não entra)."""
    def envia(pcm):
        if app._live_pcm_do_turno_cancelado(sess, pipe):
            return
        sess["fila"].put(("audio", pcm))
    return envia


class _Metal:
    def __init__(self):
        self._g = threading.Lock()
        self.agora = self.pico = 0
        self.ordem: list[str] = []

    def gera(self, quem, dur):
        with self._g:
            self.agora += 1
            self.pico = max(self.pico, self.agora)
            self.ordem.append(quem + ">")
        time.sleep(dur)
        with self._g:
            self.agora -= 1
            self.ordem.append("<" + quem)


def _pipeline(sess, tts_s=0.6, metal=None, frases=("Primeira frase. ", "Segunda frase. ")):
    gerando = threading.Event()
    cont = {"n": 0}

    def tts(texto, omni):
        cont["n"] += 1
        gerando.set()
        if metal is not None:
            with app._gen_lock:                      # a mesma trava do `_tts_app`
                metal.gera(f"T{p._turno_atual}.{cont['n']}", tts_s)
        else:
            time.sleep(tts_s)
        return np.ones(2400, dtype=np.float32) * 0.1

    def llm(msgs):
        for f in frases:
            yield f

    p = lp.LivePipeline(lambda o: app._live_envia_json(sess, o), None,
                        stt=lambda pcm, lang: "oi", llm=llm, tts=tts,
                        prewarm=lambda: None)
    p.emit_audio = _envia_pcm(sess, p)
    sess["pipe"] = p
    return p, gerando


def _roda_turno(p):
    p.push_pcm(b"\x00\x00" * 800)
    t = threading.Thread(target=p.end_of_speech, args=(False,), daemon=True)
    t.start()
    return t


def test_cancel_no_meio_do_tts_avisa_na_hora_e_o_trecho_tardio_nao_vai_ao_ar():
    sess = _sess()
    p, gerando = _pipeline(sess)
    t = _roda_turno(p)
    assert gerando.wait(5), "o TTS do turno não começou"
    time.sleep(0.05)
    sess["turno"] = p._turno_atual
    antes = _drena(sess["fila"])                     # o que saiu antes do cancel
    t0 = time.monotonic()
    app._live_cancela(sess, forcar=True)
    dt = time.monotonic() - t0
    depois_cancel = _drena(sess["fila"])
    assert [i for i in depois_cancel if i[0] == "json" and i[1]["type"] == "interrupted"], \
        f"sem interrupted imediato: {depois_cancel}"
    assert dt < 0.1, f"o cancel esperou o trecho de TTS: {dt:.2f}s"
    t.join(10)                                       # o trecho em curso termina…
    tarde = _drena(sess["fila"])
    assert not [i for i in tarde if i[0] == "audio"], "PCM do turno cancelado foi ao ar"
    assert not [i for i in tarde if i[0] == "json" and i[1]["type"] == "interrupted"], \
        "o pipeline duplicou o interrupted do mesmo turno"
    assert not [i for i in antes if i[0] == "json" and i[1].get("type") == "interrupted"]


def test_interrupted_de_turno_novo_nao_e_engolido():
    class PipeOcupado:
        ocupado = True
        _turno_atual = 5

        def cancel(self, sem_historico=False):
            pass

    pipe = PipeOcupado()
    sess = _sess(pipe=pipe, turno=5)
    app._live_cancela(sess, forcar=True)                     # turno N=5: na hora
    assert [i[1]["turn"] for i in _drena(sess["fila"])] == [5]
    app._live_envia_json(sess, {"type": "interrupted", "turn": 5,
                                "_do_pipeline": True, "_turno_pipe": 5})
    assert _drena(sess["fila"]) == [], "atrasado do turno 5 tinha de ser engolido"
    # N+1 começa logo em seguida: o `interrupted` dele passa (do pipeline e da sessão)
    pipe._turno_atual = 6
    sess["turno"] = 6
    app._live_envia_json(sess, {"type": "interrupted", "turn": 6,
                                "_do_pipeline": True, "_turno_pipe": 6})
    (item,) = _drena(sess["fila"])
    assert item[1]["turn"] == 6
    assert "_do_pipeline" not in item[1] and "_turno_pipe" not in item[1], "marca interna vazou"
    app._live_cancela(sess, forcar=True)
    assert [i[1]["turn"] for i in _drena(sess["fila"])] == [6]
    assert not app._live_pcm_do_turno_cancelado(sess, type("P", (), {"_turno_atual": 7})())


def test_barge_repetido_no_mesmo_turno_gera_um_interrupted_so():
    class PipeOcupado:
        ocupado = True
        _turno_atual = 3

        def cancel(self, sem_historico=False):
            pass

    sess = _sess(pipe=PipeOcupado(), turno=3)
    for _ in range(4):                                       # 4× a cada ~400 ms no proxy
        app._live_cancela(sess, forcar=True)
    assert len(_drena(sess["fila"])) == 1


def test_cancel_tira_da_fila_o_audio_do_turno_e_deixa_o_json():
    class PipeOcupado:
        ocupado = True
        _turno_atual = 2

        def cancel(self, sem_historico=False):
            pass

    sess = _sess(pipe=PipeOcupado(), turno=2, audio_pendente=3)
    for k in range(3):
        sess["fila"].put(("audio", b"\x00\x01" * 100))
    sess["fila"].put(("json", {"type": "assistant_text", "delta": "x"}))
    app._live_cancela(sess, forcar=True)
    itens = _drena(sess["fila"])
    assert [i[0] for i in itens] == ["json", "json"]
    assert itens[-1][1]["type"] == "interrupted"
    assert sess["fila"].bytes == 0 and sess["audio_pendente"] == 0


def test_o_trecho_em_curso_termina_com_a_trava_e_o_turno_seguinte_espera():
    metal = _Metal()
    sess = _sess()
    p, gerando = _pipeline(sess, tts_s=0.5, metal=metal, frases=("Uma frase. ",))
    t = _roda_turno(p)
    assert gerando.wait(5)
    time.sleep(0.05)
    sess["turno"] = p._turno_atual
    app._live_cancela(sess, forcar=True)
    t.join(10)
    t2 = _roda_turno(p)                                      # turno seguinte, logo depois
    t2.join(10)
    assert metal.pico == 1, f"dois trechos juntos no Metal: {metal.ordem}"
    # o trecho do turno cancelado TERMINOU antes do trecho do turno seguinte começar
    fim_n = metal.ordem.index("<T1.1")
    inicio_n1 = next(i for i, x in enumerate(metal.ordem) if x.startswith("T2"))
    assert fim_n < inicio_n1, metal.ordem
    audio_n1 = [i for i in _drena(sess["fila"]) if i[0] == "audio"]
    assert audio_n1, "o turno seguinte (não cancelado) tem de ir ao ar"


def test_o_envio_de_pcm_do_app_passa_pelo_portao():
    import inspect
    fonte = inspect.getsource(app)
    i = fonte.index("def _envia_pcm(")
    assert "_live_pcm_do_turno_cancelado(sess, pipe)" in fonte[i:i + 600]


# --- uma célula por CAMADA: cada uma sozinha tem de segurar o PCM tardio ----------

def test_camada_pipeline_trecho_que_volta_depois_do_cancel_nao_e_emitido():
    """Só o pipeline (sem o portão do app): o `_fala_worker` vê o cancel quando o
    `_tts` volta e NÃO emite aquele trecho."""
    saiu = []
    gerando = threading.Event()

    def tts(texto, omni):
        gerando.set()
        time.sleep(0.4)
        return np.ones(2400, dtype=np.float32) * 0.1

    p = lp.LivePipeline(lambda o: None, saiu.append, stt=lambda pcm, lang: "oi",
                        llm=lambda m: iter(["Uma frase. "]), tts=tts, prewarm=lambda: None)
    t = _roda_turno(p)
    assert gerando.wait(5)
    p.cancel()                                   # chega DURANTE a síntese
    t.join(10)
    assert saiu == [], "o trecho que terminou depois do cancel foi emitido"


def test_camada_app_cancel_que_chega_depois_do_check_do_pipeline_e_barrado():
    """Corrida: o pipeline já passou do `if self.cancelado` e está no `emit_audio`
    quando o cancel da sessão chega — o portão do app (`_envia_pcm`) barra o PCM."""
    sess = _sess()
    p, gerando = _pipeline(sess, tts_s=0.05, frases=("Uma frase. ",))
    porta = _envia_pcm(sess, p)

    def emite_com_cancel_no_meio(pcm):
        sess["turno"] = p._turno_atual
        app._live_cancela(sess, forcar=True)     # chega entre o check e o envio
        porta(pcm)

    p.emit_audio = emite_com_cancel_no_meio
    _roda_turno(p).join(10)
    itens = _drena(sess["fila"])
    assert not [i for i in itens if i[0] == "audio"], "o portão do app deixou passar"
    assert [i for i in itens if i[0] == "json" and i[1]["type"] == "interrupted"]


# --------------------------------------------------------------------------
# gate do qa (task_78871739): portão depois da marca, `_bytes` sem foto,
# turno marcado antes do envio, `fala_ms` no ramo do pipeline ocupado
# --------------------------------------------------------------------------
class _PipeOcupado:
    ocupado = True

    def __init__(self, turno):
        self._turno_atual = turno

    def cancel(self, sem_historico=False):
        pass


def test_cancel_durante_a_marca_nao_deixa_o_pcm_tardio_ir_ao_ar(monkeypatch):
    """P2 do qa: o portão do `_envia_pcm` passa, a marca segura o chunk (até 1,5 s) e
    o cancel chega no meio. Sem o 2º portão o chunk entrava DEPOIS do `interrupted`."""
    entrou, libera = threading.Event(), threading.Event()

    def marca(pcm, sr=16000):
        entrou.set()
        libera.wait(5)
        return pcm
    monkeypatch.setattr(app, "_marca_pcm", marca)
    sess = _sess()
    p, _ = _pipeline(sess)

    def envia_pcm(pcm):
        if app._live_pcm_do_turno_cancelado(sess, sess.get("pipe")):
            return
        app._live_envia_audio(sess, pcm, marcar=True)
    p.emit_audio = envia_pcm
    t = _roda_turno(p)
    assert entrou.wait(5)
    sess["turno"] = p._turno_atual
    app._live_cancela(sess, forcar=True)                   # chega DURANTE a marca
    libera.set()
    t.join(10)
    assert not [i for i in _drena(sess["fila"]) if i[0] == "audio"]


def test_descarta_audio_nao_apaga_o_put_concorrente_do_contador():
    """P3 do qa: o `put` da thread do TTS caindo entre a varredura (sob o mutex da
    fila) e o acerto do `_bytes` (sob `_trava`) era sobrescrito pela foto velha."""
    fila = app._LiveFila(1 << 22)
    fila.put(("audio", b"\x00" * 100))                     # o áudio do turno cancelado
    real = fila._trava
    pendente = [lambda: fila.put(("audio", b"\x01" * 100))]

    class Trava:
        def __enter__(self):
            if pendente:
                pendente.pop()()                           # o put da outra thread
            return real.__enter__()

        def __exit__(self, *a):
            return real.__exit__(*a)
    fila._trava = Trava()
    assert fila.descarta_audio() == 1
    assert fila.bytes == 100 and fila.qsize() == 1
    fila.get(False)
    assert fila.bytes == 0, "contador negativo: o teto de 2 MiB vira furo"


def test_interrupted_atrasado_entre_o_envio_e_a_marca_do_turno_e_engolido(monkeypatch):
    """P3 do qa: o turno entra em `_interrompidos` ANTES de o aviso ir à fila; o
    `interrupted` do pipeline que chegar nesse vão é engolido, não duplica."""
    sess = _sess(pipe=_PipeOcupado(5), turno=5)
    orig = app._live_envia_json
    tardio = {"type": "interrupted", "turn": 5, "_do_pipeline": True, "_turno_pipe": 5}

    def envia(s, obj):
        if tardio is not None and obj.get("type") == "interrupted" \
                and not obj.get("_do_pipeline"):
            orig(s, dict(tardio))                          # a outra thread chega antes
        orig(s, obj)
    monkeypatch.setattr(app, "_live_envia_json", envia)
    app._live_cancela(sess, forcar=True)
    tipos = [i[1].get("type") for i in _drena(sess["fila"]) if i[0] == "json"]
    assert tipos.count("interrupted") == 1


def test_fala_ms_vai_no_interrupted_do_ramo_do_pipeline_ocupado():
    """P3 do qa: os testes de `fala_ms` (#881) só cobriam o ramo antigo (`pipe=None`)."""
    sess = _sess(pipe=_PipeOcupado(2), turno=2)
    app._live_cancela(sess, forcar=True, fala_ms=850)
    avisos = [i[1] for i in _drena(sess["fila"])
              if i[0] == "json" and i[1].get("type") == "interrupted"]
    assert len(avisos) == 1 and avisos[0].get("fala_ms") == 850
