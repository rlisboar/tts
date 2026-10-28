r"""Espião de TEARDOWN (versionado, ligado pelo `pytest_plugins` do conftest).

Não é arquivo descartável: ele é o que mostra, ao fim de CADA teste, se sobrou
sessão viva (`_live_sessions`), cliente dsh no pool (`_chat_dsh_livres`) ou job
(`_jobs`). Foi assim que os vazamentos de registry apareceram antes de virarem
teste. Nome com `_` de propósito: sem isso o pytest colecionaria o arquivo como
módulo de teste.

DOIS NÍVEIS, de propósito (#582): o sinal estava misturado com ruído e ninguém
sabia mais o que caçar.

- `[espiao] … live=N dsh=N jobs=N` — ALARME. `_live_sessions` e `_chat_dsh_livres`
  são registry: deixar sessão viva ou cliente no pool é vazamento de verdade.
- `[espiao-jobs] … jobs=N` — ESTADO. Job ao fim do teste é NORMAL (o teste de TTS
  cria o job e não tem por que esperar ele morrer); sai só quando o número MUDA em
  relação ao último teste, senão seriam dezenas de linhas iguais por rodada.
- nada — teste limpo.

Por isso `grep '\[espiao\]'` (com o colchete) mostra só alarme; `grep espiao`
mostra os dois níveis. Precisa de `-s` para aparecer:

    pytest tests/ -q -s | grep '\[espiao\]'

O estado mostrado é do ÚLTIMO caso que rodou antes da linha: o pre-warm do dsh é
aterrissado ANTES do relatório (`pytest_runtest_makereport`) justamente para não
acusar o teste vizinho.
"""


import pytest

#: último `jobs` relatado — o nível de ESTADO só fala quando o número muda
_jobs_anterior = 0


def _aterrissa_prewarm() -> None:
    """Espera o pre-warm do dsh aterrissar (o cliente cai no pool na thread dele).

    Sem isto o cliente aparece no relatório do teste SEGUINTE e o veredito acusa
    o vizinho (#582)."""
    import app

    prewarm = getattr(app, "_chat_dsh_prewarm_thread", None)
    if prewarm is not None and prewarm.is_alive():
        prewarm.join(30)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Aterrissa o pre-warm ANTES do teardown (logo depois da fase de chamada)."""
    if call.when == "call":
        _aterrissa_prewarm()


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    """Reporta o que o teste deixou e SÓ DEPOIS limpa (#575).

    A limpeza fica aqui, não num fixture: quem limpa em fixture teardown roda
    antes deste hook quando ele é `trylast` (e o espiao fica cego, que foi o
    defeito do #575). O `conftest` zera o registro no SETUP, então o estado visto
    aqui é só o deste teste — nada de acúmulo do anterior."""
    global _jobs_anterior

    import app

    vivo = len(app._live_sessions)
    clientes = len(app._chat_dsh_livres)
    jobs = len(app._jobs)
    if vivo or clientes:                        # ALARME: registry com sobra
        print(f"\n[espiao] {item.name}: live={vivo} dsh={clientes} jobs={jobs}",
              flush=True)
    elif jobs and jobs != _jobs_anterior:       # ESTADO: job é normal, repetido não
        print(f"\n[espiao-jobs] {item.name}: jobs={jobs}", flush=True)
    _jobs_anterior = jobs
    # higiene para o próximo teste
    _aterrissa_prewarm()
    with app._jobs_lock:
        app._jobs.clear()
    app._chat_dsh_livres.clear()
