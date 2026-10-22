"""GATE QA #O (`task_c4562b00`): truncamento por cps, VAD serializado, rms do worker.

Três regressões medidas no #284: portão de truncamento 3× frouxo, Silero global
sendo resetado por duas transcrições ao mesmo tempo, e o `rms` do worker jogado
fora. Aqui cada uma é provada — e o contrapeso importa tanto quanto o defeito:
fala NORMAL longa e texto com números/siglas não podem disparar o portão.
"""

import numpy as np
import soundfile as sf

import common
import live_pipeline as lp

__all__ = ["common", "lp"]


def _fala(segundos: float, rms: float = 0.15) -> np.ndarray:
    t = np.arange(int(segundos * 24000)) / 24000.0
    x = (np.sin(2 * np.pi * 180 * t) + 0.4 * np.sin(2 * np.pi * 640 * t)).astype(np.float32)
    return x * (rms / float(np.sqrt(np.mean(x ** 2))))


def _wav(tmp_path, segundos=1.5, nome="v.wav"):
    caminho = tmp_path / nome
    sf.write(str(caminho), _fala(segundos), 24000, subtype="PCM_16")
    return caminho


# ------------------------------------------------------------------ cps

def test_cps_minimo_e_mais_apertado_que_o_45():
    assert common.CPS_MINIMO < 45


def test_truncado_a_40_por_cento_dispara_e_a_90_nao():
    texto = "x" * 100
    natural = len(texto) / 15.4                     # cps medido na auditoria
    curto = _fala(natural * 0.40)
    quase = _fala(natural * 0.90)
    assert "truncado" in common.motivo_anomalia(curto, 24000, texto)
    assert common.motivo_anomalia(quase, 24000, texto) == ""


def test_fala_normal_longa_ou_com_numeros_e_siglas_nao_dispara():
    """Contrapeso: o portão não pode ser sensível demais."""
    textos = [
        "Hoje a gente vai falar de três coisas na ordem primeiro o cronograma e depois os custos",
        "A reunião é às 14h30 na sala 12B com o Sr. Almeida e a Dra. Souza",
        "O SLA do IBGE em 2026 foi 99,7% e o S/N medido ficou em 45 dB",
    ]
    for texto in textos:
        dur = len(texto) / 15.4                     # ritmo natural
        assert common.motivo_anomalia(_fala(dur), 24000, texto) == "", texto


# ------------------------------------------------------------------ VAD

def test_oito_vad_paralelos_dao_o_mesmo_veredito(tmp_path):
    import threading

    import app

    caminho = _wav(tmp_path)
    saidas = []
    ts = [threading.Thread(target=lambda: saidas.append(app._vad_tem_fala(caminho)))
          for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert len(saidas) == 8 and len(set(saidas)) == 1, saidas


def test_erro_do_silero_fica_visivel_e_segue_fail_open(tmp_path, monkeypatch, capsys):
    import silero_vad

    import app

    monkeypatch.setattr(silero_vad, "get_speech_timestamps",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quebrado")))
    monkeypatch.setattr(app, "_vad_erro", "")
    assert app._vad_tem_fala(_wav(tmp_path)) is True
    assert "quebrado" in app._vad_estado()[1]
    assert "[vad]" in capsys.readouterr().out


# ------------------------------------------------------------------ worker

class _WorkerZeros:
    ativo = True

    def gerar(self, texto, omni, espera=None):
        return np.zeros(2400, dtype=np.float32)

    def fecha(self):
        return None


def test_worker_mudo_marca_indisponivel_e_regera(monkeypatch):
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: True)
    p = lp.LivePipeline(lambda o: None, lambda b: None, voice_id="v")
    p._prewarm = lambda voice_id=None, tts_in_process=True: None
    p._worker = _WorkerZeros()
    p._tts_app = lambda texto, omni, voice_id=None, espera=None: _fala(0.1, 0.3)
    audio = p._tts_live("turno", {})
    assert p._worker_indisponivel is True
    assert float(np.sqrt(np.mean(audio ** 2))) > 0.1


def test_lote_anomalo_faz_duas_tentativas(tmp_path, monkeypatch):
    import json
    import sys

    import tts_worker

    chamadas = []

    def _sintetiza(est, texto, omni=None, fx=False):
        chamadas.append(texto)
        if len(chamadas) == 1:
            return np.zeros(24000, dtype=np.float32)
        return _fala(4.0)

    cfg = {
        "status_path": str(tmp_path / "s.json"), "piece_dir": str(tmp_path / "p"),
        "outputs_dir": str(tmp_path / "o"),
        "text": "Hoje a gente vai falar de tres coisas na ordem e depois os custos",
        "voice_id": "v", "voices_dir": str(tmp_path / "voices"),
        "voice_path": str(tmp_path / "voices" / "v.wav"), "model": "omnivoice",
        "settings": {"chunk_max_chars": 140}, "omni": {"num_steps": 4},
        "base_dir": str(tmp_path),
    }
    (tmp_path / "c.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(tts_worker, "_carrega", lambda c: {
        "sr": 24000, "family": "omnivoice", "omni": {"num_steps": 4},
        "settings": {"chunk_max_chars": 140}})
    monkeypatch.setattr(tts_worker, "_sintetiza", _sintetiza)
    monkeypatch.setattr(tts_worker, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(tts_worker, "normaliza_loudness", lambda *a, **k: 0.0)
    monkeypatch.setattr(sys, "argv", ["tts_worker.py", str(tmp_path / "c.json")])
    assert tts_worker.main() == 0
    assert len(chamadas) == 2
    meta = json.loads(next((tmp_path / "o").glob("*.json")).read_text())
    assert meta["retries"] == 1 and meta["anomalias"] == 1
