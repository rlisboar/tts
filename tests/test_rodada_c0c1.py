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


def test_sobe_registra_o_pid_do_listener_e_nao_o_do_subshell() -> None:
    """O `&` no FIM de um subshell põe o SUBSHELL em background: sem `exec` o
    `$!` é um PID que já morreu, o `kill` do `trap` acerta o nada e a instância
    fica de pé com a 7890 presa (medido na 1ª rodada real)."""
    fonte = (RAIZ / "tools" / "rodada_c0c1.sh").read_text()
    assert "exec env" in fonte, "o subshell do `sobe` precisa de `exec`"
    assert "listener da 7890 é" in fonte, "falta a conferência pid × dono do listener"
    # a atribuição do APP_PID tem de vir DEPOIS do `&` do subshell com exec
    sobe = fonte.split("sobe() {", 1)[1].split("\n}", 1)[0]
    assert sobe.index("exec env") < sobe.index('APP_PID="$!"'), sobe


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


# ── #1075: a chave do provedor não pode aparecer no argv do `curl` ──────────
_SENHA = "sk-SENTINELA-1075"


def _roda_prevoo(tmp_path: Path, env_extra: dict | None = None):
    """Roda SÓ o bloco de pré-voo, com `curl` dublado que anota o argv.

    O bloco é recortado pelos marcadores do próprio script: rodar o roteiro
    inteiro exigiria mini, modelo e a 7890.
    """
    fonte = (RAIZ / "tools" / "rodada_c0c1.sh").read_text()
    bloco = fonte.split("# --- INICIO PRE-VOO DA CHAVE (#1075) ---", 1)[1]
    bloco = bloco.split("# --- FIM PRE-VOO DA CHAVE (#1075) ---", 1)[0]

    (tmp_path / "settings.json").write_text(
        '{"chat_base_url": "http://127.0.0.1:9/v1", "chat_api_key": "%s", '
        '"chat_model": "x"}' % _SENHA)

    argv = tmp_path / "argv.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> %s\necho 200\n' % ("%s", argv))
    curl.chmod(0o755)

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "TMPDIR": str(tmp_path), "PY": sys.executable,
           "TTS_CHAT_BASE_URL": "http://127.0.0.1:9/v1"}
    env.update(env_extra or {})
    r = subprocess.run(["zsh", "-c", bloco], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return argv.read_text()


def test_chave_do_provedor_nao_aparece_no_argv_do_curl(tmp_path: Path) -> None:
    """`ps` de outro processo do mesmo usuário lê argv — a chave ia junto.

    O header tem de ir por arquivo (`-H @caminho`), não colado no `-H`.
    """
    chamadas = _roda_prevoo(tmp_path, {"TTS_CHAT_API_KEY": _SENHA})
    assert _SENHA not in chamadas, f"a chave vazou no argv:\n{chamadas}"
    assert "-H @/" in chamadas, chamadas
    arquivo = Path(chamadas.split("-H @", 1)[1].split()[0])
    assert arquivo.read_text().strip() == f"Authorization: Bearer {_SENHA}"
    assert oct(arquivo.stat().st_mode & 0o777) == "0o600", "o header tem de ser 0600"


def test_chave_nao_e_herdada_pelos_filhos(tmp_path: Path) -> None:
    """Só o `sobe` repassa a chave; o resto dos filhos não precisa dela."""
    fonte = (RAIZ / "tools" / "rodada_c0c1.sh").read_text()
    bloco = fonte.split("# --- INICIO PRE-VOO DA CHAVE (#1075) ---", 1)[1]
    bloco = bloco.split("# --- FIM PRE-VOO DA CHAVE (#1075) ---", 1)[0]
    # o `unset` tem de vir DEPOIS da captura e ANTES de qualquer filho
    assert "unset TTS_CHAT_API_KEY" in bloco, "a chave fica exportada para todo filho"
    assert bloco.index("_LLM_K=") < bloco.index("unset TTS_CHAT_API_KEY")
    sobe = fonte.split("sobe() {", 1)[1].split("\n}", 1)[0]
    assert '"${_LLM_K:-' in sobe, "o `sobe` tem de repassar a chave capturada"


def test_exit_de_prevoo_nao_derruba_a_limpeza(tmp_path: Path) -> None:
    """`set -u` + `CABECALHO` criado só no fim do pré-voo: sair ANTES matava o `limpa`.

    O `limpa` lê `$CABECALHO`; qualquer `exit` de pré-voo anterior à criação do
    header (aqui, `--sha` ausente) morria em `parameter not set` no meio da
    limpeza e deixava o `llm_fake_sse.py` órfão na 7898. A célula cobra o
    CONTRATO INTEIRO da limpeza: as linhas finais do `limpa` só saem se ele
    atravessar o `rm` do header.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for nome, corpo in {
        "lsof": "#!/bin/sh\nexit 0\n",       # nada escutando
        "pkill": "#!/bin/sh\nexit 0\n",
        "pgrep": "#!/bin/sh\necho 4242\n",   # 1 uvicorn na 7860
        "curl": "#!/bin/sh\necho 200\n",
    }.items():
        p = bin_dir / nome
        p.write_text(corpo)
        p.chmod(0o755)

    # sem `--sha`: o pré-voo para em "`--sha` é obrigatório"
    r = subprocess.run(
        ["zsh", str(RAIZ / "tools" / "rodada_c0c1.sh"), "--dir", str(tmp_path / "d")],
        env={"PATH": f"{bin_dir}:/opt/homebrew/bin:/usr/bin:/bin",
             "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        capture_output=True, text=True)
    saida = r.stdout + r.stderr
    assert r.returncode == 1, saida
    assert "--sha é obrigatório" in saida
    assert "parameter not set" not in saida, f"o `limpa` morreu no meio:\n{saida}"
    # âncora: as três últimas linhas do `limpa` vêm DEPOIS do `rm` do header
    assert "uvicorn 7860:" in saida, saida
    assert "prod health:" in saida, saida
