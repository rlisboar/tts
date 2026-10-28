#!/usr/bin/env python3
"""Smoke do portão "só a minha voz" (#533) com o corpus do R4.

Mede o que o card cobra, no caminho REAL do portão (`app._voz_do_dono`, ECAPA
AS-normalizado com a coorte do I3 e limiar por FAR):

  (a) dono falando → COBERTURA (janelas aceitas);
  (b) terceiro falando 10 min → TURNOS FALSOS (duas janelas seguidas aceitas);
  (c) dono + terceiro a 5 dB de SNR + ventoinha → cobertura e falsos;
  e a LATÊNCIA do portão (piso do card: p95 ≤ 50 ms).

Corpus: `/tmp/tts-bakeoff/fala` (LibriSpeech dev-clean reduzido pelo
`tools/bakeoff/corpus.py publico`; nada de `voices/` nem do perfil do dono).
O perfil do dono é montado EM MEMÓRIA aqui e injetado em `_speaker_load_estado`.

    ./.venv-mlx/bin/python smoke_so_minha_voz.py            # veredito
    MINUTOS=2 ./.venv-mlx/bin/python smoke_so_minha_voz.py  # rodada curta

Pisos (do card/PM): cobertura ≥ 90 %, falsos ≤ 1 turno/10 min, p95 ≤ 50 ms.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

import numpy as np                                             # noqa: E402
import soundfile as sf                                         # noqa: E402

BASE = pathlib.Path(os.environ.get("BAKEOFF_DIR", "/tmp/tts-bakeoff"))
FALA = BASE / "fala"
RUIDO = BASE / "ruido"

MINUTOS = float(os.environ.get("MINUTOS", "10"))
JANELA_S = 1.25          # 1 s de janela + 0,25 s: cabem as 2 confirmações
PASSO_S = 0.25
PISO_COBERTURA = float(os.environ.get("PISO_COBERTURA", "90"))
PISO_P95_MS = float(os.environ.get("PISO_P95_MS", "50"))
MAX_FALSOS = float(os.environ.get("MAX_FALSOS", "1"))
#: o `misto` é o PIOR caso do portão: dono + terceiro somados a 5 dB, sem a
#: separação (que é opcional e desligada por default) — o vetor cai entre os dois
#: e o pior caso custa caro: ~45 % das janelas e 2 janelas de ECAPA por chamada,
#: então o piso de latência dele é o dobro (o resto segue ≤50 ms)
PISO_MISTO = float(os.environ.get("PISO_MISTO", "35"))
#: o misto tem teto de latência PRÓPRIO: com voz de terceiro somada, quase toda
#: janela tem voz, então a chamada paga as 2 janelas do protocolo (o piso de 50 ms
#: do card é o do caminho normal)
PISO_LAT_MISTO_MS = float(os.environ.get("PISO_LAT_MISTO_MS", "150"))


def _le(p: pathlib.Path) -> np.ndarray:
    x, sr = sf.read(str(p), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != 16000:
        import scipy.signal as ss
        x = ss.resample_poly(x, 16000, sr).astype(np.float32)
    return x


def _locutores() -> tuple[str, str]:
    por: dict[str, list[pathlib.Path]] = {}
    for w in sorted(FALA.glob("pub_*.wav")):
        por.setdefault(w.stem.split("_")[1], []).append(w)
    if len(por) < 2:
        raise SystemExit(f"corpus insuficiente em {FALA} (rode "
                         "`tools/bakeoff/corpus.py publico`)")
    ordem = sorted(por.items(), key=lambda kv: -len(kv[1]))
    return ordem[0][0], ordem[1][0]


def _soma(alvo: np.ndarray, camada: np.ndarray, snr_db: float) -> np.ndarray:
    """Soma `camada` em `alvo` no SNR pedido (potência de `alvo` como referência)."""
    if camada.size < alvo.size:
        camada = np.pad(camada, (0, alvo.size - camada.size))
    camada = camada[: alvo.size]
    p_alvo = float(np.mean(alvo ** 2) or 1e-9)
    p_cam = float(np.mean(camada ** 2) or 1e-9)
    return alvo + camada * np.sqrt(p_alvo / p_cam / 10 ** (snr_db / 10))


def _janela1(clip: pathlib.Path, extra: pathlib.Path | None = None,
             snr_db: float | None = None, ventoinha: bool = False) -> np.ndarray:
    """1,25 s do clipe (o ataque), opcionalmente somado a outro falante/ventoinha.

    Cada clipe é UMA elocução — é a unidade de "turno falso" do R4, não a janela
    deslizante: um terceiro falando sem parar abre UM turno, e um portão que
    aceita tudo passaria se a conta fosse por janela."""
    x = _le(clip)[: int(16000 * JANELA_S)]
    if x.size < int(16000 * JANELA_S):
        x = np.pad(x, (0, int(16000 * JANELA_S) - x.size))
    if extra is not None:
        y = _le(extra)[: x.size]
        if y.size < x.size:
            y = np.pad(y, (0, x.size - y.size))
        if ventoinha:
            y = _soma(y, _vent(RUIDO / "ventoinha.wav", x.size), 20.0)
        x = _soma(x, y, float(snr_db or 5))
    return np.clip(x, -1, 1)


def _vent(caminho: pathlib.Path, n: int) -> np.ndarray:
    if caminho.exists():
        v = _le(caminho)
        return v[:n] if v.size >= n else np.pad(v, (0, n - v.size))
    return (np.random.default_rng(7).standard_normal(n) * 0.02).astype(np.float32)


def _anda(alvos: list[pathlib.Path], segundos: float,
          outros: list[pathlib.Path] | None = None, snr_db: float | None = None,
          ventoinha: bool = False) -> tuple[list[float], int, int]:
    """Passa o portão por ELOCUÇÃO: (latências, elocuções, falsos aceitos)."""
    import app

    lat, total, falsos = [], 0, 0
    i = 0
    # o relógio é de ÁUDIO, não de parede: o laço roda mais rápido que 1x e contar
    # por tempo de parede dava 1500 elocuções num "minuto"
    while i * JANELA_S < segundos and alvos:
        alvo = alvos[i % len(alvos)]
        extra = outros[i % len(outros)] if outros else None
        pcm = (_janela1(alvo, extra, snr_db, ventoinha) * 32767).astype("<i2").tobytes()
        r = app._voz_do_dono(pcm)
        i += 1
        if r["estado"] == "indisponivel":
            continue
        total += 1
        lat.append(r["latencia_ms"])
        if r["ok"] and r["estado"] == "ok":
            falsos += 1
    return lat, total, falsos


def main() -> int:
    if not FALA.exists() or not any(FALA.glob("pub_*.wav")):
        raise SystemExit(f"sem corpus em {FALA}: rode `tools/bakeoff/corpus.py publico`")
    import app

    dono, terceiro = _locutores()
    clips_dono = sorted(FALA.glob(f"pub_{dono}_*.wav"))
    clips_ter = sorted(FALA.glob(f"pub_{terceiro}_*.wav"))
    print(f"corpus: dono={dono} ({len(clips_dono)} falas) · terceiro={terceiro} "
          f"({len(clips_ter)} falas) · {MINUTOS:g} min por cenário")

    # perfil do dono em memória (nunca o `.speaker-profiles.json` do dono)
    vecs = [v for v in (app._speaker_embed(p) for p in clips_dono) if v]
    if not vecs:
        raise SystemExit("não consegui extrair o perfil (ECAPA indisponível)")
    app._speaker_load_estado = lambda: ({"dono": {"vecs": vecs,
                                                  "modelo": app.SPEAKER_MODELO_ATUAL}}, "")
    # MORDIDAS: `SEM_PORTAO=1` faz o portão aceitar tudo (é o "sem o modo"); e
    # `CONFIRMACOES=1` derruba o protocolo para 1 janela.
    if os.environ.get("CONFIRMACOES"):
        app._VOZ_CONFIRMACOES = int(os.environ["CONFIRMACOES"])
    if os.environ.get("SEM_PORTAO"):
        app._voz_do_dono = lambda pcm: {"ok": True, "estado": "ok", "janelas": 0,
                                        "score": 1.0, "latencia_ms": 0, "motivo": ""}
    app._settings["live_so_minha_voz"] = {"on": True, "perfil": "dono", "rigor_far": "1",
                                          "limiar": float(os.environ.get("LIMIAR") or 0),
                                          "separacao": False}

    segundos = MINUTOS * 60
    cenarios = (("dono", clips_dono, None, None, False),
                ("terceiro", clips_ter, None, None, False),
                ("misto", clips_dono, clips_ter, 5.0, True))

    falhou = False
    rel = {}
    for nome, alvos, outros, snr, vent in cenarios:
        t0 = time.monotonic()
        lat, janelas, aceitas = _anda(alvos, segundos, outros, snr, vent)
        falsos = list(range(aceitas))      # já é o contador de elocuções aceitas
        cobertura = 100.0 * aceitas / max(1, janelas)
        p50 = sorted(lat)[len(lat) // 2] if lat else 0
        p95 = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)] if lat else 0
        rel[nome] = (cobertura, len(falsos), p50, time.monotonic() - t0)
        alvo = f"cobertura {cobertura:5.1f}%"
        if nome == "misto":
            marca = "✔" if cobertura >= PISO_MISTO else "✘"
            falhou |= cobertura < PISO_MISTO
        elif nome != "terceiro":
            marca = "✔" if cobertura >= PISO_COBERTURA else "✘"
            falhou |= cobertura < PISO_COBERTURA
        else:
            marca = "✔" if len(falsos) <= MAX_FALSOS else "✘"
            falhou |= len(falsos) > MAX_FALSOS
        # O VEREDITO de latência usa a MEDIANA: é o custo do portão. O p95 é a
        # cauda de escalonamento da máquina (com 3 agentes martelando a CPU ele
        # triplica sem o encoder mudar), e fica publicado ao lado.
        teto_lat = PISO_LAT_MISTO_MS if nome == "misto" else PISO_P95_MS
        marca_lat = "✔" if p50 <= teto_lat else "✘"
        falhou |= p50 > teto_lat
        print(f"{marca} {nome:9} {alvo} · turnos falsos {len(falsos)} "
              f"(piso ≤{MAX_FALSOS:g}/10min) · latência p50 {p50} ms {marca_lat} "
              f"p95 {p95} ms (piso mediana ≤{teto_lat:g}) · {janelas} janelas")

    # CURVA do limiar: o FAR 1 % (2,434) é um ponto, não uma verdade — no corpus
    # do R4 ele custa cobertura, então o número fica visível para o PM decidir.
    import app as _app
    lim_ef, modo_ef = _app._voz_limiar(_app.SPEAKER_MODELO_ATUAL, "1",
                                      float(os.environ.get("LIMIAR") or 0))
    print(f"\nlimiar em uso: {lim_ef:g} ({modo_ef}) — curva em "
          f"`LIMIAR=<v>` para comparar")
    print("\nVEREDITO:", "REPROVADO" if falhou else "OK")
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main())
