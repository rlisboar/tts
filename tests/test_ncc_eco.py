"""NCC offline do eco (#870). O correlacionador é o do motor, não uma segunda fórmula.

O eco da bancada é uma cópia atrasada da referência: casa por construção.
Aqui o par sintético só prova que a ferramenta e o dump medem a MESMA NCC.
O eco de sala (alto-falante, AEC do Chrome) continua sendo o dump da sessão
do dono, depois do deploy. A flag do duck não entra.
"""
from __future__ import annotations

import importlib.util
import json
import os
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


def test_dump_de_eco_nasce_0700_0600(tmp_path, monkeypatch):
    """Umask 022 não deixa a voz em 0755/0644. O pai que já existia não muda."""
    monkeypatch.setenv("TTS_LIVE_DUMP_DIR", str(tmp_path))
    tmp_path.chmod(0o755)
    antes = os.umask(0o022)
    try:
        sess = {"id": "t"}
        live_eco_dump.anota_mic(sess, b"\x00\x01" * 160)
        live_eco_dump.anota_ref(sess, np.zeros(32, dtype=np.float32))
        eng = _Eng()
        eng._ref_n = 32
        eng._proxima_amostra = 512
        eng._ref_play = 512
        live_eco_dump.marca(sess, eng)
        live_eco_dump.fecha(sess)
        pastas = list(tmp_path.glob("eco-*"))
        assert len(pastas) == 1, pastas
        pasta = pastas[0]
        assert tmp_path.stat().st_mode & 0o777 == 0o755
        assert pasta.stat().st_mode & 0o777 == 0o700
        for nome in ("mic.wav", "ref.f32", "relogio.jsonl", "meta.json"):
            modo = (pasta / nome).stat().st_mode & 0o777
            assert modo == 0o600, (nome, oct(modo))
    finally:
        os.umask(antes)


def test_dump_nao_chmod_pai_que_ja_existia(tmp_path, monkeypatch):
    """Dump em /tmp não pode deixar /tmp em 0700. Elo que nós criamos, sim."""
    pai = tmp_path
    pai.chmod(0o1777)
    alvo = pai / "ainda-nao" / "dump"
    monkeypatch.setenv("TTS_LIVE_DUMP_DIR", str(alvo))
    antes = os.umask(0o022)
    try:
        sess = {"id": "t"}
        live_eco_dump.anota_mic(sess, b"\x00\x01" * 160)
        live_eco_dump.fecha(sess)
        assert pai.stat().st_mode & 0o7777 == 0o1777
        assert (pai / "ainda-nao").stat().st_mode & 0o777 == 0o700
        assert alvo.stat().st_mode & 0o777 == 0o700
        pastas = list(alvo.glob("eco-*"))
        assert len(pastas) == 1
        assert pastas[0].stat().st_mode & 0o777 == 0o700
        assert (pastas[0] / "mic.wav").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(antes)


def test_flag_off_sem_dump_nao_anota_nem_poe_fala_ms(monkeypatch):
    """Flag off e sem TTS_LIVE_DUMP_DIR: sem ring e o `interrupted` é o da main."""
    import app

    monkeypatch.delenv("TTS_LIVE_DUMP_DIR", raising=False)
    monkeypatch.delenv("TTS_LIVE_BARGE_DUCK_RAPIDO", raising=False)

    class Cfg:
        duck_rapido = False

    class Eng:
        def __init__(self) -> None:
            self.config = Cfg()
            self.anotou = 0

        def anota_referencia(self, x) -> None:
            self.anotou += 1

    eng = Eng()
    sess = {"id": "s", "fila": app._LiveFila(1 << 20),
            "cancelado": app.threading.Event(), "turno": 3,
            "engine": eng, "pipe": None, "audio_pendente": 0}
    app._live_anota_ref_envio(sess, eng, b"\x01\x00" * 48)
    assert eng.anotou == 0
    assert "_eco_dump" not in sess
    app._live_cancela(sess, forcar=True, fala_ms=app._live_fala_ms_corte(sess, 480))
    msgs = [item[1] for item in list(sess["fila"]._q.queue)]
    assert msgs == [{"type": "interrupted", "turn": 3}]

    eng.config.duck_rapido = True
    sess2 = {"id": "s", "fila": app._LiveFila(1 << 20),
             "cancelado": app.threading.Event(), "turno": 3,
             "engine": eng, "pipe": None, "audio_pendente": 0}
    app._live_cancela(sess2, forcar=True, fala_ms=app._live_fala_ms_corte(sess2, 480))
    msgs2 = [item[1] for item in list(sess2["fila"]._q.queue)]
    assert msgs2 == [{"type": "interrupted", "turn": 3, "fala_ms": 480}]


def test_dump_ligado_anota_mesmo_com_flag_off(tmp_path, monkeypatch):
    """O 2º deploy grava a referência com a flag off, se o dump estiver ligado."""
    import app

    monkeypatch.setenv("TTS_LIVE_DUMP_DIR", str(tmp_path))

    class Cfg:
        duck_rapido = False

    class Eng:
        def __init__(self) -> None:
            self.config = Cfg()
            self.visto = None

        def anota_referencia(self, x) -> None:
            self.visto = x

    eng = Eng()
    sess = {"id": "s"}
    pcm = np.full(48, 1000, dtype="<i2").tobytes()
    app._live_anota_ref_envio(sess, eng, pcm)
    assert eng.visto is not None and len(eng.visto) > 0
    app._live_eco_fecha(sess)
    pastas = list(tmp_path.glob("eco-*"))
    assert len(pastas) == 1
    assert (pastas[0] / "ref.f32").stat().st_size > 0


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


# ---------------------------------------------------------------------------
# #870: o `captura` do setup tem de sobreviver até o `meta.json`
# ---------------------------------------------------------------------------

def _dump_minimo(tmp_path, monkeypatch, sess):
    monkeypatch.setenv("TTS_LIVE_DUMP_DIR", str(tmp_path))
    live_eco_dump.anota_mic(sess, b"\x00\x01" * 160)
    live_eco_dump.anota_ref(sess, np.zeros(160, dtype=np.float32))
    live_eco_dump.fecha(sess)
    pastas = list(tmp_path.glob("eco-*"))
    assert len(pastas) == 1, pastas
    return json.loads((pastas[0] / "meta.json").read_text(encoding="utf-8"))


def test_captura_do_setup_chega_ao_meta(tmp_path, monkeypatch):
    """`?aec=0` vs AEC ligado têm de ser distinguíveis no dump.

    Sem o campo, dois dumps com capturas opostas ficam idênticos na hora de
    comparar NCC — que é exatamente o que o diagnóstico do #870 separa.
    """
    meta = _dump_minimo(tmp_path, monkeypatch,
                        {"id": "t", "captura": {"aec": False}})
    assert meta["captura"] == {"aec": False}


def test_captura_ausente_vira_none_e_nao_quebra(tmp_path, monkeypatch):
    """Cliente antigo e bancada não mandam a chave: `None`, nunca erro."""
    meta = _dump_minimo(tmp_path, monkeypatch, {"id": "t"})
    assert meta["captura"] is None


def test_captura_ilegivel_vira_none(tmp_path, monkeypatch):
    """`captura` que não é objeto (ou `aec` que não é bool) não vira lixo no meta."""
    meta = _dump_minimo(tmp_path, monkeypatch, {"id": "t", "captura": "aec=0"})
    assert meta["captura"] is None


def test_janela_de_referencia_pausada_fica_fora_da_conta(tmp_path, monkeypatch):
    """`pausa=True` não descreve o eco: a referência não avança com o playback."""
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    for i, linha in enumerate(linhas):
        linha["pausa"] = i % 3 == 0
    r = ncc_eco.medir(eco, ref, linhas)
    assert r["pausado"] > 0
    assert r["medidas"] + r["pausado"] + r["silencio"] + r["busca_curta"] == len(linhas)


def test_eco_puro_e_separado_da_mistura(monkeypatch):
    """O veredito é do ECO PURO: voz por cima derruba a NCC legitimamente.

    Metade das janelas é marcada como voz e tem a NCC forçada a 0,05 (mistura).
    Sem a separação, isso arrastaria o p10 abaixo de 0,8 e o eco — que continua
    separando nas janelas limpas — seria reprovado por contaminação.
    """
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    ordem = {"i": 0}
    original = ncc_eco.ncc_no_instante

    def _ncc(*a, **kw):
        i = ordem["i"]
        ordem["i"] += 1
        ordem["voz"] = i % 2 == 0
        return (0.05, 160) if ordem["voz"] else original(*a, **kw)

    monkeypatch.setattr(ncc_eco, "ncc_no_instante", _ncc)
    monkeypatch.setattr(ncc_eco, "_tem_voz", lambda _j: ordem["voz"])
    r = ncc_eco.medir(eco, ref, linhas)
    assert r["voz_n"] > 0 and r["puro_n"] > 0
    assert r["puro_p10"] >= 0.8          # o eco puro continua separando
    assert r["p10"] < r["puro_p10"]      # a mistura é pior, como se espera
    assert r["eco"] is True


def test_fallback_do_eco_le_as_janelas_indefinidas_nao_o_total(monkeypatch):
    """Sem janela pura, o veredito cai nas INDEFINIDAS — não no total.

    A forma antiga (`if vals_puro else p10`) lia o total, que aqui está
    contaminado por 65 janelas de voz a 0,05. As 10 primeiras são indefinidas
    com NCC alta (~0,998): o veredito tem de sair delas. Receita do gate #1100.
    """
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    chamadas = {"voz": 0, "ncc": 0}

    def _tem_voz(_j):
        chamadas["voz"] += 1
        return None if chamadas["voz"] <= 10 else True

    def _ncc(*_a, **_kw):
        chamadas["ncc"] += 1
        return (0.998, 160) if chamadas["ncc"] <= 10 else (0.05, 160)

    monkeypatch.setattr(ncc_eco, "_tem_voz", _tem_voz)
    monkeypatch.setattr(ncc_eco, "ncc_no_instante", _ncc)
    r = ncc_eco.medir(eco, ref, linhas)
    assert r["indef_n"] == 10
    assert r["puro_n"] == 0
    assert r["puro_p10"] >= 0.8, "o veredito veio do total contaminado, não das indefinidas"
    assert r["eco"] is True
    assert r["p10"] < 0.8, "o total tem de estar contaminado, senão a célula não separa nada"


def test_tudo_indefinido_nao_afirma_mic_com_voz(monkeypatch):
    """Sem Silero não se sabe o que havia no mic — as duas frases não podem sair.

    Antes saíam juntas e a primeira afirmava o que não foi medido: "mic sempre
    com voz" só vale quando a separação rodou (#1101).
    """
    monkeypatch.setattr(ncc_eco, "_tem_voz", lambda _j: None)
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    r = ncc_eco.medir(eco, ref, linhas)
    assert "Silero indisponível" in r["aviso"]
    assert "mic sempre com voz" not in r["aviso"]


def test_sem_janela_pura_o_aviso_aparece(monkeypatch):
    """Microfone sempre com voz: mede-se, mas o relatório diz que é mistura.

    O `_par()` é ruído branco e o Silero real classifica tudo como PURO
    (`voz_n=0`), então a célula nunca entrava no `else` do veredito nem olhava
    o aviso — passava verde pelo motivo errado. O `_tem_voz` é forçado a True
    para a célula exercitar o ramo que o nome dela promete.
    """
    monkeypatch.setattr(ncc_eco, "_tem_voz", lambda _j: True)
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    r = ncc_eco.medir(eco, ref, linhas)
    assert r["voz_n"] > 0
    assert r["puro_n"] == 0
    assert "sem janela de eco puro" in r["aviso"]
    # sem puro o veredito cai no total, e o total TEM de ser o p10 de tudo
    assert r["eco"] == (r["p10"] >= 0.8)


def test_silero_indisponivel_avisa_e_nao_vira_puro(monkeypatch):
    """Sem Silero a separação some — e o relatório NÃO pode dizer `puro` cheio.

    Antes `_tem_voz` devolvia `False` na exceção, então o mesmo relatório saía
    idêntico ao de "ninguém falou no mic": mentira do mesmo tipo que o
    `captura` ausente fazia no `meta.json` (#1090).
    """
    monkeypatch.setattr(ncc_eco, "_tem_voz", lambda _j: None)
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    r = ncc_eco.medir(eco, ref, linhas)
    assert r["puro_n"] == 0, "janela indefinida não é evidência de eco puro"
    assert r["indef_n"] == r["medidas"]
    assert "Silero indisponível" in r["aviso"]
    # o veredito continua saindo, das janelas indefinidas
    assert r["puro_p10"] is not None


def test_tem_voz_devolve_none_quando_o_silero_estoura(monkeypatch):
    """O caminho REAL da exceção, não só o `None` injetado.

    As outras duas células trocam `_tem_voz` inteiro, então não veem se o
    `except` voltar a devolver `False` — foi o que a primeira mordida mostrou.
    Aqui quem estoura é o `scorer_silero`.
    """
    def _explode(_janela):
        raise RuntimeError("sem onnxruntime")

    monkeypatch.setattr(lt, "scorer_silero", _explode)
    assert ncc_eco._tem_voz(np.zeros(512, dtype=np.float32)) is None
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    r = ncc_eco.medir(eco, ref, linhas)
    assert r["indef_n"] == r["medidas"] > 0
    assert "Silero indisponível" in r["aviso"]
    # a linha tem de sair impressa: `aviso` some de quem corta a saída
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ncc_eco.imprime(r)
    assert "indef n=" in buf.getvalue()


def test_degradacao_parcial_diz_quantas_janelas(monkeypatch):
    """Queda em ALGUMAS janelas: o aviso diz quantas, não vira silêncio."""
    chamadas = {"n": 0}

    def _as_vezes(_j):
        chamadas["n"] += 1
        return None if chamadas["n"] <= 3 else False

    monkeypatch.setattr(ncc_eco, "_tem_voz", _as_vezes)
    ref, eco = _par()
    linhas = ncc_eco.relogio_pelo_indice(len(eco), len(ref))
    r = ncc_eco.medir(eco, ref, linhas)
    assert r["indef_n"] == 3
    assert "Silero indisponível em 3 janela(s)" in r["aviso"]
    # com puro de verdade presente, o veredito é dele, não das indefinidas
    assert r["puro_n"] > 0
