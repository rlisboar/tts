"""NCC offline do eco (#870). O correlacionador é o do motor, não uma segunda fórmula.

O eco da bancada é uma cópia atrasada da referência: casa por construção.
Aqui o par sintético só prova que a ferramenta e o dump medem a MESMA NCC.
O eco de sala (alto-falante, AEC do Chrome) continua sendo o dump da sessão
do dono, depois do deploy. A flag do duck não entra.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

import live_eco_dump
import live_turns as lt

RAIZ = Path(__file__).resolve().parent.parent
_ATRASO = 1600
_N = 48000


def _ferramenta():
    spec = importlib.util.spec_from_file_location(
        "ncc_eco", RAIZ / "tools" / "ncc_eco.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ncc_eco = _ferramenta()


def _ruido(rng: np.random.Generator, n: int, dbfs: float) -> np.ndarray:
    x = rng.standard_normal(n).astype(np.float32)
    rms = float(np.sqrt(np.mean(x * x))) + 1e-12
    return x * (np.float32(10 ** (dbfs / 20.0)) / rms)


def _par(seed: int = 870, n: int = _N, atraso: int = _ATRASO):
    """Referência em ruído branco (~−20 dBFS) e o eco atrasado 100 ms, −15 dB.

    Sem seno: seno casa com ele mesmo em vários lags. O piso de −60 dBFS é
    o da bancada. A janela pontuada começa depois do aquecimento (96 ms +
    500 ms), já com o eco inteiro dentro dela.
    """
    rng = np.random.default_rng(seed)
    ref = _ruido(rng, n, -20.0)
    eco = np.zeros(n, dtype=np.float32)
    eco[atraso:] = ref[:-atraso] * np.float32(10 ** (-15.0 / 20.0))
    eco = eco + _ruido(rng, n, -60.0)
    return ref, eco


def _wav(path: Path, amostras: np.ndarray, sr: int = 16000) -> None:
    pcm = (np.clip(amostras, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


class _Eng:
    """O mínimo que `marca` lê, debaixo do lock do motor."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proxima_amostra = 0
        self._ref_play = 0
        self._ref_origem = 0
        self._ref_n = 0
        self._ref_pausado = False

    def _playback_ativo(self) -> bool:
        return True


def test_janela_casa_com_o_correlacionador():
    """Uma janela da ferramenta é `melhor_ncc` / `TurnEngine._ncc`. Sem isso, falha."""
    ref, eco = _par()
    fim = 20000
    n = lt._CORR_N
    janela = eco[fim - n:fim]

    def trecho(lag, n_):
        ini = fim - lag - n_
        if ini < 0 or ini + n_ > len(ref):
            return None
        return ref[ini:ini + n_]

    melhor, lag = lt.melhor_ncc(janela, trecho)
    assert lag == _ATRASO
    pedaco = ref[fim - lag - n:fim - lag]
    assert melhor == lt.ncc(janela, pedaco)
    assert lt.TurnEngine._ncc(janela, pedaco) == lt.ncc(janela, pedaco)
    got = ncc_eco.ncc_no_instante(eco, ref, fim, fim, 0, len(ref))
    assert got == (melhor, lag)
    linha = [{"mic": fim, "play": fim, "origem": 0, "ref_n": len(ref)}]
    r = ncc_eco.medir(eco, ref, linha, sem_relogio=False)
    assert r["medidas"] == 1
    assert r["p10"] == pytest.approx(melhor)
    assert r["lag_p50"] == _ATRASO


def test_eco_atrasado_passa_e_voz_independente_nao():
    ref, eco = _par()
    r = ncc_eco.medir(eco, ref)
    assert r["medidas"] > 0 and r["busca_curta"] > 0, r
    assert r["p10"] >= 0.8, r
    assert r["eco"] is True
    assert abs(r["lag_p50"] - _ATRASO) <= lt._CORR_LAG_PASSO, r
    assert "sem relógio" in r["aviso"]
    rng = np.random.default_rng(871)
    voz = _ruido(rng, len(eco), -35.0)
    alvo = float(np.sqrt(np.mean(eco * eco)))
    voz *= alvo / (float(np.sqrt(np.mean(voz * voz))) + 1e-12)
    rv = ncc_eco.medir(voz, ref)
    assert rv["medidas"] > 0, rv
    assert rv["p90"] <= 0.4, rv
    assert rv["eco"] is False


def test_clipe_curto_nao_conta_como_eco_fraco():
    ref, eco = _par(n=4000)
    r = ncc_eco.medir(eco, ref)
    assert r["medidas"] == 0, r
    assert r["busca_curta"] > 0, r
    assert r["p10"] is None
    assert r["eco"] is False


def test_silencio_abaixo_de_45_nao_entra():
    rng = np.random.default_rng(872)
    ref = _ruido(rng, _N, -20.0)
    r = ncc_eco.medir(np.zeros(_N, dtype=np.float32), ref)
    assert r["medidas"] == 0, r
    assert r["silencio"] > 0, r
    assert r["eco"] is False
    assert r["p10"] is None


def test_origem_curta_nao_vira_ncc_baixa():
    ref, eco = _par()
    linha = [{"mic": 20000, "play": 20000, "origem": 19900, "ref_n": len(ref)}]
    r = ncc_eco.medir(eco, ref, linha, sem_relogio=False)
    assert r["busca_curta"] == 1, r
    assert r["medidas"] == 0
    assert r["eco"] is False


def test_dump_desligado_nao_escreve(tmp_path, monkeypatch):
    monkeypatch.delenv("TTS_LIVE_DUMP_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    sess = {}
    live_eco_dump.anota_mic(sess, b"\x00\x01" * 160)
    live_eco_dump.anota_ref(sess, np.zeros(160, dtype=np.float32))
    live_eco_dump.marca(sess, _Eng())
    live_eco_dump.fecha(sess)
    assert list(tmp_path.iterdir()) == []
    assert "_eco_dump" not in sess


def test_sem_audio_nao_cria_pasta(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_LIVE_DUMP_DIR", str(tmp_path))
    sess = {}
    live_eco_dump.anota_mic(sess, b"")
    live_eco_dump.anota_ref(sess, np.zeros(0, dtype=np.float32))
    live_eco_dump.fecha(sess)
    assert list(tmp_path.iterdir()) == []


def test_dump_roundtrip_mede_o_eco(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_LIVE_DUMP_DIR", str(tmp_path))
    ref, eco = _par()
    pcm = (np.clip(eco, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    sess = {"id": "t"}
    live_eco_dump.anota_ref(sess, ref)
    live_eco_dump.anota_mic(sess, pcm)
    eng = _Eng()
    eng._ref_n = len(ref)
    fim = lt._CORR_N
    while fim <= len(eco):
        eng._proxima_amostra = fim
        eng._ref_play = fim
        live_eco_dump.marca(sess, eng)
        fim += lt.FRAME_SAMPLES
    live_eco_dump.fecha(sess)
    pastas = list(tmp_path.glob("eco-*"))
    assert len(pastas) == 1, pastas
    meta = json.loads((pastas[0] / "meta.json").read_text(encoding="utf-8"))
    assert meta["mic_amostras"] == len(eco)
    assert meta["ref_amostras"] == len(ref)
    assert meta["truncado"] is False
    r = ncc_eco.medir_pasta(pastas[0])
    assert r["sem_relogio"] is False, r
    assert r["p10"] >= 0.8, r
    assert r["eco"] is True
    assert abs(r["lag_p50"] - _ATRASO) <= lt._CORR_LAG_PASSO, r


def test_wav_de_turno_nao_e_eco_fraco(tmp_path):
    wav = tmp_path / "20261106-120000-00001.wav"
    _wav(wav, np.zeros(1600, dtype=np.float32))
    wav.with_suffix(".json").write_text(
        json.dumps({"ms": 100, "motivo": "ok", "texto": "oi"}), encoding="utf-8")
    r = ncc_eco.medir_arquivos(wav, None)
    assert r["tipo"] == "turno"
    assert r["eco"] is False
    assert r["p10"] is None
    assert "não é o eco que falhou" in r["aviso"]
    saida = ncc_eco.medir_diretorio(tmp_path)
    assert any(x["tipo"] == "turno" for x in saida)


def test_passo_15_em_2400_casa_com_o_arquivo():
    rng = np.random.default_rng(3)
    a = (rng.standard_normal(4800).astype(np.float32) * 0.1)
    todo = ncc_eco.ref_24k_para_16k(a)
    partes = np.concatenate([
        ncc_eco.ref_24k_para_16k(a[:2400]),
        ncc_eco.ref_24k_para_16k(a[2400:]),
    ])
    assert partes.shape == todo.shape
    assert np.allclose(partes, todo, atol=1e-6)


def test_ref_24k_casa_com_o_app():
    import app

    rng = np.random.default_rng(4)
    for n in (2400, 4800):
        a = rng.standard_normal(n).astype(np.float32) * 0.1
        pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        y_app = app._live_ref_16k(pcm)
        y = ncc_eco.ref_24k_para_16k(
            np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0)
        assert y.shape == y_app.shape
        assert np.allclose(y, y_app, atol=1e-6)


def test_gancho_desligado_nao_abre_dump(monkeypatch):
    import app

    monkeypatch.delenv("TTS_LIVE_DUMP_DIR", raising=False)
    sess = {}
    app._live_eco_mic(sess, b"\x00\x01" * 32)
    app._live_eco_ref(sess, np.zeros(8, dtype=np.float32))
    app._live_eco_marca(sess, _Eng())
    app._live_eco_fecha(sess)
    assert "_eco_dump" not in sess


def test_cli_sai_zero_e_taxa_estranha_nao_e_gate(tmp_path, capsys):
    ref, eco = _par()
    _wav(tmp_path / "mic.wav", eco, 16000)
    _wav(tmp_path / "ref.wav", ref, 16000)
    rc = ncc_eco.main(["--mic", str(tmp_path / "mic.wav"),
                       "--ref", str(tmp_path / "ref.wav"), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["p10"] >= 0.8
    assert data["eco"] is True
    assert "sem relógio" in data["aviso"]
    _wav(tmp_path / "outro.wav", ref, 22050)
    rc = ncc_eco.main(["--mic", str(tmp_path / "mic.wav"),
                       "--ref", str(tmp_path / "outro.wav"), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["eco"] is False
    assert "22050" in data["aviso"]
