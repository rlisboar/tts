#!/usr/bin/env python3
"""Worker ISOLADO do AudioSeal (marca d'água anti-clone, #657).

Áudio gerado sai marcado; o detector é a outra ponta. O modelo é torch em CPU e
tem de ficar FORA do processo do app: o app já convive com MLX no Metal e o
`first_audio_ms` não pode pagar setup de torch a cada trecho.

Modo único, NDJSON em stdin/stdout. Antes de importar o torch o filho avisa
`{"tipo": "subindo"}`: a carga (~2,6 s) não pode parecer mudez para o prazo
curto do Live. `{"tipo": "pronto"}` sai depois do aquecimento e devolve o
relógio da operação.

    {"id": 1, "op": "marca", "wav": "<entrada>", "saida": "<saída>"}
    {"id": 2, "op": "detecta", "wav": "<entrada>"}

Resposta:

    {"id": 1, "ms": 41}                    (marca: escreve o wav de saída)
    {"id": 2, "prob": 0.999, "ms": 12}     (detecta: probabilidade da marca)

A grade do AudioSeal 0.2 é 16 kHz: `sample_rate` é no-op e o modelo lê as
samples como estão. Wav noutra taxa (o Live manda 24 kHz) desce para 16 kHz
antes de marcar ou detectar, e a marca volta para a taxa do arquivo no mesmo
número de samples. Sem isso a conferência passa na grade errada e o áudio que
o cliente ouve perde a marca (#908).
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np


def embute_conferido(gerador, detector, x, sr, fator, alvo=None, tentativas=4,
                     wav_original=None):
    """Embute e CONFERE: abaixo do alvo, sorteia de novo (até `tentativas`).

    A mensagem é sorteada a cada embed e algumas sortes grudam mal em áudio baixo
    (#687), então a saída é medida antes de valer. Se o DETECTOR levantar, a marca
    ainda existe: fica a última embedding feita e o erro é devolvido — perder a
    detecção não pode custar o áudio sem marca."""
    import torch

    alvo = ALVO_DETECCAO if alvo is None else alvo
    base = wav_original
    if base is None:
        base = torch.from_numpy(np.ascontiguousarray(x))[None, None]
    melhor, melhor_prob, usadas, erro = None, float("nan"), 0, ""
    for _ in range(max(1, tentativas)):
        with torch.no_grad():
            cru = gerador(base, sample_rate=sr).squeeze().numpy()
        y = (x + fator * (cru - x)).astype(np.float32)
        usadas += 1
        if melhor is None:
            melhor = y                    # já existe marca para cair de volta
        try:
            with torch.no_grad():
                prob, _m = detector.detect_watermark(
                    torch.from_numpy(np.ascontiguousarray(y))[None, None], sample_rate=sr)
            prob = float(prob.squeeze())
        except Exception as exc:  # noqa: BLE001 — sem conferência, fica a marca feita
            melhor = y                    # esta é a única que existe: fica ela
            erro = f"{type(exc).__name__}: {exc}"[:160]
            print(f"[marca] detector falhou, mantendo a marca da tentativa {usadas}: {erro}",
                  flush=True)
            break
        # SÓ troca se MELHOROU. Antes o `melhor` era reatribuído a cada volta e o
        # que saía era a ÚLTIMA tentativa, não a melhor: sob carga uma sorte ruim
        # em último lugar derrubava a marca para ~0,6 (#706).
        # `melhor_prob` começa NaN e `prob >= NaN` é SEMPRE falso: sem o teste de
        # NaN a primeira tentativa nunca seria guardada e o retorno sairia NaN.
        if melhor_prob == melhor_prob and prob <= melhor_prob:
            continue
        melhor, melhor_prob = y, prob
        if melhor_prob >= alvo:
            break
    return melhor, melhor_prob, usadas, erro


#: probabilidade mínima aceita na saída; abaixo disso o embed é repetido (máx. 4)
ALVO_DETECCAO = float(os.environ.get("TTS_MARCA_ALVO", "0.97"))

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

RAIZ = os.path.dirname(os.path.abspath(__file__))


def _log(msg: str) -> None:
    print(f"[marca] {msg}", file=sys.stderr, flush=True)


def _regrade(x: np.ndarray, de: int, para: int) -> np.ndarray:
    """Reamostra. O AudioSeal 0.2 só entende a grade de 16 kHz."""
    if int(de) == int(para) or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(int(de), int(para))
    return np.asarray(
        resample_poly(np.asarray(x, dtype=np.float32), int(para) // g, int(de) // g),
        dtype=np.float32)


def _mesmo_tamanho(y: np.ndarray, n: int) -> np.ndarray:
    if y.size == n:
        return y
    if y.size > n:
        return y[:n]
    return np.pad(y, (0, n - y.size))


def _rampa_borda(audio: np.ndarray, sr: int, ms: float = 8.0) -> np.ndarray:
    """Mesma rampa linear de `common.fade_edges` (8 ms).

    O ida e volta 24→16→24 do resample_poly espalha o zero que o fade de
    antes da marca tinha escrito. Sem reaplicar DEPOIS da volta, a borda
    crua passa de 0,002 e vira clique entre trechos. Fica neste filho para
    não puxar `common` (e o app) para o processo do AudioSeal."""
    n = int(sr * ms / 1000.0)
    if n < 1 or audio.size < 2 * n:
        return audio
    a = np.asarray(audio, dtype=np.float32).copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    a[:n] *= ramp
    a[-n:] *= ramp[::-1]
    return a


def main() -> int:
    # Antes do torch. Sem isto o pai, no prazo de 1,5 s do Live, mata a carga
    # (~2,6 s) e o áudio sai cru. O `pronto` continua no fim do aquecimento.
    print(json.dumps({"tipo": "subindo"}), flush=True)
    import numpy as np
    import soundfile as sf
    import torch
    from audioseal import AudioSeal

    pasta = os.environ.get("TTS_MARCA_MODELO_DIR") or os.path.join(
        RAIZ, "models", "audioseal")
    gerador_p = os.path.join(pasta, "gerador.pt")
    detector_p = os.path.join(pasta, "detector.pt")
    t = time.perf_counter()
    # `nbits=16` explícito: sem a carta do pacote o config vem do próprio
    # checkpoint (`xp.cfg`), e não há download nenhum aqui
    gerador = AudioSeal.load_generator(gerador_p, nbits=16).eval()
    detector = AudioSeal.load_detector(detector_p, nbits=16).eval()
    _log(f"carregado em {round((time.perf_counter() - t) * 1000)} ms")
    t = time.perf_counter()
    with torch.no_grad():                    # aquece o grafo do gerador
        gerador(torch.zeros(1, 1, 16000), sample_rate=16000)
    # scipy entra aqui, no prewarm do filho: o 1º PCM não paga o import (~0,47 s).
    _regrade(np.zeros(8, dtype=np.float32), 24000, 16000)
    _log(f"aquecido em {round((time.perf_counter() - t) * 1000)} ms")
    print(json.dumps({"tipo": "pronto"}), flush=True)

    for linha in sys.stdin:
        linha = linha.strip()
        if not linha:
            continue
        try:
            pedido = json.loads(linha)
        except Exception:  # noqa: BLE001
            continue
        if pedido.get("tipo") == "close":
            break
        t0 = time.perf_counter()
        try:
            x, sr = sf.read(pedido["wav"], dtype="float32")
            if x.ndim > 1:
                x = x.mean(axis=1)
            sr = int(sr)
            n = int(x.shape[0])
            # Fora de 16 kHz a grade do arquivo não é a do modelo. Desce antes
            # de marcar e de detectar; na marca, volta à taxa e ao tamanho de entrada.
            x16 = x if sr == 16000 else _regrade(x, sr, 16000)
            wav = torch.from_numpy(np.ascontiguousarray(x16))[None, None]
            if pedido.get("op") == "detecta":
                with torch.no_grad():
                    prob, _msg = detector.detect_watermark(wav, sample_rate=16000)
                print(json.dumps({"id": pedido.get("id"), "prob": float(prob.squeeze()),
                                  "ms": int((time.perf_counter() - t0) * 1000)}), flush=True)
            else:
                # A marca é AUDÍVEL em fala baixa (#687): no par mais baixo do ABX
                # ela ficou em -63 dBFS e o dono ouviu. O que manda não é a força
                # relativa (SNR era 24 dB, igual às outras) e sim o nível absoluto.
                # Então o tanto de marca segue o nível do trecho: piso 0,6 (onde a
                # detecção ainda fica >=0,96 medido) até 1,0 em material alto.
                # O nível é o da GRADE marcada (16 kHz), não o do arquivo.
                nivel = 20 * np.log10(max(float(np.sqrt(np.mean(x16.astype(np.float64) ** 2))), 1e-9))
                fator = float(np.clip(0.6 + (nivel + 32.0) * 0.02, 0.6, 1.0))
                # A mensagem é SORTEADA a cada embed e algumas sortes grudam mal em
                # áudio baixo: medido 0,68 a 1,00 com o MESMO trecho em processos
                # diferentes (#687). Então a marca é CONFERIDA na saída e, se não
                # passou do alvo, sorteia de novo — sai sempre uma marca que o
                # detector acha, em vez de uma fraca e silenciosa.
                y, prob, usadas, erro = embute_conferido(
                    gerador, detector, x16, 16000, fator, wav_original=wav)
                if sr != 16000:
                    y = _mesmo_tamanho(_regrade(y, 16000, sr), n)
                    # A reamostragem é a última coisa que mexe na amostra.
                    # A rampa volta aqui, no sinal que o cliente ouve. Em
                    # 16 kHz não houve ida e volta: a rampa de antes fica.
                    y = _rampa_borda(y, sr)
                sf.write(pedido["saida"], y, sr, subtype="PCM_16")
                print(json.dumps({"id": pedido.get("id"),
                                  "ms": int((time.perf_counter() - t0) * 1000),
                                  "prob": None if prob != prob else round(prob, 3),
                                  "tentativas": usadas,
                                  "detector_erro": erro}), flush=True)
                continue

        except Exception as exc:  # noqa: BLE001 — o chamador segue sem marca
            print(json.dumps({"id": pedido.get("id"),
                              "erro": f"{type(exc).__name__}: {exc}"[:200]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
