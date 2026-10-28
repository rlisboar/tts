"""Bake-off de identidade de voz (#521).

Mede EER, minDCF e FRR em FAR alvo por modelo, duração, ruído, forma de cadastro
e normalização de escore, e — a métrica que mais importa para o produto — quanto
cada modelo aceita um CLONE gerado pelo próprio app como se fosse o locutor.

Protocolo: os locutores vêm do corpus público (`corpus.py publico`). Cada locutor
tem N falas; o cadastro usa k delas (centróide ou máximo) e o teste as restantes,
cortadas nas durações do aceite. Cobertura de ensaio (não é o app): só CPU.

Uso:
    /tmp/tts-bakeoff/venv/bin/python tools/bakeoff/identidade.py --modelos wespeaker34
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import metricas  # noqa: E402

MODELOS_DIR = comum.BASE / "modelos"
DURACOES = (1.0, 2.0, 3.0, 8.0)
SNRS_TESTE = (None, 5, 10)


# ---------------------------------------------------------------------------
# Adaptadores (todos devolvem vetor L2-normalizado)
# ---------------------------------------------------------------------------

def _fbank(x: np.ndarray) -> np.ndarray:
    import kaldi_native_fbank as knf

    o = knf.FbankOptions()
    o.frame_opts.samp_freq = comum.SR
    o.frame_opts.dither = 0
    o.frame_opts.snip_edges = True
    o.mel_opts.num_bins = 80
    f = knf.OnlineFbank(o)
    f.accept_waveform(comum.SR, (x * 32768).astype(np.int16).tolist())
    f.input_finished()
    # SEM normalizar: os modelos trazem a própria normalização. Dividir pela
    # variância por utterance custou caro — CAM++ ia a 29,2 % de EER e com o fbank
    # cru fica em 7,5 %; o WeSpeaker também melhora (10,6 % x 11,6 %).
    return np.stack([f.get_frame(i) for i in range(f.num_frames_ready)]).astype(np.float32)


class _Onnx:
    def __init__(self, arquivo: str):
        import onnxruntime as ort

        self.s = ort.InferenceSession(str(MODELOS_DIR / arquivo),
                                      providers=["CPUExecutionProvider"])

    def emb(self, x: np.ndarray) -> np.ndarray:
        e = self.s.run(None, {"feats": _fbank(x)[None]})[0][0]
        return e / (np.linalg.norm(e) or 1e-9)


class _Resemblyzer:
    def __init__(self):
        from resemblyzer import VoiceEncoder

        self.enc = VoiceEncoder("cpu")

    def emb(self, x: np.ndarray) -> np.ndarray:
        from resemblyzer import preprocess_wav

        e = self.enc.embed_utterance(preprocess_wav(x, source_sr=comum.SR))
        return e / (np.linalg.norm(e) or 1e-9)


class _Campplus:
    """3D-Speaker CAM++ (192-d). O `forward` começa com `permute`: quer FBANK (B,T,80).

    Carregado pelo módulo do funasr, sem `AutoModel` — que na 3.12/3.14 quebra o
    registry inteiro por módulos ausentes no wheel."""

    CACHE = Path.home() / ".cache/modelscope/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master/campplus_cn_common.bin"

    def __init__(self):
        import torch
        from funasr.models.campplus.model import CAMPPlus

        self.torch = torch
        self.m = CAMPPlus(feat_dim=80, embedding_size=192)
        self.m.load_state_dict(torch.load(str(self.CACHE), map_location="cpu"))
        self.m.eval()

    def emb(self, x: np.ndarray) -> np.ndarray:
        with self.torch.no_grad():
            e = self.m(self.torch.from_numpy(_fbank(x))[None])[0].numpy()
        return e / (np.linalg.norm(e) or 1e-9)


class _Eres2netv2(_Campplus):
    """3D-Speaker ERes2NetV2 (192-d), mesmo protocolo do CAM++ (fbank cru)."""

    CACHE = Path.home() / ".cache/modelscope/models/iic--speech_eres2netv2_sv_zh-cn_16k-common/snapshots/master/pretrained_eres2netv2.ckpt"

    def __init__(self):
        import torch
        from funasr.models.eres2net.eres2netv2 import ERes2NetV2

        self.torch = torch
        self.m = ERes2NetV2(feat_dim=80, embedding_size=192, m_channels=64, baseWidth=26)
        self.m.load_state_dict(torch.load(str(self.CACHE), map_location="cpu"))
        self.m.eval()


class _Ecapa:
    """SpeechBrain ECAPA-TDNN (VoxCeleb). Espera WAVEFORM normalizada, não fbank."""

    def __init__(self):
        from speechbrain.inference.speaker import EncoderClassifier

        self.enc = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb", run_opts={"device": "cpu"})

    def emb(self, x: np.ndarray) -> np.ndarray:
        import torch

        with torch.no_grad():
            e = self.enc.encode_batch(torch.from_numpy(x)[None]).squeeze().numpy()
        return e / (np.linalg.norm(e) or 1e-9)


ADAPTADORES = {
    "resemblyzer": lambda: _Resemblyzer(),
    "wespeaker34": lambda: _Onnx("voxceleb_resnet34_LM.onnx"),
    "wespeaker221": lambda: _Onnx("voxceleb_resnet221_LM.onnx"),
    "campplus": lambda: _Campplus(),
    "eres2netv2": lambda: _Eres2netv2(),
    "ecapa": lambda: _Ecapa(),
}


# ---------------------------------------------------------------------------
# Corpus e recortes
# ---------------------------------------------------------------------------

def _le(p: Path) -> np.ndarray:
    x, _ = sf.read(str(p), dtype="float32")
    return x if x.ndim == 1 else x.mean(axis=1)


def _recorta(x: np.ndarray, dur: float, semente: int) -> np.ndarray:
    """Recorte de `dur` segundos — REPETINDO a fala quando ela é mais curta.

    Zerofill enganava: as falas do corpus têm ~3 s, então o "8 s" era 3 s de fala
    com 5 s de silêncio e o embedding ia para o silêncio (o WeSpeaker caía de 14 %
    em 1 s para 47 % em 8 s por isso). Repetir mantém a duração e o conteúdo."""
    n = int(dur * comum.SR)
    if x.size == n:
        return x
    if x.size > n:
        ini = np.random.default_rng(semente).integers(0, x.size - n)
        return x[ini: ini + n]
    voltas = int(np.ceil(n / max(1, x.size)))
    return np.tile(x, voltas)[:n]


def _por_locutor() -> dict[str, list[Path]]:
    saida: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(comum.FALA.glob("pub_*.wav")):
        saida[p.stem.split("_")[1]].append(p)
    return {k: v for k, v in saida.items() if len(v) >= 4}


def _com_ruido(x: np.ndarray, fam: str, snr: float, semente: int) -> np.ndarray:
    import ruido

    cam = ruido.normaliza_rms(ruido.FAMILIAS[fam](x.size / comum.SR, semente), -26)
    cam = cam[: x.size]
    pot = float(np.mean(x ** 2)) or 1e-9
    cam = cam * np.sqrt(pot / (float(np.mean(cam ** 2)) or 1e-9) / 10 ** (snr / 10))
    return (x + cam).astype(np.float32)


# ---------------------------------------------------------------------------
# Protocolo
# ---------------------------------------------------------------------------

def _centroide(vs: list[np.ndarray], modo: str) -> np.ndarray:
    """`centroide` = média L2-normalizada; `max` = MEDOIDE (a amostra mais central).

    Medoide e não "a primeira": com 1 amostra dá no mesmo, e com 3/5 é a que menos
    depende de qual arquivo veio primeiro no diretório."""
    if modo == "centroide":
        m = np.mean(vs, axis=0)
    else:
        m = max(vs, key=lambda v: float(np.mean([v @ o for o in vs])))
    return m / (np.linalg.norm(m) or 1e-9)


def _cohorte(m, dados: dict, fora: set[str], limite: int = 999) -> list[np.ndarray]:
    """Embs de locutores FORA do ensaio, para o AS-norm (coorte de impostores)."""
    embs = []
    for loc in sorted(set(dados) - fora)[:limite]:
        for p in dados[loc][:2]:
            embs.append(m.emb(_recorta(_le(p), 3.0, 7)))
    return embs


def _scores(m: dict, cadastro: dict, testes: dict, modo: str,
            cohorte: list[np.ndarray] | None = None) -> tuple[list, list]:
    pos, neg = [], []
    centros = {loc: _centroide([m.emb(x) for x in amostras], modo)
               for loc, amostras in cadastro.items()}
    coh = np.stack(cohorte) if cohorte else None
    for loc, amostras in testes.items():
        for x in amostras:
            e = m.emb(x)
            brutos = {outro: float(e @ c) for outro, c in centros.items()}
            if coh is not None:
                # AS-norm: normaliza pelo top-k da coorte para ESTA fala
                s_coh = coh @ e
                top = np.sort(s_coh)[-min(50, s_coh.size):]
                mu, sd = float(top.mean()), float(top.std()) + 1e-6
                brutos = {k: (v - mu) / sd for k, v in brutos.items()}
            for outro, v in brutos.items():
                (pos if outro == loc else neg).append(v)
    return pos, neg


def roda(modelo: str, k_cadastro: int, modo: str, ruidos: tuple[str, ...],
         limite_locutores: int = 8, asnorm: bool = False) -> dict:
    m = ADAPTADORES[modelo]()
    dados = _por_locutor()
    locutores = sorted(dados)[:limite_locutores]
    cohorte = _cohorte(m, dados, set(locutores)) if asnorm else None
    saida = {"modelo": modelo, "k": k_cadastro, "modo": modo, "asnorm": asnorm,
             "por_duracao": {},
             "latencia_ms": [], "rss_mb": 0.0}
    for dur in DURACOES:
        cadastro, testes = {}, {}
        for i, loc in enumerate(locutores):
            falas = dados[loc]
            corte = [_recorta(_le(p), dur, hash((loc, dur, j)) % 10_000)
                     for j, p in enumerate(falas)]
            cadastro[loc] = corte[:k_cadastro]
            testes[loc] = corte[k_cadastro:]
        # limpo
        t0 = time.perf_counter()
        pos, neg = _scores(m, cadastro, testes, modo, cohorte)
        saida["latencia_ms"].append(round(1000 * (time.perf_counter() - t0) /
                                          max(1, len(pos) + len(neg)), 2))
        saida["por_duracao"][f"{dur:g}s_limpo"] = metricas.resumo(pos, neg)
        # com ruído (só sobre o TESTE, como no uso real)
        for fam in ruidos:
            for snr in SNRS_TESTE:
                if snr is None:
                    continue
                cad_r = cadastro
                test_r = {loc: [_com_ruido(x, fam, snr, j) for j, x in enumerate(v)]
                          for loc, v in testes.items()}
                pos, neg = _scores(m, cad_r, test_r, modo, cohorte)
                saida["por_duracao"][f"{dur:g}s_{fam}_{snr}dB"] = metricas.resumo(pos, neg)
    saida["clone"] = _clones(m, dados, locutores, k_cadastro, modo, cohorte)
    saida["rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1)
    return saida


def _clones(m, dados, locutores, k: int, modo: str, cohorte=None) -> dict:
    """FAR de CLONE: cópia sintética do próprio locutor contra o cadastro dele."""
    clones = {}
    for p in sorted(comum.CLONE.glob("*_*.wav")):
        clones.setdefault(p.stem.split("_")[0], []).append(p)
    clones = {k2: v for k2, v in clones.items() if k2 in locutores}
    if not clones:
        return {}
    cadastro, testes = {}, {}
    for loc in locutores:
        corte = [_recorta(_le(p), 3.0, j) for j, p in enumerate(dados[loc])]
        cadastro[loc] = corte[:k]
        if loc in clones:
            testes[loc] = [_le(p) for p in clones[loc]]
    saida = {}
    for dur_ref in (1.0, 3.0):
        # limiar calibrado nos IMPOSTORES do mesmo protocolo (FAR alvo)
        cad_r = {loc: [_recorta(x, dur_ref, i) for i, x in enumerate(v)]
                 for loc, v in cadastro.items()}
        pos, neg = _scores(m, cad_r, testes, modo, cohorte)
        centros = {loc: _centroide([m.emb(_recorta(x, dur_ref, i))
                                    for i, x in enumerate(v)], modo)
                   for loc, v in cadastro.items()}
        escores = [float(m.emb(x) @ centros[loc]) for loc, v in testes.items() for x in v]
        linha = {"n_clones": len(escores)}
        for alvo, nome in ((0.01, "far1"), (0.001, "far01")):
            lim = metricas.limiar_para_far(neg, alvo)
            linha[nome] = round(100 * float(np.mean(np.array(escores) >= lim)), 1)
            linha[f"limiar_{nome}"] = round(lim, 3)
        linha["score_medio_clone"] = round(float(np.mean(escores)), 3)
        linha["score_medio_impostor"] = round(float(np.mean(neg)), 3)
        saida[f"cadastro_{dur_ref:g}s"] = linha
    return saida


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelos", default=",".join(ADAPTADORES))
    ap.add_argument("--k", default="1,3,5")
    ap.add_argument("--modos", default="centroide,max")
    ap.add_argument("--ruidos", default="ventoinha,babble,musica")
    ap.add_argument("--locutores", type=int, default=8)
    ap.add_argument("--clones", action="store_true")
    ap.add_argument("--asnorm", action="store_true")
    a = ap.parse_args()
    comum.garante()
    ruidos = tuple(x for x in a.ruidos.split(",") if x)
    for modelo in a.modelos.split(","):
        for k in (int(x) for x in a.k.split(",")):
            for modo in a.modos.split(","):
                t0 = time.perf_counter()
                r = roda(modelo, k, modo, ruidos, a.locutores, a.asnorm)
                r["segundos"] = round(time.perf_counter() - t0, 1)
                destino = comum.RESULTADOS / f"identidade_{modelo}_k{k}_{modo}{'_asnorm' if a.asnorm else ''}.json"
                destino.write_text(json.dumps(r, indent=1))
                limpo = r["por_duracao"]["1s_limpo"]
                print(f"{modelo} k={k} {modo}: EER 1s {limpo['eer']:.2f}% | "
                      f"FRR@1% {limpo['frr_far1']:.1f}% | {r['segundos']}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
