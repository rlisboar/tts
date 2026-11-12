"""#1143: o `_settings` global não vaza de um teste para o seguinte.

Fica num arquivo PRÓPRIO de propósito: o `test_api.py` tem um `_isolamento_disco`
que já restaura o `_settings` (raso), então uma célula lá dentro nunca morderia.
Os testes que vazavam (`test_live_turns.py` e outros) não passam por ele; aqui só
vale o autouse `_settings_restaurados_entre_testes` do `conftest`.

As duas células são um PAR e dependem da ordem do arquivo: a 1ª suja, a 2ª confere
que o vizinho recebe o estado de antes. Sem o autouse, a 2ª cai.
"""

import app


def test_a_suja_o_settings_direto():
    app._apply_settings({"live_barge_filtros": {"modelo": {"on": True}}})
    assert app._settings["live_barge_filtros"]["modelo"]["on"] is True
    app._settings["chave_inventada_1143"] = 1
    app._settings["live_barge_filtros"]["modelo"]["timeout_ms"] = 12345   # mutação aninhada


def test_b_recebe_o_settings_de_antes():
    modelo = (app._settings.get("live_barge_filtros") or {}).get("modelo") or {}
    assert not modelo.get("on"), "`modelo.on` vazou do teste anterior"
    assert modelo.get("timeout_ms") != 12345, "mutação aninhada vazou"
    assert "chave_inventada_1143" not in app._settings
