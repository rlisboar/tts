"""#946: campo admin de `live_clausula_palavras` no painel do Live.

O servidor (#942) aceita o inteiro 1–40 (default 6) e o aplica no próximo turno.
A célula é estática de propósito: exercitar o Live de ponta a ponta custaria
modelo e minutos, e o que se cobra aqui é a FIAÇÃO do controle — marcador de
admin, limites, leitura no load e POST com clamp.
"""
from pathlib import Path

import app

pytest_plugins = ["test_api"]

from test_api import auth, chave_de_uso, client  # noqa: E402, F401

# `# noqa` é do flake8 e o hook roda pyflakes (que o ignora — #200): os nomes viram
# fixture de teste pelo `pytest_plugins`, então o marcador que cala o F401/F811 é
# `__all__`.
__all__ = ["auth", "chave_de_uso", "client"]

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


def test_campo_existe_com_marcador_de_admin_e_limites():
    fonte = INDEX.read_text()
    assert 'id="lxClausula"' in fonte, "o campo da 1ª cláusula sumiu do painel do Live"
    assert 'data-admin-setting="live_clausula_palavras"' in fonte, \
        "o campo perdeu o marcador de admin"
    # os limites do servidor (1–40) têm de estar no próprio input
    trecho = fonte.split('id="lxClausula"', 1)[1].split(">", 1)[0]
    assert 'min="1"' in trecho and 'max="40"' in trecho, f"limites ausentes: {trecho!r}"


def test_load_le_o_setting_e_nao_sobrescreve_escolha_da_tela():
    fonte = INDEX.read_text()
    assert 's.live_clausula_palavras ?? 6' in fonte, "o load não traz o valor do servidor"
    assert "!LX.clausulaSuja" in fonte, "o load pode desfazer a escolha feita na tela"


def test_handler_limita_e_posta_o_campo():
    fonte = INDEX.read_text()
    assert "function lxDefineClausula(" in fonte, "o handler do campo sumiu"
    assert "Math.min(40, Math.max(1" in fonte, "o handler não limita a 1–40"
    assert "live_clausula_palavras: n" in fonte, "o handler não posta o campo"


def test_setting_e_admin_no_servidor():
    """O marcador da tela só vale se o servidor tratar o campo como admin."""
    app = (Path(__file__).resolve().parent.parent / "app.py").read_text()
    bloco = app.split("_SETTINGS_ADMIN = frozenset({", 1)[1].split("})", 1)[0]
    assert '"live_clausula_palavras"' in bloco, \
        "o campo saiu de _SETTINGS_ADMIN — chave `use` volta a poder mudá-lo"


def test_chave_use_nao_muda_e_admin_muda(client, auth, chave_de_uso):
    """O ajuste é GLOBAL: chave `use` não decide, admin decide (igual ao #791).

    Sonda de efeito, não de string: o POST da chave de uso volta 200 com o nome
    em `admin_ignored` e o valor guardado NÃO muda; o da admin grava.
    """
    antes = app._settings.get("live_clausula_palavras")
    assert antes == 6, f"default esperado 6, veio {antes!r}"
    r = client.post("/api/settings", headers=chave_de_uso,
                    json={"live_clausula_palavras": 40})
    assert r.status_code == 200, r.text
    assert "live_clausula_palavras" in r.json()["admin_ignored"], r.json()
    assert app._settings.get("live_clausula_palavras") == 6, \
        "chave de uso mudou o ajuste global"
    r = client.post("/api/settings", headers=auth, json={"live_clausula_palavras": 40})
    assert r.status_code == 200, r.text
    assert app._settings.get("live_clausula_palavras") == 40, "chave admin não gravou"
