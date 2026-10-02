#!/usr/bin/env python3
"""Probe read-only: UM TURNO REAL no Live da produção (Mac mini) pelo caminho PÚBLICO.

Faz o que o navegador faz — abre o WS com `?key=`, manda `setup`, envia ~3,5 s de
fala (PCM16 16 kHz extraída de uma voz de referência) e imprime TODO evento do
servidor. Não escreve nada no mini.

Uso:
    LIVE_KEY="$(ssh lisboa@192.168.15.34 'cat ~/Documents/tts-rod/.apikey')" \
        ./.venv-mlx/bin/python evidence/245-live-turno-prod.py
"""
import asyncio
import json
import os
import ssl

import numpy as np
import soundfile as sf
import websockets

URL = os.environ.get("LIVE_URL", "wss://tts.the-dudes.com/api/live/ws")
KEY = os.environ["LIVE_KEY"]
WAV = os.environ.get("LIVE_WAV", "voices/320afe68b8.wav")
SEG = float(os.environ.get("LIVE_SEG", "3.5"))
ESPERA = float(os.environ.get("LIVE_ESPERA", "180"))


def pcm16_16k() -> bytes:
    x, sr = sf.read(WAV, dtype="float32")
    if x.ndim > 1:
        x = x[:, 0]
    x = x[: int(SEG * sr)]
    n = int(len(x) * 16000 / sr)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()


async def main() -> int:
    dados = pcm16_16k()
    print(f"# turno REAL contra {URL} · fala de {len(dados) / 32000:.1f}s de {WAV}",
          flush=True)
    t0 = asyncio.get_event_loop().time()
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:                      # noqa: BLE001 — trust store do sistema
        ctx = ssl.create_default_context()
    async with websockets.connect(f"{URL}?key={KEY}", max_size=None,
                                  ssl=ctx) as ws:
        await ws.send(json.dumps({"type": "setup",
                                  "system_instruction": "Responda em uma frase curta."}))
        print("<<", (await ws.recv())[:400], flush=True)
        for i in range(0, len(dados), 3200):          # 100 ms por frame
            await ws.send(dados[i:i + 3200])
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"type": "end_of_speech"}))
        print(f">> fala enviada ({len(dados)} bytes em PCM16 16 kHz)", flush=True)
        ocioso = 0.0
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=ESPERA)
            except asyncio.TimeoutError:
                print(f"[timeout de {ESPERA:.0f}s sem evento]", flush=True)
                break
            except websockets.ConnectionClosed as exc:
                print(f"[fechou code={exc.code} reason={exc.reason!r}]", flush=True)
                break
            dt = asyncio.get_event_loop().time() - t0
            ocioso = 0.0
            if isinstance(msg, bytes):
                print(f"<< [{dt:5.1f}s] <pcm {len(msg)} bytes>", flush=True)
                continue
            ev = json.loads(msg)
            print(f"<< [{dt:5.1f}s] {json.dumps(ev, ensure_ascii=False)[:500]}", flush=True)
            if ev.get("type") == "error" and ev.get("code") not in ("pipeline",):
                break
    print("fim do probe", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
