"""#898: porta de servidor de teste fora da faixa de controle do mic-router.

O painel OBS sonda `127.0.0.1:7861-7865` para achar o roteador de microfone. Se o
servidor de uma suíte cair numa dessas portas, a sonda fala com o servidor errado
(sem CORS em `/mode`) e a suíte reprova por um vermelho que não existe — foi o que
derrubou duas integrações do #871, com uma suíte acusando a outra.

Toda escolha de porta em `tests/*.sh` tem de pular a faixa. A célula é estática de
propósito: subir duas suítes de verdade custaria minutos por rodada.
"""
from __future__ import annotations

import re
from pathlib import Path

AQUI = Path(__file__).resolve().parent
FAIXA = range(7861, 7866)


def _escolhas_de_porta(fonte: str):
    """Blocos `def porta_livre`/`def sobe_stub` e one-liners `python3 -c`."""
    for m in re.finditer(r"^def (?:porta_livre|sobe_stub)\([^)]*\):\n(.*?)(?=^\S)",
                         fonte, re.M | re.S):
        yield m.group(1)
    for m in re.finditer(r"python3 -c '([^']*)'", fonte):
        yield m.group(1)


def test_toda_escolha_de_porta_pula_a_faixa_do_mic_router():
    faltando = []
    for p in sorted(AQUI.glob("*.sh")):
        for bloco in _escolhas_de_porta(p.read_text()):
            if "bind(" not in bloco:
                continue
            if "7861" not in bloco:      # a guarda cita a faixa
                faltando.append(p.name)
                break
    assert not faltando, (
        "escolha de porta sem pular 7861-7865 (a sonda do mic-router fala com o "
        f"servidor errado): {faltando}")


def test_faixa_das_suites_e_a_do_roteador():
    """A faixa não pode ser número solto: é a mesma do `CTRL_PORTS` do cliente."""
    fonte = (AQUI.parent / "client" / "mic_router.py").read_text()
    m = re.search(r"CTRL_PORTS = \(([^)]*)\)", fonte)
    assert m, "CTRL_PORTS sumiu de client/mic_router.py"
    portas = {int(x) for x in re.findall(r"\d+", m.group(1))}
    assert portas == set(FAIXA), (
        f"a faixa das suítes {sorted(FAIXA)} divergiu do CTRL_PORTS {sorted(portas)}")
