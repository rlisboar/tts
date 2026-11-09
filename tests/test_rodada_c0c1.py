"""#876: o roteiro da janela tem de PARSEAR e o critério do eco tem de ser o PONTO.

Dois controles por onde o gate já passou (ou quase):

- `bash -n`/`zsh -n`: o script roda em zsh, mas o aceite pede os dois e é o `bash`
  que derruba — grupos de uma linha `{ ... }` sem `;` antes do `}` dão
  `unexpected end of file`. Estático, sem app no ar.
- critério do eco: com n=10 e zero falso o Wilson dá `[0; 27,8]`; exigir o IC95
  superior ≤5 % torna o ramo LIGA INALCANÇÁVEL. O critério é o ponto
  (falsos/respostas): 0/10 e ≤5 % LIGAM, 5-10 % é INDETERMINADO, >10 % não liga.

Rodam sem mini e sem modelo: só leem arquivo.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((RAIZ / "tools").glob("*.sh"))
sys.path.insert(0, str(RAIZ / "tools"))


def test_ha_scripts_para_conferir() -> None:
    assert SCRIPTS, "nenhum .sh em tools/ — o glob quebrou"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_script_parseia(script: Path, shell: str) -> None:
    """`-n` = só parseia. O aceite do card pede os dois interpretadores."""
    binario = shutil.which(shell)
    if not binario:
        pytest.skip(f"{shell} fora do PATH")
    r = subprocess.run([binario, "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"{shell} -n {script.name}:\n{r.stderr}"


def test_rodada_tem_os_dois_um_liners_com_ponto_e_virgula() -> None:
    """`fala`/`marca` são grupos de uma linha: sem `;` antes do `}` o bash cai."""
    fonte = (RAIZ / "tools" / "rodada_c0c1.sh").read_text().splitlines()
    alvos = [l for l in fonte if l.startswith(("fala()", "marca()"))]
    assert len(alvos) == 2, alvos
    for linha in alvos:
        assert linha.rstrip().endswith("; }"), linha


def _faixa_eco(*a):
    import relatorio_rodada as rr
    return rr._faixa_eco(*a)


@pytest.mark.parametrize("falsos", [0])
def test_zero_falso_em_dez_liga(falsos: int) -> None:
    """O caso que o IC95 matava: zero falso em 10 tem de PASSAR."""
    assert _faixa_eco(falsos, 10) is True


def test_um_falso_em_dez_ainda_nao_decide() -> None:
    """10 % cai na faixa 5-10 % — INDETERMINADO, não LIGA nem NÃO LIGA."""
    assert _faixa_eco(1, 10) == "INDETERMINADO"


@pytest.mark.parametrize("falsos", [6, 10])
def test_acima_de_dez_por_cento_nao_liga(falsos: int) -> None:
    assert _faixa_eco(falsos, 10) is False


def test_faixa_intermediaria_e_indeterminada() -> None:
    """1/20 = 5 % já passa; 2/20 = 10 % ainda não decide; 3/20 = 15 % reprova."""
    assert _faixa_eco(1, 20) is True
    assert _faixa_eco(2, 20) == "INDETERMINADO"
    assert _faixa_eco(3, 20) is False


def test_sem_amostra_nao_decide() -> None:
    for falsos, n in ((None, None), (0, 0), (None, 10)):
        assert _faixa_eco(falsos, n) is None


def test_texto_do_eco_declara_a_resolucao_no_zero() -> None:
    """Zero falso tem de LER que liga, com o IC e o n ao lado — não um mudo PASSA."""
    import relatorio_rodada as rr
    t = rr._texto_eco(0, 10, [0.0, 27.8])
    assert "LIGA" in t and "resolução declarada" in t and "27.8" in t and "n=10" in t
    assert "NÃO LIGA" in rr._texto_eco(5, 10, [23.7, 76.3])
    assert "INDETERMINADO" in rr._texto_eco(1, 10, [1.8, 40.4])
