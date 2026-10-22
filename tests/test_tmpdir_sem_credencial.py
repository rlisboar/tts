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
def test_basetemp_nao_carrega_secret_do_dono(request):
    import json as _json
    import pathlib

    import app

    raiz = request.config.option.basetemp
    assert raiz, "o conftest deveria ter fixado um basetemp por processo (#406)"
    dono = app.BASE / ".apikeys.json"
    if not dono.exists():
        pytest_skip("sem .apikeys.json do dono nesta máquina")
    reais = {k.get("secret") for k in _json.loads(dono.read_text()).get("keys", [])
             if k.get("secret")}
    if not reais:
        pytest_skip("arquivo do dono sem chave")

    achados = []
    for p in pathlib.Path(raiz).rglob("*"):
        if p.name not in (".apikeys.json", ".apikey") or not p.is_file():
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
