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


class _Motor:
    def __init__(self, eco):
        self._eco = eco

    def estatisticas(self):
        return {"eco_dbfs": self._eco}


def _sess(**extra):
    return {"id": "sx", "turno": 3, "st_dbfs": -41.5, "engine": _Motor(-32.0), **extra}


# --- (2) niveis -----------------------------------------------------------------

def test_niveis_so_durante_o_playback_e_com_os_dois_campos(caplog):
    caplog.set_level(logging.INFO, logger="live")
    s = _sess(falando=False)
    app._live_niveis(s)
    assert not _linhas(caplog, "niveis"), "sem playback não há nível"
    s["falando"] = True
    app._live_niveis(s)
    (linha,) = _linhas(caplog, "niveis")
    assert "dbfs=-41.5" in linha and "eco_dbfs=-32.0" in linha and "sess=sx" in linha, linha


def test_niveis_respeita_a_cadencia_de_500ms(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess(falando=True)
    for dt in (0.0, 0.1, 0.3, 0.49, 0.5, 0.7, 1.0):
        t[0] = 100.0 + dt
        app._live_niveis(s)
    # 0.0 loga; 0.1–0.49 não; 0.5 loga; 0.7 não; 1.0 loga
    assert len(_linhas(caplog, "niveis")) == 3


def test_niveis_nao_derruba_se_o_motor_falha(caplog):
    caplog.set_level(logging.INFO, logger="live")

    class Quebrado:
        def estatisticas(self):
            raise RuntimeError("x")

    s = _sess(falando=True, engine=Quebrado())
    app._live_niveis(s)
    (linha,) = _linhas(caplog, "niveis")
    assert "dbfs=-41.5" in linha and "eco_dbfs" not in linha


def test_niveis_so_leva_numero_nenhum_audio(caplog):
    caplog.set_level(logging.INFO, logger="live")
    app._live_niveis(_sess(falando=True))
    (linha,) = _linhas(caplog, "niveis")
    assert re.fullmatch(r"niveis sess=sx dbfs=-?[\d.]+ eco_dbfs=-?[\d.]+", linha), linha


# --- (3) bordas do playback -------------------------------------------------------

def test_playback_inicio_e_fim_com_duracao(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="live")
    t = [10.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: t[0])
    s = _sess()
    app._live_playback_borda(s, True)
    s["playback_chunks"] = 7
    t[0] = 12.5
    app._live_playback_borda(s, False)
    (ini,) = _linhas(caplog, "playback_inicio")
    (fim,) = _linhas(caplog, "playback_fim")
    assert "sess=sx" in ini and "turno=3" in ini, ini
    assert "duracao_ms=2500" in fim and "chunks=7" in fim, fim


def test_playback_fim_sem_inicio_nao_inventa_duracao(caplog):
    caplog.set_level(logging.INFO, logger="live")
    app._live_playback_borda(_sess(), False)
    (fim,) = _linhas(caplog, "playback_fim")
    assert "duracao_ms" not in fim, fim


# --- guardas dos pontos de chamada (o que o qa achou ausente) ----------------------

def test_speech_start_e_speech_end_levam_barge_in():
    for ev in ("speech_start", "speech_end"):
        m = re.search(rf'_live_log_kv\("{ev}".*?\)\n', APP_SRC, re.S)
        assert m and "barge_in=getattr(ev" in m.group(0), (ev, m and m.group(0))


def test_o_envio_do_audio_marca_as_bordas_e_o_frame_chama_o_niveis():
    assert APP_SRC.count("_live_playback_borda(sess, True)") == 1
    assert APP_SRC.count("_live_playback_borda(sess, False)") == 1
    # a borda de fim vem ANTES do `set_speaking(False)` do motor (mesmo ponto)
    fim = APP_SRC.index("_live_playback_borda(sess, False)")
    assert APP_SRC.index("eng.set_speaking(False)", fim) - fim < 200
    assert "_live_niveis(sess)" in APP_SRC


def test_o_motor_expoe_o_eco_dbfs_que_o_log_le():
    """`_live_niveis` lê `estatisticas()["eco_dbfs"]`: o motor tem de continuar expondo."""
    src = (REPO / "live_turns.py").read_text()
    assert '"eco_dbfs": round(self._eco_dbfs' in src
