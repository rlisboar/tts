"""Carimbo em ms nos eventos do barge e `enviado` no `ws.send` (#1129).

No proxy do #1129 (eco −15) o cliente via o duck ~1,8 s depois do onset, mas o log do
servidor só tinha `HH:MM:SS`: não dava para separar a decisão do servidor da entrega ao
cliente. Aqui:
  * os eventos do barge/turno levam `ts=` (relógio de parede, ms) — o PREFIXO da linha
    não muda, quem lê o log pelo começo segue igual;
  * cada um desses eventos, ao ser ESCRITO no WS, gera `enviado tipo=… espera_ms=…` (quanto
    esperou na fila atrás do áudio).
Só números: nada de áudio nem texto.
"""
from __future__ import annotations

import logging
import queue
import re
from pathlib import Path

import pytest

import app

TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}"


def _linhas(caplog, evento):
    return [r.getMessage() for r in caplog.records
            if r.name == "live" and r.getMessage().startswith(evento + " ")]


@pytest.mark.parametrize("evento", ["barge_suspeito", "barge_decisao", "interrupted",
                                    "speech_start", "cancel", "barge_in"])
def test_eventos_do_barge_levam_ts_em_ms(caplog, evento):
    caplog.set_level(logging.INFO, logger="live")
    app._live_log_kv(evento, sess="sx", t_ms=123)
    (linha,) = _linhas(caplog, evento)
    assert re.search(rf" ts={TS}$", linha), linha
    assert linha.startswith(f"{evento} sess=sx t_ms=123 "), "a ordem dos campos não muda"


def test_evento_fora_do_barge_nao_ganha_campo(caplog):
    caplog.set_level(logging.INFO, logger="live")
    app._live_log_kv("frame", sess="sx", n=10)
    (linha,) = _linhas(caplog, "frame")
    assert linha == "frame sess=sx n=10", linha


def test_ts_explicito_nao_e_sobrescrito(caplog):
    caplog.set_level(logging.INFO, logger="live")
    app._live_log_kv("speech_start", sess="sx", ts="X")
    (linha,) = _linhas(caplog, "speech_start")
    assert linha == "speech_start sess=sx ts=X", linha


def test_o_prefixo_da_linha_continua_hh_mm_ss():
    fmt = app._live_log.handlers[0].formatter
    assert fmt._fmt == "%(asctime)s [live] %(message)s" and fmt.datefmt == "%H:%M:%S"


def _sess():
    return {"id": "sx", "turno": 2, "fila": queue.Queue(), "visto": False}


def test_enviado_mede_quanto_o_evento_esperou_na_fila(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(app, "_live_janela_turno", lambda *a, **k: None)
    s = _sess()
    app._live_envia_json(s, {"type": "barge_suspeito", "turn": 2})
    tipo, carga = s["fila"].get_nowait()
    assert tipo == "json"
    t[0] = 101.25                           # 1,25 s atrás do áudio na fila do WS
    app._live_enviado(s, carga)
    (linha,) = _linhas(caplog, "enviado")
    assert "tipo=barge_suspeito" in linha and "espera_ms=1250" in linha, linha
    assert re.search(rf" ts={TS}$", linha), linha
    assert s["_t_enfileirado"] == {}, "a marca da fila sai quando o evento é enviado"


def test_enviado_ignora_json_que_nao_e_do_barge(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    s = _sess()
    app._live_enviado(s, {"type": "stats"})
    app._live_enviado(s, b"\x00\x01")
    assert _linhas(caplog, "enviado") == []


def test_enviado_nunca_levanta(caplog):
    app._live_enviado({}, {"type": "interrupted", "turn": 1})      # sem id nem tabela


def test_o_sender_chama_o_enviado_depois_do_send_json():
    import inspect
    fonte = inspect.getsource(app._live_sender)
    i = fonte.index("await asyncio.wait_for(ws.send_json(payload), _LIVE_SEND_TIMEOUT_S)")
    assert "_live_enviado(sess, payload)" in fonte[i:i + 200]


def test_enviado_nao_usa_o_nome_fila_ms_do_niveis(caplog, monkeypatch):
    """qa (P3): `fila_ms` no `niveis` é ÁUDIO por tocar; a espera do `enviado` é TEMPO."""
    caplog.set_level(logging.INFO, logger="live")
    monkeypatch.setattr(app, "_live_janela_turno", lambda *a, **k: None)
    s = _sess()
    app._live_envia_json(s, {"type": "cancel", "turn": 2})
    app._live_enviado(s, s["fila"].get_nowait()[1])
    (linha,) = _linhas(caplog, "enviado")
    assert "espera_ms=" in linha and "fila_ms=" not in linha, linha


def test_interrupted_dup_leva_ts(caplog):
    """qa (P3): a duplicata engolida do cancel imediato entra na conta do vão."""
    caplog.set_level(logging.INFO, logger="live")
    app._live_log_kv("interrupted_dup", sess="sx", turno=3)
    (linha,) = _linhas(caplog, "interrupted_dup")
    assert re.search(rf" ts={TS}$", linha), linha


def test_sessao_nasce_com_a_tabela_de_marcas():
    """qa (P3): a tabela do `enviado` existe desde a criação (sem `setdefault` em corrida)."""
    fonte = (Path(app.__file__)).read_text(encoding="utf-8")
    assert '"_t_enfileirado": {},' in fonte
