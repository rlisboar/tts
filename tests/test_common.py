"""Testes das utilidades compartilhadas (common.py)."""

import json
import multiprocessing
import os
import threading

import numpy as np
import pytest

import common
from common import (PAD_CAUDA_S, PAUSA_PADRAO_S, assemble_omnivoice_path, fade_edges,
                    motivo_anomalia, normalize, pausa_apos, pontua_corte,
                    sanitize_text, split_text,
                    time_stretch, trim_tail_silence, write_json_atomic,
                    write_wav_concat)


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


# ---------------------------------------------------------------------------
# #303 — não-finito não pode atravessar o DSP
# ---------------------------------------------------------------------------

def test_normalize_vetor_todo_nan_sai_finito():
    x = np.full(4800, np.nan, dtype=np.float32)
    y = normalize(x)
    assert np.isfinite(y).all(), "normalize propagou NaN"
    assert float(np.abs(y).max()) == 0.0


def test_normalize_nan_no_meio_preserva_o_resto():
    x = _sinal() * 0.05
    x[2000] = np.nan
    x[3000] = np.inf
    x[3500] = -np.inf
    y = normalize(x)
    assert np.isfinite(y).all()
    assert float(np.abs(y).max()) <= 0.95 + 1e-6
    # o miolo sem defeito continua sendo sinal, não zeros
    miolo = y[500:1500]
    assert float(np.sqrt(np.mean(miolo ** 2))) == pytest.approx(0.15, rel=0.1)


# ---------------------------------------------------------------------------
# #302 — o teto de pico é limiter, não ganho
# ---------------------------------------------------------------------------

def _com_crista(pico_rel: float) -> np.ndarray:
    """Sinal de RMS ~0,05 com um transiente `pico_rel`× o próprio RMS."""
    x = (_sinal() * 0.05).astype(np.float32)
    rms = float(np.sqrt(np.mean(x ** 2)))
    x[:50] *= (pico_rel * rms) / max(1e-9, float(np.abs(x[:50]).max()))
    return x


def test_normalize_crista_alta_NAO_derruba_o_nivel():
    """Antes o trecho de crista alta era normalizado pelo PICO e saía mais baixo."""
    alvo = 0.15
    limpo = normalize(_sinal() * 0.05, target_rms=alvo)
    com_crista = normalize(_com_crista(8.0), target_rms=alvo)   # crista ~18 dB
    r_limpo = float(np.sqrt(np.mean(limpo ** 2)))
    r_crista = float(np.sqrt(np.mean(com_crista ** 2)))
    assert r_crista == pytest.approx(r_limpo, rel=0.10), (
        f"crista alta mudou o nível: {r_limpo:.4f} -> {r_crista:.4f}")
    assert float(np.abs(com_crista).max()) <= 0.95 + 1e-6


def test_normalize_igualha_niveis_do_paragrafo_da_auditoria():
    """Os 4 níveis crus medidos na auditoria saem com espalhamento ≤2 dB.

    Nível cru do parágrafo real (item 1 do #284): 0,0532 / 0,1267 / 0,1059 /
    0,0970 (7,5 dB de espalhamento). Antes o trecho de crista alta saía ainda
    mais baixo que isso; depois do conserto o espalhamento tem de fechar.
    """
    import math

    niveis = (0.0532, 0.1267, 0.1059, 0.0970)
    # transiente de amplitude ABSOLUTA igual nos quatro: assim a crista de cada
    # trecho muda com o nível dele — é o que a normalização pelo pico castigava
    # de forma desigual (o trecho baixo virava o mais alto)
    saida = []
    for n in niveis:
        x = (_sinal() * n).astype(np.float32)
        x[:50] *= 0.5 / max(1e-9, float(np.abs(x[:50]).max()))
        saida.append(normalize(x))
    rms = [float(np.sqrt(np.mean(y ** 2))) for y in saida]
    espalhamento = 20 * math.log10(max(rms) / min(rms))
    assert espalhamento <= 2.0, f"espalhamento {espalhamento:.1f} dB (rms {rms})"
    for r in rms:
        assert abs(20 * math.log10(r / 0.15)) <= 1.0, f"trecho fora de ±1 dB do alvo: {r:.4f}"


def test_normaliza_loudness_leva_ao_alvo_e_respeita_tolerancia(tmp_path):
    import soundfile as sf

    from common import LUFS_ALVO, loudness_lufs, normaliza_loudness

    sr = 24000
    t = np.arange(3 * sr) / sr
    # fala sintética modulada: o normalize deixa em ~ -16 LUFS, fora da tolerância
    x = (0.05 * np.sin(2 * np.pi * 180 * t)
         * np.repeat(np.hanning(2400), 30)[:3 * sr]).astype(np.float32)
    p = tmp_path / "a.wav"
    sf.write(str(p), normalize(x), sr, subtype="PCM_16")
    antes = loudness_lufs(p)
    assert antes is not None and antes > LUFS_ALVO + 1.0, antes

    delta = normaliza_loudness(p)
    assert delta is not None and delta < 0
    assert abs(loudness_lufs(p) - LUFS_ALVO) <= 1.0
    # já no alvo: não reescreve, devolve 0.0
    assert normaliza_loudness(p) == 0.0


# ---------------------------------------------------------------------------
# #319 — prosódia do corte: pontuação preservada e pausa por pontuação
# ---------------------------------------------------------------------------

def test_pontua_corte_preserva_virgula_e_dois_pontos():
    """Corte por vírgula/dois-pontos/ponto-e-vírgula NÃO recebe ponto final."""
    for marca in (",", ":", ";"):
        assert pontua_corte(f"primeiro o cronograma{marca}", ultimo=False).endswith(marca)
        assert pontua_corte(f"primeiro o cronograma{marca}", ultimo=True).endswith(marca)
    # nada de `:.` nem `,.`
    assert ":. " not in " ".join([pontua_corte("na ordem:", True)])
    assert pontua_corte("na ordem:", True) == "na ordem:"


def test_pontua_corte_corte_seco_vira_virgula_so_o_ultimo_e_ponto():
    assert pontua_corte("um trecho cortado no meio", ultimo=False).endswith(",")
    assert pontua_corte("um trecho cortado no meio", ultimo=True).endswith(".")
    # pontuação terminal já presente é respeitada, não duplicada
    assert pontua_corte("Bom dia a todos.", ultimo=False) == "Bom dia a todos."
    assert pontua_corte("Sério?!", ultimo=True) == "Sério?!"


def test_paragrafo_da_auditoria_nao_gera_dois_pontos_nem_virgula_ponto():
    """O parágrafo real da auditoria: nenhum trecho pode fechar em `:.` ou `,.`."""
    texto = ("Bom dia a todos. Hoje a gente vai falar de três coisas, na ordem: "
             "primeiro o cronograma, depois os custos, e por último as dúvidas de vocês.")
    chunks = split_text(sanitize_text(texto), max_chars=60)
    finais = [pontua_corte(c, ultimo=(i == len(chunks) - 1))
              for i, c in enumerate(chunks)]
    assert len(finais) == 4
    for f in finais:
        assert not f.endswith(":."), f
        assert not f.endswith(",."), f
    assert finais[1].endswith(":")     # "…na ordem:"
    assert finais[2].endswith(",")      # "…os custos,"
    assert finais[3].endswith(".")      # último fecha


def test_pausa_por_pontuacao():
    assert pausa_apos(",") < pausa_apos(".")
    assert pausa_apos(";") <= 0.25 and pausa_apos(":") <= 0.25
    for fim in (".", "!", "?", "…"):
        assert 0.35 <= pausa_apos(fim) <= 0.50
    assert pausa_apos("palavra") == PAUSA_PADRAO_S      # corte seco = pausa de vírgula
    # vão total (pad da cauda + pausa) dentro do aceite
    assert PAD_CAUDA_S + pausa_apos(",") <= 0.25
    assert PAD_CAUDA_S + max(pausa_apos(c) for c in ".!?…") <= 0.50
    assert PAD_CAUDA_S <= 0.10


# ---------------------------------------------------------------------------
# #320 — portão de truncamento derivado do cps MEDIDO
# ---------------------------------------------------------------------------

# cps medido na auditoria (#284) em fala real do próprio modelo
CPS_MEDIDO = 15.4


def test_cps_minimo_e_derivado_do_medido_e_nao_do_45_frouxo():
    # fica ENTRE os dois pontos do aceite: reprova 40% da duração natural
    # (38,5 chars/s) e aceita 90% (17,1 chars/s). E é mais apertado que o 45.
    assert CPS_MEDIDO / 0.40 > common.CPS_MINIMO > CPS_MEDIDO / 0.90
    assert common.CPS_MINIMO < 45


def test_truncado_a_40_por_cento_dispara_e_a_90_nao():
    sr = 24000
    texto = "x" * 100
    natural = len(texto) / CPS_MEDIDO          # ~6,5 s do trecho natural
    curto = np.zeros(int(natural * 0.40 * sr), dtype=np.float32) + 0.1
    quase = np.zeros(int(natural * 0.90 * sr), dtype=np.float32) + 0.1
    assert "truncado" in motivo_anomalia(curto, sr, texto)
    assert motivo_anomalia(quase, sr, texto) == ""
