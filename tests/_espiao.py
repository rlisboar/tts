r"""Espião de TEARDOWN (versionado, ligado pelo `pytest_plugins` do conftest).

Não é arquivo descartável: ele é o que mostra, ao fim de CADA teste, se sobrou
sessão viva (`_live_sessions`), cliente dsh no pool (`_chat_dsh_livres`) ou job
(`_jobs`). Foi assim que os vazamentos de registry apareceram antes de virarem
teste. Nome com `_` de propósito: sem isso o pytest colecionaria o arquivo como
módulo de teste.

Só imprime quando há o que mostrar, e o print precisa de `-s` para aparecer:

    pytest tests/ -q -s | grep '\[espiao\]'

Uso esperado: ao escrever ou revisar teste de Live/dsh, rodar o arquivo com `-s` e
checar que o espiao fica CALADO. Barulho ali é vazamento — o teste passou deixando
estado para o próximo.
"""


import pytest


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    """Reporta o que o teste deixou e SÓ DEPOIS limpa (#575).

    A limpeza fica aqui, não num fixture: quem limpa em fixture teardown roda
    antes deste hook quando ele é `trylast` (e o espiao fica cego, que foi o
    defeito do #575). O `conftest` zera o registro no SETUP, então o estado visto
    aqui é só o deste teste — nada de acúmulo do anterior."""
    import app

    vivo = len(app._live_sessions)
    if vivo or app._chat_dsh_livres or app._jobs:
        print(f"\n[espiao] {item.name}: live={vivo} dsh={len(app._chat_dsh_livres)} "
              f"jobs={len(app._jobs)}", flush=True)
    # higiene para o próximo teste: pre-warm que ainda vai aterrissar é esperado
    prewarm = getattr(app, "_chat_dsh_prewarm_thread", None)
    if prewarm is not None and prewarm.is_alive():
        prewarm.join(30)
    with app._jobs_lock:
        app._jobs.clear()
    app._chat_dsh_livres.clear()
