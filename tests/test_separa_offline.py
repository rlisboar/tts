"""O separador de locutores carrega OFFLINE, a partir de `models/` (#526-bis).

Mesmo contrato do ECAPA (#598): pesos fora do git, sha256 no manifesto, load com
caminho LOCAL e nada no cache do HuggingFace. Aqui o alvo é o MossFormer2_SS_16K,
cujo código é vendorizado em `tools/separa_vend/`.

O que se cobra: com `HF_HOME` vazio, `HF_HUB_OFFLINE=1` e um `TTS_MODELOS_URL`
morto (nada de rede), o extrator carrega do arquivo LOCAL e devolve os DOIS fluxos,
no tamanho da entrada; sem o `.pt` a disponibilidade cai e a mensagem diz o script
(o app cai na seleção por janela); e o código vendorizado é byte a byte o da
revisão fixada, com o único desvio documentado (`torchinfo` opcional).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PESOS = REPO / "models" / "mossformer2-ss-16k" / "last_best_checkpoint.pt"
VEND = REPO / "tools" / "separa_vend" / "mf2_ss"

#: `HF_*` fecham o Hub; `TTS_MODELOS_URL` morto fecha o `curl` do script — se o
#: load tentar baixar, a célula falha em vez de esperar 670 MB de rede
_OFFLINE = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "TTS_MODELOS_URL": "http://127.0.0.1:9"}

CARREGA = """
import sys
sys.path.insert(0, "%(repo)s/tools/bakeoff")
import numpy as np, separa
assert separa.disponivel(), "disponibilidade falsa com os pesos no lugar"
m = separa.carrega()
# 2 s: o decodificador enquadra em janela de 1 s com passo de 0,75 s e PREENCHE
# até a grade, então a saída é arredondada para cima (até ~1 passo e meio) — mas
# nunca menor que a entrada
x = (0.1 * np.sin(2 * np.pi * 180 * np.arange(32000) / 16000)).astype(np.float32)
fluxos = separa.separa(m, x)
assert len(fluxos) == 2, len(fluxos)
assert all(len(f) >= len(x) for f in fluxos), [len(f) for f in fluxos]
assert all(len(f) - len(x) < 24000 for f in fluxos), [len(f) for f in fluxos]
assert not np.allclose(fluxos[0], fluxos[1]), "os dois fluxos saíram iguais"
assert all(np.isfinite(f).all() and np.abs(f).max() > 0 for f in fluxos)
print("ok", len(fluxos))
"""

_pesos = pytest.mark.skipif(not PESOS.is_file(),
                            reason="pesos do separador fora do disco — rode tools/baixa_modelos.sh")


def _roda(tmp_path: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **_OFFLINE, "HF_HOME": str(tmp_path / "hf"),
           **(env_extra or {})}
    return subprocess.run([sys.executable, "-c", CARREGA % {"repo": REPO}],
                          capture_output=True, text=True, timeout=600,
                          env=env, cwd=str(tmp_path))


@_pesos
def test_carrega_offline_com_hfhome_vazio(tmp_path):
    r = _roda(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok 2" in r.stdout, r.stdout
    assert not list((tmp_path / "hf").rglob("*.pt")), \
        "o load copiou peso para o HF_HOME: o caminho local não está valendo"


@_pesos
def test_sem_os_pesos_a_disponibilidade_cai_e_a_mensagem_diz_o_script(tmp_path):
    """Sem o `.pt` (clone cru) ninguém finge que sabe separar: `disponivel()` é
    False e o erro manda rodar o script — é o gancho do fallback do app."""
    vazio = tmp_path / "sem-pesos"
    vazio.mkdir()
    r = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(REPO / 'tools' / 'bakeoff')!r}); "
         "import separa; separa.carrega()"],
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
        env={**os.environ, **_OFFLINE, "HF_HOME": str(tmp_path / "hf"),
             "TTS_SEPARA_MODELO_DIR": str(vazio)})
    assert r.returncode != 0, r.stdout
    assert "baixa_modelos.sh" in (r.stdout + r.stderr), r.stdout + r.stderr
    # a mensagem tem de dizer o que fazer quando o diretório é por ambiente (o
    # script não conhece `TTS_SEPARA_MODELO_DIR`)
    assert "TTS_SEPARA_MODELO_DIR" in (r.stdout + r.stderr), r.stdout + r.stderr
    r = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(REPO / 'tools' / 'bakeoff')!r}); "
         "import separa; print(separa.disponivel())"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
        env={**os.environ, "TTS_SEPARA_MODELO_DIR": str(vazio)})
    assert r.stdout.strip() == "False", r.stdout + r.stderr


def test_codigo_vendorizado_e_o_da_revisao_fixada():
    """Vendor sem conferência vira upstream fantasma: o `FONTE.txt` traz o sha256
    de cada arquivo e a revisão. O único desvio admitido é o `torchinfo` opcional."""
    fonte = (REPO / "tools" / "separa_vend" / "FONTE.txt").read_text()
    esperados = {}
    for linha in fonte.splitlines():
        partes = linha.split("  ", 1)
        if len(partes) == 2 and partes[1].endswith(".py"):
            esperados[partes[1]] = partes[0]
    assert set(esperados) == {p.name for p in VEND.glob("*.py")}, "lista do FONTE.txt divergiu"
    for nome, sha in esperados.items():
        atual = hashlib.sha256((VEND / nome).read_bytes()).hexdigest()
        assert atual == sha, f"{nome} não é o da revisão fixada (esperado {sha[:12]})"
    assert "revisao=" in fonte, "FONTE.txt sem a revisão fixada"
    assert "torchinfo" in fonte, "o desvio local tem de estar declarado no FONTE.txt"


def test_vendor_nao_puxa_pacote_alem_de_torch_einops_rotary():
    """`funasr`/`clearvoice` inteiros não entram: o vendor importa só stdlib,
    torch/numpy e as duas dependências pinadas."""
    permitidos = {"torch", "numpy", "einops", "rotary_embedding_torch", "torchinfo"}
    for arq in VEND.glob("*.py"):
        for linha in arq.read_text().splitlines():
            l = linha.strip()
            if not l.startswith(("import ", "from ")) or l.startswith("from ."):
                continue
            mod = l.split()[1].split(".")[0]
            if mod in {"__future__", "math", "os", "sys", "types", "copy", "pathlib", "typing"}:
                continue
            assert mod in permitidos, f"{arq.name} importa {mod} (fora da lista mínima)"
