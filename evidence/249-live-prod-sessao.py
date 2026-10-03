#!/usr/bin/env python3
"""Sessão REAL no Live da produção (read-only) — validação do caminho de ÁUDIO (#249).

Faz o que o navegador faz: WS com `?key=`, `setup`, PCM16 16 kHz no mic, `end_of_speech`,
e consumue TODOS os eventos. Três modos:

    turnos   duas falas back-to-back na MESMA sessão (worker/latência entre turnos)
    vao      1 turno; 900 ms DEPOIS do turn_complete injeta fala alta com o cliente
             ainda com áudio na fila (célula MODO=vao do #216) — espera barge_in
    eco      1 turno; DURANTE o playback devolve o áudio recebido (24k->16k) no mic
             (célula de eco) — espera NENHUM barge_in / speech_start fantasma

Nada é escrito no mini. Uso:
    LIVE_KEY="$(ssh lisboa@192.168.15.34 'cat ~/Documents/tts-rod/.apikey')" \
        ./.venv-mlx/bin/python evidence/249-live-prod-sessao.py [turnos|vao|eco]
"""
import asyncio
import json
import os
import sys

import numpy as np
import soundfile as sf
import websockets

URL = os.environ.get("LIVE_URL", "ws://192.168.15.34:7860/api/live/ws")
KEY = os.environ["LIVE_KEY"]
WAV = os.environ.get("LIVE_WAV", "voices/320afe68b8.wav")
SEG = float(os.environ.get("LIVE_SEG", "3.5"))
ESPERA = float(os.environ.get("LIVE_ESPERA", "120"))
MODO = sys.argv[1] if len(sys.argv) > 1 else "turnos"
INJ = 0.9 if MODO == "vao" else 0.0   # atraso da injeção pós turn_complete (s)


def voz_16k(seg: float) -> bytes:
    x, sr = sf.read(WAV, dtype="float32")
    if x.ndim > 1:
        x = x[:, 0]
    x = x[: int(seg * sr)]
    n = int(len(x) * 16000 / sr)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()


def pcm24_para_mic16(pcm24: bytes) -> bytes:
    """Áudio recebido (PCM16 24 kHz) -> mic PCM16 16 kHz (o eco do alto-falante)."""
    x = np.frombuffer(pcm24, dtype="<i2").astype(np.float32) / 32768.0
    n = int(len(x) * 16000 / 24000)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()


class Sessao:
    def __init__(self, ws):
        self.ws = ws
        self.t0 = asyncio.get_event_loop().time()
        self.pcm24_total = 0
        self.pcm24_por_turno = 0
        self.primeiro_audio_ms = None
        self.fala_ms = None
        self.ultimo_pcm = b""
        self.eventos = []

    async def enviar_mic(self, pcm: bytes, cadencia: float = 0.0):
        passo = 3200                                  # 100 ms @16 kHz
        for i in range(0, len(pcm), passo):
            await self.ws.send(pcm[i:i + passo])
            if cadencia:
                await asyncio.sleep(cadencia)

    async def proximo(self, timeout: float):
        msg = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        dt = asyncio.get_event_loop().time() - self.t0
        if isinstance(msg, bytes):
            self.pcm24_total += len(msg)
            self.pcm24_por_turno += len(msg)
            self.ultimo_pcm = msg
            if self.primeiro_audio_ms is None:
                self.primeiro_audio_ms = round(dt * 1000)
            print(f"<< [{dt:7.2f}s] <pcm {len(msg)} bytes> "
                  f"(turno acumulado {self.pcm24_por_turno})", flush=True)
            return {"type": "<pcm>"}
        ev = json.loads(msg)
        tipo = ev.get("type")
        if tipo not in ("stats",):                    # stats poluem: 1 a cada 4
            print(f"<< [{dt:7.2f}s] {json.dumps(ev, ensure_ascii=False)[:600]}",
                  flush=True)
        elif ev.get("t_ms", 0) % 1000 < 250:          # ~1/s
            m = ev.get("motor", {})
            t = ev.get("turno", {})
            p = ev.get("playback", {})
            print(f"<< [{dt:7.2f}s] stats motor={json.dumps(m)} "
                  f"turno.stage={t.get('stage')}/{t.get('ms')}ms "
                  f"n={t.get('n')} playback.speaking={p.get('speaking')} "
                  f"chunks={p.get('chunks')}", flush=True)
        self.eventos.append((round(dt, 2), tipo, ev))
        return ev


async def um_turno(s: Sessao, dados: bytes, eco_ao_playback=False) -> str:
    """Uma fala + coleta até evento terminal. Devolve o tipo do terminal."""
    s.pcm24_por_turno = 0
    s.primeiro_audio_ms = None
    await s.ws.send(json.dumps({"type": "setup"}))
    # drena ready + prewarm (prewarm é o "quente" do pipeline)
    while True:
        try:
            ev = await s.proximo(ESPERA)
        except asyncio.TimeoutError:
            print("[sem prewarm — seguindo mesmo assim]", flush=True)
            break
        if ev.get("type") == "prewarm":
            break
    print(f">> fala enviada ({len(dados)} bytes PCM16 16 kHz)", flush=True)
    await s.enviar_mic(dados, cadencia=0.02)
    await s.ws.send(json.dumps({"type": "end_of_speech"}))

    eco_pendente: list[bytes] = []
    injetado = False
    terminal = None
    while terminal is None:
        try:
            ev = await s.proximo(ESPERA)
        except asyncio.TimeoutError:
            print(f"[timeout {ESPERA:.0f}s sem terminal]", flush=True)
            return "timeout"
        tipo = ev.get("type")
        if tipo == "turn_complete":
            terminal = "turn_complete"
            if ev.get("descartado"):
                print(f"   turn_complete descartado (eco): {ev}", flush=True)
        elif tipo == "interrupted":
            terminal = "interrupted"
        elif tipo == "error":
            terminal = f"error:{ev.get('code')}"
        elif tipo == "<pcm>":
            if eco_ao_playback:
                eco_pendente.append(s.ultimo_pcm)
        # modo eco: devolve o áudio recebido no mic ~em tempo real (um bloco por vez)
        if eco_ao_playback and eco_pendente and not injetado:
            bloco = eco_pendente.pop(0)
            await s.enviar_mic(pcm24_para_mic16(bloco))
        # modo vao: injeta INJ s depois do turn_complete (cliente ainda com fila)
        if MODO == "vao" and terminal == "turn_complete" and not injetado:
            injetado = True
            await asyncio.sleep(INJ)
            print(f">> INJEÇÃO pós-turn_complete (+{INJ:.2f}s, cliente ainda tocando): "
                  f"{len(dados)} bytes de fala alta", flush=True)
            await s.enviar_mic(dados[:22400], cadencia=0.0)   # 700 ms sustentados
            terminal = None                                    # segue coletando
            # recolhe por mais 5 s para ver o resultado da injeção
            try:
                while True:
                    ev = await s.proximo(5.0)
                    if ev.get("type") in ("turn_complete", "interrupted"):
                        terminal = ev["type"]
                        break
                    if ev.get("type", "").startswith("error"):
                        terminal = ev["type"]
                        break
            except asyncio.TimeoutError:
                terminal = "pos-injecao-sem-terminal"
    if eco_ao_playback:
        # 2 s de silêncio no mic depois do fim: nenhuma "fala fantasma" pode abrir
        await s.enviar_mic(b"\x00\x00" * 32000)
        try:
            while True:
                ev = await s.proximo(3.0)
                if ev.get("type") == "speech_start":
                    print("!!! speech_start FANTASMA no pós-playback", flush=True)
                    return "fantasma"
        except asyncio.TimeoutError:
            pass
    return terminal


async def main() -> int:
    dados = voz_16k(SEG)
    rms = float(np.sqrt(np.mean(np.frombuffer(dados, "<i2").astype(np.float64) ** 2)))
    dbfs = 20 * np.log10(max(rms, 1e-10) / 32768)
    print(f"# sessão REAL no Live da produção · {URL} · modo={MODO}\n"
          f"# fala: {SEG:.1f}s de {WAV} · RMS {dbfs:.1f} dBFS\n"
          f"# read-only: nenhum settings/chave do dono é tocado", flush=True)
    async with websockets.connect(f"{URL}?key={KEY}", max_size=None) as ws:
        s = Sessao(ws)
        if MODO == "turnos":
            for i in (1, 2):
                print(f"\n===== TURNO {i} (mesma sessão) =====", flush=True)
                fim = await um_turno(s, dados)
                print(f"== turno {i}: terminal={fim} "
                      f"1º_áudio={s.primeiro_audio_ms}ms "
                      f"áudio_24k={s.pcm24_por_turno}B", flush=True)
                if fim.startswith("error"):
                    print("[turno morreu antes do TTS — worker/latência não "
                          "exercitáveis nesta config]", flush=True)
                    break
        else:
            print(f"\n===== MODO {MODO} =====", flush=True)
            fim = await um_turno(s, dados, eco_ao_playback=(MODO == "eco"))
            print(f"== resultado: terminal={fim}", flush=True)
        print(f"\n# total: áudio 24 kHz recebido={s.pcm24_total}B, "
              f"eventos={len(s.eventos)}", flush=True)
    print("fim da sessão", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
