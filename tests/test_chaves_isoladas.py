"""#435: a suíte NÃO encosta nos arquivos de estado do dono.

O `import app` lê o `.apikeys.json` REAL; a partir daí, qualquer `_save_apikeys()`
(toda criação/rotação/deleção de chave nos testes) grava o arquivo apontado por
`app.APIKEYS_PATH` — e `_sync_legacy_apikey_file()` grava o `.apikey` também. Sem
isolamento, uma rodada de testes reescrevia o arquivo do dono, plantava chaves de
teste com poder de ADMIN (as `rot` do #368, achado do PM) e, no teardown, um
snapshot antigo restaurado por outro processo pendente podia DESFAZER uma rotação
feita pelo dono durante a rodada.

O isolamento é de SESSÃO e mora no `conftest` (`_estado_isolado`): `APIKEYS_PATH`,
`LEGACY_APIKEY_PATH`, `SPEAKER_PATH` e `SETTINGS_PATH` apontam para cópia em tmp.

Duas pontas, no formato do repo (controle + contraprova):
1. aqui dentro: os atributos do app NÃO apontam para o repo;
2. num FILHO de verdade: rodar um recorte que cria chave e comparar o hash do
   arquivo do dono antes/depois — sem o isolamento o hash muda.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parent.parent

#: arquivos de estado do dono que a suíte não pode ler nem escrever
DO_DONO = (".apikeys.json", ".apikey", "settings.json", ".speaker-profiles.json")


def _hash(caminho: Path) -> str | None:
    try:
        return hashlib.sha256(caminho.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _hashes_do_dono() -> dict:
    return {nome: _hash(BASE / nome) for nome in DO_DONO}


def test_app_aponta_para_copia_em_tmp(monkeypatch):
    """O app tem de estar com os caminhos em tmp, não nos do repo."""
    import app

    for atributo, arquivo in (("APIKEYS_PATH", ".apikeys.json"),
                              ("LEGACY_APIKEY_PATH", ".apikey"),
                              ("SPEAKER_PATH", ".speaker-profiles.json"),
                              ("SETTINGS_PATH", "settings.json")):
        atual = Path(getattr(app, atributo))
        assert atual != BASE / arquivo, f"{atributo} aponta para o arquivo do dono"
        assert str(atual).startswith(("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")), atual


def test_gravar_chave_nao_muda_o_arquivo_do_dono():
    """Escrever chave no processo de teste não pode tocar o arquivo real."""
    import app

    antes = _hashes_do_dono()
    with app._apikeys_lock:
        app._apikeys.setdefault("keys", []).append({
            "id": "isolada01", "name": "pytest-isolada", "secret": "sk-isolada-123",
            "created_at": "2026-01-01 00:00:00", "role": "use"})
        app._save_apikeys()
    try:
        assert _hashes_do_dono() == antes, "gravação de teste mexeu no estado do dono"
    finally:
        with app._apikeys_lock:
            app._apikeys["keys"] = [k for k in app._apikeys["keys"]
                                    if k["id"] != "isolada01"]
            app._save_apikeys()


def test_rodada_filha_nao_muda_o_hash_do_arquivo_do_dono():
    """Filho de verdade: uma rodada que cria chave tem de deixar o hash igual.

    É o aceite do #435 em forma de teste — e a mordida: sem o isolamento de sessão
    no `conftest`, o filho grava no arquivo do dono e o hash muda."""
    antes = _hashes_do_dono()
    # o alvo é ESTE arquivo: fora do `test_api.py` não existe a fixture por teste que
    # também isola — aqui quem está sob prova é o isolamento de SESSÃO do conftest.
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_chaves_isoladas.py",
                        "-q", "-k", "gravar", "-p", "no:cacheprovider"],
                       cwd=str(BASE), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-1500:]
    assert _hashes_do_dono() == antes, (
        "a rodada do filho mexeu no estado do dono — o isolamento do conftest caiu?")


# --- #535/#601: `import app` não escreve o espelho legado do dono
# A suíte roda com `TTS_*` de caminho neutralizados; a rodada de suítes `.sh` não
# tem esse conftest. O espelho era escrito SEMPRE (todo import mexia no arquivo do
# dono); depois do #601 nem na instalação nova ele é criado — a materialização é a
# primeira escrita explícita ou `POST /api/apikeys/persistir`.
def test_import_nao_reescreve_o_legado_quando_o_conteudo_e_o_mesmo(tmp_path):
    env = {**os.environ, "TTS_APIKEYS_PATH": str(tmp_path / "apikeys.json"),
           "TTS_LEGACY_APIKEY_PATH": str(tmp_path / "apikey"),
           "TTS_SETTINGS_PATH": str(tmp_path / "settings.json"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_ROD_API_KEY", "TTS_ADMIN_KEY"):
        env.pop(var, None)
    import_ = [sys.executable, "-c", "import app"]
    subprocess.run(import_, cwd=str(BASE), env=env, check=True, capture_output=True)
    legado = tmp_path / "apikey"
    # instalação nova: o import gerou a chave padrão na RAM e NÃO a materializou
    assert not legado.exists() and not (tmp_path / "apikeys.json").exists()
    # com o espelho já gravado, importar (2×) não pode tocar no arquivo
    legado.write_text("sk-sintetica\n")
    antes = legado.stat().st_mtime_ns
    for _ in range(2):
        subprocess.run(import_, cwd=str(BASE), env=env, check=True, capture_output=True)
    assert legado.stat().st_mtime_ns == antes, "o import reescreveu o espelho"


# --- #593: `import app` NÃO grava o `settings.json` (nem para materializar default)
# O launcher só reaponta `TTS_SETTINGS_PATH` DEPOIS de importar o app; qualquer
# escrita no import acerta o arquivo do DONO. Faltando chave default no disco, o
# import mesclava na RAM e chamava `_save_settings()` — reescrevia tudo (ordem
# canônica, chave de volta ao default) antes de alguém redirecionar o caminho.
def test_import_nao_grava_settings_que_o_dono_nao_gravou(tmp_path):
    """Settings sem as chaves default: sha256, mtime e BYTES iguais depois do import.

    Quem persistir o default é a 1ª gravação explícita (POST /api/settings), não o
    import. `speech_queue`/`speech_queue_gap_s` são o caso real: são exatamente as
    chaves que disparavam o save."""
    settings = tmp_path / "settings.json"
    original = '{"speed": 1.15, "idle_unload_minutes": 3}'
    settings.write_text(original)
    assert "speech_queue" not in original
    antes_hash, antes_mtime = _hash(settings), settings.stat().st_mtime_ns
    env = {**os.environ, "TTS_APIKEYS_PATH": str(tmp_path / "apikeys.json"),
           "TTS_LEGACY_APIKEY_PATH": str(tmp_path / "apikey"),
           "TTS_SETTINGS_PATH": str(settings),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_ROD_API_KEY", "TTS_ADMIN_KEY"):
        env.pop(var, None)
    subprocess.run([sys.executable, "-c", "import app"], cwd=str(BASE), env=env,
                   check=True, capture_output=True)
    assert settings.read_text() == original, "o import reescreveu o settings do dono"
    assert _hash(settings) == antes_hash and settings.stat().st_mtime_ns == antes_mtime
    # o valor não se perde: o default segue VIGENTE na RAM mesmo sem ir ao disco
    vivo = subprocess.run(
        [sys.executable, "-c", "import app; print(app._settings['speech_queue'])"],
        cwd=str(BASE), env=env, check=True, capture_output=True, text=True)
    assert vivo.stdout.strip() == "True"


def test_espelho_legado_e_reparado_mesmo_com_arquivo_fora_do_utf8(tmp_path, monkeypatch):
    """.apikey editado à mão em latin-1 não pode desligar o reparo em silêncio.

    `read_text()` sem `errors=` levanta `UnicodeDecodeError`, que NÃO é `OSError`:
    escapava para o `except` externo e o espelho ficava com a chave velha (ou lixo)
    para sempre. Lido com `errors="replace"`, o conteúdo diverge e ele é reescrito."""
    import app

    legado = tmp_path / "apikey"
    legado.write_bytes(b"chave-antiga-\xe7\xe3o\n")     # latin-1, não é UTF-8 válido
    monkeypatch.setattr(app, "LEGACY_APIKEY_PATH", legado)
    monkeypatch.setattr(app, "_apikeys", {"enabled": True, "keys": [
        {"id": "a", "name": "pytest", "secret": "sk-nova", "role": "admin"}]})
    app._sync_legacy_apikey_file()
    assert legado.read_text().strip() == "sk-nova", "o espelho não foi reparado"


# --- #601: o import também não grava os arquivos de CHAVE --------------------
# Mesma família do #593 (`_save_settings` no import). O launcher importa antes de
# reapontar `TTS_APIKEYS_PATH`, então importar não pode reescrever `.apikey` (o
# espelho) nem migrar `.apikeys.json` — a migração fica na RAM até a 1ª escrita
# explícita (criar/rotacionar/apagar chave) ou `POST /api/apikeys/persistir`.
def test_import_nao_grava_arquivos_de_chave(tmp_path):
    """.apikeys.json vazio + .apikey presente: o import migra, mas NÃO grava.

    Esse estado é o que mais disparava escrita (a migração salvava os dois
    arquivos). Depois do import os dois têm de estar idênticos em bytes, sha256 e
    mtime — e a chave lida do `.apikey` tem de autenticar mesmo sem ir ao disco."""
    apikeys = tmp_path / "apikeys.json"
    apikeys.write_text('{"enabled": true, "keys": []}')
    legado = tmp_path / "apikey"
    legado.write_text("sk-legado-sintetica\n")
    antes = {p.name: (_hash(p), p.stat().st_mtime_ns, p.read_bytes())
             for p in (apikeys, legado)}
    env = {**os.environ, "TTS_APIKEYS_PATH": str(apikeys),
           "TTS_LEGACY_APIKEY_PATH": str(legado),
           "TTS_SETTINGS_PATH": str(tmp_path / "settings.json"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("TTS_ROD_API_KEY", "TTS_ADMIN_KEY"):
        env.pop(var, None)
    subprocess.run([sys.executable, "-c", "import app"], cwd=str(BASE), env=env,
                   check=True, capture_output=True)
    for p in (apikeys, legado):
        assert (_hash(p), p.stat().st_mtime_ns, p.read_bytes()) == antes[p.name], \
            f"o import reescreveu {p.name}"
    # a migração VALE na RAM: sem isso "não gravar" seria "não migrar"
    autentica = subprocess.run(
        [sys.executable, "-c",
         "import app; print(app._key_is_valid('sk-legado-sintetica'))"],
        cwd=str(BASE), env=env, check=True, capture_output=True, text=True)
    assert autentica.stdout.strip() == "True"


# --- #535: a guarda NOMEIA quem escreveu no arquivo do dono ───────────────────
# Sem atribuição, "o settings do dono mudou" é fato sem autor (foi o que travou a
# investigação de 01:24: o qa já rodava isolado e ninguém sabia a origem).
def test_guarda_nomeia_quem_tocou_o_arquivo_do_dono(monkeypatch, tmp_path):
    """O alvo é o "dono" — e a célula usa um dono FALSO, sem tocar a raiz principal.

    Fotografar o tmp fazia todo teste que grava settings virar "escrita no dono"
    (37 linhas de ruído, #685). E mexer no arquivo REAL (mexer no mtime e na flag `uchg`) é mutação de
    dado do dono: aqui `TTS_DONO_DIR` aponta para um tmp com um `settings.json`
    sintético e a raiz não é tocada (#710)."""
    import conftest

    dono = tmp_path / "dono"
    dono.mkdir()
    (dono / "settings.json").write_text('{"model": "sintetico", "pre_prompt": ""}')
    monkeypatch.setenv("TTS_DONO_DIR", str(dono))
    antes = conftest._impressao_do_dono()
    (dono / "settings.json").write_text('{}')
    conftest._atribuir_escritas(antes, "tests/test_exemplo.py::test_x")
    assert "tests/test_exemplo.py::test_x escreveu settings.json" in conftest._escritas_no_dono


def test_guarda_nao_acusa_escrita_no_tmp_da_sessao():
    """Escrever no tmp é o comportamento CERTO da sessão: não pode virar aviso."""
    import conftest

    import app

    conftest._escritas_no_dono.clear()
    antes = conftest._impressao_do_dono()
    app._save_settings()                          # grava no caminho isolado (tmp)
    os.utime(app.SETTINGS_PATH)
    conftest._atribuir_escritas(antes, "tests/test_exemplo.py::test_tmp")
    assert conftest._escritas_no_dono == [], conftest._escritas_no_dono
    assert str(app.SETTINGS_PATH) != str(conftest._raiz_do_dono() / "settings.json")


def test_guarda_nao_acusa_quando_nada_muda():
    import conftest

    antes = conftest._impressao_do_dono()
    assert conftest._diferencas(antes, conftest._impressao_do_dono()) == []
