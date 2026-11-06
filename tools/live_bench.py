#!/usr/bin/env python3
"""Bancada E2E do Live (#875): dirige o WS `/api/live/ws` como um cliente real e
mede os SLOs S1–S5 do épico #874.

Roda NO MINI, contra a instância que está no ar (loopback dispensa chave):

    # o áudio é COPIADO para /tmp antes (nunca mova o original de voices/)
    mkdir -p /tmp/live-bench-audio && cp voices/*.wav voices/*.json /tmp/live-bench-audio/
    .venv-mlx/bin/python -I /tmp/live_bench.py --audio /tmp/live-bench-audio \
        --url ws://127.0.0.1:7860/api/live/ws --out /tmp/live-bench-<sha>.json

O cliente simulado é o do navegador:
  * mic em tempo real, frames PCM16 mono 16 kHz de 100 ms (o tamanho que a UI manda),
    com piso de ruído — o frame sai DEPOIS de capturado, como no AudioWorklet;
  * playback simulado em tempo real: o áudio recebido (24 kHz) é "tocado" no ritmo
    do relógio e volta ao mic como ECO (atenuado `--eco-db`, atraso `--eco-atraso-ms`)
    — é o resíduo que sobra depois do AEC do navegador;
  * `interrupted` esvazia o playback (o cliente corta); `speech_start` e
    `barge_suspeito` contam como DUCK (o cliente em modo `inteligente` abaixa o
    volume nesses dois eventos, rampa de 50 ms).

Roteiro (10 ciclos = 20 falas, áudio real com transcrição de referência):
  ciclo k: fala A (alternando LONGA e CURTA ≤1 s) → a resposta começa a tocar →
  após `--interrompe-apos-ms` de playback entra a fala B por cima (INTERRUPÇÃO) →
  a resposta de B toca INTEIRA com eco reinjetado e ninguém fala (ECO: não pode
  cortar) → pausa aleatória → ciclo seguinte.

SLOs medidos:
  S1 % de falas que viraram turno com transcript + WER (falas com referência);
     falas ≥0,8 s descartadas;
  S2 fim real da fala → 1º áudio da resposta (p50/p95), DECOMPOSTO pelo
     `latency` do turno em `llm_ttft` (1º token) e `pos_llm` (TTS + fila);
  S3 início da fala por cima → duck (evento + rampa de 50 ms), pausa reversível
     (`barge_pausa` ≤800 ms) e corte por conteúdo (`interrupted`, sem
     `motivo:"continuacao"`); falso corte/duck/pausa = sinal durante resposta só
     com eco (o player simulado suspende/retoma como o `340af4b`);
  S4 respostas com símbolo de markdown/emoji/URL no texto que vai ao TTS —
     `texto_para_fala(assistant_text)` (#871; o `assistant_text` cru segue
     medido em `cru_com_simbolo`). Sem `common.py` no diretório do app, cai no
     texto cru e o relatório marca `limpador: null`;
  S5 (`--sessao-min N`) repete os ciclos por N minutos e compara RSS/threads do
     servidor e a saúde no fim.

Privacidade: o JSON de saída tem MÉTRICAS por fala (WER, ms, contagem de
símbolos) — nunca áudio. Os textos (transcript/resposta) só vão para
`--textos <arq>` se pedido, e esse arquivo fica em /tmp.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

SR_MIC = 16000
SR_OUT = 24000
FRAME = 1600                      # 100 ms
FRAME_S = FRAME / SR_MIC
DUCK_RAMPA_MS = 50                # LX_DUCK_MS da UI
PAUSA_TETO_S = 3.0                # LX_PAUSA_TETO_MS do player (#881)
CURTA_S = 0.9                     # fala curta: ≤1 s


# ───────────────────────── áudio ─────────────────────────

def _carrega(caminho: Path) -> np.ndarray:
    import soundfile as sf
    from scipy.signal import resample_poly

    a, sr = sf.read(str(caminho), dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    if sr != SR_MIC:
        from math import gcd
        g = gcd(int(sr), SR_MIC)
        a = resample_poly(a, SR_MIC // g, int(sr) // g).astype(np.float32)
    return a


def _apara(a: np.ndarray, limiar_db: float = -40.0) -> np.ndarray:
    """Tira silêncio do começo e do fim (janelas de 20 ms, relativo ao pico RMS).

    O início real da fala vira a amostra 0 — é dele que o S3 conta."""
    j = 320
    n = len(a) // j
    if n == 0:
        return a
    rms = np.sqrt(np.mean(a[: n * j].reshape(n, j) ** 2, axis=1) + 1e-12)
    ref = rms.max()
    voz = np.where(20 * np.log10(rms / ref + 1e-12) > limiar_db)[0]
    if not len(voz):
        return a
    return a[voz[0] * j: (voz[-1] + 1) * j]


def _normaliza_rms(a: np.ndarray, dbfs: float = -20.0) -> np.ndarray:
    rms = float(np.sqrt(np.mean(a ** 2) + 1e-12))
    a = a * (10 ** (dbfs / 20) / rms)
    return np.clip(a, -0.99, 0.99).astype(np.float32)


def carrega_corpus(pasta: Path) -> list[dict]:
    """WAV + JSON (`ref_text`) de voices/ — só gravações reais (sem preset)."""
    out = []
    for js in sorted(pasta.glob("*.json")):
        try:
            meta = json.loads(js.read_text())
        except Exception:                         # noqa: BLE001
            continue
        wav = js.with_suffix(".wav")
        if meta.get("preset") or not wav.exists() or not meta.get("ref_text"):
            continue
        a = _carrega(wav)
        # o `ref_text` tem de ser a transcrição DESTE áudio. Em voices/ há par
        # cruzado (JSON da Andrea com o WAV da Valentina): o WER do ciclo vira 1
        # sem o servidor ter errado nada. `duration` é o comprimento pretendido.
        dur_meta = float(meta.get("duration") or 0)
        dur_wav = len(a) / SR_MIC
        if dur_meta and abs(dur_wav - dur_meta) > 1.0:
            print(f"[corpus] {js.name}: WAV de {dur_wav:.1f}s contra "
                  f"duration {dur_meta:.1f}s — par cruzado, fora", file=sys.stderr)
            continue
        a = _normaliza_rms(_apara(a))
        out.append({"id": meta.get("id") or js.stem, "audio": a,
                    "ref": str(meta["ref_text"])})
    if len(out) < 2:
        raise SystemExit(f"corpus insuficiente em {pasta}: {len(out)} gravações com ref_text")
    return out


# ───────────────────────── métricas ─────────────────────────

def _palavras(t: str) -> list[str]:
    t = unicodedata.normalize("NFKC", t or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return t.split()


def wer(ref: str, hip: str) -> float:
    r, h = _palavras(ref), _palavras(hip)
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        ant, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, ant + (r[i - 1] != h[j - 1]))
            ant = cur
    return d[len(h)] / len(r)


_SIMBOLOS = {
    "negrito/itálico": re.compile(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)"),
    "título": re.compile(r"(^|\n)\s*#{1,6}\s"),
    "lista": re.compile(r"(^|\n)\s*([-*+•]|\d+[.)])\s"),
    "código": re.compile(r"`"),
    "link": re.compile(r"\[[^\]]*\]\([^)]*\)"),
    "url": re.compile(r"https?://|www\.", re.I),
    "tabela": re.compile(r"\|"),
    "emoji": re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️]"),
}


def simbolos(texto: str) -> dict[str, int]:
    return {k: len(rx.findall(texto or "")) for k, rx in _SIMBOLOS.items()
            if rx.findall(texto or "")}


def _carrega_limpador():
    """`texto_para_fala` do APP — o MESMO limpador que o servidor usa (#871).

    Carrega `common.py` por caminho (o script roda de /tmp, com `-I`): nada de
    `sys.path`. Sem o arquivo — build anterior ao #871 — devolve identidade e o
    relatório sai com `limpador: null`, para o número não ser lido como prova de
    que o servidor limpou."""
    import importlib.util

    base = Path(os.environ.get("TTS_APP_DIR") or os.getcwd())
    alvo = base / "common.py"
    try:
        spec = importlib.util.spec_from_file_location("_bench_common", alvo)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "texto_para_fala"), str(alvo)
    except Exception:                   # noqa: BLE001 — bench não cai por isso
        return (lambda t: t), None


_PARA_FALA, _LIMPA_DE = _carrega_limpador()


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


# ───────────────────────── cliente ─────────────────────────

class Cliente:
    """Mic + playback em tempo real sobre um WS do Live."""

    def __init__(self, ws, eco_db: float, eco_atraso_ms: int, ruido_dbfs: float):
        self.ws = ws
        self.t0 = time.monotonic()
        self.eventos: list[tuple[float, str, dict]] = []
        self.bin: list[tuple[float, int]] = []          # (t, n amostras 24k)
        self.play = np.zeros(0, dtype=np.float32)       # fila de playback (24k)
        self.tocando_desde: float | None = None
        self.tocado_s = 0.0                             # do turno corrente
        self.eco_g = 10 ** (eco_db / 20) if eco_db > -120 else 0.0
        self.eco_fila: list[np.ndarray] = [np.zeros(FRAME, np.float32)] * max(
            0, round(eco_atraso_ms / 100))
        self.ruido = 10 ** (ruido_dbfs / 20)
        self.voz: np.ndarray | None = None
        self.voz_pos = 0
        self.voz_t_ini: float | None = None
        self.voz_t_fim: float | None = None
        self.voz_fim = asyncio.Event()
        self.rng = np.random.default_rng(7)
        self.vivo = True
        self.erro: str | None = None
        self.pausado = False                            # #881: contexto suspenso
        self.pausa_desde = 0.0

    def agora(self) -> float:
        return time.monotonic() - self.t0

    # ---- recepção ----
    async def recebe(self):
        try:
            async for m in self.ws:
                t = self.agora()
                if isinstance(m, (bytes, bytearray)):
                    pcm = np.frombuffer(m, dtype="<i2").astype(np.float32) / 32768.0
                    self.bin.append((t, len(pcm)))
                    self.play = np.concatenate([self.play, pcm])
                    continue
                try:
                    ev = json.loads(m)
                except json.JSONDecodeError:
                    continue
                tipo = str(ev.get("type"))
                if tipo == "stats":
                    self.ultimo_stats = ev
                    continue
                self.eventos.append((t, tipo, ev))
                # #881: o player real SUSPENDE o contexto no `barge_pausa` e
                # retoma no `barge_retoma`; `interrupted` corta. O `interrupted`
                # de continuação (#877) NÃO é corte: a fila fica.
                if tipo == "barge_pausa":
                    self.pausado, self.pausa_desde = True, time.monotonic()
                elif tipo == "barge_retoma":
                    self.pausado = False
                elif tipo == "interrupted":
                    self.pausado = False
                    if ev.get("motivo") != "continuacao":
                        self.play = np.zeros(0, dtype=np.float32)   # o cliente corta
                elif tipo == "pausa_teto":
                    self.pausado = False
        except Exception as exc:                                  # noqa: BLE001
            self.erro = f"{type(exc).__name__}: {exc}"
        finally:
            self.vivo = False

    # ---- mic ----
    def falar(self, audio: np.ndarray) -> asyncio.Event:
        self.voz, self.voz_pos = audio, 0
        self.voz_t_ini = self.voz_t_fim = None
        self.voz_fim = asyncio.Event()
        return self.voz_fim

    async def mic(self):
        from scipy.signal import resample_poly
        prox = time.monotonic() + FRAME_S
        while self.vivo:
            await asyncio.sleep(max(0.0, prox - time.monotonic()))
            # o frame cobre [prox-100ms, prox): sai depois de capturado
            quadro = (self.rng.standard_normal(FRAME).astype(np.float32) * self.ruido)
            # playback: 100 ms tocados neste quadro → eco (atrasado) no mic.
            # PAUSADO (#881) o relógio do contexto congela: nada é consumido e
            # não há eco. O teto local de 3 s devolve sozinho.
            n24 = FRAME * SR_OUT // SR_MIC
            if self.pausado and time.monotonic() - self.pausa_desde > PAUSA_TETO_S:
                self.pausado = False
                self.eventos.append((self.agora(), "pausa_teto", {}))
            tocado = self.play[:n24] if not self.pausado else np.zeros(0, np.float32)
            if not self.pausado:
                self.play = self.play[n24:]
            if len(tocado):
                self.tocado_s += len(tocado) / SR_OUT
                eco = resample_poly(tocado, 2, 3).astype(np.float32)[:FRAME]
                eco = np.pad(eco, (0, FRAME - len(eco)))
            else:
                eco = np.zeros(FRAME, np.float32)
            self.eco_fila.append(eco * self.eco_g)
            quadro += self.eco_fila.pop(0)
            t_quadro_ini = prox - FRAME_S - self.t0
            if self.voz is not None:
                pedaco = self.voz[self.voz_pos: self.voz_pos + FRAME]
                if self.voz_pos == 0:
                    self.voz_t_ini = t_quadro_ini
                quadro[: len(pedaco)] += pedaco
                self.voz_pos += len(pedaco)
                if self.voz_pos >= len(self.voz):
                    self.voz_t_fim = t_quadro_ini + len(pedaco) / SR_MIC
                    self.voz = None
                    self.voz_fim.set()
            pcm = (np.clip(quadro, -1, 1) * 32767).astype("<i2").tobytes()
            try:
                await self.ws.send(pcm)
            except Exception as exc:                               # noqa: BLE001
                self.erro = f"send: {exc}"
                self.vivo = False
                return
            prox += FRAME_S

    # ---- consultas ----
    def evs(self, desde: float, tipos: tuple[str, ...], ate: float | None = None):
        return [(t, ty, e) for t, ty, e in self.eventos
                if t >= desde and ty in tipos and (ate is None or t < ate)]

    def bin_apos(self, t0: float) -> float | None:
        for t, _ in self.bin:
            if t >= t0:
                return t
        return None

    async def espera(self, cond, teto_s: float) -> bool:
        fim = time.monotonic() + teto_s
        while time.monotonic() < fim and self.vivo:
            if cond():
                return True
            await asyncio.sleep(0.05)
        return cond()


async def ciclo(c: Cliente, k: int, fa: dict, fb: dict, curta: bool,
                args, linhas: list[dict]):
    """Uma fala A (resposta interrompida por B) + fala B (resposta toca com eco)."""
    audio_a = fa["audio"][: int(CURTA_S * SR_MIC)] if curta else fa["audio"]
    ref_a = ref_injetada(fa, audio_a)      # cortou o áudio: corta a referência junto
    # ---------- fala A ----------

    await c.falar(audio_a).wait()
    a_ini, a_fim = c.voz_t_ini, c.voz_t_fim
    la = {"fala": 2 * k, "tipo": "curta" if curta else "longa", "voz": fa["id"],
          "dur_s": round(len(audio_a) / SR_MIC, 2), "papel": "A"}
    # espera o 1º áudio da resposta (ou o turno fechar sem áudio)
    await c.espera(lambda: c.bin_apos(a_fim) is not None
                        or c.evs(a_fim, ("turn_complete", "error")), args.teto_turno_s)
    t_audio_a = c.bin_apos(a_fim)
    # resposta falando POR CIMA da própria fala (turno cortado numa pausa natural)
    la["audio_durante_fala"] = any(a_ini + FRAME_S <= t < a_fim for t, _ in c.bin)
    la["s2_ms"] = round((t_audio_a - a_fim) * 1000) if t_audio_a else None
    # (a decomposição do S2 é preenchida mais abaixo: o `latency` do turno só
    # chega no FECHAMENTO, depois do 1º áudio.)
    # ---------- interrupção (fala B por cima da resposta de A) ----------
    interrompeu = False
    if t_audio_a is not None:
        c.tocado_s = 0.0
        await c.espera(lambda: c.tocado_s >= args.interrompe_apos_ms / 1000
                       or (len(c.play) == 0 and c.evs(a_fim, ("turn_complete",))),
                       args.teto_turno_s)
        interrompeu = len(c.play) > 0 or c.tocado_s >= args.interrompe_apos_ms / 1000
    la["interrupcao_valida"] = interrompeu
    await asyncio.sleep(0)
    fim_b = c.falar(fb["audio"])
    await fim_b.wait()
    b_ini, b_fim = c.voz_t_ini, c.voz_t_fim
    lb = {"fala": 2 * k + 1, "tipo": "interrupcao" if interrompeu else "longa",
          "voz": fb["id"], "dur_s": round(len(fb["audio"]) / SR_MIC, 2), "papel": "B"}
    if interrompeu:
        # o 1º frame com voz só chega ao servidor 100 ms depois do início real
        # (o frame sai depois de capturado): evento antes disso não é desta fala
        # — é eco do playback, e entra no `eco_falso_duck`, não aqui.
        causal = b_ini + FRAME_S
        lb["duck_antes_da_fala"] = bool(c.evs(b_ini - 1.0, ("speech_start", "barge_suspeito",
                                                             "barge_pausa"), causal))
        duck = c.evs(causal, ("speech_start", "barge_suspeito"))
        pausa = c.evs(causal, ("barge_pausa",))
        corte = [(t, ty, e) for t, ty, e in c.evs(causal, ("interrupted",))
                 if e.get("motivo") != "continuacao"]
        lb["s3_duck_ms"] = (round((duck[0][0] - b_ini) * 1000) + DUCK_RAMPA_MS
                            if duck else None)
        # pausa reversível (#881): alvo ≤800 ms; o cancel por conteúdo vem depois
        lb["s3_pausa_ms"] = round((pausa[0][0] - b_ini) * 1000) if pausa else None
        lb["s3_stop_ms"] = round((corte[0][0] - b_ini) * 1000) if corte else None
    # ---------- resposta de B toca inteira com eco ----------
    await c.espera(lambda: c.bin_apos(b_fim) is not None
                   or c.evs(b_fim, ("turn_complete", "error")), args.teto_turno_s)
    t_audio_b = c.bin_apos(max(b_fim, (c.evs(b_ini, ("interrupted",)) or [(b_fim,)])[0][0]))
    lb["s2_ms"] = round((t_audio_b - b_fim) * 1000) if t_audio_b else None
    # até o turno fechar E o playback esvaziar
    await c.espera(lambda: c.evs(b_fim, ("turn_complete", "error")) and len(c.play) == 0,
                   args.teto_turno_s * 3)
    eco_fim = c.agora()
    # S2 DECOMPOSTO: o `latency` do turno chega no FECHAMENTO e separa o 1º
    # token da LLM (TTS + fila). `first_token_ms` conta do início da thread do
    # turno, logo depois do fim da fala — o resíduo é a decisão do VAD.
    if t_audio_a is not None:
        for _t, _ty, _e in c.evs(a_fim, ("latency",)):
            if _e.get("cancelado") or _e.get("first_audio_ms") is None:
                continue                   # turno cancelado pela continuação
            la["s2_llm_ttft_ms"] = _e.get("first_token_ms")
            if la["s2_llm_ttft_ms"] is not None:
                la["s2_pos_llm_ms"] = max(0, la["s2_ms"] - la["s2_llm_ttft_ms"])
            break
    lb["eco_tocado_s"] = round(c.tocado_s, 1) if t_audio_b else 0.0
    if t_audio_b is not None:
        lb["eco_falso_corte"] = bool([e for _, _, e in c.evs(
            t_audio_b, ("interrupted",), eco_fim) if e.get("motivo") != "continuacao"])
        lb["eco_falso_duck"] = bool(c.evs(t_audio_b, ("speech_start", "barge_suspeito"),
                                          eco_fim))
        lb["eco_falso_pausa"] = bool(c.evs(t_audio_b, ("barge_pausa",), eco_fim))
    await asyncio.sleep(1.0)            # cauda: evento atrasado ainda conta
    # S1 por CICLO: o servidor corta fala longa em pausa natural, acumula trecho
    # como pendente e funde trechos — atribuir transcript por janela de tempo
    # erra. Cada fala com referência conta pela COBERTURA das palavras dela no
    # que foi transcrito no ciclo (LCS); o WER é do ciclo inteiro.
    hip = _hipotese(c, a_ini, c.agora())
    descartes = [e for _, _, e in c.evs(a_ini, ("turn_complete",))
                 if e.get("descartado")]
    lb["_hip"] = hip
    la["_hip"] = ""
    rb = cobertura(fb["ref"], hip)
    lb.update({"cobertura": rb, "aceito": rb >= ACEITE_COBERTURA})
    if curta:
        # sem referência palavra-a-palavra: aceita se sobrou transcrição além da de B
        sobra = len(_palavras(hip)) - _lcs(_palavras(fb["ref"]), _palavras(hip))
        la.update({"aceito": sobra >= 1, "sobra_palavras": sobra})
    else:
        ra = cobertura(ref_a, hip)
        la.update({"cobertura": ra, "aceito": ra >= ACEITE_COBERTURA})
    # WER em TODO ciclo, com a referência do áudio INJETADO (curta = começo da
    # gravação). Antes só ciclo longo tinha WER e a referência curta era a
    # transcrição inteira.
    lb["wer_ciclo"] = round(min(1.0, wer(ref_a + " " + fb["ref"], hip)), 3)
    lb["turnos_descartados"] = len(descartes)
    lb["pendente"] = bool(c.evs(a_ini, ("turno_pendente",)))
    if t_audio_b is not None and "eco_falso_corte" in lb:
        lb["eco_falso_corte"] = lb["eco_falso_corte"] or bool(
            c.evs(eco_fim, ("interrupted",)))
    linhas += [la, lb]
    if args.verbose:
        for l in (la, lb):
            print(json.dumps({k: v for k, v in l.items() if not k.startswith("_")},
                             ensure_ascii=False), flush=True)
    await asyncio.sleep(random.uniform(args.pausa_min_s, args.pausa_max_s))


ACEITE_COBERTURA = 0.5


def _lcs(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    d = [0] * (len(b) + 1)
    for x in a:
        ant = 0
        for j, y in enumerate(b, 1):
            cur = d[j]
            d[j] = ant + 1 if x == y else max(d[j], d[j - 1])
            ant = cur
    return d[len(b)]


def cobertura(ref: str, hip: str) -> float:
    r = _palavras(ref)
    return round(_lcs(r, _palavras(hip)) / len(r), 3) if r else 1.0


def ref_injetada(fa: dict, audio: "np.ndarray") -> str:
    """Referência da fala A que REALMENTE entrou no mic.

    A fala curta é o COMEÇO da gravação (corte em `CURTA_S`), mas a referência
    era o texto inteiro: comparar 0,9 s de áudio com a transcrição completa dava
    WER ~1 em todo ciclo curto (mediana 0,98 na linha de base). O `ref_text` não
    tem timestamps, então a fração de palavras acompanha a fração de áudio —
    aproximação de ritmo, mas some com o viés de ordem de grandeza.
    """
    total = len(fa["audio"])
    palavras = _palavras(fa["ref"])
    if total <= 0 or len(audio) >= total or not palavras:
        return " ".join(palavras)
    n = max(1, round(len(palavras) * len(audio) / total))
    return " ".join(palavras[:n])


def _hipotese(c: Cliente, ini: float, fim: float) -> str:
    """Transcrição do usuário na janela: `transcript_user` (ou o `transcript`
    do `turn_complete` quando o servidor não mandou o evento)."""
    partes = []
    for _, ty, e in c.evs(ini, ("transcript_user", "turn_complete"), fim):
        if ty == "transcript_user":
            partes.append(e.get("delta") if e.get("delta") is not None else e.get("text", ""))
            e["_usado"] = True
    if not any(partes):
        partes = [e.get("transcript") or "" for _, _, e in c.evs(ini, ("turn_complete",), fim)]
    return " ".join(p for p in partes if p).strip()


def respostas(c: Cliente) -> list[dict]:
    """Respostas da sessão: deltas de `assistant_text` até `turn_complete`,
    `interrupted` ou `error` (S4 é por resposta, não por fala).

    O S4 conta no texto LIMPO (`_PARA_FALA`, o mesmo do servidor); `s4_cru`
    guarda o cru para a comparação antes/depois continuar possível no mesmo
    relatório."""
    out, buf, t_ini = [], [], None

    def _fecha(txt: str, fim: str) -> dict:
        return {"t": round(t_ini, 2), "fim": fim, "chars": len(txt),
                "s4_simbolos": simbolos(_PARA_FALA(txt)),
                "s4_cru": simbolos(txt), "_resp": txt}

    for t, ty, e in c.eventos:
        if ty == "assistant_text":
            if t_ini is None:
                t_ini = t
            buf.append(e.get("delta") or e.get("text") or "")
        elif ty in ("turn_complete", "interrupted", "error") and buf:
            out.append(_fecha("".join(buf), ty))
            buf, t_ini = [], None
    if buf:
        out.append(_fecha("".join(buf), "aberta"))
    return out


def _servidor_proc(porta: int | None = None) -> dict:
    """RSS/threads do processo do app (no mini, via ps).

    Com a porta, acha o DONO do listener (`lsof`): `pgrep -f app.py` casa
    qualquer processo cujo comando cite o arquivo (inclusive o agente que
    escreveu o prompt) e o RSS saía de outro processo."""
    try:
        if porta:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{porta}", "-sTCP:LISTEN", "-t"],
                                 capture_output=True, text=True).stdout.split()
        else:
            out = subprocess.run(["pgrep", "-f", "uvicorn app:app"],
                                 capture_output=True, text=True).stdout.split()
        if not out:
            return {}
        pid = out[0]
        ps = subprocess.run(["ps", "-o", "rss=,%cpu=", "-p", pid],
                            capture_output=True, text=True).stdout.split()
        nth = subprocess.run(["ps", "-M", "-p", pid], capture_output=True,
                             text=True).stdout.count("\n") - 1
        filhos = subprocess.run(["pgrep", "-P", pid], capture_output=True,
                                text=True).stdout.split()
        return {"pid": int(pid), "rss_mb": round(int(ps[0]) / 1024), "threads": nth,
                "filhos": len(filhos)}
    except Exception as exc:                                       # noqa: BLE001
        return {"erro": str(exc)}


async def sessao(args, corpus: list[dict]) -> dict:
    import websockets
    try:
        from websockets.asyncio.client import connect
    except ImportError:                                            # websockets < 13
        connect = websockets.connect

    from urllib.parse import urlparse
    porta = urlparse(args.url).port
    random.seed(args.seed)
    proc_ini = _servidor_proc(porta)
    linhas: list[dict] = []
    t_ini = time.time()
    async with connect(args.url, max_size=None, open_timeout=30,
                       ping_interval=20, ping_timeout=60) as ws:
        setup = {"type": "setup"}
        if args.voice:
            setup["voice_id"] = args.voice
        await ws.send(json.dumps(setup))
        c = Cliente(ws, args.eco_db, args.eco_atraso_ms, args.ruido_dbfs)
        rx = asyncio.create_task(c.recebe())
        if not await c.espera(lambda: c.evs(0, ("ready", "error")), 60):
            raise SystemExit("sem `ready` em 60 s")
        if c.evs(0, ("error",)):
            raise SystemExit(f"setup recusado: {c.evs(0, ('error',))[0][2]}")
        await c.espera(lambda: c.evs(0, ("prewarm",)), args.prewarm_s)
        # O handshake do dsh é ASSÍNCRONO no setup e o prewarm do chat pode
        # falhar antes dele (estado local sem provedor): sem esta folga o 1º
        # turno paga o boot, estoura o teto de 1º delta e a sessão cai no
        # openai — que no estado sintético não tem base URL.
        await asyncio.sleep(6.0)
        mic = asyncio.create_task(c.mic())
        await asyncio.sleep(1.5)                     # ruído de fundo antes da 1ª fala
        ordem = list(corpus)
        k = 0
        prazo = time.monotonic() + args.sessao_min * 60 if args.sessao_min else None
        while c.vivo:
            if prazo is None and k >= args.ciclos:
                break
            if prazo is not None and time.monotonic() >= prazo:
                break
            if k % len(ordem) == 0:
                random.shuffle(ordem)
            fa = ordem[(2 * k) % len(ordem)]
            fb = ordem[(2 * k + 1) % len(ordem)]
            await ciclo(c, k, fa, fb, curta=(k % 2 == 1), args=args, linhas=linhas)
            k += 1
        vivo_no_fim = c.vivo
        mic.cancel()
        await ws.close()
        rx.cancel()
    proc_fim = _servidor_proc(porta)
    resps = respostas(c)
    erros = [e.get("code") for _, ty, e in c.eventos if ty == "error"]
    textos = {"falas": [{"fala": l["fala"], "voz": l["voz"], "hip": l.pop("_hip", "")}
                        for l in linhas],
              "respostas": [r.pop("_resp") for r in resps]}
    return {"linhas": linhas, "respostas": resps, "erros": erros, "textos": textos,
            "proc_ini": proc_ini,
            "proc_fim": proc_fim, "sessao_s": round(time.time() - t_ini),
            "ciclos": k, "ws_vivo_no_fim": vivo_no_fim, "erro_ws": c.erro}


def resumo(res: dict, args) -> dict:
    L = res["linhas"]
    wers = [l["wer_ciclo"] for l in L if "wer_ciclo" in l]
    longas = [l for l in L if l["dur_s"] >= 0.8]
    s2 = [l["s2_ms"] for l in L if l.get("s2_ms") is not None]
    llm = [l["s2_llm_ttft_ms"] for l in L if l.get("s2_llm_ttft_ms") is not None]
    pos = [l["s2_pos_llm_ms"] for l in L if l.get("s2_pos_llm_ms") is not None]
    inter = [l for l in L if l["tipo"] == "interrupcao"]
    eco = [l for l in L if "eco_falso_corte" in l]
    resp = [r for r in res["respostas"] if r["chars"]]
    r = {
        "S1": {"falas": len(L), "aceitas": sum(l["aceito"] for l in L),
               "pct_aceitas": round(100 * sum(l["aceito"] for l in L) / max(1, len(L)), 1),
               "cobertura_media": (round(statistics.mean(
                   l["cobertura"] for l in L if "cobertura" in l), 3)
                   if any("cobertura" in l for l in L) else None),
               "ciclos_com_wer": len(wers),
               "wer_ciclo_medio": round(statistics.mean(wers), 3) if wers else None,
               "wer_ciclo_mediana": round(statistics.median(wers), 3) if wers else None,
               "ciclos_wer_ok_10": sum(w <= 0.10 for w in wers),
               "falas_ge_0_8s_perdidas": sum(not l["aceito"] for l in longas),
               "turnos_descartados_srv": sum(l.get("turnos_descartados", 0) for l in L),
               "meta": "≥95 % aceitas, WER ≤10 %, 0 descarte ≥0,8 s"},
        "S2": {"n": len(s2), "p50_ms": pct(s2, .5), "p95_ms": pct(s2, .95),
               "max_ms": max(s2) if s2 else None,
               "llm_ttft_n": len(llm),
               "llm_ttft_p50_ms": pct(llm, .5),
               "llm_ttft_p95_ms": pct(llm, .95),
               "pos_llm_p50_ms": pct(pos, .5),
               "pos_llm_p95_ms": pct(pos, .95),
               "meta": "p50 ≤1500, p95 ≤2500 (llm_ttft = 1º token; pos_llm = TTS+fila)"},
        "S3": {"interrupcoes": len(inter),
               "duck_n": sum(l.get("s3_duck_ms") is not None for l in inter),
               "duck_p50_ms": pct([l["s3_duck_ms"] for l in inter
                                   if l.get("s3_duck_ms") is not None], .5),
               "duck_max_ms": max([l["s3_duck_ms"] for l in inter
                                   if l.get("s3_duck_ms") is not None], default=None),
               "duck_ok_300": sum((l.get("s3_duck_ms") or 1e9) <= 300 for l in inter),
               "stop_n": sum(l.get("s3_stop_ms") is not None for l in inter),
               "stop_p50_ms": pct([l["s3_stop_ms"] for l in inter
                                   if l.get("s3_stop_ms") is not None], .5),
               "stop_max_ms": max([l["s3_stop_ms"] for l in inter
                                   if l.get("s3_stop_ms") is not None], default=None),
               "stop_ok_800": sum((l.get("s3_stop_ms") or 1e9) <= 800 for l in inter),
               "pausa_n": sum(l.get("s3_pausa_ms") is not None for l in inter),
               "pausa_p50_ms": pct([l["s3_pausa_ms"] for l in inter
                                    if l.get("s3_pausa_ms") is not None], .5),
               "pausa_max_ms": max([l["s3_pausa_ms"] for l in inter
                                    if l.get("s3_pausa_ms") is not None], default=None),
               "pausa_ok_800": sum((l.get("s3_pausa_ms") or 1e9) <= 800 for l in inter),
               "eco_respostas": len(eco),
               "eco_falso_corte": sum(l["eco_falso_corte"] for l in eco),
               "eco_falso_duck": sum(l.get("eco_falso_duck", False) for l in eco),
               "eco_falso_pausa": sum(l.get("eco_falso_pausa", False) for l in eco),
               "meta": "duck ≤300, pausa ≤800, falso corte ≤5 %, falso duck ≤5 %"},
        "S4": {"respostas": len(resp),
               "com_simbolo": sum(bool(r["s4_simbolos"]) for r in resp),
               "por_tipo": _soma([r["s4_simbolos"] for r in resp]),
               "cru_com_simbolo": sum(bool(r["s4_cru"]) for r in resp),
               "cru_por_tipo": _soma([r["s4_cru"] for r in resp]),
               "limpador": _LIMPA_DE,
               "meta": "0 em 50 respostas (texto LIMPO, o que vai ao TTS)"},
        "S5": {"sessao_s": res["sessao_s"], "ciclos": res["ciclos"],
               "ws_vivo_no_fim": res["ws_vivo_no_fim"], "erro_ws": res["erro_ws"],
               "proc_ini": res["proc_ini"], "proc_fim": res["proc_fim"],
               "erros": _soma([{str(e): 1} for e in res["erros"]]),
               "meta": "30 min sem travar nem vazar worker/lock"
                       + ("" if args.sessao_min else " (não rodado: use --sessao-min 30)")},
    }
    return r


def _soma(ds: list[dict]) -> dict:
    out: dict = {}
    for d in ds:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


def imprime(r: dict, L: list[dict]):
    print("\nfala tipo        dur   aceito cobert. WERciclo  S2ms  duck  pausa  stop  eco_corte eco_duck eco_pausa")
    for l in L:
        print(f"{l['fala']:>4} {l['tipo']:<11} {l['dur_s']:>4.1f}  {str(l['aceito']):<6}"
              f" {l.get('cobertura', '—')!s:<7} {l.get('wer_ciclo', '—')!s:<8}"
              f" {l.get('s2_ms') if l.get('s2_ms') is not None else '—'!s:>5}"
              f"  {l.get('s3_duck_ms') if l.get('s3_duck_ms') is not None else '—'!s:>4}"
              f"  {l.get('s3_pausa_ms') if l.get('s3_pausa_ms') is not None else '—'!s:>5}"
              f"  {l.get('s3_stop_ms') if l.get('s3_stop_ms') is not None else '—'!s:>4}"
              f"  {str(l.get('eco_falso_corte', '—')):<9} {l.get('eco_falso_duck', '—')!s:<9}"
              f" {l.get('eco_falso_pausa', '—')!s}")
    print()
    for s, v in r.items():
        print(s, json.dumps(v, ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="ws://127.0.0.1:7860/api/live/ws")
    ap.add_argument("--audio", type=Path, required=True,
                    help="pasta com CÓPIA de voices/*.wav+*.json (nunca o original)")
    ap.add_argument("--voice", default="", help="voice_id da sessão (padrão: o do servidor)")
    ap.add_argument("--ciclos", type=int, default=10, help="2 falas por ciclo")
    ap.add_argument("--sessao-min", type=float, default=0, help="S5: roda N minutos")
    ap.add_argument("--interrompe-apos-ms", type=int, default=1500)
    ap.add_argument("--eco-db", type=float, default=-15.0)
    ap.add_argument("--eco-atraso-ms", type=int, default=100)
    ap.add_argument("--ruido-dbfs", type=float, default=-60.0)
    ap.add_argument("--pausa-min-s", type=float, default=1.0)
    ap.add_argument("--pausa-max-s", type=float, default=4.0)
    ap.add_argument("--teto-turno-s", type=float, default=30.0)
    ap.add_argument("--prewarm-s", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=875)
    ap.add_argument("--out", type=Path, help="JSON de métricas (sem texto/áudio)")
    ap.add_argument("--textos", type=Path, help="transcripts/respostas (fica em /tmp)")
    ap.add_argument("--rotulo", default="", help="ex.: o SHA no ar")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if "voices" in args.audio.resolve().parts:
        raise SystemExit("use uma CÓPIA do áudio (ex.: /tmp/live-bench-audio), não voices/")

    corpus = carrega_corpus(args.audio)
    res = asyncio.run(sessao(args, corpus))
    r = resumo(res, args)
    imprime(r, res["linhas"])
    if args.out:
        args.out.write_text(json.dumps({
            "rotulo": args.rotulo, "quando": time.strftime("%Y-%m-%d %H:%M:%S"),
            "params": {k: (str(v) if isinstance(v, Path) else v)
                       for k, v in vars(args).items() if k not in ("textos",)},
            "corpus": len(corpus), "resumo": r, "falas": res["linhas"],
            "respostas": res["respostas"]},
            ensure_ascii=False, indent=1))
    if args.textos:
        args.textos.write_text(json.dumps(res["textos"], ensure_ascii=False, indent=1))
        os.chmod(args.textos, 0o600)


if __name__ == "__main__":
    sys.exit(main())
