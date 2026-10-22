"""GATE QA (task_c4562b00): truncamento por cps, VAD sob lock e rms do worker.

Os quatro pontos do aceite, cada um com a sua mordida:

1. truncado a 40 % dispara; a 90 % não — e a régua é derivada do cps MEDIDO, não
   da constante (senão o teste seria auto-referente);
2. fala normal longa e texto com números/siglas NÃO disparam o limiar novo;
3. 8 `_vad_tem_fala` concorrentes no mesmo WAV: mesma resposta, e a inferência
   de fato serializa (detector de sobreposição); erro forçado do Silero aparece
   no estado e no log, sem perder o fail-open;
4. lote com trecho anômalo faz 2 tentativas (e o filho de zeros marca worker
   indisponível, caindo no in-process).

Nada usa o `:7860`: funções puras, `TestClient` e subprocesso falso.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app
import common

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: cps medido no modelo (não a constante): 15,4 chars/s
CPS_MEDIDO = 15.4
TEXTO = "A rápida raposa marrom pula sobre o cão preguiçoso enquanto o sol se põe"
DURACAO_NATURAL = len(TEXTO) / CPS_MEDIDO


def _fala(segundos: float, sr: int = 24000):
    """Sinal com cara de fala: RMS bem acima do piso, sem silêncio."""
    n = int(segundos * sr)
    t = np.arange(n) / sr
    return (0.25 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)


# ------------------------------------------------------------------- cps (1 e 2)

def test_truncado_a_40_por_cento_dispara():
    motivo = common.motivo_anomalia(_fala(DURACAO_NATURAL * 0.40), 24000, TEXTO)
    assert "truncado" in motivo, motivo


def test_a_90_por_cento_passa():
    assert common.motivo_anomalia(_fala(DURACAO_NATURAL * 0.90), 24000, TEXTO) == ""


def test_duracao_natural_passa():
    assert common.motivo_anomalia(_fala(DURACAO_NATURAL), 24000, TEXTO) == ""


def test_speed_alto_afrouxa_a_regua():
    """Falar 1,5× mais rápido encolhe a duração legítima na mesma proporção."""
    curto = _fala(DURACAO_NATURAL * 0.5)   # 2,3 s: reprovado a 1×, aceito a 1,5×
    assert "truncado" in common.motivo_anomalia(curto, 24000, TEXTO)
    assert common.motivo_anomalia(curto, 24000, TEXTO, speed=1.5) == ""


def test_fala_normal_longa_nao_dispara():
    """Texto longo não pode cair na régua só por ser longo: o cps é que manda."""
    longo = TEXTO * 6                                 # ~450 chars
    natural = len(longo) / CPS_MEDIDO
    assert common.motivo_anomalia(_fala(natural), 24000, longo) == ""


@pytest.mark.parametrize("texto", [
    "O ano de 2024 fechou com 3.500 unidades e 12% de alta",
    "A API do TTS-STUDIO roda em HTTP/2 com TLS 1.3",
    "Ele pagou R$ 1.299,90 no dia 07/03 às 14h30",
])
def test_texto_com_numeros_e_siglas_nao_dispara(texto):
    natural = len(texto) / CPS_MEDIDO
    assert common.motivo_anomalia(_fala(natural), 24000, texto) == ""


def test_audio_inaudivel_continua_reprovado():
    mudo = (np.zeros(24000 * 3)).astype(np.float32)
    assert "inaudível" in common.motivo_anomalia(mudo, 24000, "oi")


# ------------------------------------------------------------------- VAD (3)

@pytest.fixture
def wav_fala(tmp_path):
    caminho = tmp_path / "fala.wav"
    sf.write(str(caminho), _fala(2.0), 16000, subtype="PCM_16")
    return caminho


def test_vad_concorrente_serializa_e_concorda(wav_fala, monkeypatch):
    """Duas inferências simultâneas embaralhavam o estado RNN do Silero."""
    dentro = {"agora": 0, "pico": 0}
    travas = {"n": 0}

    def _inferencia(*_a, **_k):
        travas["n"] += 1
        dentro["agora"] += 1
        dentro["pico"] = max(dentro["pico"], dentro["agora"])
        time.sleep(0.05)
        dentro["agora"] -= 1
        return [{"start": 0, "end": 16000}]

    import silero_vad

    monkeypatch.setattr(app, "_vad_load", lambda: object())
    monkeypatch.setattr(silero_vad, "get_speech_timestamps", _inferencia)

    respostas = []
    import threading

    threads = [threading.Thread(target=lambda: respostas.append(app._vad_tem_fala(wav_fala)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(respostas) == 8
    assert set(respostas) == {True}, respostas
    assert dentro["pico"] == 1, f"inferências sobrepostas (pico {dentro['pico']})"


def test_erro_do_silero_fica_visivel_e_mantem_fail_open(wav_fala, monkeypatch, capsys):
    import silero_vad

    monkeypatch.setattr(app, "_vad_load", lambda: object())
    monkeypatch.setattr(app, "_vad_erro", "")
    monkeypatch.setattr(silero_vad, "get_speech_timestamps",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert app._vad_tem_fala(wav_fala) is True          # fail-open
    caminho, erro = app._vad_estado()
    assert "boom" in erro, erro
    assert "fail-open" in capsys.readouterr().out


def test_sucesso_limpa_o_erro_anterior(wav_fala, monkeypatch):
    import silero_vad

    monkeypatch.setattr(app, "_vad_load", lambda: object())
    monkeypatch.setattr(app, "_vad_erro", "RuntimeError: antigo")
    monkeypatch.setattr(silero_vad, "get_speech_timestamps",
                        lambda *a, **k: [{"start": 0, "end": 16000}])
    app._vad_tem_fala(wav_fala)
    assert app._vad_estado()[1] == ""


# --------------------------------------------------- worker de lote (4)

def test_lote_regera_trecho_anomalo(tmp_path, monkeypatch):
    """Trecho mudo na 1ª tentativa tem de gerar uma 2ª e não passar calado."""
    import tts_worker

    tentativas = {"n": 0}

    def _sintetiza(est, texto, omni=None, fx=False):
        tentativas["n"] += 1
        if tentativas["n"] == 1:
            return np.zeros(24000, dtype=np.float32)      # muda -> inaudível
        return _fala(1.5)

    monkeypatch.setattr(tts_worker, "_carrega",
                        lambda cfg: {"sr": 24000, "omni": {}, "family": "kokoro"})
    monkeypatch.setattr(tts_worker, "_sintetiza", _sintetiza)
    monkeypatch.setattr(tts_worker, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(tts_worker, "normaliza_loudness", lambda *a, **k: 0.0)
    monkeypatch.setattr(tts_worker, "normalize", lambda a, *x, **k: a)
    monkeypatch.setattr(tts_worker, "fade_edges", lambda a, *x, **k: a)

    cfg = {"text": "frase de teste", "voice_id": "v1", "language": "pt",
           "model": "omnivoice", "piece_dir": str(tmp_path / "pieces"),
           "outputs_dir": str(tmp_path / "out"), "status_path": str(tmp_path / "st.json"),
           "settings": {}}
    caminho = tmp_path / "cfg.json"
    caminho.write_text(__import__("json").dumps(cfg))
    monkeypatch.setattr(sys, "argv", ["tts_worker.py", str(caminho)])

    assert tts_worker.main() == 0
    assert tentativas["n"] == 2, f"não houve 2ª tentativa (n={tentativas['n']})"
    meta = __import__("json").loads(next((tmp_path / "out").glob("*.json")).read_text())
    assert meta["retries"] >= 1 and meta["anomalias"] >= 1


# ------------------------------------------- worker do Live mudo -> in-process

def test_worker_que_devolve_zeros_marca_indisponivel_e_regera(monkeypatch):
    """O pai confere o áudio do worker: mudo = falha, e o trecho é refeito
    in-process (o usuário não fica sem áudio nem os turnos seguintes tentam)."""
    import live_pipeline

    class _Sessao(live_pipeline.LivePipeline):
        def __init__(self):                       # sem WS de verdade
            self.voice_id = "v1"
            self._worker = None
            self._worker_indisponivel = False
            self._worker_motivo = ""
            self._espera = 1.0

    class _WorkerMudo:
        ativo = True

        def gerar(self, texto, omni, espera=None):
            return np.zeros(2400, dtype=np.float32)

        def fecha(self):
            self.fechado = True

    refeito = {"n": 0}

    def _app_tts(texto, omni, **k):
        refeito["n"] += 1
        return _fala(1.5)

    monkeypatch.setattr(live_pipeline, "_worker_habilitado", lambda: True)
    monkeypatch.setattr(live_pipeline, "_LiveWorker", lambda **k: _WorkerMudo())
    monkeypatch.setattr(live_pipeline, "_tts_app", _app_tts)
    monkeypatch.setattr(live_pipeline, "_pos_live", lambda a, *x, **k: a)
    monkeypatch.setattr(app, "_tts_app", _app_tts, raising=False)

    sessao = _Sessao()
    audio = sessao._tts_live("uma frase qualquer", {})
    assert refeito["n"] == 1, "não regerou in-process"
    assert sessao._worker_indisponivel is True
    assert "mudo" in sessao._worker_motivo
    assert np.sqrt(np.mean(np.asarray(audio) ** 2)) > 0.01

