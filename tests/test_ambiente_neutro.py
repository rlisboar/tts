"""#206: o ambiente do DONO não pode decidir o veredito da suíte.

O `conftest` neutraliza, no import, os knobs `TTS_CHAT_*`/`TTS_LIVE_*`/`TTS_TEST_*`
(as duas exceções são interruptores do dono: `TTS_TEST_WORKER` e `TTS_LIVE_WORKER`).
Aqui isso vira controle executável: o teste de cima RODA a suíte num FILHO com um
ambiente hostil exportado e cobra verde; o de baixo, já dentro do filho, cobra que
os knobs não chegaram lá.

Caso real que isto fecha (medido no gate #206, conftest do HEAD × conftest com a
neutralização): com `TTS_CHAT_DSH_BIN=/nao/existe/dsh` exportado,
`tests/test_api.py::test_compressao_do_live_segue_o_backend_efetivo` ficava VERMELHO
(o env tem precedência sobre o settings, por desenho) e passava com a neutralização.
"""
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MARCADOR = "QA206_ESPERADO"
HOSTIL = {
    "TTS_CHAT_BACKEND": "dsh",
    "TTS_CHAT_BACKEND_LIVE": "openai",
    "TTS_CHAT_DSH_BIN": "/nao/existe/dsh",
    "TTS_LIVE_TTL_S": "1",
}
INTERRUPTORES = ("TTS_TEST_WORKER", "TTS_LIVE_WORKER")


def test_suite_nasce_neutra_com_ambiente_hostil():
    """Roda ESTE arquivo num filho com o ambiente hostil exportado (fora do pai)."""
    if os.environ.get(MARCADOR):
        return          # já estamos no filho: quem cobra lá é o teste de baixo
    env = {**os.environ, **HOSTIL, "TTS_TEST_WORKER": "1",
           MARCADOR: ",".join(HOSTIL),
           "QA206_INTERRUPTORES": ",".join(INTERRUPTORES)}
    r = subprocess.run([sys.executable, "-m", "pytest", str(Path(__file__)), "-q"],
                       cwd=str(BASE), env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"suíte vermelha com ambiente hostil:\n{r.stdout[-3000:]}"


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