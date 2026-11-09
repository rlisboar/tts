"""O ECAPA carrega OFFLINE, a partir de `models/` (#598).

O defeito medido no gate do #526: os carregadores do bake-off chamavam
`from_hparams(source="speechbrain/spkrec-ecapa-voxceleb")` sem `savedir`, então o
speechbrain resolvia pelo CACHE do HuggingFace; e o `label_encoder.ckpt` era symlink
para dentro desse cache. Numa máquina limpa, com `HF_HOME` vazio e
`HF_HUB_OFFLINE=1`, o load morria em `LocalEntryNotFoundError`.

Aqui as duas coisas são cobradas juntas: o load tem de acontecer com o HF vazio,
offline, devolvendo vetor de 192 dimensões — e tem de FALHAR quando o derivado não
está no diretório, provando que ele é obrigatório (é o que o script materializa a
partir do `.txt` versionado, sem rede).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MODELO = REPO / "models" / "ecapa-voxceleb"
SCRIPT = REPO / "tools" / "baixa_modelos.sh"

_OFFLINE = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
}

#: carrega pelo carregador do bake-off (é ele que precisa passar `savedir`)
CARREGA = """
import sys
sys.path.insert(0, "%(repo)s/tools/bakeoff")
import numpy as np, torch
from identidade import _Ecapa
e = _Ecapa().emb(np.zeros(16000, dtype=np.float32))
print("dim", e.shape, "norma", round(float(np.linalg.norm(e)), 3))
assert e.shape == (192,), e.shape
"""

_pesos = pytest.mark.skipif(
    not (MODELO / "embedding_model.ckpt").is_file(),
    reason="pesos do ECAPA fora do disco — rode tools/baixa_modelos.sh")


def _roda_carga(tmp_path: Path, modelo_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, **_OFFLINE, "HF_HOME": str(tmp_path / "hf"),
           "TTS_SPEAKER_MODELO_DIR": str(modelo_dir)}
    return subprocess.run([sys.executable, "-c", CARREGA % {"repo": REPO}],
                          capture_output=True, text=True, timeout=180, env=env,
                          cwd=str(tmp_path))


def _dir_com_symlinks(tmp_path: Path, sem: set[str]) -> Path:
    """Diretório do extrator com os arquivos reais por symlink, menos `sem`."""
    destino = tmp_path / "ecapa"
    destino.mkdir()
    for arq in MODELO.iterdir():
        if arq.name not in sem:
            (destino / arq.name).symlink_to(arq)
    return destino


@_pesos
def test_carrega_offline_com_hfhome_vazio(tmp_path):
    """O caso do card: `HF_HOME` vazio + offline, sem o cache do HF por perto."""
    r = _roda_carga(tmp_path, MODELO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dim (192,)" in r.stdout, r.stdout
    assert not list((tmp_path / "hf").rglob("*.ckpt")), \
        "o load copiou peso para o HF_HOME: o `savedir` não está valendo"


@_pesos
def test_sem_o_label_encoder_derivado_o_load_offline_falha(tmp_path):
    """O derivado é OBRIGATÓRIO: sem ele o `Pretrainer` busca no Hub e, offline,
    estoura. (Com o symlink para o cache fora do lugar era o mesmo desfecho.)

    O par dentro da célula é o que a torna específica: só o arquivo muda entre a
    reprovação e o sucesso."""
    destino = _dir_com_symlinks(tmp_path, {"label_encoder.ckpt"})
    r = _roda_carga(tmp_path, destino)
    assert r.returncode != 0, r.stdout
    assert "LocalEntryNotFoundError" in (r.stdout + r.stderr)
    (destino / "label_encoder.ckpt").symlink_to(MODELO / "label_encoder.ckpt")
    r = _roda_carga(tmp_path, destino)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dim (192,)" in r.stdout, r.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="precisa de zsh")
@pytest.mark.slow
@pytest.mark.timeout(600)   # #783: o teto default é 90 s; estes passam de 100
def test_clone_cru_deriva_do_arquivo_versionado_sem_rede(tmp_path):
    """Clone limpo: `models/` só com o que está no git. O script materializa o
    derivado a partir do `.txt` (sem rede) e aí o load offline passa."""
    (tmp_path / "tools").mkdir()
    shutil.copy(SCRIPT, tmp_path / "tools" / "baixa_modelos.sh")
    modelo = tmp_path / "models" / "ecapa-voxceleb"
    modelo.mkdir(parents=True)
    # no clone real só os arquivos versionados estão lá; aqui o resto vem por
    # symlink (os 83 MB baixados não são o assunto desta célula) — o DERIVADO não.
    for arq in MODELO.iterdir():
        if arq.name != "label_encoder.ckpt":
            (modelo / arq.name).symlink_to(arq)
    (tmp_path / "models" / "asnorm-coorte-ecapa.npy").symlink_to(
        REPO / "models" / "asnorm-coorte-ecapa.npy")
    shutil.copy(REPO / "models" / "sha256sums.txt", tmp_path / "models" / "sha256sums.txt")
    assert not (modelo / "label_encoder.ckpt").exists()

    # Teto FOLGADO de propósito (#1086): a derivação hasheia os 83 MB dos pesos e a
    # duração é dominada por I/O — medida de 52 s a 124 s com uma suíte inteira
    # rodando ao lado, e o antigo 60 s derrubava a suíte cheia. Não é teste de
    # performance: 420 s é ~3,4x o pior medido e deixa folga sob o `timeout(600)`
    # do próprio teste, que continua sendo o teto de verdade.
    r = subprocess.run([shutil.which("zsh"), "-f", str(tmp_path / "tools" / "baixa_modelos.sh")],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=420,
                       env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    assert "derivado e conferido" in r.stdout, r.stdout + r.stderr
    ckpt = modelo / "label_encoder.ckpt"
    assert ckpt.is_file() and not ckpt.is_symlink()

    carga = _roda_carga(tmp_path, modelo)
    assert carga.returncode == 0, carga.stdout + carga.stderr
    assert "dim (192,)" in carga.stdout, carga.stdout


def _sha(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()
