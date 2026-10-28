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


# --- #535: `import app` reescrevia o `.apikey` do dono (mtime novo, conteúdo igual)
# A suíte roda com `TTS_*` de caminho neutralizados; a rodada de suítes `.sh` não
# tem esse conftest. O espelho legado era escrito SEMPRE, então todo import mexia
# no arquivo do dono. Agora ele só grava quando o conteúdo muda.
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
    assert legado.exists() and legado.read_text().strip(), "o legado nem foi criado"
    antes = legado.stat().st_mtime_ns
    subprocess.run(import_, cwd=str(BASE), env=env, check=True, capture_output=True)
    assert legado.stat().st_mtime_ns == antes, "o 2º import reescreveu o mesmo conteúdo"


# --- #535: a guarda NOMEIA quem escreveu no arquivo do dono ───────────────────
# Sem atribuição, "o settings do dono mudou" é fato sem autor (foi o que travou a
# investigação de 01:24: o qa já rodava isolado e ninguém sabia a origem).
def test_guarda_nomeia_quem_tocou_o_arquivo_do_dono():
    import conftest

    import app

    antes = conftest._impressao_do_dono()
    os.utime(app.BASE / "settings.json")          # mesmo conteúdo, mtime novo
    conftest._atribuir_escritas(antes, "tests/test_exemplo.py::test_x")
    assert "tests/test_exemplo.py::test_x escreveu settings.json" in conftest._escritas_no_dono


def test_guarda_nao_acusa_quando_nada_muda():
    import conftest

    antes = conftest._impressao_do_dono()
    assert conftest._diferencas(antes, conftest._impressao_do_dono()) == []
