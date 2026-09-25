"""Testes das utilidades compartilhadas (common.py)."""

import json
import multiprocessing
import os
import threading

import numpy as np
import pytest

import common
from common import (assemble_omnivoice_path, fade_edges, normalize,
                    sanitize_text, split_text, time_stretch,
                    trim_tail_silence, write_json_atomic, write_wav_concat)


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def test_sanitize_text_expande_simbolos_e_pontua():
    assert sanitize_text("50% & 2+2") == "50 por cento e 2 mais 2."
    assert sanitize_text("olá") == "olá."           # pontuação terminal garantida
    assert sanitize_text("tudo  bem?") == "tudo bem?"  # espaços colapsados


def test_sanitize_text_nfc():
    # "á" em NFD (decomposto) vira o composto
    out = sanitize_text("ca\u0301")
    assert out == "cá."


def test_split_text_respeita_max_chars():
    texto = " ".join(f"Sentença número {i} com algumas palavras extras aqui." for i in range(30))
    trechos = split_text(texto, max_chars=140)
    assert all(len(t) <= 140 for t in trechos)
    assert " ".join(trechos).split() == texto.split()  # nada perdido


def test_split_text_primeira_sentenca_sozinha():
    texto = "Primeira frase curta. Segunda frase que é bem mais comprida e deveria ir para o próximo trecho sozinha."
    trechos = split_text(texto, max_chars=80)
    assert trechos[0] == "Primeira frase curta."


def test_split_text_funde_fragmentos_minusculos():
    trechos = split_text("Disparo: algo aconteceu agora mesmo com força total.", max_chars=60)
    assert all(len(t) >= 15 for t in trechos)


# ---------------------------------------------------------------------------
# DSP
# ---------------------------------------------------------------------------

SR = 24000


def _sinal(dur_s=0.6, sr=SR):
    t = np.arange(int(dur_s * sr), dtype=np.float32) / sr
    x = 0.5 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 880 * t)
    return x.astype(np.float32)


def test_normalize_atinge_rms_alvo():
    x = _sinal() * 0.05
    y = normalize(x)
    rms = float(np.sqrt(np.mean(y ** 2)))
    assert rms == pytest.approx(0.15, rel=0.05)


def test_normalize_limita_pico():
    x = _sinal() * 0.05
    x[:100] = 10.0                        # transiente gigante
    y = normalize(x)
    assert float(np.abs(y).max()) <= 0.95 + 1e-6


def test_normalize_vazio_e_silencio():
    assert normalize(np.zeros(0, dtype=np.float32)).size == 0
    assert float(np.abs(normalize(np.zeros(1000, dtype=np.float32))).max()) == 0.0


def test_trim_tail_silence_corta_cauda():
    x = _sinal()
    com_silencio = np.concatenate([x, np.zeros(SR, dtype=np.float32)])
    y = trim_tail_silence(com_silencio, SR, pad_s=0.1)
    assert len(y) < len(com_silencio)
    assert len(y) >= len(x)               # fala preservada


def test_fade_edges_zera_bordas():
    x = np.ones(SR // 2, dtype=np.float32)
    y = fade_edges(x, SR, ms=10)
    assert y[0] == 0.0 and y[-1] == 0.0
    assert float(y[len(y) // 2]) == pytest.approx(1.0)


def test_apply_audio_fx_identidade_ganho_e_limiter():
    from common import apply_audio_fx

    sr = 8000
    t = np.arange(sr, dtype=np.float32) / sr
    x = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert np.array_equal(apply_audio_fx(x, sr), x)            # zeros → intocatado
    y = apply_audio_fx(x, sr, gain_db=6.0)                     # +6 dB ≈ ×2
    rms_x = float(np.sqrt(np.mean(x ** 2)))
    assert float(np.sqrt(np.mean(y ** 2))) == pytest.approx(2 * rms_x, rel=0.15)
    z = apply_audio_fx(x, sr, gain_db=24.0)                    # limiter segura o pico
    assert float(np.abs(z).max()) <= 0.98


def test_time_stretch_identity_e_dimensao():
    x = _sinal()
    assert time_stretch(x, 1.0) is x
    y = time_stretch(x, 1.5)
    esperado = int(round(len(x) / 1.5))
    assert abs(len(y) - esperado) <= 1024   # borda de janela


def test_time_stretch_preserva_frequencia():
    sr, freq = 16000, 300.0
    t = np.arange(sr, dtype=np.float32) / sr
    x = (0.6 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    for speed in (0.8, 1.3):
        y = time_stretch(x, speed)
        # pico do espectro continua na frequência original (tom preservado)
        spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        pico = float(np.fft.rfftfreq(len(y), 1 / sr)[int(np.argmax(spec))])
        assert pico == pytest.approx(freq, abs=15)


def test_time_stretch_fallback_preserva_nivel():
    """Sem ffmpeg (sr ausente = fallback phase vocoder) o nível de entrada tem de
    sobreviver: o PV cancelava ~15x do sinal e a fala saía 'muda' — foi o que
    aconteceu com o OmniVoice numa máquina sem imageio-ffmpeg (todo backend que
    não tem speed nativa passa por aqui)."""
    sr, freq = 16000, 220.0
    t = np.arange(sr, dtype=np.float32) / sr
    x = (0.6 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    rms_in = float(np.sqrt((x ** 2).mean()))
    for speed in (0.8, 0.9, 1.3):
        y = time_stretch(x, speed)          # sem sr: força o PV
        rms_out = float(np.sqrt((y ** 2).mean()))
        assert rms_out == pytest.approx(rms_in, rel=0.2), speed


def test_time_stretch_equivalente_ao_referencia_em_laco():
    """A versão vetorial deve reproduzir o phase vocoder original (que vivia
    duplicado em app.py/tts_worker.py) dentro de tolerância float."""
    speed = 1.4
    x = (_sinal(0.5) + 0.05 * np.random.default_rng(7).standard_normal(int(0.5 * SR)).astype(np.float32))

    n_fft, hop = 1024, 256
    win = np.hanning(n_fft).astype(np.float32)

    def stft(sig):
        n = 1 + (len(sig) - n_fft) // hop
        return np.stack([np.fft.rfft(sig[i * hop:i * hop + n_fft] * win) for i in range(n)], axis=1)

    def istft(D):
        frames = D.shape[1]
        out = np.zeros((frames - 1) * hop + n_fft, dtype=np.float32)
        wsum = np.zeros_like(out)
        for i in range(frames):
            seg = np.fft.irfft(D[:, i], n_fft).astype(np.float32) * win
            out[i * hop:i * hop + n_fft] += seg
            wsum[i * hop:i * hop + n_fft] += win * win
        wsum[wsum < 1e-8] = 1e-8
        return out / wsum

    D = stft(x)
    bins = D.shape[0]
    omega = 2.0 * np.pi * hop * np.arange(bins) / n_fft
    steps = np.arange(0, D.shape[1] - 1, speed)
    ks = np.minimum(np.floor(steps).astype(np.int64), D.shape[1] - 2)  # clamp idêntico
    fracs = np.clip(steps - ks, 0.0, 1.0)
    out = np.zeros((bins, len(steps)), dtype=np.complex64)
    phase = np.angle(D[:, 0])
    for i in range(len(steps)):
        k = int(ks[i]); frac = float(fracs[i])
        mag = (1.0 - frac) * np.abs(D[:, k]) + frac * np.abs(D[:, k + 1])
        out[:, i] = mag * np.exp(1j * phase)
        dp = np.angle(D[:, k + 1]) - np.angle(D[:, k]) - omega
        dp -= 2.0 * np.pi * np.round(dp / (2.0 * np.pi))
        phase = phase + omega + dp
    ref = istft(out)
    peak = float(np.abs(ref).max() or 0.0)
    if peak > 0.99:
        ref *= 0.99 / peak
    ref = ref.astype(np.float32)

    got = time_stretch(x, speed)
    assert got.shape == ref.shape
    assert float(np.abs(got - ref).max()) < 1e-4


def test_atempo_chain_extremos():
    """Speed 0.25–4.0 passa de uma instância de atempo (0.5–2.0) — cadeia correta."""
    from common import atempo_chain

    assert atempo_chain(1.0) == "atempo=1"
    assert atempo_chain(1.5) == "atempo=1.5"
    assert atempo_chain(0.25) == "atempo=0.5,atempo=0.5"
    assert atempo_chain(4.0) == "atempo=2,atempo=2"
    assert atempo_chain(3.0) == "atempo=2,atempo=1.5"
    assert atempo_chain(0.3) == "atempo=0.5,atempo=0.6"


def test_time_stretch_ffmpeg_atempo():
    """Caminho primário (sr dado): ffmpeg atempo — duração escala e o tom fica."""
    pytest.importorskip("imageio_ffmpeg")
    sr, freq = 24000, 300.0
    t = np.arange(sr, dtype=np.float32) / sr
    x = (0.6 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    y = time_stretch(x, 1.5, sr)
    assert abs(len(y) - len(x) / 1.5) <= sr * 0.05     # duração ≈ 1/1.5 (±50 ms)
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    pico = float(np.fft.rfftfreq(len(y), 1 / sr)[int(np.argmax(spec))])
    assert pico == pytest.approx(freq, abs=15)


def test_time_stretch_edge_flutuante_nao_estoura():
    """Regressão: arange com passo 1.4 gerava último índice fora dos bounds
    (IndexError) na versão em laço — o clamp precisa cobrir este caso."""
    for dur_s, speed in ((0.5, 1.4), (0.5, 0.7), (0.3, 2.5)):
        y = time_stretch(_sinal(dur_s), speed)
        assert np.isfinite(y).all()


# ---------------------------------------------------------------------------
# WAV concat + montagem OmniVoice
# ---------------------------------------------------------------------------

def test_write_wav_concat_duracao(tmp_path):
    import soundfile as sf

    pdir = tmp_path / "pieces"
    pdir.mkdir()
    for i in range(3):
        sf.write(pdir / f"{i}.wav", _sinal(0.25), SR, subtype="PCM_16")
    out = tmp_path / "final.wav"
    dur = write_wav_concat(pdir, 3, out, SR)
    # o retorno é arredondado a 1 casa decimal (metadado de histórico)
    assert dur == pytest.approx(0.75, abs=0.06)
    import wave
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() / w.getframerate() == pytest.approx(0.75, abs=0.02)


def test_resolve_omni_fp32_sem_rede(tmp_path):
    from common import OMNI_FP32_REPO, resolve_omni_source

    assert resolve_omni_source({"omni_precision": "fp32"}, tmp_path) == OMNI_FP32_REPO


def test_assemble_omnivoice_reusa_dir_montado(tmp_path):
    backbone = tmp_path / "model.safetensors"
    tokenizer = tmp_path / "audio_tokenizer" / "model.safetensors"
    tokenizer.parent.mkdir()
    backbone.write_bytes(b"x")
    tokenizer.write_bytes(b"x")
    # dir já pronto tem o nome fixo .omnivoice-bf16 sob a base
    base = tmp_path
    (base / ".omnivoice-bf16").mkdir()
    (base / ".omnivoice-bf16" / "model.safetensors").write_bytes(b"x")
    (base / ".omnivoice-bf16" / "audio_tokenizer").mkdir()
    (base / ".omnivoice-bf16" / "audio_tokenizer" / "model.safetensors").write_bytes(b"x")
    assert assemble_omnivoice_path(base) == str(base / ".omnivoice-bf16")


# ---------------------------------------------------------------------------
# Escrita atômica sob concorrência (o gate da suíte é compartilhado: 6 agentes
# rodando pytest ao mesmo tempo no mesmo settings.json do repo)
# ---------------------------------------------------------------------------

def _batida(path, tag, rounds, erros=None):
    """Escritor concorrente: acumula falhas sem estourar a thread/processo e
    RETORNA a lista (em subprocesso a lista recebida é cópia, não a do pai)."""
    erros = [] if erros is None else erros
    for i in range(rounds):
        try:
            write_json_atomic(path, {"quem": tag, "i": i})
        except Exception as e:  # noqa: BLE001 — queremos ver QUAL exceção
            erros.append(f"{tag}/{i}: {type(e).__name__}: {e}")
    return erros


def test_write_json_atomic_tmp_unico_no_meio_de_outro_escritor(tmp_path, monkeypatch):
    """Regressão determinística: B escreve por INTEIRO entre o write do tmp de A
    e o replace de A — o entrelaçamento que o tmp de nome fixo não sobrevive
    (ambos usavam x.json.tmp: B substituía e o tmp sumia da mão de A, que
    estourava FileNotFoundError)."""
    dst = tmp_path / "settings.json"
    real_replace = os.replace
    estado = {"armado": True}

    def replace_espiao(src, dst_):
        if estado["armado"]:
            estado["armado"] = False
            write_json_atomic(dst, {"quem": "B"})   # escritor concorrente completo
            estado["armado"] = True
        return real_replace(src, dst_)

    monkeypatch.setattr(common.os, "replace", replace_espiao)
    write_json_atomic(dst, {"quem": "A"})           # não pode dar FileNotFoundError

    assert json.loads(dst.read_text()) == {"quem": "A"}   # A substitui por último e vence
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]   # sem .tmp órfão


def test_write_json_atomic_sem_corrida_entre_threads(tmp_path):
    dst = tmp_path / "settings.json"
    erros = []
    ths = [threading.Thread(target=_batida, args=(dst, f"t{k}", 150, erros))
           for k in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    assert erros == []
    assert set(json.loads(dst.read_text())) == {"quem", "i"}   # JSON íntegro, não truncado
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_write_json_atomic_sem_corrida_entre_processos(tmp_path):
    """Prova com processos de verdade (é assim que o falso vermelho apareceu:
    pytest rodando em paralelo pelos agentes)."""
    ctx = multiprocessing.get_context("spawn")   # evita o fork() em processo multi-thread
    dst = tmp_path / "settings.json"
    with ctx.Pool(6) as pool:
        res = pool.starmap(_batida, [(dst, f"p{k}", 120) for k in range(6)])
    erros = [e for r in res for e in r]

    assert erros == []
    assert set(json.loads(dst.read_text())) == {"quem", "i"}
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_write_json_atomic_limpa_tmp_quando_replace_falha(tmp_path, monkeypatch):
    dst = tmp_path / "settings.json"

    def boom(src, dst_):
        raise OSError("disco cheio")

    monkeypatch.setattr(common.os, "replace", boom)
    with pytest.raises(OSError):
        write_json_atomic(dst, {"a": 1})

    assert list(tmp_path.iterdir()) == []   # tmp único não fica órfão


def test_write_json_atomic_preserva_modo_do_destino(tmp_path):
    """mkstemp cria 0600; o destino (settings.json/.apikeys.json: 0644 no repo)
    não deve virar 0600 só por ser reescrito."""
    dst = tmp_path / "chaves.json"
    dst.write_text("{}")
    os.chmod(dst, 0o640)

    write_json_atomic(dst, {"k": 1})

    assert (dst.stat().st_mode & 0o777) == 0o640
    assert json.loads(dst.read_text()) == {"k": 1}
