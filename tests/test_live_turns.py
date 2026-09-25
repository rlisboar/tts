"""Testes do motor de turnos do Live (LIVE-2) — sem carregar Metal/MLX.

Duas camadas, de propósito:

1. FSM com scorer injetado (sem modelo): determinística e rápida, cobre prefix
   padding, fim por silêncio, barge-in, eco residual, hold-to-talk e a
   contabilidade de falso barge-in.
2. Integração com o Silero ONNX REAL, em subprocesso — mesmo estilo de
   `tests/test_vad_deprecation.py`. Prova o que só o modelo prova: o caminho é
   ONNX, o módulo não puxa MLX e ninguém abre socket.

Nota de realismo (medido): o Silero responde ~0.95 em fala de verdade e fica
*borderline* (0.02–0.8, instável) em vogal sintética — os harmônicos não têm
transição de formante. Por isso a integração prefere um wav de `voices/`
(quando existe) e, no caminho sintético, calibra o limiar pela própria resposta
do modelo ao sinal usado, em vez de fingir que 0.5 vale para os dois.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import live_turns as lt

F = lt.FRAME_SAMPLES
FALA_HZ = 150.0
RAIZ = Path(__file__).resolve().parent.parent
# pré-roll de um speech_start: frames confirmados + prefixo + rampa do envelope
PRE_ROLL = lt.Config()._frames_onset + lt.Config()._frames_prefix + \
    lt.Config()._frames_envelope


def _tom(amp: float, hz: float = FALA_HZ) -> np.ndarray:
    t = np.arange(F) / lt.SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


SILENCIO = np.zeros(F, dtype=np.float32)
ECO = _tom(0.03)          # ~-33 dBFS: o que o alto-falante devolve no mic
VOZ = None                # preenchido por _motor(): depende do limiar


def _motor(voz_amp: float = 0.3, **cfg) -> tuple[lt.TurnEngine, list[float]]:
    """Engine com scorer de mentira: prob alta se o frame tem energia.

    Assim os testes exercitam a FSM (e não o Silero), com relógio controlado."""
    rel = [0.0]
    limite = voz_amp * 0.4

    def scorer(frame: np.ndarray) -> float:
        return 0.9 if float(np.sqrt(np.mean(frame * frame))) > limite else 0.02

    motor = lt.TurnEngine(lt.Config(**cfg), scorer=scorer,
                          relogio=lambda: rel[0], agora=lambda: 1_700_000_000.0)
    return motor, rel


def _tocar(motor: lt.TurnEngine, rel: list[float], frame: np.ndarray, n: int):
    """Empurra `n` frames, avançando o relógio 1 frame por vez."""
    eventos = []
    for _ in range(n):
        eventos += motor.feed(frame)
        rel[0] += F / lt.SAMPLE_RATE
    return eventos


def _mistura(*sinais: np.ndarray) -> np.ndarray:
    return sum(sinais).astype(np.float32)


def _abrir_por_barge(motor: lt.TurnEngine, rel: list[float], voz_amp: float = 0.3):
    """Deixa o motor falando com eco estável e dispara um barge-in sustentado."""
    _tocar(motor, rel, _tom(0.001), 40)
    _tocar(motor, rel, ECO, 40)
    motor.set_speaking(True, nivel_dbfs=-18.0)
    _tocar(motor, rel, ECO, 40)
    return _tocar(motor, rel, _mistura(ECO, _tom(voz_amp)), lt.Config()._frames_barge)


# --------------------------------------------------------------------------
# contrato / defaults
# --------------------------------------------------------------------------
def test_defaults_do_protocolo():
    cfg = lt.Config()
    assert cfg.silence_ms == 600
    assert cfg.prefix_ms == 100
    assert cfg.barge_in_ms == 300
    assert cfg.silence_ms >= lt.SILENCE_MS_MINIMO_STT


def test_silencio_abaixo_de_500_avisa():
    assert lt.Config().avisos == []
    avisos = lt.Config(silence_ms=400).avisos
    assert len(avisos) == 1 and "Whisper" in avisos[0]


@pytest.mark.parametrize("cfg", [
    {"silence_ms": 0}, {"onset_ms": 1}, {"fala_threshold": 1.5},
    {"fala_threshold": 0.0}, {"piso_ruido_dbfs": -20.0, "teto_ruido_dbfs": -40.0},
    {"prefix_ms": -1},
])
def test_config_invalida_estoura(cfg):
    with pytest.raises(ValueError):
        lt.Config(**cfg)


def test_aviso_sai_no_stdout(capsys):
    lt.TurnEngine(lt.Config(silence_ms=300))
    assert "silence_ms=300" in capsys.readouterr().out


# --------------------------------------------------------------------------
# abertura de turno: híbrido + prefix padding
# --------------------------------------------------------------------------
def test_hibrido_exige_prob_e_energia():
    """Nem energia sozinha, nem Silero sozinho: os dois."""
    # sinal alto, mas o modelo diz "não é fala"
    motor = lt.TurnEngine(lt.Config(), scorer=lambda f: 0.01, relogio=lambda: 0.0)
    assert _tocar(motor, [0.0], _tom(0.5), 20) == []
    # o modelo diz "fala", mas o sinal está no chão
    motor = lt.TurnEngine(lt.Config(), scorer=lambda f: 0.9, relogio=lambda: 0.0)
    assert _tocar(motor, [0.0], _tom(0.0002), 20) == []


def test_onset_retroage_timestamp_e_traz_prefixo():
    motor, rel = _motor()
    assert _tocar(motor, rel, SILENCIO, 30) == []
    voz = _tom(0.3)
    assert _tocar(motor, rel, voz, 1) == []          # 1 frame não basta (onset=2)

    t_antes = rel[0] * 1000
    eventos = motor.feed(voz)
    rel[0] += F / lt.SAMPLE_RATE
    assert [e.tipo for e in eventos] == ["speech_start"]
    ev = eventos[0]

    prefixo_e_onset = PRE_ROLL                            # confirmados + pré-roll + envelope
    assert ev.amostra == (32 - prefixo_e_onset) * F          # frame 31 é o gatilho
    assert len(ev.audio) == prefixo_e_onset * F * 2          # PCM16
    assert abs(ev.t_ms - (t_antes - prefixo_e_onset * lt.FRAME_MS)) <= 1
    assert ev.t_ms < t_antes                                 # retroagido, não "agora"
    assert ev.barge_in is False and ev.prob > 0.5


def test_evento_carrega_timestamp_de_parede():
    motor, rel = _motor()
    ev = _tocar(motor, rel, _tom(0.3), 3)[0]
    assert ev.ts == 1_700_000_000.0
    assert isinstance(ev.t_ms, int)


# --------------------------------------------------------------------------
# fim de turno por silêncio
# --------------------------------------------------------------------------
@pytest.mark.parametrize("silence_ms", [lt.SILENCE_MS_PADRAO, 900])
def test_fim_de_fala_no_silencio_configurado(silence_ms):
    motor, rel = _motor(silence_ms=silence_ms)
    _tocar(motor, rel, _tom(0.3), 20)
    n_silencio = lt.Config(silence_ms=silence_ms)._frames_silencio
    eventos = _tocar(motor, rel, SILENCIO, n_silencio - 1)
    assert eventos == []                                     # ainda não fechou
    eventos = _tocar(motor, rel, SILENCIO, 1)
    assert [e.tipo for e in eventos] == ["speech_end"]
    assert eventos[0].detalhe == "silencio" and eventos[0].curto is False
    assert eventos[0].fala_ms == 20 * lt.FRAME_MS


def test_audio_do_turno_tem_prefixo_fala_e_cauda_curta():
    motor, rel = _motor()
    _tocar(motor, rel, SILENCIO, 20)                    # sobra de histórico
    iniciar = _tocar(motor, rel, _tom(0.3), 20)[0]
    prefixo_e_onset = len(iniciar.audio) // 2 // F
    assert prefixo_e_onset == PRE_ROLL
    assert iniciar.amostra == (20 + 2 - PRE_ROLL) * F
    eos = _tocar(motor, rel, SILENCIO, lt.Config()._frames_silencio)[0]
    frames_total = len(eos.audio) // 2 // F
    # prefixo+onset + fala restante + cauda; o silêncio que sobra é descartado
    assert frames_total == prefixo_e_onset + (20 - 2) + lt.Config()._frames_cauda
    amostras = np.frombuffer(eos.audio, dtype="<i2").astype(np.float32) / 32768.0
    por_frame = amostras.reshape(-1, F)
    energia = np.sqrt((por_frame ** 2).mean(axis=1))
    # o pré-roll de 4 frames antes do onset é silêncio; os 20 frames de fala entram
    assert float(np.max(energia[: lt.Config()._frames_prefix])) < 0.01
    assert int((energia > 0.1).sum()) == 20


def test_turno_curto_e_marcado():
    motor, rel = _motor(min_fala_ms=250)
    _tocar(motor, rel, _tom(0.3), 3)                        # 96 ms de fala
    eos = _tocar(motor, rel, SILENCIO, lt.Config()._frames_silencio)[0]
    assert eos.curto is True and eos.fala_ms == 3 * lt.FRAME_MS


def test_turno_maximo_fecha_sozinho():
    motor, rel = _motor(turno_max_ms=200)
    eventos = _tocar(motor, rel, _tom(0.3), 20)
    # a fala contínua reabre turno depois do corte: o que importa é o motivo
    assert [e.tipo for e in eventos][:2] == ["speech_start", "speech_end"]
    assert eventos[1].detalhe == "turno_max"
    assert len(eventos[1].audio) // 2 // F == lt.Config(turno_max_ms=200)._frames_turno_max


def test_flush_fecha_e_e_idempotente():
    """`flush` é o que o handler chama quando o cliente manda `end_of_speech`."""
    motor, rel = _motor()
    _tocar(motor, rel, _tom(0.3), 5)
    assert [e.detalhe for e in motor.flush()] == ["flush"]
    assert motor.flush() == []
    assert motor.estado is lt.Estado.OCIOSO


def test_cancel_nao_emite_speech_end():
    motor, rel = _motor()
    _tocar(motor, rel, _tom(0.3), 20)
    info = motor.cancel()
    assert info == {"cancelado": True, "fala_ms": 20 * lt.FRAME_MS,
                    "curto": False, "barge_in": False}
    assert motor.cancel() == {"cancelado": False}
    assert _tocar(motor, rel, SILENCIO, 30) == []


# --------------------------------------------------------------------------
# alinhamento de frames
# --------------------------------------------------------------------------
def test_chunks_de_tamanho_arbitrario_dao_o_mesmo_resultado():
    rng = np.random.default_rng(4)
    fala = np.concatenate([np.zeros(16000, np.float32),
                           (0.3 * np.sin(2 * np.pi * FALA_HZ * np.arange(16000) / 16000)
                            ).astype(np.float32),
                           np.zeros(16000, np.float32)])
    fala += (rng.normal(0, 0.001, fala.size)).astype(np.float32)

    motor, rel = _motor()
    referencia = _tocar(motor, rel, None, 0) or []
    eventos_ref = []
    for i in range(0, fala.size, F):
        eventos_ref += motor.feed(fala[i:i + F])

    for chunk in (1600, 1024, 333, 7):
        outro, rel2 = _motor()
        eventos = []
        for i in range(0, fala.size, chunk):
            eventos += outro.feed(fala[i:i + chunk])
            rel2[0] += chunk / lt.SAMPLE_RATE
        assert [(e.tipo, e.amostra) for e in eventos] == \
               [(e.tipo, e.amostra) for e in eventos_ref]
    assert referencia == []
    assert [e.tipo for e in eventos_ref] == ["speech_start", "speech_end"]


def test_pcm16_e_float_dao_o_mesmo_resultado():
    fala = _tom(0.3)
    pcm = (fala * 32767).astype("<i2").tobytes()
    motor_a, rel_a = _motor()
    motor_b, rel_b = _motor()
    eventos_a = _tocar(motor_a, rel_a, fala, 5)
    eventos_b = _tocar(motor_b, rel_b, pcm, 5)
    assert [e.tipo for e in eventos_a] == [e.tipo for e in eventos_b] == ["speech_start"]
    assert (eventos_a[0].amostra, eventos_a[0].t_ms) == (eventos_b[0].amostra,
                                                         eventos_b[0].t_ms)


def test_scorer_recebe_exatamente_512_amostras():
    vistos = []

    def scorer(frame):
        vistos.append(frame.shape)
        return 0.9

    motor = lt.TurnEngine(lt.Config(), scorer=scorer, relogio=lambda: 0.0)
    motor.feed(np.zeros(1500, np.float32))       # 2 frames + sobra
    assert vistos == [(F,), (F,)]


# --------------------------------------------------------------------------
# barge-in e eco
# --------------------------------------------------------------------------
def test_barge_in_exige_energia_sustentada():
    motor, rel = _motor()
    moto = _abrir_por_barge(motor, rel)
    assert [e.tipo for e in moto] == ["barge_in", "speech_start"]
    assert moto[0].barge_in and moto[1].barge_in
    assert "300ms" in moto[0].detalhe
    # o áudio do speech_start cobre barge_in_ms + prefix: nada de perder sílaba
    assert len(moto[1].audio) >= (300 + 100) // lt.FRAME_MS * F * 2
    assert moto[0].amostra == moto[1].amostra


def test_transiente_curto_nao_dispara_barge_in():
    motor, rel = _motor()
    _tocar(motor, rel, _tom(0.001), 40)
    _tocar(motor, rel, ECO, 80)
    motor.set_speaking(True, nivel_dbfs=-18.0)
    _tocar(motor, rel, ECO, 40)
    assert _tocar(motor, rel, _mistura(ECO, _tom(0.3)), 5) == []   # 160 ms < 300 ms
    assert motor.estatisticas()["barge_in"] == 0


def test_eco_puro_nao_dispara_barge_in():
    motor, rel = _motor()
    assert _abrir_por_barge(motor, rel) != []                   # controle: dispara
    motor2, rel2 = _motor()
    _tocar(motor2, rel2, _tom(0.001), 40)
    _tocar(motor2, rel2, ECO, 80)
    motor2.set_speaking(True, nivel_dbfs=-18.0)
    assert _tocar(motor2, rel2, ECO, 60) == []


def test_eco_nao_contamina_o_piso_de_ruido():
    motor, rel = _motor()
    _tocar(motor, rel, _tom(0.0005), 80)
    piso = motor.estatisticas()["noise_dbfs"]
    motor.set_speaking(True, nivel_dbfs=-18.0)
    _tocar(motor, rel, ECO, 80)
    assert motor.estatisticas()["noise_dbfs"] == piso


def test_eco_residual_nao_fecha_turno_nem_vira_fala_fantasma():
    """O handler mata o playback no barge-in, mas o cliente ainda tem áudio."""
    motor, rel = _motor()
    assert [e.tipo for e in _abrir_por_barge(motor, rel)] == ["barge_in", "speech_start"]
    motor.set_speaking(False)
    assert _tocar(motor, rel, ECO, 8) == []                     # eco ainda no mic
    eos = _tocar(motor, rel, SILENCIO, 40)
    assert [e.tipo for e in eos] == ["speech_end"]
    assert _tocar(motor, rel, SILENCIO, 40) == []               # nada de fantasma
    assert motor.estatisticas()["turnos"] == 1                  # só o turno do barge


def test_eco_com_fade_in_nao_dispara_barge():
    """Eco que abre baixinho e sobe devagar (medido em vários wavs do app).

    Com calibração de janela fixa o motor calibrava a RAMPA (nível baixo) e o
    próprio eco estourava o limiar depois — barge-in fantasma em toda fala."""
    rel = [0.0]
    motor = lt.TurnEngine(lt.Config(), scorer=lambda f: 0.02, relogio=lambda: rel[0])
    _tocar(motor, rel, SILENCIO, 5)
    motor.set_speaking(True, nivel_dbfs=-30.0)
    for amp in np.linspace(0.0, 0.05, 15):
        motor.feed(_tom(float(amp)))
        rel[0] += F / lt.SAMPLE_RATE
    eventos = _tocar(motor, rel, _tom(0.05), 60)
    assert eventos == []
    assert motor.estatisticas()["eco_dbfs"] > -36      # pegou o patamar, não a rampa


@pytest.mark.parametrize("pausa_frames", [10, 40])
def test_calibracao_espera_audio_de_verdade(pausa_frames):
    """TTS que abre com pausa longa: calibrar o SILÊNCIO seria pior que nada."""
    motor, rel = _motor()
    motor.set_speaking(True, nivel_dbfs=-30.0)
    assert _tocar(motor, rel, SILENCIO, pausa_frames) == []
    assert motor.estatisticas()["eco_dbfs"] < -38        # ainda no palpite, não no silêncio
    assert _tocar(motor, rel, _tom(0.05), 20) == []      # calibra quando o áudio chega
    assert motor.estatisticas()["eco_dbfs"] > -35


def test_pausa_entre_chunks_do_tts_nao_afunda_o_eco():
    """Na pausa do TTS o envelope cai; o estimador não pode escorregar junto."""
    motor, rel = _motor()
    _tocar(motor, rel, SILENCIO, 10)
    motor.set_speaking(True, nivel_dbfs=-30.0)
    eco = _tom(0.05)
    _tocar(motor, rel, eco, 60)
    calibrado = motor.estatisticas()["eco_dbfs"]
    assert calibrado > -36
    _tocar(motor, rel, SILENCIO, 6)                    # pausa de ~190 ms
    _tocar(motor, rel, eco, 20)
    assert motor.estatisticas()["eco_dbfs"] > calibrado - 1.0


def test_barge_in_nao_conta_falso_quando_o_humano_fala():
    motor, rel = _motor()
    _abrir_por_barge(motor, rel)
    motor.set_speaking(False)
    _tocar(motor, rel, _mistura(ECO, _tom(0.3)), 30)
    eos = _tocar(motor, rel, SILENCIO, lt.Config()._frames_silencio)[0]
    assert eos.barge_falso is False
    stats = motor.estatisticas()
    assert stats["barge_in"] == 1 and stats["barge_in_falso"] == 0
    assert stats["taxa_falso_barge_in"] == 0.0


def test_falso_barge_in_sugere_hold_to_talk():
    motor, rel = _motor()
    eco2 = _tom(0.0005)
    for _ in range(lt.Config().sugestao_apos_barge_in):
        _tocar(motor, rel, eco2, 30)
        motor.set_speaking(True, nivel_dbfs=-18.0)
        _tocar(motor, rel, ECO, 40)
        # 2 frames de voz: dispara o barge-in (onset 300 ms) mas fecha "curto"
        eventos = _tocar(motor, rel, _mistura(ECO, _tom(0.3)), lt.Config()._frames_barge)
        assert "barge_in" in [e.tipo for e in eventos]
        motor.set_speaking(False)
        eos = _tocar(motor, rel, ECO, lt.Config()._frames_silencio + 2)[0]
        assert eos.barge_falso is True          # nenhuma fala além da janela
    stats = motor.estatisticas()
    assert stats["barge_in"] == lt.Config().sugestao_apos_barge_in
    assert stats["taxa_falso_barge_in"] == 1.0
    assert stats["hold_to_talk_sugerido"] is True


def test_hold_to_talk_no_config_desliga_a_sugestao():
    motor, rel = _motor(hold_to_talk=True)
    for _ in range(6):
        _tocar(motor, rel, _tom(0.0005), 20)
        motor.set_hold(True)
        motor.set_hold(False)
    assert motor.estatisticas()["hold_to_talk_sugerido"] is False


# --------------------------------------------------------------------------
# fallback de UI: segurar pra falar
# --------------------------------------------------------------------------
def test_set_hold_abre_e_fecha_o_turno():
    motor, rel = _motor()
    _tocar(motor, rel, _tom(0.0005), 40)          # voz baixa: o heurístico não pega
    abrir = motor.set_hold(True)
    assert [e.tipo for e in abrir] == ["speech_start"] and abrir[0].detalhe == "hold"
    _tocar(motor, rel, _tom(0.0005), 20)          # frame fraco conta como fala
    fechar = motor.set_hold(False)
    assert [e.tipo for e in fechar] == ["speech_end"]
    assert fechar[0].curto is False and fechar[0].fala_ms == 20 * lt.FRAME_MS


def test_hold_durante_playback_vira_barge_in():
    motor, rel = _motor()
    _tocar(motor, rel, _tom(0.001), 20)
    motor.set_speaking(True, nivel_dbfs=-18.0)
    _tocar(motor, rel, ECO, 20)
    eventos = motor.set_hold(True)
    assert [e.tipo for e in eventos] == ["barge_in", "speech_start"]
    assert motor.estatisticas()["barge_in"] == 1


def test_modo_hold_to_talk_sem_botao_nao_abre():
    motor, rel = _motor(hold_to_talk=True)
    assert _tocar(motor, rel, _tom(0.5), 30) == []
    motor.set_hold(True)
    _tocar(motor, rel, _tom(0.5), 10)
    assert [e.tipo for e in motor.set_hold(False)] == ["speech_end"]


# --------------------------------------------------------------------------
# fio
# --------------------------------------------------------------------------
def test_evento_json_e_serializavel():
    motor, rel = _motor()
    ev = _tocar(motor, rel, _tom(0.3), 3)[0]
    d = json.loads(json.dumps(ev.to_json()))
    assert d["type"] == "speech_start" and d["t_ms"] == ev.t_ms
    assert d["audio_bytes"] == len(ev.audio) and d["barge_in"] is False


def test_relatorio_tem_stats_e_linha_de_tempo():
    motor, rel = _motor()
    eventos = _tocar(motor, rel, _tom(0.3), 20)
    eventos += _tocar(motor, rel, SILENCIO, lt.Config()._frames_silencio)
    rel_ = lt.gerar_relatorio(motor, eventos)
    assert [linha["type"] for linha in rel_["eventos"]] == ["speech_start", "speech_end"]
    assert "taxa_falso_barge_in" in rel_["stats"]


# --------------------------------------------------------------------------
# integração com o Silero ONNX real (subprocesso: isolado e sem Metal)
# --------------------------------------------------------------------------
def _subproc(codigo: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ORT_DISABLE_TELEMETRY": "1"}
    return subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True,
                          cwd=str(RAIZ), env=env, timeout=600)


INTEGRACAO = r'''
import socket
import sys

import numpy as np

_rede = []
_orig = socket.socket.connect


def _recusar(self, address):
    _rede.append(address)
    raise AssertionError("o modulo tentou abrir rede: %r" % (address,))


socket.socket.connect = _recusar

import live_turns as lt

assert "mlx" not in sys.modules, "o motor nao pode arrastar MLX/Metal"
assert lt.FRAME_SAMPLES == 512

vistos = []


def scorer(frame):
    vistos.append(frame.shape)
    return lt.scorer_silero(frame)


def sintetica(n):
    t = np.arange(n) / 16000.0
    y = np.zeros(n)
    for k in range(1, 60):
        f = 110.0 * k
        if f > 7000:
            break
        e = sum(g * np.exp(-0.5 * ((f - c) / bw) ** 2)
                for c, bw, g in ((700, 120, 1.0), (1220, 180, 0.6),
                                 (2600, 300, 0.35), (3400, 400, 0.2)))
        y += (e + 1e-3) / k ** 1.0 * np.sin(2 * np.pi * f * t)
    y -= y.mean()
    y /= np.max(np.abs(y))
    y *= 0.55 + 0.45 * np.sin(2 * np.pi * 3.2 * t)
    return (y * 0.3).astype(np.float32)


def voz_de_referencia(n):
    from pathlib import Path
    wavs = sorted(Path("voices").glob("*.wav"))
    if not wavs:
        return sintetica(n), False
    import soundfile as sf
    from scipy.signal import resample_poly
    a, sr = sf.read(str(wavs[0]), dtype="float32")
    a = resample_poly(a, 16000, int(sr)).astype(np.float32)
    a = a / max(1e-9, float(np.max(np.abs(a)))) * 0.3
    return np.tile(a, int(np.ceil(n / len(a))))[:n], True


voz, real = voz_de_referencia(16000)
limiar = 0.5
if not real:
    probs = [lt.scorer_silero(voz[i:i + 512]) for i in range(0, 16000 - 511, 512)]
    limiar = max(0.05, min(0.5, float(np.percentile(probs, 50)) * 0.9))

prob = float(np.median([lt.scorer_silero(voz[i:i + 512]) for i in range(0, 8192, 512)]))
assert prob > limiar, "voz de referencia sem resposta do modelo"

motor = lt.TurnEngine(lt.Config(fala_threshold=limiar), scorer=scorer)
silencio = np.zeros(16000, dtype=np.float32)
eventos = motor.feed(silencio[:8000]) + motor.feed(voz) + motor.feed(silencio)

tipos = [e.tipo for e in eventos]
assert tipos == ["speech_start", "speech_end"], tipos
inicio, fim = eventos
assert inicio.barge_in is False
assert len(inicio.audio) // 2 // 512 == (2 + 4 + 7)      # onset + prefixo + envelope
assert fim.detalhe == "silencio" and fim.curto is False and fim.fala_ms >= 400
assert vistos and all(s == (512,) for s in vistos)
assert lt.BACKEND == "onnx", "caminho do VAD saiu do ONNX: %r" % (lt.BACKEND,)
assert not _rede, _rede
print("ok onnx prob=%.3f limiar=%.3f real=%s fala_ms=%d" % (prob, limiar, real, fim.fala_ms))
'''


def test_integracao_onnx_sem_metal_e_sem_rede():
    r = _subproc(INTEGRACAO)
    assert r.returncode == 0, r.stderr
    assert "ok onnx" in r.stdout, r.stdout
    assert "vad" not in r.stderr.lower() or "torch jit" not in r.stderr


def test_fallback_para_o_jit_e_logado_nao_silencioso():
    codigo = """
import sys
sys.modules["onnxruntime"] = None      # o load ONNX estoura
import live_turns as lt
assert lt.carregar_modelo() is not None
assert lt.BACKEND == "torch-jit", lt.BACKEND
print("ok", lt.BACKEND)
"""
    r = _subproc(codigo)
    assert r.returncode == 0, r.stderr
    assert "ok torch-jit" in r.stdout
    assert "ONNX indisponível" in r.stdout and "caindo no torch jit" in r.stdout