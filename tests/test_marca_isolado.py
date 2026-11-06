"""Marca d'água nas famílias ISOLADAS (#797) e fora do `_gen_lock` (#915).

O filho grava o trecho cru: quem marca é o pai, depois de soltar a trava da
geração. A célula que morde é `test_marca_nao_segura_o_gen_lock` — se a
chamada de `marca_wav` voltar para dentro do `with`, ela reprova.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import app  # noqa: E402
import marca_dagua  # noqa: E402
import tts_worker  # noqa: E402


def _fala() -> np.ndarray:
    t = np.arange(16000) / 16000
    x = np.sin(2 * np.pi * 190 * t) + 0.4 * np.sin(2 * np.pi * 520 * t)
    return (x / np.abs(x).max() * 0.25).astype(np.float32)


def _roda_worker(tmp_path, monkeypatch, familia: str) -> Path:
    """Roda o worker de VERDADE com a geração dublada: prova o laço, não o helper."""
    pdir, outdir = tmp_path / "pecas", tmp_path / "saida"
    pdir.mkdir(), outdir.mkdir()
    cfg = {
        "job_id": f"j-{familia}", "text": "uma frase curta", "voice_id": "",
        "voice_path": "", "language": "pt", "omni": {},
        "model": "kokoro", "marca": True,
        "settings": {"chunk_max_chars": 140},
        "piece_dir": str(pdir), "outputs_dir": str(outdir),
        "voices_dir": str(tmp_path), "base_dir": str(RAIZ),
        "status_path": str(pdir / "status.json"),
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))

    def _gera(*a, **k):
        return _fala()

    monkeypatch.setattr(tts_worker, "generate_with_backend", _gera)
    class _Estado(dict):
        """Estado do modelo dublado: o que o laço pedir e não existir vem vazio."""

        def __missing__(self, chave):
            return ""

    estado = _Estado({"sr": 16000, "family": familia, "model": None, "conds": None,
                      "omni": {}, "voice_id": "", "be_meta": {},
                      "settings": {}})
    monkeypatch.setattr(tts_worker, "_carrega", lambda cfg: estado)
    monkeypatch.setattr(tts_worker, "resolve_backend",
                        lambda *a, **k: {"id": familia, "family": familia, "path": "x",
                                         "is_shortcut": False, "meta": {}})
    monkeypatch.setattr(tts_worker, "normaliza_loudness", lambda *a, **k: None)
    monkeypatch.setattr(tts_worker, "apply_audio_fx", lambda a, *x, **k: a)
    monkeypatch.setattr(tts_worker, "trim_tail_silence", lambda a, *x, **k: a)
    monkeypatch.setattr(tts_worker, "fade_edges", lambda a, *x, **k: a)
    monkeypatch.setattr(tts_worker, "resolve_omni_source", lambda *a, **k: ("", ""))
    monkeypatch.setattr(tts_worker, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["tts_worker.py", str(cfg_path)])
    tts_worker.main()
    return sorted(pdir.glob("*.wav"))[0]


@pytest.mark.parametrize("familia", sorted(app._ISOLATED_FAMILIES))
def test_filho_isolado_nao_marca(familia, tmp_path, monkeypatch):
    """O cfg pede marca e o filho não chama: marcar aqui segurava o `_gen_lock` do pai."""
    chamadas = []
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "marca_wav",
                        lambda p: chamadas.append(Path(p).name) or p)

    _roda_worker(tmp_path, monkeypatch, familia)

    assert chamadas == [], f"{familia}: o filho marcou {chamadas}"


def test_trecho_cru_nao_acusa_marca(tmp_path):
    """O negativo: sem a marca, o detector não pode achar nada."""
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    peca = tmp_path / "cru.wav"
    sf.write(str(peca), _fala(), 16000, subtype="PCM_16")
    prob = marca_dagua.detecta_prob(peca)
    assert prob is not None and prob < 0.5, f"cru acusou marca ({prob})"


def test_marca_nao_segura_o_gen_lock(monkeypatch, tmp_path):
    """`marca_wav` roda sem o `_gen_lock` nos três caminhos de produção (#915).

    Morde de três jeitos: a trava estava tomada e a chamada a viu solta; o
    trecho só publica depois da marca; o filho isolado não marca enquanto
    ainda está no Metal (o pai marca trecho e wav final depois do `with`).
    """
    from test_app import _TEXTO_1_TRECHO, _job_de_render

    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)
    monkeypatch.setattr(marca_dagua, "disponivel", lambda: True)
    monkeypatch.setattr(marca_dagua, "motivo", lambda: "")

    # --- Live: a produção não segura a trava; se um dia segurar, solta aqui ---
    live = []

    def _espia_live(caminho, **_kw):
        live.append(app._gen_lock._is_owned())
        return caminho

    monkeypatch.setattr(marca_dagua, "marca_wav", _espia_live)
    pcm = b"\x00\x01" * 800
    sess = {"id": "s-marca", "fila": app._LiveFila(), "engine": None, "audio_pendente": 0}
    app._live_envia_audio(sess, pcm)
    app._gen_lock.acquire()
    try:
        app._live_envia_audio(
            {"id": "s-marca-2", "fila": app._LiveFila(), "engine": None, "audio_pendente": 0},
            pcm)
        assert app._gen_lock._is_owned(), "a trava não voltou depois da marca do Live"
    finally:
        if app._gen_lock._is_owned():
            app._gen_lock.release()
    assert live == [False, False], f"Live marcou com o _gen_lock: {live}"

    # --- job local in-process: gera COM a trava, marca SEM, publica depois ---
    vistas = []
    gerou = []

    def _espia_job(caminho):
        vistas.append((Path(caminho).name, app._gen_lock._is_owned(),
                       app._jobs[jid].get("pieces")))
        return caminho

    def _gerador(*_a, **_k):
        gerou.append(app._gen_lock._is_owned())
        t = np.arange(24000) / 24000.0
        return (0.2 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)

    monkeypatch.setattr(marca_dagua, "marca_wav", _espia_job)
    jid = _job_de_render(monkeypatch, tmp_path, _gerador, texto=_TEXTO_1_TRECHO)
    app._run_tts_job(jid, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    assert gerou and all(gerou), "a geração local saiu do _gen_lock"
    assert vistas, "o job local não marcou"
    assert all(not dono for _, dono, _ in vistas), f"marcou com a trava: {vistas}"
    assert all(pecas in (0, None) for _, _, pecas in vistas), (
        f"publicou o trecho antes da marca: {vistas}")
    assert app._jobs[jid]["status"] == "done"
    assert app._jobs[jid]["pieces"] >= 1
    assert not app._gen_lock._is_owned(), "a trava ficou presa depois do job"

    # --- isolado: filho no Metal sem marca; pai marca os dois wavs fora ---
    marcas = []
    cfg_visto = {}
    saida = tmp_path / "saida-iso"
    saida.mkdir()
    monkeypatch.setattr(app, "OUTPUTS_DIR", saida)
    monkeypatch.setattr(app, "_piece_dir", lambda job_id: tmp_path / job_id)
    monkeypatch.setattr(app, "_unload_local_tts", lambda *_a, **_k: None)
    monkeypatch.setattr(app, "_jobs_hist_salva", lambda *_a, **_k: None)
    monkeypatch.setattr(app, "_jobs", {})
    app._jobs["jiso"] = {"status": "queued", "pieces": 0}

    class _Proc:
        pid = 4242
        returncode = None
        acabou = False

        def poll(self):
            if self.acabou:
                self.returncode = 0
                return 0
            return None

        def wait(self, timeout=None):
            self.acabou = True
            self.returncode = 0
            return 0

        def terminate(self):
            self.acabou = True

        def kill(self):
            self.acabou = True

    proc = _Proc()
    pronto = {"json": ""}

    def _espia_iso(caminho):
        marcas.append((Path(caminho).name, app._gen_lock._is_owned(),
                       app._jobs["jiso"].get("pieces")))
        return caminho

    def _popen(argv, **_kw):
        cfg = json.loads(Path(argv[2]).read_text())
        cfg_visto.update(cfg)
        pdir = Path(cfg["piece_dir"])
        sf.write(pdir / "0.wav", _fala(), 16000, subtype="PCM_16")
        sf.write(saida / "out1.wav", _fala(), 16000, subtype="PCM_16")
        (pdir / "status.json").write_text(json.dumps({
            "status": "running", "pieces": 1, "total": 1,
            "progress": {"stage": "gerando"},
        }))
        pronto["json"] = json.dumps({
            "status": "done", "pieces": 1, "total": 1, "progress": None,
            "output": {"id": "out1", "duration": 1.0}, "error": None,
        })
        return proc

    giros = {"n": 0}
    dormir = time.sleep
    dono = threading.current_thread()

    def _sleep(s):
        # o watchdog de /health também dorme em `time.sleep`; só o job interessa
        if threading.current_thread() is not dono:
            return dormir(s)
        giros["n"] += 1
        assert not marcas, f"marcou com o filho ainda no Metal: {marcas}"
        assert app._jobs["jiso"].get("pieces") in (0, None)
        assert app._gen_lock._is_owned(), "o poll do filho soltou o _gen_lock"
        (tmp_path / "jiso" / "status.json").write_text(pronto["json"])
        proc.acabou = True

    monkeypatch.setattr(marca_dagua, "marca_wav", _espia_iso)
    monkeypatch.setattr(app.subprocess, "Popen", _popen)
    monkeypatch.setattr(time, "sleep", _sleep)
    try:
        app._run_tts_job_isolated(
            "jiso", "oi", "voz", tmp_path / "voz.wav", "pt", {},
            {"id": "kokoro", "family": "kokoro", "meta": {}})
    finally:
        monkeypatch.setattr(time, "sleep", dormir)

    assert "marca" not in cfg_visto, "o cfg ainda manda o filho marcar"
    assert giros["n"] >= 1, "o filho não chegou a rodar sob a trava"
    assert {nome for nome, _, _ in marcas} == {"0.wav", "out1.wav"}
    assert all(not dono for _, dono, _ in marcas), f"isolado marcou com a trava: {marcas}"
    assert all(pecas in (0, None) for _, _, pecas in marcas), (
        f"publicou trecho cru: {marcas}")
    assert app._jobs["jiso"]["status"] == "done"
    assert app._jobs["jiso"]["pieces"] == 1
    assert not app._gen_lock._is_owned()


def _pcm_que_o_cliente_ouve(pcm: bytes) -> bytes:
    """O PCM que sai na fila do Live — é o que o cliente toca, a 24 kHz."""
    sess = {"id": "s-908", "fila": app._LiveFila(), "engine": None,
            "audio_pendente": 0}
    app._live_envia_audio(sess, pcm, marcar=True)
    itens = []
    while not sess["fila"].empty():
        itens.append(sess["fila"].get())
    audios = [c[1] for c in itens if c[0] == "audio"]
    assert len(audios) == 1, audios
    return audios[0]


def _playback_16k(pcm: bytes) -> np.ndarray:
    """Grade de 16 kHz do PCM tocado a 24 kHz (resample 2/3, contrato do AudioSeal)."""
    from scipy.signal import resample_poly

    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return np.asarray(resample_poly(x, 2, 3), dtype=np.float32)


def test_pcm_final_do_live_detecta_no_audio_que_o_cliente_ouve(tmp_path, monkeypatch):
    """#908: o cliente toca 24 kHz. A marca tem de aparecer depois de trazer
    esse PCM de volta a 16 kHz. O defeito gravava as samples de 24 kHz com
    cabeçalho 16 kHz: a conferência dava 1,0 e o playback dava 0,0."""
    if not marca_dagua.disponivel():
        pytest.skip(f"marca indisponível: {marca_dagua.motivo()}")

    monkeypatch.setitem(app._settings, "tts_marca_dagua", True)
    monkeypatch.setitem(app._settings, "live_marca_primeiro_trecho", True)
    fala, sr = sf.read(RAIZ / "tests/fixtures/voz_fala_5s_16k.wav", dtype="float32")
    assert sr == 16000
    from scipy.signal import resample_poly

    x24 = np.asarray(resample_poly(fala[: int(1.8 * 16000)], 3, 2), dtype=np.float32)
    pcm = np.clip(np.round(x24 * 32767.0), -32768, 32767).astype("<i2").tobytes()
    out = _pcm_que_o_cliente_ouve(pcm)
    assert len(out) == len(pcm), "o cliente segue recebendo 24 kHz do mesmo tamanho"
    assert out != pcm, "a marca não alterou o PCM"

    def prob(samples: np.ndarray) -> float:
        caminho = tmp_path / "ouve.wav"
        sf.write(str(caminho), np.asarray(samples, dtype=np.float32), 16000,
                 subtype="PCM_16")
        achado = marca_dagua.detecta_prob(caminho)
        assert achado is not None, "detector não respondeu"
        return achado

    marcado = prob(_playback_16k(out))
    cru = prob(_playback_16k(pcm))
    assert marcado >= 0.9, f"playback do Live sem marca legível ({marcado})"
    assert cru < 0.5, f"PCM cru acusou marca ({cru})"
