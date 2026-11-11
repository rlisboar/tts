"""O worker da marca roda o AudioSeal SEM `torch.compile` (#1116).

O AudioSeal 0.2 (via moshi) embrulha o `forward` do SEANet, das convs e do RoPE
em `torch_compile_lazy`. O dynamo guarda o formato exato do tensor: canais por
camada, comprimento do trecho e instância (gerador x detector). O aquecimento
com `zeros(1, 1, 16000)` compilava um formato só, e o 1º trecho real do Live
recompilava tudo — ~0,74 s de `marca` no 1º turno do mini, contra 58–71 ms dos
seguintes. Em CPU o eager tem os mesmos ms por trecho e aquece em ~0,7 s, não ~6 s.

1. Sem pesos: um `audioseal` de mentira na frente do `PYTHONPATH` registra o que
   o worker fez antes do `pronto` — a chave `NO_TORCH_COMPILE` já valia no
   IMPORT do pacote (é lá que o `torch_compile_lazy` a lê) e o DETECTOR também
   foi aquecido (o `embute_conferido` usa os dois a cada trecho).
2. Com pesos (pula sem eles): o 1º trecho no formato do Live (24 kHz, 1,3 s) não
   dispara recompilação do dynamo e sai com a marca detectável.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import marca_dagua  # noqa: E402

WORKER = RAIZ / "marca_worker.py"

_FALSO = '''
import json, os

_LOG = os.environ["MARCA_FALSA_LOG"]


def _grava(ev):
    with open(_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\\n")


_grava({"ev": "import", "NO_TORCH_COMPILE": os.environ.get("NO_TORCH_COMPILE")})


class _Gerador:
    def eval(self):
        return self

    def __call__(self, x, sample_rate=None):
        _grava({"ev": "gerador", "shape": list(x.shape)})
        return x * 0.0


class _Detector:
    def eval(self):
        return self

    def detect_watermark(self, x, sample_rate=None):
        import torch
        _grava({"ev": "detector", "shape": list(x.shape)})
        return torch.tensor(1.0), None


class AudioSeal:
    @staticmethod
    def load_generator(caminho, nbits=16):
        return _Gerador()

    @staticmethod
    def load_detector(caminho, nbits=16):
        return _Detector()
'''


def _ambiente(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "NO_TORCH_COMPILE"}
    env.update(extra)
    return env


def _ate_pronto(proc: subprocess.Popen, teto: float) -> None:
    fim = time.monotonic() + teto
    while time.monotonic() < fim:
        linha = proc.stdout.readline()
        if not linha:
            break
        if '"pronto"' in linha:
            return
    raise AssertionError("o worker não chegou ao `pronto`")


def _fecha(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.write('{"tipo": "close"}\n')
        proc.stdin.flush()
        proc.wait(timeout=30)
    except Exception:  # noqa: BLE001
        proc.kill()
        proc.wait(timeout=5)


def test_worker_importa_audioseal_sem_compile_e_aquece_o_detector(tmp_path):
    pacote = tmp_path / "falso" / "audioseal"
    pacote.mkdir(parents=True)
    (pacote / "__init__.py").write_text(_FALSO, encoding="utf-8")
    log = tmp_path / "eventos.ndjson"
    env = _ambiente(PYTHONPATH=str(pacote.parent), MARCA_FALSA_LOG=str(log),
                    TTS_MARCA_MODELO_DIR=str(tmp_path))
    proc = subprocess.Popen([sys.executable, str(WORKER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, env=env, cwd=str(tmp_path))
    try:
        _ate_pronto(proc, 120)
        eventos = [json.loads(linha) for linha in log.read_text().splitlines()]
    finally:
        _fecha(proc)

    assert eventos and eventos[0]["ev"] == "import", eventos
    assert eventos[0]["NO_TORCH_COMPILE"] == "1", (
        "o audioseal foi importado com o torch.compile ligado: o 1º trecho real "
        "recompila o SEANet (~0,74 s no 1º turno do Live)")
    tipos = [e["ev"] for e in eventos]
    assert "gerador" in tipos, tipos
    assert "detector" in tipos, (
        "o detector não foi aquecido antes do `pronto`: o 1º trecho paga o 1º "
        f"detect_watermark — {tipos}")


@pytest.mark.skipif(not marca_dagua.disponivel(),
                    reason=f"marca indisponível: {marca_dagua.motivo()}")
def test_primeiro_trecho_do_live_nao_recompila_e_sai_marcado(tmp_path):
    sr = 24000                              # `_LIVE_AUDIO_SR`
    t = np.arange(int(sr * 1.3)) / sr
    x = (0.2 * np.sin(2 * np.pi * 180 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)))
    entrada, saida = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(str(entrada), x.astype(np.float32), sr, subtype="PCM_16")
    erros = tmp_path / "stderr.txt"
    with open(erros, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen([sys.executable, str(WORKER)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=fh, text=True,
                                env=_ambiente(TORCH_LOGS="recompiles"), cwd=str(RAIZ))
        try:
            _ate_pronto(proc, 120)
            for ident, pedido in (
                    (1, {"op": "marca", "wav": str(entrada), "saida": str(saida)}),
                    (2, {"op": "detecta", "wav": str(saida)})):
                proc.stdin.write(json.dumps({"id": ident, **pedido}) + "\n")
                proc.stdin.flush()
                while True:
                    resp = json.loads(proc.stdout.readline())
                    if resp.get("id") == ident:
                        break
                assert not resp.get("erro"), resp
        finally:
            _fecha(proc)

    log = erros.read_text(encoding="utf-8", errors="replace")
    assert "Recompiling" not in log, f"o dynamo recompilou no 1º trecho:\n{log[-1500:]}"
    assert resp["prob"] >= 0.5, f"a saída perdeu a marca: {resp}"
    assert sf.info(str(saida)).samplerate == sr
