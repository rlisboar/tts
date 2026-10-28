"""O DNSMOS carrega OFFLINE, a partir de `models/dnsmos` (#649).

Mesmo contrato dos outros pesos: artefato fora do git, sha256 no manifesto, carga
por `onnxruntime` do arquivo LOCAL — sem Hub, sem cache do HF e sem o pacote
`speechmos`. O wheel dele fica no diretório porque é a PROVENIÊNCIA (o ONNX é
extraído dele pelo script, conferido por sha256).

Sem os modelos a prévia da limpeza mostra "não medido": não quebra, mas também não
mede — e a licença é a razão de existir deste card (MIT da Microsoft).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PASTA = REPO / "models" / "dnsmos"
OVRL = PASTA / "sig_bak_ovr.onnx"

_OFFLINE = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "TTS_MODELOS_URL": "http://127.0.0.1:9"}

#: o OVRL recebe o áudio CRU (só o P.808 pediria mel-espectrograma/librosa)
CARREGA = """
import os
import numpy as np, onnxruntime as ort
sess = ort.InferenceSession(os.environ["TTS_DNSMOS_DIR"] + "/sig_bak_ovr.onnx",
                            providers=["CPUExecutionProvider"])
entrada = sess.get_inputs()[0]
assert entrada.shape[1] in (-1, 144160), entrada.shape   # 9,01 s a 16 kHz
x = np.zeros((1, 144160), dtype=np.float32)
sig, bak, ovr = sess.run(None, {entrada.name: x})[0][0]
vals = [float(sig), float(bak), float(ovr)]
assert all(v == v for v in vals), vals                  # sem NaN
print("ok", " ".join(f"{v:.3f}" for v in vals))
"""

_peso = pytest.mark.skipif(not OVRL.is_file(),
                           reason="DNSMOS fora do disco — rode tools/baixa_modelos.sh")


def _roda(tmp_path: Path, pasta: Path = PASTA) -> subprocess.CompletedProcess:
    env = {**os.environ, **_OFFLINE, "HF_HOME": str(tmp_path / "hf"),
           "TTS_DNSMOS_DIR": str(pasta)}
    return subprocess.run([sys.executable, "-c", CARREGA], capture_output=True,
                          text=True, timeout=180, cwd=str(tmp_path), env=env)


@_peso
def test_ovrl_roda_offline_com_hfhome_vazio(tmp_path):
    r = _roda(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("ok "), r.stdout
    assert not list((tmp_path / "hf").rglob("*.onnx")), \
        "o load copiou modelo para o HF_HOME: o caminho local não está valendo"


def test_modelos_sao_regulares_e_a_licenca_esta_ao_lado():
    """MIT da Microsoft: o NOTICE tem de estar no diretório, com o texto, e os
    arquivos não podem ser symlink (clone limpo)."""
    for arq in ("sig_bak_ovr.onnx", "model_v8.onnx"):
        alvo = PASTA / arq
        assert alvo.is_file() and not alvo.is_symlink(), arq
    notice = (PASTA / "NOTICE").read_text()
    assert "MIT License" in notice and "Microsoft" in notice
    assert "speechmos" in (PASTA / "FONTE.txt").read_text()


def test_artefato_fica_fora_do_git():
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "models/dnsmos"],
                       capture_output=True, text=True)
    rastreados = set(r.stdout.split())
    assert rastreados <= {"models/dnsmos/NOTICE", "models/dnsmos/FONTE.txt"}, \
        f"peso/pacote no índice: {sorted(rastreados)}"
    for arq in ("sig_bak_ovr.onnx", "model_v8.onnx", "speechmos-0.0.1.1-py3-none-any.whl"):
        ign = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q",
                              str(PASTA / arq)], capture_output=True)
        assert ign.returncode == 0, f"{arq} não está no .gitignore"
