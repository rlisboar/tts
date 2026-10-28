"""O espiao de teardown: alarme para registry, estado para job (#582).

O `_espiao` é a ferramenta que revela vazamento de registry; ele vale enquanto o
sinal for LEGÍVEL. O #575 devolveu a detecção dos três registros e, junto, o ruído:
`jobs` ao fim do teste é normal, então a rodada limpa voltou a ter dezenas de linhas
e o alarme de verdade ficou indistinguível do esperado.

Aqui a régua fica cravada:
- `live`/`dsh` → linha de ALARME `[espiao]`;
- `jobs` → linha de ESTADO `[espiao-jobs]`, só quando o número MUDA;
- o pre-warm do dsh aterrissa ANTES do relatório (senão o cliente do teste anterior
  cai no pool e a linha acusa o vizinho).

Os hooks são chamados à mão: o estado do `app` é do processo, então cada teste
devolve o que mexeu.
"""
import threading
import time
import types

import pytest

import _espiao
import app


@pytest.fixture(autouse=True)
def _limpo_e_zerado(monkeypatch):
    """Estado dos três registros vazio e contador do nível de estado zerado."""
    monkeypatch.setattr(_espiao, "_jobs_anterior", 0)

    def _zera():
        app._live_sessions.clear()
        app._chat_dsh_livres.clear()
        with app._jobs_lock:
            app._jobs.clear()
        app._chat_dsh_prewarm_thread = None

    _zera()
    yield
    _zera()


def _item(nome="test_x"):
    return types.SimpleNamespace(name=nome)


def _passa_pelo_teardown(nome="test_x"):
    """Fase de chamada + teardown, como o pytest faz (o join mora no makereport)."""
    _espiao.pytest_runtest_makereport(_item(nome), types.SimpleNamespace(when="call"))
    _espiao.pytest_runtest_teardown(_item(nome))


def _jobs(n):
    with app._jobs_lock:
        for i in range(n):
            app._jobs[f"j{i}"] = {"id": f"j{i}"}


def test_sessao_live_sobrando_e_alarme(capsys):
    app._live_sessions["s1"] = {"id": "s1"}
    _passa_pelo_teardown("test_deixou_sessao")
    saida = capsys.readouterr().out
    assert "[espiao] test_deixou_sessao: live=1 dsh=0 jobs=0" in saida
    assert "[espiao-jobs]" not in saida, "alarme já mostra o jobs; dois níveis não"


def test_cliente_no_pool_e_alarme(capsys):
    app._chat_dsh_livres.append(object())
    _passa_pelo_teardown("test_deixou_cliente")
    assert "[espiao] test_deixou_cliente: live=0 dsh=1 jobs=0" in capsys.readouterr().out


def test_job_sozinho_e_estado_e_o_mesmo_numero_nao_repete(capsys):
    """`jobs` fala uma vez por mudança; repetir o mesmo número é ruído.

    O hook limpa depois de reportar, então cada passo monta o estado do zero e o
    que se compara é o NÚMERO com o do relatório anterior."""
    _jobs(1)
    _passa_pelo_teardown("test_com_job")
    assert "[espiao-jobs] test_com_job: jobs=1" in capsys.readouterr().out

    _jobs(2)                                 # 2 ≠ 1: mudou, fala
    _passa_pelo_teardown("test_com_dois_jobs")
    assert "[espiao-jobs] test_com_dois_jobs: jobs=2" in capsys.readouterr().out

    _jobs(2)                                 # mesmo número do anterior: cala
    _passa_pelo_teardown("test_repetindo_dois")
    assert "test_repetindo_dois" not in capsys.readouterr().out, "mesmo número repetiu"


def test_limpeza_continua_no_hook_depois_do_report(capsys):
    """O teste seguinte não herda job nem cliente: reporta e SÓ DEPOIS limpa (#575).

    Sessão viva NÃO é limpa aqui de propósito — quem a fecha é o código da sessão;
    sobrar sessão é exatamente o alarme."""
    _jobs(1)
    app._chat_dsh_livres.append(object())
    app._live_sessions["s1"] = {"id": "s1"}
    _passa_pelo_teardown("test_sujo")
    assert "test_sujo" in capsys.readouterr().out, "não reportou o que havia"
    assert app._jobs == {} and app._chat_dsh_livres == []
    assert list(app._live_sessions) == ["s1"], "sessão viva fica para o alarme"


def test_prewarm_aterrissa_antes_do_relatorio(capsys):
    """O cliente que chega na thread de pre-warm entra na linha do teste DELE.

    É o `dsh=1` que a revisão do #581 viu no teste seguinte: sem o join no
    `makereport`, o pool é lido antes de o cliente cair e a acusação vai para o
    vizinho."""
    def _aterrissa():
        time.sleep(0.05)
        app._chat_dsh_livres.append(object())

    thread = threading.Thread(target=_aterrissa)
    thread.start()
    app._chat_dsh_prewarm_thread = thread
    _passa_pelo_teardown("test_que_pediu_prewarm")
    assert "test_que_pediu_prewarm: live=0 dsh=1" in capsys.readouterr().out
    assert not thread.is_alive()
