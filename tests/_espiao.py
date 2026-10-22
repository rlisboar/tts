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


def pytest_runtest_teardown(item):
    import app

    vivo = len(app._live_sessions)
    if vivo or app._chat_dsh_livres or app._jobs:
        print(f"\n[espiao] {item.name}: live={vivo} dsh={len(app._chat_dsh_livres)} "
              f"jobs={len(app._jobs)}", flush=True)
