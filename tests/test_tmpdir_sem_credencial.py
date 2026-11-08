"""#224: a suíte não pode deixar o `chat_api_key` do dono em TMPDIR.

O `conftest` COPIA o `settings.json` do dono para o basetemp (é o vetor ARQUIVO do
#206: o caminho é isolado, o conteúdo atravessa). O default do pytest
(`tmp_path_retention_policy = all`) retinha as últimas 3 sessões, então CADA
rodada deixava uma cópia com a chave em texto claro em
`/private/tmp/pytest-of-<user>/pytest-N/` — sobrevivendo à suíte e à sessão do
agente, num diretório que o macOS só limpa no reboot (achado da varredura do
api-backend: 28 `settings.json` com chave, nenhum em uso).

O fix tem DUAS pontas, e este arquivo cobra as duas:

1. `pytest.ini` → `tmp_path_retention_policy = failed`: sessão VERDE não fica
   (ninguém vai inspecionar), VERMELHA fica para depuração.
2. teardown de SESSÃO no `conftest`: apaga a cópia que ele mesmo criou. É o que
   cobre o run VERMELHO (quando o basetemp É retido) e o filho com `--basetemp`
   (onde a política do ini nem se aplica).

CONTROLE em duas pontas, no formato da casa (`tests/test_ambiente_neutro.py`): o
pai lança um filho com `--basetemp` PRÓPRIO e o filho cobra o que só ele vê — que
a cópia EXISTE, tem a chave e mora DENTRO daquele basetemp (senão o controle do
pai passaria vazio). O pai cobra o que só ele vê: a cópia não sobreviveu à
sessão e o basetemp ficou em paz. O `--basetemp` é o que separa as duas pontas:
com ele o pytest NÃO apaga o basetemp no fim (nem verde), então o que sumir da
cópia foi o teardown — sem ele o teste não distinguiria teardown de política.

O arquivo "do dono" do controle é SINTÉTICO e chega pelo plugin `_estado_hostil`
(o mesmo do #206, que aponta `app.SETTINGS_PATH` para ele antes do conftest
copiar): o do dono pode não existir na máquina (é gitignored) ou vir sem chave, e
o controle não pode depender disso.
"""
import json
import re
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FILHO = "QA224_FILHO"
RAIZ_FILHO = "QA224_RAIZ"
CHAVE = "chave-do-controle-224"
# contrato do plugin do #206: `-p _estado_hostil` + este env apontam o settings
# "do dono" para o arquivo do controle (o plugin é no-op sem o env).
ARQ_PLUGIN = "_estado_hostil"
ARQ_ENV = "QA206_SETTINGS"


def test_ini_nao_retem_sessao_verde(request):
    """A decisão registrada no `pytest.ini` (o default `all` era o que vazava)."""
    assert request.config.getini("tmp_path_retention_policy") == "failed", (
        "a sessão verde voltaria a ser retida com a cópia da chave dentro")


def test_copia_da_chave_nao_sobrevive_a_sessao(tmp_path):
    raiz = tmp_path / "base"
    if os.environ.get(FILHO):
        return _cobra_no_filho()
    # settings "do dono" do controle: o real, se existir, com a chave forçada.
    origem = BASE / "settings.json"
    dados = json.loads(origem.read_text()) if origem.exists() else {}
    dados["chat_api_key"] = CHAVE
    hostil = tmp_path / "hostil.json"
    hostil.write_text(json.dumps(dados, ensure_ascii=False))
    env = {**os.environ, FILHO: "1", RAIZ_FILHO: str(raiz), ARQ_ENV: str(hostil),
           "PYTHONPATH": os.pathsep.join(
               [str(BASE / "tests"), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-q",
         "--basetemp", str(raiz), "-p", ARQ_PLUGIN],
        cwd=str(BASE), env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"suíte do filho vermelha:\n{r.stdout[-3000:]}"
    assert raiz.is_dir(), (
        "o teardown apagou o basetemp INTEIRO — era para sair só a cópia")
    restantes = sorted(str(p.relative_to(raiz))
                       for p in raiz.rglob("settings.json"))
    assert not restantes, (
        f"cópia do settings.json (com a chave) sobreviveu à sessão: {restantes}")


def _cobra_no_filho():
    """Dentro do filho: a cópia do dono existe, tem a chave e está no basetemp."""
    import app

    raiz = Path(os.environ[RAIZ_FILHO]).resolve()
    copia = app.SETTINGS_PATH
    assert copia.exists(), "o conftest não criou a cópia do settings.json"
    assert raiz in copia.parents, (
        f"a cópia saiu do basetemp que o pai vai varrer: {copia} (raiz {raiz})")
    assert json.loads(copia.read_text()).get("chat_api_key") == CHAVE, (
        "a cópia não trouxe a chave — o controle do pai seria vazio")

def test_apikeys_do_processo_tambem_vive_no_basetemp(tmp_path):
    """Regra do PM (2026-10-21): suíte não mexe em estado do dono, nem "move e
    devolve". O `.apikeys.json` era REESCRITO a cada sessão (toda gravação passa
    por `_save_apikeys`); agora o processo aponta para uma cópia em tmp e o
    arquivo do dono fica byte a byte igual."""
    import app

    original = app.BASE / ".apikeys.json"
    assert app.APIKEYS_PATH != original, "o processo está escrevendo no arquivo do dono"
    antes = original.read_bytes() if original.exists() else None

    app._apikeys.setdefault("keys", []).append(
        {"id": "zzregressao", "name": "pytest-regressao", "secret": "s" * 8,
         "created_at": "2026-01-01", "role": None})
    app._save_apikeys()

    assert app.APIKEYS_PATH.exists(), "gravou fora do tmp — o arquivo esperado não apareceu"
    depois = original.read_bytes() if original.exists() else None
    assert depois == antes, "a gravação da suíte alterou o .apikeys.json do dono"
    assert "pytest-regressao" not in (depois or b"").decode()


# ── #435: o basetemp não pode carregar o SECRET do dono ────────────────────────
# A cópia do `.apikeys.json` para tmp resolvia "não escrever no arquivo dele", mas
# espalhava o SEGREDO real por todo basetemp retido (medido: 48 sobras, 38 com
# secret). A sessão agora semeia chave SINTÉTICA; esta guarda cobra que nenhum
# arquivo de chave sob o basetemp desta sessão traga secret que também exista no
# arquivo do dono — inclusive numa rodada VERMELHA, em que o basetemp é retido.
#: os QUATRO arquivos de estado do dono (o app grava nos quatro)
ARQUIVOS_DO_DONO = (".apikeys.json", ".apikey", "settings.json",
                    ".speaker-profiles.json")


def test_os_quatro_arquivos_de_estado_apontam_para_tmp(request):
    """Isolamento de SESSÃO dos quatro caminhos — nenhum pode ficar no repo.

    Cobre o que a guarda de hash não vê: `settings.json` e os perfis de locutor
    continuam indo por CÓPIA (são conteúdo, não credencial), mas o processo tem de
    estar escrevendo em tmp, senão a próxima gravação cai no arquivo do dono.
    """
    import pathlib

    import app

    pares = (("APIKEYS_PATH", ".apikeys.json"), ("LEGACY_APIKEY_PATH", ".apikey"),
             ("SETTINGS_PATH", "settings.json"), ("SPEAKER_PATH", ".speaker-profiles.json"))
    for atributo, nome in pares:
        atual = pathlib.Path(getattr(app, atributo))
        assert atual != app.BASE / nome, f"{atributo} aponta para o arquivo do dono"
        assert str(atual).startswith(("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")), atual


def test_outputs_do_processo_e_do_ambiente_apontam_para_tmp(request):
    """`OUTPUTS_DIR` em tmp nas DUAS pontas (#542).

    A RAM só vale neste processo: worker de job e subprocesso recalculam o caminho
    no import. Sem a variável, os testes que gravam áudio de verdade voltavam a
    deixar `t<hash>.wav` no `outputs/` do dono (3.510 medidos) — e a guarda de
    sessão do `conftest` só acusa no FIM da rodada.
    """
    import pathlib

    import app

    atual = pathlib.Path(app.OUTPUTS_DIR)
    assert atual != app.BASE / "outputs", "OUTPUTS_DIR aponta para o diretório do dono"
    assert os.environ.get("TTS_OUTPUTS_DIR") == str(atual), (
        "o caminho não foi para o ambiente: subprocesso cairia no diretório do dono")


def test_basetemp_nao_carrega_secret_do_dono(request):
    import json as _json
    import pathlib

    import app

    raiz = request.config.option.basetemp
    assert raiz, "o conftest deveria ter fixado um basetemp por processo (#406)"
    # Referência: o arquivo de chaves EM USO pela sessão (o conftest manda para
    # tmp). Sem ele, o do dono; sem nenhum, não há segredo para vazar — num
    # worktree, que nasce sem estado, os dois podem faltar (#659).
    dono = pathlib.Path(app.APIKEYS_PATH)
    if not dono.exists():
        dono = app.BASE / ".apikeys.json"
    if not dono.exists():
        pytest_skip("sem arquivo de chaves nesta sessão")
    reais = {k.get("secret") for k in _json.loads(dono.read_text()).get("keys", [])
             if k.get("secret")}
    if not reais:
        pytest_skip("arquivo do dono sem chave")

    achados = []
    proprio = dono.parent.resolve()
    for p in pathlib.Path(raiz).rglob("*"):
        if p.name not in (".apikeys.json", ".apikey") or not p.is_file():
            continue
        if p.resolve().parent == proprio:      # o estado do app não é "cópia"
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        if any(s in txt for s in reais):
            achados.append(str(p))
    assert not achados, (
        f"{len(achados)} arquivo(s) de chave com SECRET REAL do dono sob o basetemp "
        f"(semear sintética em vez de copiar): {achados[:3]}")


def pytest_skip(motivo: str) -> None:
    import pytest as _pytest
    _pytest.skip(motivo, allow_module_level=False)


# ── #1018: quem ainda LÊ o estado real do dono ────────────────────────────────
# A lista mora em `tests/_dono_leem.py` (fora do `test_*`): os próprios trechos
# listados casariam com a varredura se ficassem aqui. O alvo é ela ir a zero.
from _dono_leem import LEEM_O_DONO  # noqa: E402

#: `BASE / "<estado>"` em Python e `$RAIZ/voices` no shell
_LEITURA_DO_DONO = re.compile(
    r"""(?:app\.)?BASE\s*/\s*["'](?:settings\.json|\.apikeys\.json|\.apikey|
                                     \.speaker-profiles\.json|voices)["']""",
    re.X)
_LEITURA_SHELL = re.compile(r"\$(?:RAIZ|AQUI|BASE)/voices(?![\w-])")


def _arquivos_que_leem_o_dono() -> dict[str, set[str]]:
    """{arquivo: {trechos}} que montam caminho para o estado REAL do dono."""
    achados: dict[str, set[str]] = {}
    for arq in sorted((BASE / "tests").glob("*")):
        if arq.name == "conftest.py" or arq.name.startswith("_") \
                or arq.suffix not in (".py", ".sh"):
            continue
        padrao = _LEITURA_SHELL if arq.suffix == ".sh" else _LEITURA_DO_DONO
        trechos = {linha.strip() for linha in arq.read_text(errors="replace").splitlines()
                   if padrao.search(linha) and not linha.lstrip().startswith("#")}
        if trechos:
            achados[arq.name] = trechos
    return achados


def test_nenhum_teste_nao_listado_le_o_estado_real_do_dono():
    """Leitor NOVO do dono tem de entrar na lista (ou virar sintético) — #1018.

    Bidirecional: trecho da lista que não casa mais também falha, senão a lista
    vira decoração e ninguém percebe que o alvo é zero."""
    achados = _arquivos_que_leem_o_dono()
    novos = {arq: sorted(trechos - LEEM_O_DONO.get(arq, set()))
             for arq, trechos in achados.items()}
    novos = {a: t for a, t in novos.items() if t}
    assert not novos, (
        "teste(s) lendo o estado REAL do dono (regra #728 — use TTS_*_PATH/tmp_path "
        f"ou justifique em tests/_dono_leem.py): {novos}")
    obsoletos = {arq: sorted(trechos - achados.get(arq, set()))
                 for arq, trechos in LEEM_O_DONO.items()}
    obsoletos = {a: t for a, t in obsoletos.items() if t}
    assert not obsoletos, (
        f"tests/_dono_leem.py lista trecho(s) que não existem mais — remova: {obsoletos}")
