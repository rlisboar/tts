"""Gate QA da task_9322e509 (#177): backend de IA POR CAMINHO (Conversa × Live).

A #176 criou `chat_backend_live` para tornar acionável a recomendação de rota do
LIVE.md (#175). Aqui ficam as pontas que o gate cobrou e os testes do autor não
cobriam: o campo sendo ADMIN (chave de uso não reaponta), a inversão dos dois
caminhos valendo com o valor vindo de SETTINGS (não só de env), e a prova de que
o `stats.ia.pedido` segue o Live mesmo com a Conversa em dsh.
"""

import queue

import pytest

import app
# fixtures de `test_api.py` (não estão no conftest): o `_isolamento_disco` é
# autouse e isola settings/chaves num tmp — sem ele o POST grava no repo.
from test_api import _isolamento_disco, auth, chave_de_uso, client  # noqa: F401
from test_live_dsh import DshClienteFalso

# Idem para as fixtures de `test_api`: chegam por `import` e são usadas só como
# parâmetro de teste, uso que o pyflakes do hook não enxerga (ele ignora `noqa`).
# `__all__` marca os imports como usados e mata o F401/F811.
__all__ = ["_isolamento_disco", "auth", "chave_de_uso", "client"]


@pytest.fixture()
def sem_env_backend(monkeypatch):
    for var in ("TTS_CHAT_BACKEND", "TTS_CHAT_BACKEND_LIVE"):
        monkeypatch.delenv(var, raising=False)


def _sessao(nome="sx"):
    return {"id": nome, "voice_id": None, "system": None, "history": [],
            "fila": queue.Queue()}


# --------------------------------------------------------------- admin/uso
def test_chave_de_uso_nao_reaponta_o_backend_do_live(client, auth, chave_de_uso,
                                                     sem_env_backend):
    """`chat_backend_live` é de admin: a chave de uso manda o blob inteiro no
    Salvar e o campo tem de voltar em `admin_ignored` com o valor intacto."""
    client.post("/api/settings", headers=auth, json={"chat_backend": "openai",
                                                     "chat_backend_live": "dsh"})
    assert app._chat_backend_live() == "dsh"

    r = client.post("/api/settings", headers=chave_de_uso,
                    json={"chat_backend": "dsh", "chat_backend_live": "openai"})
    assert r.status_code == 200, r.text
    assert "chat_backend_live" in r.json()["admin_ignored"]
    assert app._settings["chat_backend_live"] == "dsh", "valor de admin preservado"
    assert app._chat_backend_live() == "dsh"


def test_admin_aceita_vazio_e_invalido_faz_rollback(client, auth, sem_env_backend):
    """`""` é VÁLIDO (herda); valor inválido é 400 SEM deixar o campo pela metade."""
    client.post("/api/settings", headers=auth, json={"chat_backend": "dsh",
                                                     "chat_backend_live": "dsh"})
    antes = dict(app._settings)
    r = client.post("/api/settings", headers=auth, json={"chat_backend_live": "xpto"})
    assert r.status_code == 400
    assert app._settings == antes, "rollback: nada aplicado no payload inválido"

    r = client.post("/api/settings", headers=auth, json={"chat_backend_live": ""})
    assert r.status_code == 200 and r.json().get("admin_ignored") == []
    assert app._chat_backend_live() == "dsh", "vazio herda o global (que é dsh)"


# --------------------------------------------------------------- inversão
def test_inversao_vale_com_valor_vindo_de_settings(monkeypatch, sem_env_backend):
    """Os testes do autor forçam por ENV; aqui o campo vem de `_settings`, que é
    como o dono usa (Configurações → IA) — e a Conversa segue no global."""
    monkeypatch.setattr(app.dsh_client, "DshClient", DshClienteFalso)
    DshClienteFalso.criados.clear()
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")

    pipe = app._live_pipe_novo(_sessao("live-settings"))
    try:
        assert len(DshClienteFalso.criados) == 1
    finally:
        pipe.close()

    monkeypatch.setattr(app, "_chat_llm_openai", lambda _m: "via-openai")
    monkeypatch.setattr(app, "_chat_llm_dsh", lambda _m: "via-dsh")
    assert app._chat_llm([]) == "via-openai", "a Conversa não foi arrastada"

    # e o inverso: Conversa em dsh, Live no provedor
    DshClienteFalso.criados.clear()
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    monkeypatch.setitem(app._settings, "chat_backend_live", "openai")
    pipe = app._live_pipe_novo(_sessao("live-openai"))
    assert pipe._dsh is None and not DshClienteFalso.criados
    assert app._chat_llm([]) == "via-dsh"


def test_stats_ia_segue_o_live_e_nao_a_conversa(monkeypatch, sem_env_backend):
    """`pedido`/`backend` são do LIVE: com a Conversa em dsh e o Live em openai o
    painel NÃO pode anunciar dsh (é o falso-verde que o #151 já tratou)."""
    monkeypatch.setitem(app._settings, "chat_backend", "dsh")
    monkeypatch.setitem(app._settings, "chat_backend_live", "openai")
    ia = app._live_stats_ia({"pipe": None})
    assert ia["pedido"] == "openai" and ia["backend"] == "openai"

    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
    ia = app._live_stats_ia({"pipe": None})
    assert ia["pedido"] == "dsh" and ia["backend"] == "dsh"


def test_so_o_live_e_afetado_quando_a_conversa_esta_em_openai(monkeypatch,
                                                              sem_env_backend):
    """A pre-warm da CONVERSA não pode ser ligada pelo campo do Live (é o caminho
    do `_chat_llm`): com Conversa em openai o pool da Conversa fica quieto."""
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
    monkeypatch.setattr(app, "_chat_dsh_livres", [])
    monkeypatch.setattr(app, "_chat_dsh_prewarm_thread", None)
    assert app._chat_dsh_prewarm("teste") is None, "Conversa em openai não sobe dsh"

def test_stats_ia_de_sessao_ja_aberta_apos_trocar_o_campo(monkeypatch, sem_env_backend):
    """RESIDUAL (#177): o cliente dsh nasce no CONNECT e a troca do campo não
    reconstrói a sessão aberta. `stats.ia.pedido` é o configurado (certo), mas
    `backend` hoje é derivado dele — então uma sessão aberta com o Live em openai
    passa a anunciar dsh no painel assim que o dono troca o seletor, enquanto o
    texto continua vindo do endpoint (mesma classe do falso-verde do #151)."""
    monkeypatch.setattr(app.dsh_client, "DshClient", DshClienteFalso)
    DshClienteFalso.criados.clear()
    monkeypatch.setitem(app._settings, "chat_backend", "openai")
    monkeypatch.setitem(app._settings, "chat_backend_live", "openai")
    sess = _sessao("aberta")
    pipe = app._live_pipe_novo(sess)
    try:
        assert pipe._dsh is None
        ia = app._live_stats_ia(sess)
        assert ia["pedido"] == "openai" and ia["backend"] == "openai"

        # o dono muda o seletor com a sessão ABERTA (nada reconstrói o pipe)
        monkeypatch.setitem(app._settings, "chat_backend_live", "dsh")
        ia = app._live_stats_ia(sess)
        assert ia["pedido"] == "dsh", "pedido é o configurado"
        # RESIDUAL: o painel anuncia dsh sem haver cliente dsh nesta sessão
        assert pipe._dsh is None
        assert ia["backend"] == "dsh", (
            "hoje o backend segue o configurado; o honesto seria olhar o pipe "
            "(_dsh) — o texto deste turno ainda vem do endpoint")
    finally:
        pipe.close()
