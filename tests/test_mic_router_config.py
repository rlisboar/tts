"""`client/mic_router.py`: o `config.json` do cliente guarda a CHAVE da API.

POR QUE ESTE ARQUIVO EXISTE (#348, achado #15 da auditoria #286): `save_config`
gravava com `write_text` e o umask da máquina (0644 numa padrão), deixando a chave
em claro legível para qualquer conta do Mac. O arquivo também nasce do zip, cujo
modo é 0600 — o `save_config` não podia rebaixar isso em silêncio.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not all(importlib.util.find_spec(m) for m in ("tkinter", "sounddevice", "numpy")),
    reason="cliente depende de tkinter/sounddevice")


@pytest.fixture
def mod(tmp_path, monkeypatch):
    """Módulo com o config apontando para um tmp (não toca o config do dono)."""
    sys.path.insert(0, str(BASE))
    sys.modules.pop("mic_router", None)
    spec = importlib.util.spec_from_file_location("mic_router", BASE / "client" / "mic_router.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["mic_router"] = m
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "CFG_PATH", tmp_path / "config.json")
    return m


def test_save_config_grava_0600(mod):
    mod.CFG.update(url="http://192.168.15.34:7860", key="chave-secreta")
    mod.save_config()
    alvo = mod.CFG_PATH
    assert alvo.is_file()
    assert (alvo.stat().st_mode & 0o777) == 0o600, "config.json com a chave ficou legível"
    assert "chave-secreta" in alvo.read_text()


def test_save_config_sobre_arquivo_0644_existente_reaperta(mod):
    """Arquivo que já existe 0644 (vindo de zip antigo/manual) tem de voltar a 0600."""
    mod.CFG_PATH.write_text("{}")
    mod.CFG_PATH.chmod(0o644)
    mod.save_config()
    assert (mod.CFG_PATH.stat().st_mode & 0o777) == 0o600


def test_url_publica_exige_tls_e_rede_local_aceita_http(mod):
    """#794: em `http` o Bearer e o WAV de referência viajam em claro.

    Host público tem de ser `https`. Loopback e rede local seguem `http` — é o que
    o zip entrega (IP da LAN) e o caso de servidor na mesma máquina.
    """
    assert mod._normaliza_url("http://127.0.0.1:7860") == "http://127.0.0.1:7860"
    assert mod._normaliza_url("http://[::1]:7860") == "http://[::1]:7860"
    assert mod._normaliza_url("http://192.168.15.48:7860") == "http://192.168.15.48:7860"
    assert mod._normaliza_url("http://172.20.1.1:7860") == "http://172.20.1.1:7860"
    assert mod._normaliza_url("http://mac.local:7860") == "http://mac.local:7860"
    assert mod._normaliza_url("https://tts.exemplo.com") == "https://tts.exemplo.com"

    with pytest.raises(ValueError) as exc:
        mod._normaliza_url("http://8.8.8.8:7860")
    assert "https://" in str(exc.value), "a recusa não diz o que fazer"
    with pytest.raises(ValueError):
        mod._normaliza_url("http://tts.exemplo.com")
    # esquema desconhecido também para
    with pytest.raises(ValueError):
        mod._normaliza_url("ftp://127.0.0.1")


def test_url_publica_recusa_na_carga_antes_de_qualquer_pedido(mod, monkeypatch):
    """A recusa é CEDO: `_load_config` já valida (não cai para http depois)."""
    monkeypatch.setenv("TTS_ROD_URL", "http://tts.exemplo.com:7860")
    with pytest.raises(ValueError):
        mod._load_config()
    monkeypatch.setenv("TTS_ROD_URL", "https://tts.exemplo.com:7860")
    assert mod._load_config()["url"] == "https://tts.exemplo.com:7860"
