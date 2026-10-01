"""Varredura da casa: arquivo temporário em /tmp tem de ser ÚNICO por execução.

Classe de bug que já mordeu a equipe quatro vezes (#12, #83, #224, #240): caminho FIXO
em /tmp compartilhado entre processos — dois scripts ao mesmo tempo reescrevem/apagam o
mesmo arquivo e um lê o do outro. No #240 o efeito foi barrar o commit de um colega com
falso vermelho que MUDava de rodada em rodada (o hook roda a suíte inteira).

Aqui a asserção é ESTÁTICA (não depende de rodar nada em paralelo): para cada literal
`/tmp/<algo>` nos scripts/testes da casa, ou o caminho carrega um token por execução
(`$$`, `$RUN`, `$SUF`, `mktemp`, `XXXXXX`, `{os.getpid()}`), ou está na lista de
COMPARTILHADOS-POR-DESENHO abaixo — cada entrada com o porquê.

O par funcional deste teste é o `test_dois_deploys_em_paralelo_nao_disputam_o_preview`
(o caso que originou a varredura).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Arquivos varríveis: scripts e testes executáveis da casa (não docs, não vendor).
ALVOS = sorted(
    [p for p in (REPO / "remote").glob("*.sh")]
    + [p for p in (REPO / "tests").glob("*.sh")]
    + [p for p in (REPO / "tests").glob("*.py")]
    + [p for p in (REPO / "evidence").glob("*.sh")]
    + [p for p in (REPO / "evidence").glob("*.py")]
    + [REPO / n for n in ("run.sh", "tunnel.sh", "cloudflare.sh", "TTS-STUDIO.command",
                          "smoke_worker_persist.py")]
)

# Compartilhados POR DESENHO: dois processos DEVEM enxergar o mesmo arquivo.
COMPARTILHADOS = {
    "tts-rod-modelo.lock": "mutex de modelo entre processos (serial.sh/serial_lock.sh)",
    "tts-studio.log": "log do app — um por máquina (run.sh/TTS-STUDIO.command)",
    "tts-tunnel.log": "log do túnel — um por máquina (tunnel.sh)",
    "dsh4a-trace.on": "handshake do probe com o app: a PRESENÇA do arquivo liga o trace",
    "dsh4a-trace.jsonl": "trace escrito pelo app, lido pelo probe (doc-referenciado)",
    "dsh-home": "DSH_HOME (cache de sessões) — compartilhar é o ponto",
    "pytest-of": "diretório do próprio pytest (o sufixo -<user>/pytest-N é dele)",
    "bak": "receita do tests/limpa_bytecode.sh (só comentário, cópia manual)",
    "deploy-mini-requirements-preview": "nome-base: no script leva -$$; nos scripts de mordida é a forma quebrada DE PROPÓSITO",
    "dsh4b-probe.py": "probe CARREGADO pelo app a partir desse caminho (protocolo do #94/#246): um por vez",
    "dsh4b-wire.py": "idem, variante wire",
    "medir_aceite_eco.py": "receita manual (comentário): script solto em /tmp, rodado à mão",
    "nome-antigo.log": "literal de exemplo no docstring do próprio teste da varredura",
}

# `os.environ` entra porque a linha do fallback lê a variável que a suíte exporta
# com sufixo por execução (`... or "/tmp/nome-antigo.log"`).
TOKEN_POR_EXECUCAO = re.compile(
    r"\$\$|\$RUN|\$\{RUN\}|\$SUF|\$T\b|XXXXXX|\{os\.getpid\(\)\}|mktemp|os\.environ")
LITERAL = re.compile(r"/tmp/([A-Za-z0-9._-]+)")


def _achados() -> list[tuple[Path, int, str, str]]:
    fora = []
    for arq in ALVOS:
        if not arq.exists():
            continue
        for n, linha in enumerate(arq.read_text(errors="replace").splitlines(), 1):
            for m in LITERAL.finditer(linha):
                fora.append((arq, n, m.group(1).rstrip("-"), linha.strip()))
    return fora


def test_todo_tmp_da_casa_e_unico_por_execucao_ou_compartilhado_por_desenho():
    problemas = []
    for arq, linha, nome, texto in _achados():
        if nome in COMPARTILHADOS:
            continue
        if TOKEN_POR_EXECUCAO.search(texto):
            continue
        problemas.append(f"{arq.relative_to(REPO)}:{linha}  /tmp/{nome}  ({texto[:70]})")
    assert not problemas, (
        "caminho FIXO em /tmp (classe #12/#83/#224/#240) — use mktemp/$$ ou justifique em "
        "COMPARTILHADOS:\n  " + "\n  ".join(problemas))


def test_allowlist_nao_apodrece():
    """Cada entrada de COMPARTILHADOS tem de continuar existindo em algum arquivo —
    senão a lista vira desculpa para caminho fixo que ninguém usa."""
    achados = {nome for _, _, nome, _ in _achados()}
    for nome in COMPARTILHADOS:
        assert nome in achados, f"COMPARTILHADOS cita /tmp/{nome}, que não aparece mais"


def test_varredura_alcanca_os_alvos_esperados():
    """Se um glob quebrar, a varredura passaria vazia e o teste acima seria mentira."""
    nomes = {p.name for p in ALVOS}
    assert {"deploy_mini.sh", "serial.sh", "223-verifica.sh"} <= nomes
    assert len(_achados()) >= 8


def test_os_consertos_do_240_continuam_de_pe():
    """Regressão direta do achado que originou a varredura."""
    script = (REPO / "remote" / "deploy_mini.sh").read_text()
    assert "/tmp/deploy-mini-requirements-preview-$$" in script
    assert "TTS_MINI_PREVIEW" in script


@pytest.mark.parametrize("arq", ["evidence/202-gate.sh", "evidence/225-celulas.sh",
                                 "evidence/225-extra.sh", "evidence/179-controle.sh",
                                 "evidence/223-verifica.sh", "evidence/232-mordidas.sh",
                                 "evidence/246-verifica.sh"])
def test_scripts_consertados_tem_run_por_execucao(arq):
    texto = (REPO / arq).read_text()
    assert 'RUN="$$"' in texto, f"{arq} perdeu o RUN por execução"
