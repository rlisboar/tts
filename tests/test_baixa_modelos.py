"""`tools/baixa_modelos.sh`: idempotente, com sha256 e sem rede de verdade (#526).

O contrato do card é que o app NUNCA baixa em runtime e que o peso baixado é
auditável. Aqui o "servidor" é um http.server local (nada de HuggingFace no CI) e
o root é uma cópia pequena de `models/`: dá para exercitar download, verificação,
recusa por divergência e o no-op da segunda rodada.
"""
from __future__ import annotations

import functools
import hashlib
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "baixa_modelos.sh"
ZSH = shutil.which("zsh")

pytestmark = pytest.mark.skipif(ZSH is None, reason="precisa de zsh")

#: mesmo conjunto do `FONTES` do script (os grandes viram arquivinhos de teste)
PESOS = ("models/ecapa-voxceleb/hyperparams.yaml",
         "models/ecapa-voxceleb/embedding_model.ckpt",
         "models/ecapa-voxceleb/mean_var_norm_emb.ckpt",
         "models/ecapa-voxceleb/classifier.ckpt",
         "models/ecapa-voxceleb/label_encoder.txt")
#: revisão que o script fixa — o caminho do espelho tem de ser idêntico ao real
REV = "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class _Espelho:
    """http.server local servindo `<repo>/resolve/<rev>/<arquivo>`."""

    def __init__(self, raiz: Path):
        self.raiz = raiz
        tratador = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(raiz))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), tratador)
        self.httpd.log_message = lambda *a: None  # type: ignore[method-assign]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def fecha(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
def amb(tmp_path: Path):
    """Root com o script + `models/` (vazio) + manifesto dos arquivos pequenos."""
    (tmp_path / "tools").mkdir()
    shutil.copy(SCRIPT, tmp_path / "tools" / "baixa_modelos.sh")
    (tmp_path / "models").mkdir()
    origem = tmp_path / "origem" / "speechbrain" / "spkrec-ecapa-voxceleb" / "resolve" / REV
    origem.mkdir(parents=True)
    linhas = []
    for rel in PESOS:
        fake = origem / Path(rel).name
        fake.write_bytes(f"conteudo de {rel}\n".encode() * 20)
        linhas.append(f"{_sha(fake)}  {rel}")
    # a coorte é gerada: entra no manifesto mas não é baixável (repo "-")
    coorte_fake = tmp_path / "origem" / "coorte.npy"
    coorte_fake.write_bytes(b"\x93NUMPY falso")
    linhas.append(f"{_sha(coorte_fake)}  models/asnorm-coorte-ecapa.npy")
    (tmp_path / "models" / "sha256sums.txt").write_text("\n".join(linhas) + "\n")
    # a coorte já está no lugar (é gerada): o teste da ausência a apaga
    shutil.copy(coorte_fake, tmp_path / "models" / "asnorm-coorte-ecapa.npy")
    espelho = _Espelho(tmp_path / "origem")
    yield {"root": tmp_path, "espelho": espelho}
    espelho.fecha()


def _roda(amb, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([ZSH, "-f", str(amb["root"] / "tools" / "baixa_modelos.sh"), *args],
                          cwd=str(amb["root"]), capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                               "TTS_MODELOS_URL": amb["espelho"].url})


def test_baixa_o_que_falta_e_confere_o_sha(amb):
    r = _roda(amb)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "baixado e conferido" in r.stdout
    assert (amb["root"] / "models" / "ecapa-voxceleb" / "hyperparams.yaml").is_file()


def test_segunda_rodada_nao_rebaixa(amb):
    """Idempotência: com tudo no lugar, a rodada seguinte é no-op."""
    assert _roda(amb).returncode == 0
    r = _roda(amb)
    assert r.returncode == 0
    assert "baixando" not in r.stdout


def test_divergencia_e_erro_e_forca_rebaixa(amb):
    assert _roda(amb).returncode == 0
    alvo = amb["root"] / "models" / "ecapa-voxceleb" / "hyperparams.yaml"
    alvo.write_text("peso trocado\n")

    r = _roda(amb)
    assert r.returncode == 1, r.stdout
    assert "divergente" in r.stdout
    assert alvo.read_text() == "peso trocado\n", "sobrescreveu sem --forca"
    assert "força" not in r.stdout

    r = _roda(amb, "--forca")
    assert r.returncode == 0, r.stdout
    assert alvo.read_text() != "peso trocado\n"


def test_check_nao_baixa_e_lista_o_que_falta(amb):
    r = _roda(amb, "--check")
    assert r.returncode == 0, r.stdout
    assert "faltando" in r.stdout and "baixando" not in r.stdout


def test_coorte_que_falta_nao_e_baixada_e_aponta_o_gerador(amb):
    """O `.npy` da coorte é gerado localmente: o script não inventa download."""
    (amb["root"] / "models" / "asnorm-coorte-ecapa.npy").unlink()
    r = _roda(amb)
    assert r.returncode == 1, r.stdout
    assert "coorte_asnorm.py" in r.stdout


def test_app_nao_baixa_modelo_no_boot():
    """Contrato do card: quem baixa é o script; o boot não."""
    for arq in ("run.sh", "app.py"):
        texto = (REPO / arq).read_text()
        assert "baixa_modelos" not in texto, f"{arq} chama o downloader no boot"
        assert "from_hparams(" not in texto, f"{arq} carrega o extrator baixando sozinho"


#: gerado pelo speechbrain no primeiro load (não é baixável nem versionado)
GERADOS = {"label_encoder.ckpt"}


def test_manifesto_lista_todos_os_pesos_do_repo():
    """Peso sem sha256 no manifesto é peso que ninguém confere."""
    manifesto = (REPO / "models" / "sha256sums.txt").read_text()
    listados = {linha.split("  ", 1)[1] for linha in manifesto.splitlines() if "  " in linha}
    no_disco = {f"models/{p.relative_to(REPO / 'models').as_posix()}"
                for p in (REPO / "models").rglob("*")
                if p.is_file() and p.suffix in {".ckpt", ".yaml", ".txt", ".npy"}
                and p.name not in {"sha256sums.txt"} | GERADOS}
    assert not (no_disco - listados), f"peso fora do manifesto: {sorted(no_disco - listados)}"


def test_ckpt_fica_fora_do_git_e_o_script_materializa():
    """83 MB de um `.ckpt` não podem entrar no índice; quem põe é o script (#526)."""
    pesos = sorted((REPO / "models").rglob("*.ckpt"))
    assert pesos, "nenhum peso no disco — rode tools/baixa_modelos.sh"
    rastreados = subprocess.run(["git", "-C", str(REPO), "ls-files", "models"],
                                capture_output=True, text=True, check=True).stdout
    assert not [p for p in pesos if f"models/{p.relative_to(REPO / 'models')}" in rastreados]
    for p in pesos:
        ignorado = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", str(p)])
        assert ignorado.returncode == 0, f"{p.name} não está no .gitignore"
