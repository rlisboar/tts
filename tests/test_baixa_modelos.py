"""`tools/baixa_modelos.sh`: idempotente, com sha256 e sem rede de verdade (#526).

O contrato do card é que o app NUNCA baixa em runtime e que o peso baixado é
auditável. Aqui o "servidor" é um http.server local (nada de HuggingFace no CI) e
o root é uma cópia pequena de `models/`: dá para exercitar download, verificação,
recusa por divergência e o no-op da segunda rodada.
"""
from __future__ import annotations

import functools
import hashlib
import re
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

#: mesmo conjunto do `FONTES` do script (os grandes viram arquivinhos de teste) e
#: o (repo, revisão) de cada um — o caminho do espelho tem de ser idêntico ao real
_ECAPA = ("speechbrain/spkrec-ecapa-voxceleb", "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286")
PESOS = (("models/ecapa-voxceleb/hyperparams.yaml", *_ECAPA),
         ("models/ecapa-voxceleb/embedding_model.ckpt", *_ECAPA),
         ("models/ecapa-voxceleb/mean_var_norm_emb.ckpt", *_ECAPA),
         ("models/ecapa-voxceleb/classifier.ckpt", *_ECAPA),
         ("models/ecapa-voxceleb/label_encoder.txt", *_ECAPA),
         ("models/mossformer2-ss-16k/last_best_checkpoint.pt",
          "alibabasglab/MossFormer2_SS_16K", "407cb030cd66340918ebb6c8cc63b18f8592cdbe"),
         ("models/dfn3-onnx/deepfilter.onnx",
          "soniqo/DeepFilterNet3-ONNX", "63d8ba442ba900143c468b798e94a04009b2f0c9"),
         ("models/dfn3-onnx/config.json",
          "soniqo/DeepFilterNet3-ONNX", "63d8ba442ba900143c468b798e94a04009b2f0c9"),
         # `repo` = "url": o script baixa da URL literal (o wheel do PyPI); o espelho
         # local substitui a URL no manifesto
         # #657: marca d'água (o manifesto confere sha256 como os demais)
         ("models/audioseal/gerador.pt", "facebook/audioseal", "main"),
         ("models/audioseal/detector.pt", "facebook/audioseal", "main"),
         ("models/dnsmos/speechmos-0.0.1.1-py3-none-any.whl", "url", "-"))
#: membros extraídos do wheel (DERIVADOS `zip:`) e o nome deles dentro do pacote
WHEEL_MEMBROS = ("speechmos/dnsmos_models/sig_bak_ovr.onnx",
                 "speechmos/dnsmos_models/model_v8.onnx")


def _sha_bytes(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


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
    espelho = _Espelho(tmp_path / "origem")
    # o script é a fonte das URLs: a cópia aponta a URL `url:` do wheel para o
    # espelho local (nada de PyPI no CI)
    copia = (tmp_path / "tools" / "baixa_modelos.sh")
    copia.write_text(re.sub(
        r"url:https://files\.pythonhosted\.org/\S+",
        lambda m: f"url:{espelho.url}/url/{m.group(0).rsplit('/', 1)[-1]}",
        SCRIPT.read_text()))
    (tmp_path / "models").mkdir()
    linhas = []
    for rel, repo, rev in PESOS:
        if repo == "url":
            origem = tmp_path / "origem" / "url"
            origem.mkdir(parents=True, exist_ok=True)
            fake = origem / Path(rel).name
            if fake.suffix == ".whl":       # wheel de mentira, mas ZIP de verdade
                import zipfile
                with zipfile.ZipFile(fake, "w") as z:
                    for membro in WHEEL_MEMBROS:
                        z.writestr(membro, f"conteudo de {membro}\n" * 20)
            else:
                fake.write_bytes(f"conteudo de {rel}\n".encode() * 20)
            linhas.append(f"{_sha(fake)}  {rel}")
            continue
        origem = tmp_path / "origem" / repo / "resolve" / rev
        origem.mkdir(parents=True, exist_ok=True)
        fake = origem / Path(rel).name
        fake.write_bytes(f"conteudo de {rel}\n".encode() * 20)
        linhas.append(f"{_sha(fake)}  {rel}")
    # o `label_encoder.ckpt` é DERIVADO do `.txt` (mesmo sha256) e não é baixável
    txt = tmp_path / "origem" / _ECAPA[0] / "resolve" / _ECAPA[1] / "label_encoder.txt"
    linhas.append(f"{_sha(txt)}  models/ecapa-voxceleb/label_encoder.ckpt")
    # os membros do wheel são DERIVADOS dele (`zip:`) — mesmo sha256 dos membros
    with __import__("zipfile").ZipFile(tmp_path / "origem" / "url" / Path(PESOS[-1][0]).name) as z:
        for membro in WHEEL_MEMBROS:
            linhas.append(f"{_sha_bytes(z.read(membro))}  models/dnsmos/{Path(membro).name}")
    # a coorte é gerada: entra no manifesto mas não é baixável (repo "-")
    coorte_fake = tmp_path / "origem" / "coorte.npy"
    coorte_fake.write_bytes(b"\x93NUMPY falso")
    linhas.append(f"{_sha(coorte_fake)}  models/asnorm-coorte-ecapa.npy")
    (tmp_path / "models" / "sha256sums.txt").write_text("\n".join(linhas) + "\n")
    # a coorte já está no lugar (é gerada): o teste da ausência a apaga
    shutil.copy(coorte_fake, tmp_path / "models" / "asnorm-coorte-ecapa.npy")
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
    alvo.chmod(0o644)      # o script baixa 444 (peso imutável), como os do repo
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
    """`--check` com arquivo faltando é REPROVAÇÃO (saía 0 com tudo faltando)."""
    r = _roda(amb, "--check")
    assert r.returncode == 1, r.stdout
    assert "faltando" in r.stdout and "baixando" not in r.stdout


def test_check_com_tudo_no_lugar_passa(amb):
    assert _roda(amb).returncode == 0
    r = _roda(amb, "--check")
    assert r.returncode == 0, r.stdout
    assert "faltando" not in r.stdout


def test_coorte_que_falta_nao_e_baixada_e_aponta_o_gerador(amb):
    """O `.npy` da coorte é gerado localmente: o script não inventa download."""
    (amb["root"] / "models" / "asnorm-coorte-ecapa.npy").unlink()
    r = _roda(amb)
    assert r.returncode == 1, r.stdout
    assert "coorte_asnorm.py" in r.stdout


def test_label_encoder_ckpt_e_derivado_e_nao_symlink(amb):
    """#598: o `Pretrainer` busca `<savedir>/label_encoder.ckpt` e, sem ele, cai no
    Hub (offline quebra). O arquivo é cópia REGULAR do `.txt` — symlink para o
    cache do HF não sobrevive a uma máquina limpa."""
    r = _roda(amb)
    assert r.returncode == 0, r.stdout + r.stderr
    ckpt = amb["root"] / "models" / "ecapa-voxceleb" / "label_encoder.ckpt"
    assert ckpt.is_file() and not ckpt.is_symlink()
    assert not list(ckpt.parent.glob("*.tmp*")), "sobrou temporário de nome fixo"
    assert ckpt.read_bytes() == (amb["root"] / "models" / "ecapa-voxceleb"
                                 / "label_encoder.txt").read_bytes()


def test_derivado_fora_do_lugar_reprova_o_check_e_volta_sem_forca(amb):
    assert _roda(amb).returncode == 0
    ckpt = amb["root"] / "models" / "ecapa-voxceleb" / "label_encoder.ckpt"
    ckpt.chmod(0o644)          # o derivado sai 444 (peso imutável), como os outros
    ckpt.write_text("lixo\n")
    r = _roda(amb, "--check")
    assert r.returncode == 1, r.stdout
    r = _roda(amb)
    assert r.returncode == 1 and "divergente" in r.stdout, r.stdout
    assert ckpt.read_text() == "lixo\n", "sobrescreveu sem --forca"
    assert _roda(amb, "--forca").returncode == 0
    assert ckpt.read_bytes() == (amb["root"] / "models" / "ecapa-voxceleb"
                                 / "label_encoder.txt").read_bytes()


def test_app_nao_baixa_modelo_no_boot():
    """Contrato do card: quem baixa é o script; o boot não.

    `from_hparams(` sozinho baixaria do Hub. O app é liberado a usar porque passa
    `source`/`savedir` no DIRETÓRIO local (`_speaker_embed_ecapa`, #599) e só liga o
    extrator quando os pesos já estão em disco (`_speaker_ecapa_ok`); o que não pode
    é chamar com id de repositório cravado."""
    for arq in ("run.sh", "app.py"):
        for linha in (REPO / arq).read_text().splitlines():
            # o script pode ser CITADO (mensagem "rode ./tools/baixa_modelos.sh");
            # o que não pode é ser EXECUTADO no boot
            if "baixa_modelos" in linha and any(
                    m in linha for m in ("subprocess", "os.system", "call(", ".run(")):
                raise AssertionError(f"{arq} executa o downloader no boot: {linha.strip()}")
    for trecho in (REPO / "app.py").read_text().split("from_hparams(")[1:]:
        chamada = trecho[:200]
        assert "source=str(" in chamada, "`from_hparams` sem `source` local (baixa do Hub)"
        assert "savedir=" in chamada, "`from_hparams` sem `savedir` (baixa p/ o cache do usuário)"


#: fora do manifesto e do git: nada. Os `.ckpt` saem do índice pelo `.gitignore`
#: (são materializados pelo script) e o `label_encoder.ckpt` ENTRA no manifesto
#: como derivado — deixou de ser "gerado no primeiro load" (#598).
GERADOS: set[str] = set()


def test_manifesto_lista_todos_os_pesos_do_repo():
    """Peso sem sha256 no manifesto é peso que ninguém confere."""
    manifesto = (REPO / "models" / "sha256sums.txt").read_text()
    listados = {linha.split("  ", 1)[1] for linha in manifesto.splitlines() if "  " in linha}
    no_disco = {f"models/{p.relative_to(REPO / 'models').as_posix()}"
                for p in (REPO / "models").rglob("*")
                if p.is_file() and p.suffix in {".ckpt", ".pt", ".onnx", ".th", ".yaml", ".txt", ".npy"}
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
