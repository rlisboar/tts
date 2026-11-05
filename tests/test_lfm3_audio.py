"""LFM3-Audio entra no catálogo e a geração lê o áudio do resultado STS."""

from types import SimpleNamespace

import numpy as np

from backends import generate_with_backend, resolve_backend


def test_atalho_aponta_para_o_repo_mlx():
    be = resolve_backend("lfm3-audio")
    assert be["family"] == "lfm3_audio"
    assert be["meta"]["repo"] == "LiquidAI/LFM3-Audio-2B-MLX-8bit"
    assert be["is_shortcut"] is True
    assert any(c["setting"] == "gen_lfm3_voice" for c in be["controls"])


def test_id_livre_com_lfm3_no_nome_cai_na_familia():
    be = resolve_backend("LiquidAI/LFM3-Audio-2B-MLX-8bit")
    assert be["family"] == "lfm3_audio"
    assert be["is_shortcut"] is False


class _Modelo:
    def __init__(self):
        self.kwargs = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(audio=np.array([0.1, -0.2], dtype=np.float32),
                               sample_rate=24000)


def test_gerar_pede_tts_e_devolve_o_audio():
    modelo = _Modelo()
    audio = generate_with_backend(
        modelo, "lfm3_audio", "olá", language="pt",
        omni={"lfm3_voice": "male", "max_tokens": 128},
    )
    assert modelo.kwargs["task"] == "tts"
    assert modelo.kwargs["voice"] == "male"
    assert modelo.kwargs["language"] == "pt"
    assert modelo.kwargs["text"] == "olá"
    assert audio.dtype == np.float32
    assert audio.shape == (2,)
    assert modelo.sample_rate == 24000


def test_voz_fora_da_lista_cai_em_female():
    modelo = _Modelo()
    generate_with_backend(
        modelo, "lfm3_audio", "oi", omni={"lfm3_voice": "robot"},
    )
    assert modelo.kwargs["voice"] == "female"


# --- #849: revisão obrigatória e `trust_remote_code` só no caminho do LFM3 ----
def test_entrada_do_catalogo_tem_revisao_fixa():
    from backends import BACKENDS

    meta = BACKENDS["lfm3-audio"]
    assert meta.get("revision") == "b44c493832ce3435ec3c97fe1fa5fb6e7f3e687f"


def test_loader_exige_revisao_e_repassa(monkeypatch):
    import backends

    vistos = {}

    class _Sts:
        @staticmethod
        def load(path, **kw):
            vistos.update(path=path, kw=kw)
            return SimpleNamespace(sample_rate=24000)

    import sys as _sys

    monkeypatch.setitem(_sys.modules, "mlx_audio.sts", _Sts)
    backends.load_tts_model("LiquidAI/LFM3-Audio-2B-MLX-8bit", "lfm3_audio",
                            "abc123")
    assert vistos["kw"]["revision"] == "abc123"
    # sem revisão: falha alto, não cai no HEAD
    try:
        backends.load_tts_model("LiquidAI/LFM3-Audio-2B-MLX-8bit", "lfm3_audio")
    except RuntimeError as exc:
        assert "revision" in str(exc)
    else:                                     # pragma: no cover
        raise AssertionError("sem revision deveria levantar")


def test_trust_remote_code_so_no_caminho_do_lfm3(monkeypatch):
    """O loader de TTS do mlx_audio NÃO pode passar `revision` por engano."""
    import backends

    chamado = {}

    class _Tts:
        @staticmethod
        def load_model(path, **kw):
            chamado.update(path=path, kw=kw)
            return SimpleNamespace(sample_rate=24000)

    import sys as _sys

    monkeypatch.setitem(_sys.modules, "mlx_audio.tts.utils", _Tts)
    backends.load_tts_model("algum/repo", "omnivoice", "sha-que-nao-importa")
    assert chamado["kw"] == {}                # loader de TTS segue com um argumento


def test_use_so_alcanca_modelo_com_revisao(monkeypatch):
    """Invariante do #849: com `revision` o id serve para `use`; sem ela, não."""
    import app

    assert app._modelo_no_catalogo("lfm3") is True
    # entrada sem revisão deixa de valer para `use`
    monkeypatch.setitem(app._CATALOGO_BACKENDS["lfm3-audio"], "revision", "")
    assert app._modelo_no_catalogo("lfm3") is False
    # e `admin_only` também barra
    monkeypatch.setitem(app._CATALOGO_BACKENDS["lfm3-audio"], "revision", "abc")
    monkeypatch.setitem(app._CATALOGO_BACKENDS["lfm3-audio"], "admin_only", True)
    assert app._modelo_no_catalogo("lfm3") is False
