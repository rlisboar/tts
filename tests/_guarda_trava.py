"""Lógica PURA do guarda de suíte cheia fora da trava (#1131).

Mora fora do `conftest.py` de propósito: um teste que carregasse o `conftest.py` de novo
(importlib) reexecutaria o módulo inteiro e REINICIALIZARIA o ambiente isolado da rodada
(`TTS_OUTPUTS_DIR`, `TTS_STATE_DIR`…), quebrando os testes seguintes do mesmo processo —
foi o que derrubou o commit do #1131 duas vezes.
"""
from __future__ import annotations

#: fração dos `tests/test_*.py` que a coleta precisa cobrir para contar como "suíte cheia"
FRACAO_SUITE_CHEIA = 0.9


def suite_cheia_fora_da_trava(coletados: set, todos: set, env) -> bool:
    if env.get("TTS_FILA_SEGURA") == "1" or env.get("TTS_FILA") == "0" or env.get("CI"):
        return False
    return bool(todos) and len(coletados & todos) >= FRACAO_SUITE_CHEIA * len(todos)
