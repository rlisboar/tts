"""#206: o ambiente e o ARQUIVO do DONO não podem decidir o veredito da suíte.

São os DOIS vetores da família "teste refém do estado do dono":

- ENV: o `conftest` neutraliza, no import, os knobs `TTS_CHAT_*`/`TTS_LIVE_*`/
  `TTS_TEST_*` (as duas exceções são interruptores do dono: `TTS_TEST_WORKER` e
  `TTS_LIVE_WORKER`).
- ARQUIVO: o `conftest` COPIA o `settings.json` do dono — o caminho é isolado, o
  CONTEÚDO não: um campo escolhido na tela (ex.: `chat_backend_live`) atravessa.
  O que o barra é a fixture `dsh_limpo` (`tests/test_api.py`), usada pelos testes
  que medem a rota.

Cada vetor tem o seu controle executável no mesmo formato: o teste de cima RODA um
filho com o estado hostil e cobra verde; o de baixo, já dentro do filho, cobra que
o hostil chegou (senão o controle seria vazio).

Caso real que isto fecha (medido no gate #206, conftest do HEAD × conftest com a
neutralização): com `TTS_CHAT_DSH_BIN=/nao/existe/dsh` exportado,
`tests/test_api.py::test_compressao_do_live_segue_o_backend_efetivo` ficava VERMELHO
(o env tem precedência sobre o settings, por desenho) e passava com a neutralização.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
MARCADOR = "QA206_ESPERADO"
HOSTIL = {
    "TTS_CHAT_BACKEND": "dsh",
    "TTS_CHAT_BACKEND_LIVE": "openai",
    "TTS_CHAT_DSH_BIN": "/nao/existe/dsh",
    "TTS_LIVE_TTL_S": "1",
}
INTERRUPTORES = ("TTS_TEST_WORKER", "TTS_LIVE_WORKER")

# Vetor ARQUIVO: o filho aponta o settings para uma cópia hostil (plugin abaixo) e
# roda o arquivo que mede a rota do Live. As DUAS direções do campo: o dono na tela
# em `openai` (o teste do caminho dsh media openai) e em `dsh` (o inverso).
ARQ_MARCADOR = "QA206_ARQUIVO"
ARQ_PLUGIN = "_estado_hostil"
ARQ_CAMPO = "chat_backend_live"
ARQ_FILHOS = ("tests/test_live_dsh.py", "tests/test_ambiente_neutro.py")


def _env_filho(**extra):
    """Ambiente do filho: o do pai + o hostil, com `tests/` importável (plugin)."""
    env = {**os.environ, **extra}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parent),
         os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def _rodar_filho(alvos, env, porque):
    """Roda o filho com o plugin do ARQUIVO carregado (ele é no-op sem o env dele).

    Os dois controles compartilham este lançamento de propósito: o filho de um
    pode ser o pai do outro (o controle de ENV roda ESTE arquivo, que tem o de
    ARQUIVO), e sem o plugin o neto perderia o arquivo hostil."""
    r = subprocess.run([sys.executable, "-m", "pytest", *alvos, "-q",
                        "-p", ARQ_PLUGIN],
                       cwd=str(BASE), env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"suíte vermelha {porque}:\n{r.stdout[-3000:]}"


def test_suite_nasce_neutra_com_ambiente_hostil():
    """Roda ESTE arquivo num filho com o ambiente hostil exportado (fora do pai)."""
    if os.environ.get(MARCADOR):
        return          # já estamos no filho: quem cobra lá é o teste de baixo
    _rodar_filho([str(Path(__file__))],
                 _env_filho(**HOSTIL, TTS_TEST_WORKER="1",
                            **{MARCADOR: ",".join(HOSTIL),
                               "QA206_INTERRUPTORES": ",".join(INTERRUPTORES)}),
                 "com ambiente hostil")


def test_knobs_do_dono_nao_chegam_nos_testes():
    """Dentro do filho: os knobs exportados sumiram, os interruptores ficaram."""
    esperado = os.environ.get(MARCADOR)
    if not esperado:
        return          # no pai o ambiente é o do dono: nada a cobrar aqui
    for var in esperado.split(","):
        assert var not in os.environ, f"{var} atravessou a neutralização (#206)"
    interruptores = (os.environ.get("QA206_INTERRUPTORES") or INTERRUPTORES[1]).split(",")
    for var in interruptores:
        assert var in os.environ, f"{var} é interruptor do dono e não podia ser tirado"
    # e o app, que é quem lê esses knobs, enxerga o settings em vez do ambiente
    import app
    assert app._chat_backend_live() == (app._settings.get("chat_backend_live") or
                                        app._chat_backend()), "env ainda vencendo o settings"


@pytest.mark.parametrize("valor", ["openai", "dsh"])
def test_suite_nao_e_refem_do_arquivo_do_dono(tmp_path, valor):
    """O campo que o dono escolhe na tela não pode virar veredito (vetor ARQUIVO).

    O filho roda `test_live_dsh.py` (é onde moram os testes que medem a rota do
    Live): sem a neutralização do campo, os dois testes do caminho dsh mediam
    openai e ficavam vermelhos com o código certo (achado #205)."""
    if os.environ.get(ARQ_MARCADOR):
        return          # já estamos no filho: quem cobra lá é o teste de baixo
    origem = BASE / "settings.json"
    dados = json.loads(origem.read_text()) if origem.exists() else {}
    dados[ARQ_CAMPO] = valor
    destino = tmp_path / "settings.json"
    destino.write_text(json.dumps(dados, ensure_ascii=False))
    _rodar_filho(ARQ_FILHOS,
                 _env_filho(**{"QA206_SETTINGS": str(destino), ARQ_MARCADOR: valor}),
                 f"com o arquivo do dono em {ARQ_CAMPO}={valor}")


def test_o_arquivo_hostil_chegou_no_app():
    """Dentro do filho: o campo hostil de fato entrou (senão o controle seria vazio)."""
    valor = os.environ.get(ARQ_MARCADOR)
    if not valor:
        return          # no pai o arquivo é o do dono: nada a cobrar aqui
    import app
    # `app.SETTINGS_PATH` já foi trocado pelo conftest (cópia do arquivo hostil):
    # é o CONTEÚDO copiado que tem de trazer o campo, e o app tem de enxergá-lo.
    copiado = json.loads(app.SETTINGS_PATH.read_text())
    assert copiado.get(ARQ_CAMPO) == valor, (
        f"o arquivo hostil não foi o copiado: SETTINGS_PATH={app.SETTINGS_PATH} "
        f"QA206_SETTINGS={os.environ.get('QA206_SETTINGS')}")
    assert app._settings.get(ARQ_CAMPO) == valor, "o campo hostil não chegou no app"