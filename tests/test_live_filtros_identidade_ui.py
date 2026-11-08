"""#528: filtros de identidade/marca d'água e etapas da limpeza na UI.

A célula é estática de propósito (precedente do #946): o que se cobra aqui é a
FIAÇÃO do controle — elemento presente, defaults DESLIGADOS (contrato do #527),
limites do servidor no próprio input e classificação admin das etapas globais.
O efeito no motor já tem célula no gate do #527.

O passo de EFEITO que só a UI pode errar é o do servidor: `voice_limpar_musica`
e `voice_limpar_dereverb` são ajustes GLOBAIS (mudam o áudio de toda limpeza) —
uma chave `use` não pode trocá-los em silêncio.
"""
from pathlib import Path

import app

pytest_plugins = ["test_api"]

from test_api import auth, chave_de_uso, client  # noqa: E402, F401

RAIZ = Path(__file__).resolve().parent.parent
INDEX = RAIZ / "static" / "index.html"


def _fonte():
    return INDEX.read_text()


def test_toggles_existem_e_nascem_desligados():
    fonte = _fonte()
    assert 'id="lxLiga_identidade"' in fonte, "o toggle de identidade sumiu do painel"
    assert 'id="lxLiga_marca_eco"' in fonte, "o toggle da marca d'água sumiu do painel"
    padrao = fonte.split("const LX_FILTROS_PADRAO = {", 1)[1].split("};", 1)[0]
    assert "identidade: { on: false" in padrao, "identidade deixou de nascer DESLIGADA"
    assert "marca_eco: { on: false }" in padrao, "marca_eco deixou de nascer DESLIGADA"


def test_identidade_tem_perfis_e_rigor_com_os_limites_do_servidor():
    fonte = _fonte()
    assert 'id="lxPerfis_identidade"' in fonte, "a lista de perfis sumiu"
    trecho = fonte.split('id="lxPerfis_identidade"', 1)[1].split(">", 1)[0]
    assert "multiple" in trecho, "a lista de perfis deixou de ser múltipla"
    assert 'id="lxFar_identidade"' in fonte, "o seletor de rigor sumiu"
    for valor in ('value="0.01"', 'value="0.001"'):
        assert valor in fonte, f"o rigor perdeu a opção {valor}"
    # o parser tem de respeitar o MESMO intervalo do `_normaliza_filtro_barge`
    assert "lxClampFloat(v && v.far, 0.001, 0.2" in fonte, \
        "o parser não limita o far a 0.001–0.2"
    assert ".slice(0, 8)" in fonte.split("if (padrao.perfis)", 1)[1].split("}", 1)[0], \
        "o parser não respeita o teto de 8 perfis do servidor"


def test_handlers_postam_perfis_e_far():
    fonte = _fonte()
    assert 'lxFiltroMuda("identidade", "perfis"' in fonte, "perfis não vão no POST"
    assert 'lxFiltroMuda("identidade", "far"' in fonte, "o rigor não vai no POST"


def test_dica_de_ordem_cita_os_dois_filtros_novos():
    fonte = _fonte()
    dica = fonte.split('id="lxFiltrosDica"', 1)[1].split("</p>", 1)[0]
    assert "marca d'água" in dica and "identidade" in dica, \
        f"a dica não cita a ordem real do servidor: {dica!r}"
    # o texto do modo travado também sai daqui: a ordem não pode divergir
    assert "marca d'água > eco > identidade" in fonte


def test_contador_de_falas_ignoradas_existe_no_painel_do_live():
    """O JS escreve em #lxIgnoradas/#lxIgnoradasChip; sem o elemento no HTML o
    contador nunca aparece (achado do #963: a entrega do #528 não tinha o markup)."""
    painel = _fonte().split('id="view-live"', 1)[1].split('id="view-config"', 1)[0]
    for cid in ("lxIgnoradasChip", "lxIgnoradas"):
        assert f'id="{cid}"' in painel, f"{cid} não está no painel do Live"


def test_etapas_da_limpeza_tem_marcador_de_admin_no_html():
    fonte = _fonte()
    assert 'data-admin-setting="voice_limpar_musica"' in fonte
    assert 'data-admin-setting="voice_limpar_dereverb"' in fonte


def test_etapas_sao_admin_no_servidor():
    fonte = (RAIZ / "app.py").read_text()
    bloco = fonte.split("_SETTINGS_ADMIN = frozenset({", 1)[1].split("})", 1)[0]
    for chave in ("voice_limpar_musica", "voice_limpar_dereverb"):
        assert f'"{chave}"' in bloco, \
            f"{chave} saiu de _SETTINGS_ADMIN — chave `use` volta a mudar o áudio global"


def test_chave_use_nao_muda_etapas_e_admin_muda(client, auth, chave_de_uso):
    """Sonda de efeito: `use` volta 200 com o nome em `admin_ignored` e NÃO grava;
    admin grava. (Mesmo molde do #946/#791.)"""
    for chave in ("voice_limpar_musica", "voice_limpar_dereverb"):
        antes = app._settings.get(chave)
        assert antes is False, f"default esperado False em {chave}, veio {antes!r}"
        r = client.post("/api/settings", headers=chave_de_uso, json={chave: True})
        assert r.status_code == 200, r.text
        assert chave in r.json()["admin_ignored"], r.json()
        assert app._settings.get(chave) is False, f"chave de uso mudou {chave}"
        r = client.post("/api/settings", headers=auth, json={chave: True})
        assert r.status_code == 200, r.text
        assert app._settings.get(chave) is True, f"chave admin não gravou {chave}"
