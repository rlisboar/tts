"""#891: o large-v3 da transcrição de arquivo sai por TTL; o turbo do Live fica.

O idle geral olha `_last_use["stt"]`, e o Live renova esse relógio o tempo todo.
Sem um relógio só do arquivo, o pesado ficava residente no mini enquanto o turno
usava o turbo. Nada de modelo real: o holder é falso e o relógio é injetado.
"""
from __future__ import annotations

import sys
import types
import weakref

import numpy as np

import app

LARGE = "mlx-community/whisper-large-v3-mlx"


def _holder_falso(monkeypatch):
    """ModelHolder de mentira no lugar do mlx_whisper (não carrega peso)."""
    class Holder:
        model = None
        model_path = None

    sub = types.ModuleType("mlx_whisper.transcribe")
    sub.ModelHolder = Holder
    mod = types.ModuleType("mlx_whisper")

    def transcribe(audio, path_or_hf_repo=None, **kw):
        Holder.model = object()
        Holder.model_path = path_or_hf_repo
        return {"text": "ok", "language": "pt", "segments": []}

    mod.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", mod)
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", sub)
    monkeypatch.setattr(app, "_release_mlx_memory", lambda aggressive=False: None)
    return Holder


def _relogio(monkeypatch, t0: float = 1000.0):
    agora = [t0]
    monkeypatch.setattr(app.time, "monotonic", lambda: agora[0])
    return agora


def test_large_v3_ocioso_sai_e_o_turbo_do_live_fica(monkeypatch):
    """Passou o TTL, o large-v3 sai (objeto morto). O turbo do Live não sai."""
    Holder = _holder_falso(monkeypatch)
    agora = _relogio(monkeypatch)
    monkeypatch.setitem(app._settings, "whisper_arquivo_ttl_s", 600)
    monkeypatch.setitem(app._settings, "live_stt_whisper_repo", "")
    app._whisper_arquivo_uso = agora[0]

    class _Peso:
        pass

    pesado = _Peso()
    Holder.model = pesado
    Holder.model_path = LARGE
    fraco = weakref.ref(pesado)
    del pesado

    assert app._whisper_arquivo_passo() is False
    assert Holder.model is not None, "dentro do TTL o large-v3 não pode sair"

    agora[0] += 601.0
    assert app._whisper_arquivo_passo() is True
    assert Holder.model is None and Holder.model_path is None
    assert fraco() is None, "a referência sumiu mas o modelo CONTINUA vivo"

    # o turbo do Live, mesmo com o relógio do arquivo vencido, fica
    turbo = object()
    Holder.model = turbo
    Holder.model_path = app.LIVE_WHISPER_PADRAO
    assert app._whisper_arquivo_passo() is False
    assert Holder.model is turbo

    # 0 desliga: o large-v3 vencido fica
    Holder.model = object()
    Holder.model_path = LARGE
    monkeypatch.setitem(app._settings, "whisper_arquivo_ttl_s", 0)
    assert app._whisper_arquivo_passo() is False
    assert Holder.model_path == LARGE
    app._whisper_arquivo_uso = 0.0


def test_ttl_curto_solta_antes_do_padrao(monkeypatch):
    """A chave muda o prazo: 30 s solta, o padrão de 600 s ainda segura."""
    Holder = _holder_falso(monkeypatch)
    agora = _relogio(monkeypatch)
    monkeypatch.setitem(app._settings, "live_stt_whisper_repo", "")
    app._whisper_arquivo_uso = agora[0]
    Holder.model = object()
    Holder.model_path = LARGE

    monkeypatch.setitem(app._settings, "whisper_arquivo_ttl_s", 600)
    agora[0] += 31.0
    assert app._whisper_arquivo_passo() is False

    monkeypatch.setitem(app._settings, "whisper_arquivo_ttl_s", 30)
    assert app._whisper_arquivo_passo() is True
    assert Holder.model is None
    app._whisper_arquivo_uso = 0.0


def test_arquivo_marca_o_relogio_e_o_live_nao(monkeypatch, tmp_path):
    """Transcrever arquivo arma o TTL. Um turno do Live não renova o large-v3."""
    Holder = _holder_falso(monkeypatch)
    agora = _relogio(monkeypatch)
    monkeypatch.setitem(app._settings, "stt_whisper_repo", LARGE)
    monkeypatch.setitem(app._settings, "live_stt_whisper_repo", "")
    monkeypatch.setitem(app._settings, "stt_local_engine", "whisper")
    monkeypatch.setattr(app, "_vad_tem_fala", lambda p, minimo_s=0.3: True)
    monkeypatch.setattr(app, "_wav_to_mono16k", lambda p: np.zeros(16000, dtype=np.float32))
    app._whisper_arquivo_uso = 0.0

    app._transcribe(tmp_path / "a.wav", language="pt")
    assert app._whisper_arquivo_uso == 1000.0
    assert Holder.model_path == LARGE

    agora[0] = 1500.0
    with app._stt_do_live():
        app._transcribe(tmp_path / "a.wav", language="pt")
    assert app._whisper_arquivo_uso == 1000.0, "o Live renovou o relógio do large-v3"
    assert Holder.model_path == app.LIVE_WHISPER_PADRAO
    # vencido o prazo, o que está carregado é o turbo: não sai
    monkeypatch.setitem(app._settings, "whisper_arquivo_ttl_s", 600)
    agora[0] = 1000.0 + 601.0
    assert app._whisper_arquivo_passo() is False
    assert Holder.model_path == app.LIVE_WHISPER_PADRAO
    app._whisper_arquivo_uso = 0.0


def test_ttl_e_configuravel_e_nao_e_admin():
    """Política de RAM, como o idle_unload: chave de uso, 0–86400, padrão 600."""
    assert app._SETTINGS_DEFAULTS["whisper_arquivo_ttl_s"] == 600
    assert "whisper_arquivo_ttl_s" not in app._SETTINGS_ADMIN
    antes = app._settings["whisper_arquivo_ttl_s"]
    try:
        app._apply_settings({"whisper_arquivo_ttl_s": 999999})
        assert app._settings["whisper_arquivo_ttl_s"] == 86400
        app._apply_settings({"whisper_arquivo_ttl_s": -4})
        assert app._settings["whisper_arquivo_ttl_s"] == 0
        app._apply_settings({"whisper_arquivo_ttl_s": "nao"})
        assert app._settings["whisper_arquivo_ttl_s"] == 600
    finally:
        app._settings["whisper_arquivo_ttl_s"] = antes
        app._save_settings()


class _BloqueiaMlxWhisper:
    """Barra o pacote real se alguém importar o whisper só para olhar."""

    def __init__(self):
        self.vistos = []

    def find_spec(self, nome, path, target=None):
        if nome == "mlx_whisper" or nome.startswith("mlx_whisper."):
            self.vistos.append(nome)
            raise AssertionError(nome)
        return None


def test_passo_nao_importa_o_whisper_so_para_olhar(monkeypatch):
    """TTL 600 e pacote ausente: o passo e o unload não puxam o mlx_whisper (#957).

    O laço de 30 s chama o passo. O import em voo reescrevia o dublê de
    test_stt_local_engine_dispatch e a célula lia "E aí"."""
    bloqueio = _BloqueiaMlxWhisper()
    monkeypatch.setitem(app._settings, "whisper_arquivo_ttl_s", 600)
    monkeypatch.delitem(sys.modules, "mlx_whisper", raising=False)
    monkeypatch.delitem(sys.modules, "mlx_whisper.transcribe", raising=False)
    monkeypatch.setattr(app, "_release_mlx_memory", lambda aggressive=False: None)
    if "torch" not in sys.modules:
        monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    sys.meta_path.insert(0, bloqueio)
    try:
        assert app._whisper_arquivo_passo() is False
        corpo = app._unload_local_models(tts=False, stt=True, mt=False, ser=False)
        assert "whisper" not in corpo["unloaded"]
        assert bloqueio.vistos == []
        assert "mlx_whisper" not in sys.modules
        assert "mlx_whisper.transcribe" not in sys.modules
    finally:
        sys.meta_path.remove(bloqueio)
