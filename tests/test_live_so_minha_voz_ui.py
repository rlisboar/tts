"""#963: UI do modo "entender só a minha voz" (I5, #533) e do "só a voz de" (I6, #534).

Estática de propósito (mesmo molde do #528): o que se cobra aqui é a FIAÇÃO —
controles com marcador de admin, rigor com os valores que o servidor aceita, chips
do OBS que o JS escreve, seletores que mandam `speaker_alvo` — e um passo de efeito
no servidor: a chave `use` não liga o modo; admin liga.
"""
from pathlib import Path

import app

pytest_plugins = ["test_api"]

from test_api import auth, chave_de_uso, client  # noqa: E402, F401

RAIZ = Path(__file__).resolve().parent.parent
INDEX = RAIZ / "static" / "index.html"

CONTROLES_MODO = ("lxSoVoz_on", "lxSoVoz_perfil", "lxSoVoz_rigor")
CHIPS_VOZ = ("lxVozChip", "lxVozEst", "lxVozScore", "lxVozRecusChip", "lxVozRecus")
SELETORES_SO_VOZ_DE = ("sttSoVoz", "tvSoVoz", "cvSoVoz")


def _fonte():
    return INDEX.read_text()


def _painel_live():
    """Do `view-live` até o `view-config`: onde mora o painel do Live e seus chips."""
    return _fonte().split('id="view-live"', 1)[1].split('id="view-config"', 1)[0]


def _tag(fonte: str, elemento_id: str) -> str:
    """Texto da tag que contém `id="<elemento_id>"`, do id até o próximo `>`."""
    assert f'id="{elemento_id}"' in fonte, f"{elemento_id} não existe no HTML"
    return fonte.split(f'id="{elemento_id}"', 1)[1].split(">", 1)[0]


def test_controles_do_modo_existem_e_sao_admin():
    fonte = _fonte()
    for cid in CONTROLES_MODO:
        assert 'data-admin-setting="live_so_minha_voz"' in _tag(fonte, cid), \
            f"{cid} sem marcador de admin — chave `use` poderia ligar o modo pela tela"
    # o aviso de marcador faltante acusaria a chave se ela continuasse na lista
    lista = fonte.split("const ADMIN_SEM_UI = new Set([", 1)[1].split("]);", 1)[0]
    assert '"live_so_minha_voz"' not in lista, \
        "live_so_minha_voz continua em ADMIN_SEM_UI depois de ganhar controle"


def test_rigor_do_modo_usa_os_valores_que_o_servidor_aceita():
    fonte = _fonte()
    trecho = fonte.split('id="lxSoVoz_rigor"', 1)[1].split("</select>", 1)[0]
    presentes = {v for v in ("joelho", "1", "0.1") if f'value="{v}"' in trecho}
    assert presentes == {"joelho", "1", "0.1"}, \
        f"o rigor do modo perdeu opções do servidor: {sorted(presentes)}"
    # o servidor normaliza para o joelho qualquer valor fora da tabela (app.py, #533)
    assert app._VOZ_RIGOR_JOELHO == "joelho"


def test_modo_fica_fora_do_painel_de_filtros_de_barge():
    """O painel #lxFiltros conta 11 toggles de barge (suíte do #527) e é travado
    pelo modo `imediata`; o "só a minha voz" é modo que abre o turno — não entra nele."""
    fonte = _fonte()
    assert 'id="lxSoVoz_on"' not in fonte.split('id="lxFiltros"', 1)[1].split("</details>", 1)[0], \
        "o modo voltou para dentro do painel de filtros de barge"
    assert fonte.index('id="lxSoVozBloco"') < fonte.index('id="lxFiltros"')


def test_modo_tem_dica_de_microfone_e_leva_ao_cadastro():
    fonte = _fonte()
    assert 'id="lxSoVozCadastra"' in fonte, "sem perfil, o modo não leva ao cadastro"
    assert "headset" in _painel_live(), \
        "falta a dica de que headset/direcional ajuda mais que qualquer filtro"


def test_obs_tem_o_veredito_da_voz_e_a_contagem_de_recusas():
    bloco = _painel_live()
    for cid in CHIPS_VOZ:
        assert f'id="{cid}"' in bloco, \
            f"o chip {cid} não está no painel do Live — o JS escreve num elemento que não existe"


def test_seletores_so_voz_de_existem_sem_marcador_de_admin():
    fonte = _fonte()
    for sel in SELETORES_SO_VOZ_DE:
        tag = _tag(fonte, sel)
        assert "data-admin-setting" not in tag, \
            f"{sel} é escolha por envio, não ajuste administrativo"
        trecho = fonte.split(f'id="{sel}"', 1)[1].split("</select>", 1)[0]
        assert '<option value="">' in trecho, f"{sel} não nasce em 'sem filtro'"


def test_envios_de_stt_traducao_e_conversa_mandam_speaker_alvo():
    fonte = _fonte()
    assert fonte.count('fd.append("speaker_alvo"') >= 3, \
        "STT, Tradutor e Conversa precisam mandar speaker_alvo no envio"


def test_chave_use_nao_liga_o_modo_e_admin_liga(client, auth, chave_de_uso, monkeypatch):
    """Sonda de efeito (molde do #946/#791/#528): `use` volta 200 com o nome em
    `admin_ignored` e NÃO grava; admin grava e o rigor vem normalizado."""
    base = {"on": False, "perfil": "", "rigor_far": "joelho", "limiar": 0.0, "separacao": False}
    monkeypatch.setitem(app._settings, "live_so_minha_voz", dict(base))
    r = client.post("/api/settings", headers=chave_de_uso, json={"live_so_minha_voz": {"on": True}})
    assert r.status_code == 200, r.text
    assert "live_so_minha_voz" in r.json()["admin_ignored"], r.json()
    assert app._settings["live_so_minha_voz"]["on"] is False, "chave de uso ligou o modo"
    r = client.post("/api/settings", headers=auth,
                    json={"live_so_minha_voz": {"on": True, "perfil": "dono", "rigor_far": "0.1"}})
    assert r.status_code == 200, r.text
    cfg = app._settings["live_so_minha_voz"]
    assert cfg["on"] is True and cfg["perfil"] == "dono" and cfg["rigor_far"] == "0.1", cfg
