"""#1129 — o log persistente da 7860 carrega os campos do pré-registro `22d2298`.

O qa reprovou o #1128 (`e225b9c`): o destino e a rotação estavam certos, mas o log não
tinha 3 dos 4 campos que a medida do #1129 exige. Aqui as células EXIGEM cada campo:

1. `speech_start` e `speech_end` com `barge_in=` (o critério do H1 conta `speech_start{barge_in}`);
2. `niveis` com `dbfs=` (mic) e `eco_dbfs=` a cada ~500 ms, SÓ durante o playback e só
   como número — nenhuma amostra de áudio;
3. `playback_inicio` / `playback_fim` com `duracao_ms` (bordas do `set_speaking`).

Nomes de campo exatos do pré-registro `evidence/1129-barge-nao-detectado-prereg.md`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import app

REPO = Path(__file__).resolve().parent.parent
APP_SRC = (REPO / "app.py").read_text()


def _linhas(caplog, evento):
    return [r.getMessage() for r in caplog.records
            if r.name == "live" and r.getMessage().startswith(evento + " ")]


class _Quadro:
    def __init__(self, amostra, prob):
        self.amostra, self.prob = amostra, prob


class _Motor:
    """Motor falso: `_historico` é o deque de quadros de 32 ms pontuados (como o real)."""

    def __init__(self, eco, ruido=-60.0, prob=0.0, ativo=False, tocando=True):
        self._eco, self._ruido, self.ativo, self.tocando = eco, ruido, ativo, tocando
        self._historico = [_Quadro(0, prob)]

    def estatisticas(self):
        return {"eco_dbfs": self._eco, "noise_dbfs": self._ruido, "playback_ativo": self.ativo,
                "tocando": self.tocando}

    def quadros(self, *probs):
        """Um frame do cliente traz VÁRIOS quadros: injeta todos de uma vez."""
        n = self._historico[-1].amostra
        for v in probs:
            n += 512
            self._historico.append(_Quadro(n, v))


def _sess(falando=None, **extra):
    """`falando` é só açúcar de teste: liga/desliga a JANELA de playback do motor falso
    (o gate real é `estatisticas()["playback_ativo"]`, não `sess["falando"]`)."""
    motor = extra.pop("engine", None) or _Motor(-32.0, ativo=bool(falando))
    s = {"id": "sx", "turno": 3, "st_dbfs": -41.5, "engine": motor, **extra}
    if falando:                    # janela já aberta, histórico inteiro ainda por ler
        s["pb_ativo"] = True
        s["niv_amostra"] = -1
    return s


def _campos(linha):
    return dict(kv.split("=", 1) for kv in linha.split()[1:])


# --- (2) niveis -----------------------------------------------------------------

def test_niveis_so_durante_o_playback_e_com_todos_os_campos(caplog):
    caplog.set_level(logging.INFO, logger="live")
    s = _sess(falando=False)
    app._live_niveis(s)
    assert not _linhas(caplog, "niveis"), "sem playback não há nível"
    s = _sess(falando=True)
    s["engine"].quadros(0.0)
    app._live_niveis(s)
    (linha,) = _linhas(caplog, "niveis")
    c = _campos(linha)
    assert c["sess"] == "sx" and c["dbfs_pico"] == "-41.5" and c["dbfs_media"] == "-41.5"
    assert c["eco_dbfs"] == "-32.0" and c["noise_dbfs"] == "-60.0" and c["prob_pico"] == "0.0"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}", c["ts"]), c["ts"]


def test_a_janela_de_500ms_leva_pico_e_media_e_o_pico_da_prob(caplog, monkeypatch):
    """Uma amostra instantânea perde a fala curta (o barge decide em ~224 ms): a janela
    acumula CADA frame e emite pico/média no fim dos 500 ms."""
    caplog.set_level(logging.INFO, logger="live")
    t = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True)
    app._live_niveis(s)                      # t=0: emite a 1ª janela (1 frame) e a zera
    caplog.clear()
    for dt, dbfs, prob in ((0.1, -50.0, 0.1), (0.2, -20.0, 0.9), (0.3, -40.0, 0.3), (0.4, -50.0, 0.2)):
        t[0] = 100.0 + dt
        s["st_dbfs"] = dbfs
        s["engine"].quadros(prob)
        app._live_niveis(s)
    assert not _linhas(caplog, "niveis"), "ainda dentro da janela"
    t[0] = 100.5
    s["st_dbfs"] = -30.0
    s["engine"].quadros(0.5)
    app._live_niveis(s)
    (linha,) = _linhas(caplog, "niveis")
    c = _campos(linha)
    assert c["dbfs_pico"] == "-20.0", c            # o pico de uma fala curta NÃO se perde
    assert c["dbfs_media"] == "-38.0", c           # (-50-20-40-50-30)/5
    assert c["prob_pico"] == "0.9", c


def test_prob_pico_e_o_maximo_dos_quadros_da_janela_nao_o_ultimo(caplog, monkeypatch):
    """Critério do speech (#1129): quadro com prob 0,9 NO MEIO de quadros com 0,1 e o
    `prob_pico` tem de ser 0,9. O último quadro (ou `st_prob`, velho entre eventos) daria 0,1.
    Cada frame do cliente traz vários quadros de 32 ms, e o pico pode cair em qualquer um."""
    caplog.set_level(logging.INFO, logger="live")
    t = [200.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True, st_prob=0.1)         # `st_prob` velho, como entre eventos
    app._live_niveis(s)                          # 1ª janela (descartada)
    caplog.clear()
    for dt, probs in ((0.1, (0.1, 0.1, 0.1)), (0.2, (0.1, 0.9, 0.1)), (0.3, (0.1, 0.1, 0.1)),
                      (0.4, (0.1, 0.1, 0.1))):
        t[0] = 200.0 + dt
        s["engine"].quadros(*probs)
        app._live_niveis(s)
    t[0] = 200.5
    s["engine"].quadros(0.1, 0.1, 0.1)
    app._live_niveis(s)
    (linha,) = _linhas(caplog, "niveis")
    assert _campos(linha)["prob_pico"] == "0.9", linha


def test_dbfs_pico_maior_que_a_media_com_um_pico_no_meio(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [300.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True)
    app._live_niveis(s)
    caplog.clear()
    for dt, dbfs in ((0.1, -55.0), (0.2, -55.0), (0.3, -12.0), (0.4, -55.0), (0.5, -55.0)):
        t[0] = 300.0 + dt
        s["st_dbfs"] = dbfs
        app._live_niveis(s)
    c = _campos(_linhas(caplog, "niveis")[0])
    assert float(c["dbfs_pico"]) == -12.0
    assert float(c["dbfs_pico"]) > float(c["dbfs_media"]) + 20, c


def test_quadros_de_antes_do_playback_nao_entram_na_janela(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [50.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=False)
    s["engine"].quadros(0.99)                    # voz do dono ANTES da resposta tocar
    s["engine"].ativo = True
    app._live_niveis(s)                          # borda de início: ancora o histórico
    caplog.clear()
    s["engine"].quadros(0.1, 0.2)
    t[0] = 50.6
    app._live_niveis(s)
    assert _campos(_linhas(caplog, "niveis")[0])["prob_pico"] == "0.2"


def test_cada_janela_comeca_zerada(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [10.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True, st_dbfs=-10.0)
    app._live_niveis(s)
    t[0] = 10.6
    s["st_dbfs"] = -70.0
    app._live_niveis(s)
    segunda = _campos(_linhas(caplog, "niveis")[1])
    assert segunda["dbfs_pico"] == "-70.0", segunda   # o -10 da janela anterior não vaza


def test_niveis_respeita_a_cadencia_de_500ms(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True)
    for dt in (0.0, 0.1, 0.3, 0.49, 0.5, 0.7, 1.0):
        t[0] = 100.0 + dt
        app._live_niveis(s)
    assert len(_linhas(caplog, "niveis")) == 3


def test_niveis_nao_derruba_se_o_motor_falha(caplog):
    caplog.set_level(logging.INFO, logger="live")

    class Quebrado:
        _historico: list = []

        def estatisticas(self):
            raise RuntimeError("x")

    s = _sess(engine=Quebrado())
    app._live_niveis(s)
    assert not _linhas(caplog, "niveis"), "motor que falha = sem janela confirmada, sem linha"


def test_niveis_so_leva_numero_e_carimbo_nenhum_audio(caplog):
    caplog.set_level(logging.INFO, logger="live")
    app._live_niveis(_sess(falando=True))
    (linha,) = _linhas(caplog, "niveis")
    assert re.fullmatch(
        r"niveis sess=sx ts=\S+ n=\d+ tocando=(True|False) fila_ms=\d+ dbfs_pico=-?[\d.]+ dbfs_media=-?[\d.]+ prob_pico=[\d.]+ "
        r"eco_dbfs=-?[\d.]+ noise_dbfs=-?[\d.]+", linha), linha


# --- (3) bordas do playback -------------------------------------------------------

def _chunk(ms):
    return b"\0\0" * int(app._LIVE_TTS_RATE * ms / 1000)


def test_bordas_derivam_da_janela_do_motor_e_somam_o_episodio(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [10.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=False)
    app._live_niveis(s)                          # janela fechada: nada
    assert not _linhas(caplog, "playback_inicio")
    for ms in (400, 400, 300):                   # 3 chunks do episódio (o `falando` não importa)
        app._live_playback_chunk(s, _chunk(ms))
        t[0] += 0.5
    s["engine"].ativo = True
    app._live_niveis(s)
    (ini,) = _linhas(caplog, "playback_inicio")
    assert "turno=3" in ini and "ts=" in ini, ini
    s["engine"].ativo = False                    # a janela fecha (cauda do cliente)
    app._live_niveis(s)
    (fim,) = _linhas(caplog, "playback_fim")
    c = _campos(fim)
    assert c["chunks"] == "3" and c["audio_ms"] == "1100" and c["motivo"] == "janela", fim
    assert c["duracao_ms"] == "1000", fim        # 1º→último envio (t0=10.0, último=11.0)
    assert "ts" in c


def test_playback_fim_sem_chunk_nao_inventa_duracao(caplog):
    caplog.set_level(logging.INFO, logger="live")
    s = _sess(falando=True)
    app._live_niveis(s)
    s["engine"].ativo = False
    app._live_niveis(s)
    (fim,) = _linhas(caplog, "playback_fim")
    assert "duracao_ms" not in fim and "audio_ms" not in fim, fim


def test_regime_real_um_chunk_por_vez_o_falando_volta_a_false(caplog, monkeypatch):
    """Bug do speech (#1129): no regime real o handler liga e desliga `falando` e chama
    `set_speaking(True)`+`(False)` NO MESMO INSTANTE por chunk, então `sess["falando"]` fica
    ~0 ms ligado. Esta célula usa o MOTOR REAL e NÃO força `falando=True`: o `niveis` tem de
    sair enquanto o cliente toca o buffer, e o episódio tem 1 par de bordas com o `audio_ms`
    inteiro — não pares de ~0 ms que zeram tudo."""
    import numpy as np
    import live_turns
    caplog.set_level(logging.INFO, logger="live")
    eng = live_turns.TurnEngine(scorer=lambda f: 0.1)
    s = {"id": "sx", "turno": 3, "st_dbfs": -40.0, "engine": eng, "falando": False}
    for ms in (200, 200, 200):
        payload = _chunk(ms)
        s["falando"] = True                      # o handler liga…
        eng.set_speaking(True, nivel_dbfs=-30.0, duracao_ms=ms)
        app._live_playback_chunk(s, payload)
        s["falando"] = False                     # …e desliga no mesmo instante
        eng.set_speaking(False)
    assert s["falando"] is False and eng.speaking is False
    app._live_niveis(s)                          # o cliente ainda toca: janela ATIVA
    assert len(_linhas(caplog, "playback_inicio")) == 1
    assert _linhas(caplog, "niveis"), "sem `niveis` enquanto o cliente toca o buffer"
    silencio = np.zeros(512 * 8, dtype=np.int16).tobytes()
    for _ in range(400):                         # a cauda de playback escoa com os frames
        eng.feed(silencio)
        s["st_dbfs"] = -70.0
        app._live_niveis(s)
        if _linhas(caplog, "playback_fim"):
            break
    assert len(_linhas(caplog, "playback_inicio")) == 1, "bordas de ~0 ms por chunk"
    (fim,) = _linhas(caplog, "playback_fim")
    c = _campos(fim)
    assert c["chunks"] == "3" and c["audio_ms"] == "600", fim


# --- guardas dos pontos de chamada (o que o qa achou ausente) ----------------------

def test_speech_start_e_speech_end_levam_barge_in():
    for ev in ("speech_start", "speech_end"):
        m = re.search(rf'_live_log_kv\("{ev}".*?\)\n', APP_SRC, re.S)
        assert m and "barge_in=getattr(ev" in m.group(0), (ev, m and m.group(0))


def test_o_envio_acumula_o_episodio_e_o_niveis_roda_depois_do_feed():
    assert APP_SRC.count("_live_playback_chunk(sess, payload)") == 1
    assert "_live_playback_borda" not in APP_SRC, "borda lida de `falando` (bug do regime real)"
    # o gate é a JANELA do motor, não `sess["falando"]`
    corpo = APP_SRC[APP_SRC.index("def _live_niveis"):APP_SRC.index("def _live_stage")]
    assert 'st.get("playback_ativo")' in corpo and 'sess.get("falando")' not in corpo
    # DEPOIS do feed do mesmo frame: o `prob_pico` não fica um frame atrás
    assert APP_SRC.count("_live_niveis(sess)") == 1
    assert APP_SRC.index("eng.feed(pcm)") < APP_SRC.index("_live_niveis(sess)")


def test_o_motor_expoe_o_eco_dbfs_que_o_log_le():
    """`_live_niveis` lê `estatisticas()["eco_dbfs"]`: o motor tem de continuar expondo."""
    src = (REPO / "live_turns.py").read_text()
    assert '"eco_dbfs": round(self._eco_dbfs' in src


# --- acréscimos do speech (3): tocando, n, playback_fim ao fechar a sessão ---------

def test_motor_real_vao_de_geracao_tocando_true_e_fila_ms_zero_turno_fechado_cala(
        caplog, monkeypatch):
    """O motor REAL, config de produção. A célula antiga usava um motor falso com
    `tocando=False` no vão e passava enganando: no motor real, com o turno ABERTO, o backlog
    não escoa (#167) e `tocando` fica True com o assistente calado. Quem diz "mudo" é o
    `fila_ms`. Turno FECHADO: a janela e o `tocando` caem sozinhos."""
    import numpy as np
    import live_turns
    caplog.set_level(logging.INFO, logger="live")
    t = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    silencio = np.zeros(512 * 8, dtype=np.int16).tobytes()     # 256 ms

    def envia(eng, s, ms):
        eng.set_speaking(True, nivel_dbfs=-30.0, duracao_ms=ms)
        app._live_playback_chunk(s, _chunk(ms))
        eng.set_speaking(False)

    # turno ABERTO (como no Live): 300 ms de áudio, depois um vão de geração de 2 s
    aberto = live_turns.TurnEngine(scorer=lambda f: 0.05)
    aberto.set_turno_aberto(True)
    s = {"id": "sx", "turno": 3, "st_dbfs": -40.0, "engine": aberto, "falando": False}
    envia(aberto, s, 300)
    aberto.feed(silencio)
    app._live_niveis(s)                                  # t=100,0: 300 ms na fila
    t[0] = 100.7                                         # o áudio acabou há 400 ms
    for _ in range(8):                                   # ~2 s de vão: o motor segue "tocando"
        aberto.feed(silencio)
    st = aberto.estatisticas()
    assert st["playback_ativo"] is True and st["tocando"] is True, st
    app._live_niveis(s)
    a, b = (_campos(x) for x in _linhas(caplog, "niveis"))
    assert a["fila_ms"] == "300" and a["tocando"] == "True", a
    assert b["fila_ms"] == "0" and b["tocando"] == "True", b   # mudo, e o motor não sabe

    # turno FECHADO: depois da cauda a janela fecha e o `tocando` cai
    caplog.clear()
    t[0] = 200.0
    fechado = live_turns.TurnEngine(scorer=lambda f: 0.05)
    s2 = {"id": "sy", "turno": 4, "st_dbfs": -40.0, "engine": fechado, "falando": False}
    envia(fechado, s2, 300)
    fechado.feed(silencio)
    app._live_niveis(s2)
    for _ in range(12):                                  # ~3 s: backlog e cauda escoaram
        fechado.feed(silencio)
    st = fechado.estatisticas()
    assert st["playback_ativo"] is False and st["tocando"] is False, st
    t[0] = 203.0
    app._live_niveis(s2)
    assert len(_linhas(caplog, "playback_fim")) == 1
    assert len(_linhas(caplog, "niveis")) == 1, "janela fechada não emite niveis"


def test_niveis_traz_n_os_frames_da_janela(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [500.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True)
    app._live_niveis(s)                               # 1ª do episódio: 1 frame
    for dt in (0.1, 0.2, 0.3, 0.4):
        t[0] = 500.0 + dt
        app._live_niveis(s)
    t[0] = 500.5
    app._live_niveis(s)                               # fecha a janela com 5 frames
    primeira, segunda = (_campos(x) for x in _linhas(caplog, "niveis"))
    assert primeira["n"] == "1" and segunda["n"] == "5", (primeira, segunda)


def test_sessao_fechando_com_a_janela_aberta_emite_playback_fim_fechou(caplog):
    caplog.set_level(logging.INFO, logger="live")
    s = _sess(falando=True)
    s["pb_ativo"] = True
    app._live_playback_chunk(s, _chunk(400))
    app._live_playback_chunk(s, _chunk(400))
    app._live_libera_sessao(s, "cliente_fechou")
    (fim,) = _linhas(caplog, "playback_fim")
    c = _campos(fim)
    assert c["motivo"] == "fechou" and c["chunks"] == "2" and c["audio_ms"] == "800", fim
    app._live_libera_sessao(s, "cliente_fechou")      # idempotente: não duplica
    assert len(_linhas(caplog, "playback_fim")) == 1


def test_sessao_fechando_com_a_janela_fechada_nao_inventa_playback_fim(caplog):
    caplog.set_level(logging.INFO, logger="live")
    app._live_libera_sessao(_sess(falando=False), "cliente_fechou")
    assert not _linhas(caplog, "playback_fim")


# --- #1129c: `fila_ms` (áudio audível) no lugar do `tocando` do motor --------------------

def test_fila_ms_estima_o_audio_que_o_cliente_ainda_toca_e_zera_no_vao(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True)
    app._live_playback_chunk(s, _chunk(400))     # 400 ms de áudio a t=100
    app._live_playback_chunk(s, _chunk(400))     # o servidor manda mais rápido: fila 800 ms
    assert app._live_fila_ms(s) == 800
    t[0] = 100.5                                 # o cliente tocou 500 ms
    assert app._live_fila_ms(s) == 300
    t[0] = 101.0                                 # vão de geração: a fila secou há 200 ms
    assert app._live_fila_ms(s) == 0
    app._live_playback_chunk(s, _chunk(300))     # chunk novo depois do vão: recomeça de t
    assert app._live_fila_ms(s) == 300
    app._live_niveis(s)
    (linha,) = _linhas(caplog, "niveis")
    assert _campos(linha)["fila_ms"] == "300"


def test_cancel_zera_a_fila_estimada_do_cliente():
    import queue
    import threading
    s = _sess(falando=True)
    s.update({"cancelado": threading.Event(), "turno": 1, "pb_fim_est": 9e9,
              "fila": queue.Queue()})
    app._live_cancela(s, forcar=True)
    assert s["pb_fim_est"] == 0.0 and app._live_fila_ms(s) == 0


def test_barge_in_zera_a_fila_estimada_do_cliente():
    """O barge do motor chega por `_live_trata_eventos` e passa por `_live_cancela`: o
    cliente descarta a fila no `interrupted`, então a estimativa tem de zerar."""
    import queue
    import threading
    import types
    s = _sess(falando=True)
    s.update({"cancelado": threading.Event(), "turno": 2, "pb_fim_est": 9e9,
              "fila": queue.Queue(), "id": "sb"})
    ev = types.SimpleNamespace(tipo="barge_in", t_ms=0, prob=0.9, barge_in=True,
                               fala_ms=0, to_json=lambda: {"type": "barge_in"})
    app._live_trata_eventos(s, [ev])
    assert s["cancelado"].is_set()
    assert s["pb_fim_est"] == 0.0 and app._live_fila_ms(s) == 0
