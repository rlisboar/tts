"""O caminho REAL do worker TTS é EXIGIDO quando a área muda (#388).

Até aqui a regra vivia só em comentário (`conftest.py:132`, `.githooks/pre-commit`):
o resumo do pytest avisava que o caminho com modelo real não correu e ninguém era
obrigado a nada. Mexer em `tts_worker.py`/`live_pipeline.py` e fechar gate com
`worker_real` fora já produziu verde falso.

Este arquivo vira a exigência. Base do diff:

- `TTS_REAL_BASE` — argumento do `git diff` (`--cached` no hook,
  `origin/main...HEAD` no CI);
- sem a variável, a união de árvore + índice, que é o caso de quem roda à mão.

Fora da área, o teste passa e ninguém paga o runtime real.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent

# Módulos que os três `worker_real` de `tests/test_worker.py` realmente exercitam:
# worker isolado, worker persistente (`_LiveWorker`) e o caminho ligado ponta a
# ponta. Não é a lista "do Live" — é a dos arquivos que a suíte real cobre.
AREA = ("tts_worker.py", "live_pipeline.py")

COMANDO = "TTS_TEST_WORKER=1 ./.venv-mlx/bin/python -m pytest -m worker_real -q"


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=RAIZ, capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _mudados() -> list[str] | None:
    """None = não deu para saber (sem git, sem base); lista vazia = nada mudou."""
    base = os.environ.get("TTS_REAL_BASE")
    if base:
        saida = _git("diff", "--name-only", base, "--")
        return None if saida is None else saida.split()
    pedacos = [_git("diff", "--name-only", "--"), _git("diff", "--cached", "--name-only", "--")]
    if all(p is None for p in pedacos):
        return None
    nomes: list[str] = []
    for p in pedacos:
        nomes += (p or "").split()
    return sorted(set(nomes))


def test_area_do_worker_real_muda_exige_o_marcador():
    mudados = _mudados()
    if mudados is None:
        pytest.skip("sem git/base para saber o que mudou")

    tocados = [f for f in mudados if f in AREA]
    if not tocados:
        return                      # área intacta: nada a exigir, nada a pagar

    if os.environ.get("TTS_TEST_WORKER"):
        return                      # o caminho real está rodando nesta sessão

    pytest.fail(
        "o diff mexe na área coberta pelo caminho REAL e o `worker_real` não vai rodar.\n"
        f"tocados: {', '.join(tocados)}\n"
        "sem isso o gate fica verde sem nunca executar o worker nem o lock de Metal.\n"
        f"rode: {COMANDO}\n"
        "(rodando o teste: `TTS_REAL_BASE=--cached` no hook, `origin/main...HEAD` no CI)",
        pytrace=False,
    )


# ── controle do próprio controle ───────────────────────────────────────────────
# Sem isto o arquivo só morde quando a árvore está suja: numa árvore limpa ele
# passaria por vacuidade e ninguém saberia se a lógica está certa.

def _com_diff(monkeypatch, mudados):
    import test_worker_real_exigido as mod
    monkeypatch.setattr(mod, "_mudados", lambda: mudados)
    monkeypatch.delenv("TTS_TEST_WORKER", raising=False)


def test_area_tocada_sem_env_reprova(monkeypatch):
    _com_diff(monkeypatch, ["app.py", "live_pipeline.py"])
    with pytest.raises(pytest.fail.Exception) as erro:
        test_area_do_worker_real_muda_exige_o_marcador()
    assert "live_pipeline.py" in str(erro.value)
    assert "TTS_TEST_WORKER=1" in str(erro.value)


def test_area_intacta_nao_exige_nada(monkeypatch):
    _com_diff(monkeypatch, ["app.py", "README.md"])
    test_area_do_worker_real_muda_exige_o_marcador()      # não levanta


def test_area_tocada_com_env_passa(monkeypatch):
    _com_diff(monkeypatch, ["tts_worker.py"])
    monkeypatch.setenv("TTS_TEST_WORKER", "1")
    test_area_do_worker_real_muda_exige_o_marcador()      # não levanta
