"""Motor de turnos do modo Live — VAD híbrido (Silero ONNX + energia) e barge-in.

Módulo *standalone*, consumido pelo handler WS do LIVE-1 (`/api/live/ws`):
o handler empurra PCM16 mono 16 kHz em `TurnEngine.feed()` e traduz o que sai
(`speech_start` / `speech_end` / `barge_in`, todos com timestamp) para o
protocolo do socket. `end_of_speech` é o nome do COMANDO do cliente (o humano
aperta parar) → `TurnEngine.flush()`, espelhando o contrato do LIVE-1. Não importa `app` — logo não puxa MLX/Metal — e não faz
saída de rede: o ONNX do Silero mora dentro do pacote `silero_vad`.

Por que híbrido: a probabilidade do Silero sozinha não separa fala de um tom
puro (medido: tom de 200 Hz dá prob 0.68) nem do eco do próprio TTS (o eco é
fala de verdade). Então cada frame só conta como voz com **os dois** — prob do
Silero acima do limiar *e* energia acima de um limiar adaptativo. O limiar de
energia é o que muda durante o playback: enquanto o alto-falante toca, a base
vira uma EMA do nível observado (o eco), não o ruído de fundo.

Barge-in (MVP): durante `speaking` o mic continua sendo consumido. O eco do
alto-falante é tratado com **limiar adaptativo + energia sustentada por
`barge_in_ms` (~300 ms)**, sem AEC: o playback começa (`set_speaking`), o motor
calibra o nível do eco na primeira janela de áudio de verdade e o candidato
precisa passar `eco + barge_in_margin_db` por 300 ms seguidos. Voz que não
chega nesses 4 dB ainda interrompe se o Silero fica alto (`barge_prob_alta`,
~0,85) e o envelope dá um salto sobre o piso de eco recente (`barge_degrau_db`):
eco plano não passa, e a margem de 4 dB não zera. O pré-roll do
buffer cobre `barge_in_ms + prefix_ms + rampa do envelope`, então a primeira
sílaba do humano entra no turno mesmo tendo sido gravada antes de o detector
decidir. Nada disso é chute: `smoke_live_turns.py` mede 85% de barge-in
verdadeiro detectado, **zero** disparo antes de o humano falar e 96% das
rodadas com eco puro sem disparo nenhum (21 wavs do app, 96 rodadas, eco de
-42 a -24 dBFS).

O motor VERIFICA se o playback acrescentou eco: se o nível durante o playback é
~igual ao de antes dele, ou se o mic está bem acima do nível do payload (o
alto-falante só atenua), o eco é declarado ausente e vale o limiar de ocioso. É
o que cobre mic falso de teste, fone de ouvido e eco muito baixo — sem isso a
calibração comia a fala do humano como se fosse eco e ele teria de superar o
PRÓPRIO p90 + margem (medido: fala contínua não supera; e amplificar o estímulo
não resolvia, porque o limiar é relativo e portanto invariante à escala).

`estatisticas()["taxa_falso_barge_in"]` mede o erro — um barge-in que fecha sem
fala NENHUMA além da janela de confirmação (ou que é cancelado) é falso: só o
eco sustentado por 300 ms explica um turno sem fala depois dele. Acima do
`taxa_falso_barge_in_limite` a engine passa a sugerir o fallback de UI "segurar
pra falar" (`hold_to_talk_sugerido`), que o handler liga com `set_hold`.

Fim de fala por silêncio: default **600 ms** — e não abaixo de 500 ms. Abaixo
disso o Whisper perde a última sílaba e o STT degrada (o turno fecha com o
falante ainda respirando no fim da frase); acima, a resposta demora. Está no
guia do Live (`LIVE.md`) junto do resto do contrato.

Uso típico (lado do handler):

    eng = TurnEngine()                        # config default do protocolo
    eng.set_speaking(True, nivel_dbfs=-18.0)  # começou o playback do TTS
    for ev in eng.feed(frame_pcm16):          # frames de ~100 ms vindos do WS
        enviar(ev.to_json())                  # speech_start / speech_end / barge_in
    eng.set_speaking(False)                   # playback acabou
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, NamedTuple

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512                       # janela que o Silero ONNX exige
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE
PREFIX_MS_PADRAO = 100
SILENCE_MS_PADRAO = 600
SILENCE_MS_MINIMO_STT = 500               # abaixo disto o STT come a última sílaba

Scorer = Callable[[np.ndarray], float]

# ── classificação de barge por CONTEÚDO (#501) ────────────────────────────
# O corte por energia derruba a resposta em "uhum" e em tosse. A decisão passa a
# olhar o TEXTO do trecho: comando/pergunta/3+ palavras cortam; backchannel puro,
# ruído e eco deixam a resposta seguir. Listas pt/en, normalizadas (sem acento,
# minúsculas) — comparação por conjunto, não por substring.
_COMANDO_PARAR = frozenset("""
para pare parar espera esperar perai pera calma chega chega_ai nao n pare_ai
stop wait hold hold_on holdon hang_on quiet silence enough cancel cancela
""".split())
_BACKCHANNEL = frozenset("""
uhum aham hum hmm hm mm ahn aha a e eh sim sei ok okay ta tá certo certinho entendi entendido isso
claro beleza legal nossa mhm yeah yep right sure humhum uhum ah ahh oh
""".split())
# onomatopeias/ruído escrito: NÃO são conteúdo próprio (não cortam sozinhas)
_RUIDO_ONO = frozenset("pff tss psst shh zzz".split())
_PERGUNTA = frozenset("""
que o_que qual quais quando onde como quem quanto quantos porque por_que
cadê cade what why how when where who which
""".split())


def _sem_acento(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


def _palavras(texto: str) -> list[str]:
    """Palavras normalizadas, sem pontuação (``peraí,`` → ``perai``)."""
    import re
    low = _sem_acento((texto or "").lower())
    return re.findall(r"[a-z0-9_]+", low)


def classifica_barge(texto: str, fala_ms: int, ref_eco: str = "",
                     idioma: str = "pt") -> str:
    """Classifica o trecho que interrompeu: `interrupcao` | `backchannel` |
    `ruido` | `eco`.

    Função PURA (sem motor, sem I/O) — é o que permite tabela de casos em teste.
    A ordem responde ao risco de cada erro: cortar por engano custa a resposta,
    deixar de cortar custa o turno do usuário, então comando e pergunta vêm
    antes de qualquer coisa; backchannel só depois de descartar comando/pergunta;
    eco (o assistente ouvindo a si mesmo) por último e com a regra do #301."""
    palavras = _palavras(texto)
    # 1) fala LONGA é intenção, mesmo sem texto útil (o STT pode atrasar)
    if fala_ms >= 1200:
        return "interrupcao"
    if not palavras:
        return "ruido"
    if ref_eco:
        # import tardio: `live_pipeline` é quem tem a regra do #301 e importar no
        # topo criaria ciclo (o pipeline consome este módulo)
        from live_pipeline import _parece_eco
        if _parece_eco(texto, ref_eco):
            return "eco"
    # 2) comando de parar: 1 palavra basta, e só vale nas posições de comando
    # (começo ou fim) — "sim, espera" corta; "espera" no meio de frase longa já
    # caiu na regra de 3+ palavras.
    if palavras[0] in _COMANDO_PARAR or palavras[-1] in _COMANDO_PARAR:
        return "interrupcao"
    # 3) pergunta
    if "?" in (texto or "") or any(p in _PERGUNTA for p in palavras):
        return "interrupcao"
    # 4) fala com conteúdo próprio
    if len(palavras) >= 3 and not all(p in _BACKCHANNEL for p in palavras):
        return "interrupcao"
    # 5) backchannel puro (curto e todo da lista)
    if len(palavras) <= 2 and all(p in _BACKCHANNEL for p in palavras):
        return "backchannel"
    return "interrupcao"


class Estado(str, Enum):
    OCIOSO = "ocioso"
    FALANDO = "falando"


# --------------------------------------------------------------------------
# modelo (Silero ONNX explícito, sem fallback silencioso para o jit)
# --------------------------------------------------------------------------
_modelo = None
BACKEND = ""  # "onnx" | "torch-jit" — caminho realmente carregado


def carregar_modelo():
    """Silero VAD carregado sob demanda, pedindo ONNX EXPLICITAMENTE.

    Mesma razão do `app._vad_load`: em `silero-vad` 6.x o default de
    `load_silero_vad()` virou `onnx=False`, então chamar sem argumento carrega
    o jit do torch mesmo com onnxruntime instalado — silenciosamente. Aqui o
    fallback existe, mas é logado e observável (`BACKEND`), nunca silencioso.

    O `silero_vad.onnx` é lido do pacote (nenhum download → o módulo não faz
    saída de rede)."""
    global _modelo, BACKEND
    if _modelo is None:
        from silero_vad import load_silero_vad
        try:
            _modelo = load_silero_vad(onnx=True)
            BACKEND = "onnx"
        except Exception as e:  # noqa: BLE001 — sem onnxruntime: cai no torch
            print(f"[live_turns] modelo ONNX indisponível ({str(e)[:120]}) — "
                  "caindo no torch jit", flush=True)
            BACKEND = "torch-jit"
            _modelo = load_silero_vad(onnx=False)
    return _modelo


def scorer_silero(f32: np.ndarray) -> float:
    """Probabilidade de fala (0..1) do frame de 512 amostras @16 kHz."""
    import torch

    modelo = carregar_modelo()
    with torch.no_grad():
        return float(modelo(torch.from_numpy(np.ascontiguousarray(f32, dtype=np.float32)),
                            SAMPLE_RATE))


def _dbfs(rms: float) -> float:
    """dBFS de um RMS (piso em -140 dBFS: silêncio digital não vira -inf)."""
    return 20.0 * math.log10(max(rms, 1e-7))


def _pcm16(f32: np.ndarray) -> bytes:
    return (np.clip(f32, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


# --------------------------------------------------------------------------
# configuração
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    prefix_ms: int = PREFIX_MS_PADRAO
    silence_ms: int = SILENCE_MS_PADRAO
    onset_ms: int = 2 * FRAME_MS                  # 2 frames confirmam o início
    barge_in_ms: int = 300                        # energia sustentada exigida
    # #707: FOLGA dentro do burst. O contador do braço do barge zerava no
    # primeiro frame abaixo do limiar e fala com pausa entre palavras ("Para,
    # para, espera...") nunca somava `barge_in_ms`: o pico ficava em 2–3 de 10,
    # nenhuma suspeita saía e a rodada caía num turno de 64 ms, que o VAD do STT
    # recusa (texto vazio em 3 ms). Zerar só depois desta folga seguida mantém a
    # defesa contra o eco: eco é PLANO, não tem burst intermitente.
    barge_folga_ms: int = 128
    barge_in_margin_db: float = 4.0               # margem sobre o nível de eco (medido)
    # #273: a voz no alto-falante sobe o piso e a fala normal não passa
    # eco+4 dB, então o speech_start não sai e o cliente não corta. Caminho
    # paralelo — não substitui a margem. Silero de fala nítida (a ordem do
    # barge da Conversa) E um salto do envelope sobre o piso recente. Eco
    # plano fica abaixo do degrau.
    barge_prob_alta: float = 0.85
    barge_degrau_db: float = 2.0
    fala_threshold: float = 0.5                   # prob mínima do Silero
    energia_margin_db: float = 6.0                # margem p/ ABRIR turno
    energia_margin_turno_db: float = 3.0          # margem p/ MANTER turno (histerese)
    min_fala_ms: int = 250                        # turno mais curto que isto é "curto"
    # #501: interrupção INTELIGENTE. `imediata` = comportamento antigo (corta no
    # limiar de energia); `so_comando` = só a lista de parar corta; `inteligente`
    # (default) ouve até `barge_decisao_ms`/`speech_end` e decide pelo CONTEÚDO.
    # Default do MOTOR é `imediata` (contrato antigo, sem handler de STT); o app
    # passa o modo do setting, cujo default é `inteligente`.
    interrupcao: str = "imediata"
    barge_decisao_ms: int = 700                   # janela de escuta antes de decidir
    cauda_ms: int = 200                           # silêncio guardado no áudio do turno
    turno_max_ms: int = 30000
    piso_ruido_dbfs: float = -60.0
    teto_ruido_dbfs: float = -30.0
    eco_perda_inicial_db: float = 12.0            # palpite ao abrir o playback
    eco_calibracao_ms: int = 400                  # janela que acha o nível do eco
    # 6 e não 3 (#674): o teto é a trava contra calibração que não converge; com 3
    # janelas (1,25 s) um TTS que abre com pausa legítima fechava no silêncio. 6
    # (~2,5 s) respeita a pausa e ainda trava muito antes dos ~14 s medidos.
    # FUSÃO (#674): depois de um `interrupted`, suspeita nova dentro desta janela se
    # FUNDE na fala em curso (não corta a resposta que acabou de nascer). "Para,
    # para, para" abria um barge por repetição e a resposta nunca saía.
    barge_fusao_ms: int = 1500
    eco_calibracao_teto: int = 6
    eco_calibracao_quantil: float = 90.0          # estatística da janela (0-100)
    eco_pre_janela_ms: int = 1500                 # janela de nível ANTES do playback
    eco_contrib_min_db: float = 3.0               # abaixo disto o playback não somou eco
    eco_folga_payload_db: float = 8.0             # folga de pico: eco não passa do payload
    envelope_ms: int = 224                        # suavização do nível (decisão)
    playback_janela_ms: int = 900                 # janela de playback (toca + cauda)
    # Janela de barge INTRA-TURNO (#167) — LIGADA por default desde o #216.
    #
    # Ligada, o handler marca o turno do assistente em `set_turno_aberto` e a janela
    # não fecha nos vãos de LLM/TTS (medidos até 20 s). Ficou desligada por uma
    # medição EM ISOLADO (o sentido do falso barge por eco piorava); a medição da
    # COMBINAÇÃO (`tests/live_barge_rep.sh`, REP=20, evidência em `evidence/216-*`)
    # mostrou que o prejuízo era da outra alavanca sozinha, não desta: junto com
    # `playback_por_duracao` o cenário do defeito (MODO=vao) sai de 0/20 para 20/20
    # (duas rodadas) e o sentido do eco fica no melhor valor medido (20 cortes
    # falsos, contra 34 e 20 do baseline e 36-45 de toda célula com `eco_so_tocando`).
    # Env: TTS_LIVE_BARGE_JANELA_TURNO=0 volta ao comportamento antigo.
    barge_janela_turno: bool = True
    playback_turno_max_ms: int = 120000           # trava: turno "aberto" esquecido
    playback_audio_max_ms: int = 60000            # teto do áudio "em voo" acumulado
    # #167: janela dimensionada pela DURAÇÃO REAL do chunk (+ backlog em voo).
    # LIGADA por default desde o #216: é ela que cobre o áudio que o cliente AINDA
    # tem na fila quando o turno termina — o residual que o harness mede com
    # MODO=vao (0/20 sem o par, 20/20 com ele). O `live_ui.sh` que ela derrubava
    # quando isolada foi reconciliado (o dublê do `set_speaking` recebe `duracao_ms`)
    # e passa com ela ligada. Env: TTS_LIVE_PLAYBACK_DURACAO=0 desliga.
    playback_por_duracao: bool = True
    # #167 (direção c): a referência de ENERGIA (piso de eco, limiares, adaptação)
    # passa a olhar "há áudio audível AGORA?" — e não a JANELA. A janela é sobre o
    # TURNO (um onset nela é interrupção); o eco só existe quando algo toca. Nas
    # duas alavancas acima a janela fica aberta nos VÃOS de geração, onde NADA
    # toca: mantendo o eco como referência ali, o humano teria de superar o nível
    # do TTS que já parou. DESLIGADA: medida em COMBINAÇÃO (#216) ela PIORA o
    # sentido do ECO com folga — toda célula com ela ficou em 36-45 cortes falsos
    # contra 20-35 das sem ela (o par A+B, que é o default, dá 20). No vão ela NÃO
    # separa: 15/20 e 20/20 nas duas rodadas, contra 20/20 e 20/20 do par sem ela.
    # O caso para o qual ela foi inventada — fala BAIXA no vão (limiar de ocioso em
    # vez do eco) — também foi medido e TAMBÉM não a favorece: `vao-quieto-*`
    # (AMP=0.2, ~-31 dBFS) dá 20/20 sem ela e 13/20 com ela, pelo mesmo mecanismo
    # (o dreno do backlog durante o turno esvazia a janela antes do `turn_complete`).
    # Ver `evidence/216-DECISAO.md`.
    eco_so_tocando: bool = False
    adaptacao_ruido_db: float = 0.25              # por frame, só fora de voz
    adaptacao_eco_db: float = 0.25                # por frame, só fora de voz
    eco_faixa_morta_db: float = 3.0               # zona em que o eco não se mexe
    hold_to_talk: bool = False                    # fallback: só turno com botão
    sugestao_apos_barge_in: int = 5
    taxa_falso_barge_in_limite: float = 0.3

    def __post_init__(self) -> None:
        if self.interrupcao not in ("inteligente", "imediata", "so_comando"):
            object.__setattr__(self, "interrupcao", "inteligente")
        for nome in ("prefix_ms", "silence_ms", "onset_ms", "barge_in_ms", "min_fala_ms",
                     "barge_decisao_ms", "barge_folga_ms",
                     "cauda_ms", "turno_max_ms", "playback_janela_ms", "eco_calibracao_ms",
                     "envelope_ms", "eco_pre_janela_ms"):
            if int(getattr(self, nome)) < 0:
                raise ValueError(f"{nome} não pode ser negativo")
        if self.silence_ms <= 0:
            raise ValueError("silence_ms precisa ser > 0 (senão o turno nunca fecha)")
        if self.onset_ms < FRAME_MS:
            raise ValueError(f"onset_ms precisa ser >= {FRAME_MS} (1 frame)")
        if not 0.0 < self.fala_threshold < 1.0:
            raise ValueError("fala_threshold precisa estar em (0, 1)")
        if not 0.0 < self.barge_prob_alta < 1.0:
            raise ValueError("barge_prob_alta precisa estar em (0, 1)")
        if self.barge_degrau_db <= 0:
            raise ValueError("barge_degrau_db precisa ser > 0 (eco plano não é barge)")
        if self.barge_degrau_db >= self.barge_in_margin_db:
            raise ValueError(
                "barge_degrau_db fica abaixo da margem de eco — não a substitui")
        if not self.piso_ruido_dbfs < self.teto_ruido_dbfs < 0:
            raise ValueError("piso/teto de ruído são dBFS (negativos), com piso < teto")

    @property
    def avisos(self) -> list[str]:
        avisos = []
        if self.silence_ms < SILENCE_MS_MINIMO_STT:
            avisos.append(
                f"silence_ms={self.silence_ms} < {SILENCE_MS_MINIMO_STT}: abaixo de "
                "meio segundo o Whisper perde a última sílaba — use >= 500 (default 600)")
        return avisos

    @property
    def _frames_onset(self) -> int:
        return max(1, math.ceil(self.onset_ms / FRAME_MS))

    @property
    def _frames_barge(self) -> int:
        return max(1, math.ceil(self.barge_in_ms / FRAME_MS))

    @property
    def _frames_barge_folga(self) -> int:
        """Frames seguidos SEM voz que ainda não zeram o braço (#707)."""
        return max(1, math.ceil(self.barge_folga_ms / FRAME_MS))

    @property
    def _frames_decisao_barge(self) -> int:
        return max(1, math.ceil(self.barge_decisao_ms / FRAME_MS))

    @property
    def _frames_prefix(self) -> int:
        return math.ceil(self.prefix_ms / FRAME_MS)

    @property
    def _historico_frames(self) -> int:
        # o pré-roll de um barge-in pode ter de cobrir a calibração do eco
        # inteira: o humano pode começar a falar durante ela. A suavização do
        # envelope entra porque ela atrasa a decisão em ~envelope_ms/2.
        return self._frames_prefix + self._frames_envelope + 2 + max(
            self._frames_onset,
            self._frames_barge + self.eco_calibracao_teto * self._frames_calibracao)

    @property
    def _frames_envelope(self) -> int:
        return max(1, round(self.envelope_ms / FRAME_MS))

    @property
    def _frames_cauda(self) -> int:
        return math.ceil(self.cauda_ms / FRAME_MS)

    @property
    def _frames_playback(self) -> int:
        return max(1, math.ceil(self.playback_janela_ms / FRAME_MS))

    @property
    def _frames_turno_assistente_max(self) -> int:
        return max(1, math.ceil(self.playback_turno_max_ms / FRAME_MS))

    @property
    def _frames_audio_max(self) -> int:
        return max(1, math.ceil(self.playback_audio_max_ms / FRAME_MS))

    @property
    def _frames_pre(self) -> int:
        return max(1, round(self.eco_pre_janela_ms / FRAME_MS))

    @property
    def _frames_calibracao(self) -> int:
        return max(1, math.ceil(self.eco_calibracao_ms / FRAME_MS))

    @property
    def _frames_silencio(self) -> int:
        return max(1, math.ceil(self.silence_ms / FRAME_MS))

    @property
    def _frames_turno_max(self) -> int:
        return max(1, math.ceil(self.turno_max_ms / FRAME_MS))


@dataclass
class TurnEvent:
    """Evento de turno. `t_ms` é medido do início da sessão (relógio monotônico)
    e `amostra` é a posição no stream @16 kHz — nos eventos de início ele é
    RETROAGIDO ao começo do prefixo (o áudio veio antes da decisão)."""
    tipo: str                       # speech_start | speech_end | barge_in
    t_ms: int                       # início do evento no stream (retroagido)
    ts: float
    amostra: int
    t_decisao_ms: int = 0           # quando o detector DECIDIU (latência real)
    audio: bytes = b""              # PCM16 16k mono
    prob: float = 0.0
    rms_dbfs: float = -120.0
    detalhe: str = ""
    fala_ms: int = 0
    curto: bool = False
    barge_in: bool = False
    barge_falso: bool = False       # barge-in sem fala além da janela de confirmação
    ms_desde_evento_anterior: int | None = None
    # #501: decisão pelo conteúdo
    tipo_barge: str | None = None   # interrupcao | backchannel | ruido | eco
    texto: str | None = None
    p_cortar: float | None = None
    modelo_ms: int | None = None
    filtro: str | None = None       # QUAL filtro decidiu (auditoria do adendo)

    def to_json(self) -> dict:
        """Forma de fio do protocolo (o áudio do turno vai em frame binário)."""
        d = {"type": self.tipo, "t_ms": self.t_ms, "ts": round(self.ts, 3),
             "amostra": self.amostra, "t_decisao_ms": self.t_decisao_ms,
             "prob": round(self.prob, 3),
             "rms_dbfs": round(self.rms_dbfs, 1), "curto": self.curto,
             "barge_in": self.barge_in}
        if self.detalhe:
            d["detalhe"] = self.detalhe
        if self.tipo == "speech_end":
            d["fala_ms"] = self.fala_ms
            d["barge_falso"] = self.barge_falso
        if self.tipo_barge is not None:
            d["tipo"] = self.tipo_barge
        if self.texto is not None:
            d["texto"] = self.texto
        if self.p_cortar is not None:
            d["p_cortar"] = round(self.p_cortar, 3)
        if self.modelo_ms is not None:
            d["modelo_ms"] = self.modelo_ms
        if self.filtro is not None:
            d["filtro"] = self.filtro
        if self.audio:
            d["audio_bytes"] = len(self.audio)
        return d


class _Quadro(NamedTuple):
    """Frame já pontuado, guardado no pré-roll (prob/env prontos: recalcular o
    Silero no pré-roll seria uma segunda passada no modelo)."""
    amostra: int
    pcm: np.ndarray
    prob: float
    env_dbfs: float


class TurnEngine:
    """FSM de turnos. Use uma instância por sessão de WS."""

    def __init__(self, config: Config | None = None, scorer: Scorer | None = None,
                 relogio: Callable[[], float] = time.monotonic,
                 agora: Callable[[], float] = time.time) -> None:
        self.config = config or Config()
        self._scorer = scorer
        self._relogio = relogio
        self._agora = agora
        self._t0 = relogio()

        self._estado = Estado.OCIOSO
        self._speaking = False
        self._hold = False
        self._pendente = np.zeros(0, dtype=np.float32)
        self._proxima_amostra = 0

        self._historico: deque[_Quadro] = deque(maxlen=self.config._historico_frames)
        self._turno: list[np.ndarray] = []
        self._turno_amostra = 0
        self._turno_barge = False
        self._turno_hold = False
        self._fala_frames = 0
        self._fala_extra_frames = 0
        self._silencio_frames = 0
        self._ultimo_evento_ms: int | None = None

        self._onset_frames = 0
        self._barge_frames = 0
        self._playback_frames = 0
        self._turno_aberto = False          # turno do ASSISTENTE em voo (#167)
        self._turno_aberto_frames = 0
        self._playback_restante = 0         # frames a tocar do último chunk (#167)
        self._debug = os.environ.get("LIVE_TURNS_DEBUG") == "1"
        self._calibrando = False
        self._calibracao_vals: list[float] = []
        self._eco_ausente = False
        self._degrau_base: float | None = None   # piso travado no salto (#273)
        self._barge_no_turno_emitido = False
        self._barge_sem_voz = 0      # #707: folga corrida dentro do burst
        self._barge_pico = 0         # #707: maior burst desde o último zero
        self._funde_ate = 0.0        # monotonic: até aqui não abre barge novo
        self._cortado = False        # cliente descartou a fila (barge/cancel)
        self._suspeito = False          # #501: turno aberto por suspeita, sem decisão
        self._decisao_frames = 0
        self._suspeito_audio = b""
        self._nivel_payload: float | None = None
        self._pre_env: deque[float] = deque(maxlen=self.config._frames_pre)
        self._env: deque[float] = deque(maxlen=self.config._frames_envelope)
        self._noise_dbfs = -50.0
        self._eco_dbfs = -50.0

        self._stat = {"turnos": 0, "turnos_curtos": 0, "barge_in": 0,
                      "barge_in_falso": 0, "cancelados": 0,
                      "barge_in_com_turno_aberto": 0}

        for aviso in self.config.avisos:
            print(f"[live_turns] aviso: {aviso}", flush=True)

    # -- leitura ---------------------------------------------------------
    @property
    def estado(self) -> Estado:
        return self._estado

    @property
    def turno_aberto(self) -> bool:
        return self._estado is Estado.FALANDO

    @property
    def speaking(self) -> bool:
        return self._speaking

    def _playback_ativo(self) -> bool:
        """JANELA de playback — e não a flag instantânea.

        O handler marca o playback no ENVIO e desmarca quando a fila esvazia, o
        que no regime de um chunk por vez vira um par `set_speaking(True)` +
        `set_speaking(False)` no mesmo instante: a flag fica ligada ~0 ms e
        qualquer decisão que dependa dela (contador de 300 ms do barge,
        calibração de eco) nunca acumula. A janela mantém o regime de playback
        enquanto ele toca E por `playback_janela_ms` depois do último stop (o
        cliente ainda tem áudio bufferizado).

        Vale também como "eco ainda é referência": é o que impede o eco
        residual de virar fala fantasma.

        #167: com `set_turno_aberto(True)` a janela NÃO fecha nos VÃOS de
        geração (LLM/TTS entre chunks) — antes, um vão maior que
        `playback_janela_ms` (medido: até 20 s) fazia o onset do humano virar
        turno NOVO em vez de interrupção. O turno fecha por
        `set_turno_aberto(False)` e aí vale a cauda normal."""
        return self._speaking or self._turno_aberto or self._playback_frames > 0

    def _tocando(self) -> bool:
        """Há áudio AUDÍVEL agora (o eco existe) — distinto da JANELA.

        A janela (`_playback_ativo`) responde "um onset aqui é interrupção?" e
        inclui o turno em voo, com os vãos de geração dentro. O eco, porém, só
        existe enquanto algo está no alto-falante: servidor enviando
        (`_speaking`) ou o cliente ainda com áudio na fila (a cauda
        `_playback_frames` e o backlog real `_playback_restante`). Num vão sem
        nada tocando o regime tem de ser o de OCIOSO — senão o humano disputa o
        limiar com o nível de um TTS que já parou."""
        return (self._speaking or self._playback_frames > 0
                or self._playback_restante > 0)

    def _ref_audio(self) -> bool:
        """Qual das duas leituras governa a ENERGIA (eco x ruído)."""
        return (self._tocando() if self.config.eco_so_tocando
                else self._playback_ativo()) and not self._fundindo()

    def _fundindo(self) -> bool:
        """Ainda na fusão pós-corte: o playback é FUNDO, a fala é o primeiro plano.

        Logo depois de um `interrupted` (e da borda da resposta nova) o eco não
        serve de referência de energia: a pessoa já está falando, a resposta
        acabou de nascer e o que se ouve no mic é ela. Medido no `misto`, a
        calibração comia a fala como eco (eco -12,8 contra voz -13,4 dBFS) e o
        limiar subia acima da própria voz — o turno não abria e a sessão ficava
        parada com o motor `ocioso` recebendo frames de voz."""
        return time.monotonic() < self._funde_ate

    def _janela_log(self, motivo: str, antes: bool) -> None:
        """Diagnóstico das transições da JANELA (LIVE_TURNS_DEBUG=1).

        Serve para correlacionar com a injeção do harness de barge: é o estado que
        arma (ou não) o barge-in."""
        if not self._debug:
            return
        agora = self._playback_ativo()
        if agora != antes:
            print(f"[JANELA-ENG] {'ABERTA' if agora else 'fechada'} ({motivo})",
                  flush=True)

    def _base_energia(self) -> float:
        # eco declarado ausente (o playback não somou nível): o regime é o de
        # ocioso, senão o estímulo humano teria de superar o PRÓPRIO p90 + margem
        if self._eco_ausente:
            return self._noise_dbfs
        return max(self._noise_dbfs, self._eco_dbfs) if self._ref_audio() else self._noise_dbfs

    @property
    def limiar_energia_dbfs(self) -> float:
        """Limiar para ABRIR turno (dBFS). Durante o playback parte do eco."""
        margem = (self.config.barge_in_margin_db if self._ref_audio()
                  else self.config.energia_margin_db)
        return self._base_energia() + margem

    @property
    def limiar_energia_turno_dbfs(self) -> float:
        """Limiar para MANTER o turno aberto — mais frouxo que o de abertura
        (histerese): pausa de respiração não pode fechar o turno. Com eco ativo
        a margem volta a ser a do barge: o eco tem picos de ~6 dB acima da
        mediana e não pode ficar resetando o contador de silêncio."""
        margem = (self.config.barge_in_margin_db if self._ref_audio()
                  else self.config.energia_margin_turno_db)
        return self._base_energia() + margem

    def estatisticas(self) -> dict:
        total = self._stat["barge_in"]
        taxa = (self._stat["barge_in_falso"] / total) if total else 0.0
        return {**self._stat,
                "taxa_falso_barge_in": round(taxa, 4),
                # #707: maior burst do episódio (de `_frames_barge`). Abaixo do
                # alvo = a fala não sustentou; no alvo sem barge = outra coisa.
                "barge_pico": self._barge_pico,
                "noise_dbfs": round(self._noise_dbfs, 1),
                "eco_dbfs": round(self._eco_dbfs, 1),
                "playback_ativo": self._playback_ativo(),
                "tocando": self._tocando(),
                "turno_assistente_aberto": self._turno_aberto,
                "eco_ausente": self._eco_ausente,
                "hold_to_talk": self._hold or self.config.hold_to_talk,
                "hold_to_talk_sugerido": bool(
                    total >= self.config.sugestao_apos_barge_in
                    and taxa > self.config.taxa_falso_barge_in_limite
                    and not self.config.hold_to_talk)}

    # -- controle do handler ---------------------------------------------
    def set_speaking(self, ligado: bool, nivel_dbfs: float | None = None,
                     duracao_ms: float | None = None) -> None:
        """Liga/desliga o estado de playback. `nivel_dbfs` (opcional) é o nível
        do chunk de TTS que vai para o alto-falante: dele sai o palpite inicial
        do eco, refinado pela EMA enquanto o usuário não fala.

        #167: a BORDA de áudio (`ligado` vindo de `não speaking`) é o que
        recalibra o eco — com a janela colada ao turno o pulso True/False por
        chunk deixou de ser a borda que reabre a janela, e sem isto o
        `_eco_ausente` declarado num VÃO (nada tocando) ficaria preso mesmo com o
        TTS voltando a tocar, derrubando o limiar e convidando barge falso."""
        janela_antes = self._playback_ativo()
        borda_audio = bool(ligado) and not self._speaking
        if ligado and duracao_ms and self.config.playback_por_duracao:
            # #167: o cliente BUFFERIZA — depois do último envio ele ainda vai tocar
            # TODO o áudio já mandado. Sem isto a janela expirava no meio do próprio
            # chunk e um onset ali virava turno novo (medido: falha exatamente
            # depois de `turn_complete`, com o cliente ainda com áudio na fila).
            # ACUMULA (o backlog é a soma dos chunks) e escoa em tempo real.
            # A cauda NÃO entra por chunk (#547): somada a cada envio ela inflava o
            # backlog em `cauda_ms` por chunk — 40 chunks numa resposta de 25 s
            # davam ~8 s de fantasma, e o `fim_de_resposta` (que lê este número)
            # disparava segundos ANTES do fim real (medido: cliente com fila 0 e o
            # motor dizendo 7,4 s). A cauda real já é coberta no `set_speaking(False)`
            # abaixo, que arma `_frames_playback` (900 ms) sobre o que restar.
            self._playback_restante = min(
                self.config._frames_audio_max,
                self._playback_restante
                + int(math.ceil(float(duracao_ms) / FRAME_MS)))
        if borda_audio:
            palpite = (nivel_dbfs - self.config.eco_perda_inicial_db
                       if nivel_dbfs is not None else self._noise_dbfs + 6.0)
            self._eco_dbfs = float(np.clip(palpite, self.config.piso_ruido_dbfs,
                                           self.config.teto_ruido_dbfs + 20.0))
            self._eco_ausente = False
            if self._cortado:
                # resposta NOVA depois de um corte: a fusão vale a partir da BORDA
                # dela. A calibração do eco começa junto e leva
                # `eco_calibracao_ms` (~1,3 s) com `_avaliar_abertura` parada; sem
                # esta extensão a janela de fusão expirava DURANTE a calibração e a
                # continuação da fala caía no braço do barge outra vez — matando a
                # resposta que acabara de nascer.
                self._funde_ate = max(self._funde_ate, time.monotonic()
                                      + self.config.barge_fusao_ms / 1000.0)
                self._cortado = False
            # NÃO reinicia a calibração se ela já está em curso (#674): o handler
            # marca a borda a CADA chunk (a flag `_speaking` cai entre eles), e o
            # reset apagava as amostras antes de a janela fechar — com chunks
            # espaçados a calibração nunca convergia e `_avaliar_abertura` nunca
            # rodava: o turno posterior ao corte só abria ~14 s depois, quando o
            # TTS parava.
            if not self._calibrando:
                self._calibrando = True
                self._calibracao_vals = []
            if not self._playback_ativo():
                # só com a janela FECHADA (episódio novo de playback) o acumulador
                # de barge zera e o aviso de "barge com turno aberto" rearma —
                # assim o par True/False por chunk não repete o evento (#115)
                self._barge_frames = 0
                self._barge_sem_voz = 0
                self._barge_no_turno_emitido = False
        self._speaking = bool(ligado)
        self._nivel_payload = nivel_dbfs if ligado else None
        if not ligado:
            # o stop não fecha a janela na hora: o cliente ainda tem áudio na fila
            # E ainda vai TOCAR o chunk que acabou de chegar (duração real dele).
            # Depois de um CORTE não sobra fila nenhuma (`playback_cortado`), e
            # rearmar a cauda aqui reabria 900 ms de janela para uma fala que não
            # tem o que interromper — a fala seguinte voltava a ser lida como
            # barge. O `_cortado` só cai quando chega áudio NOVO (ver a borda).
            if not self._cortado:
                self._playback_frames = max(self.config._frames_playback,
                                            self._playback_restante)
        self._janela_log("set_speaking", janela_antes)

    def set_turno_aberto(self, aberto: bool) -> None:
        """Marca que o assistente AINDA tem turno em voo (mesmo com a fila vazia).

        É o que mantém a janela de playback viva nos vãos de geração (#167). Quem
        chama é o handler: `True` no 1º áudio do turno, `False` em
        `turn_complete`/`interrupted`/cancel/fechamento (idempotente). Sem estas
        chamadas nada muda — a janela segue a fila, como antes."""
        janela_antes = self._playback_ativo()
        self._turno_aberto = bool(aberto) and self.config.barge_janela_turno
        self._turno_aberto_frames = self.config._frames_turno_assistente_max
        self._janela_log("set_turno_aberto", janela_antes)
        if aberto:
            self._cortado = False
        elif not self._cortado:
            # fechou o turno: vale a cauda normal (pode haver áudio no cliente).
            # Depois de um corte não: o cliente descartou a fila (`playback_cortado`).
            self._playback_frames = max(self._playback_frames,
                                        self.config._frames_playback)

    def set_hold(self, pressionado: bool) -> list[TurnEvent]:
        """Fallback de UI "segurar pra falar": abre/fecha o turno no botão,
        ignorando os heurísticos de onset/barge-in."""
        eventos: list[TurnEvent] = []
        self._hold = bool(pressionado)
        if self._hold:
            if self._estado is Estado.OCIOSO:
                eventos.extend(self._abrir_turno(self._proxima_amostra, 0,
                                                 barge=self._speaking, hold=True))
        elif self._estado is Estado.FALANDO and self._turno_hold:
            eventos.extend(self._fechar_turno("hold"))
        return eventos

    def cancel(self) -> dict:
        """Descarta o turno aberto sem emitir `speech_end` (o humano
        interrompeu e o handler já decidiu outro caminho)."""
        if self._estado is Estado.OCIOSO:
            return {"cancelado": False}
        fala_ms = self._fala_frames * FRAME_MS
        info = {"cancelado": True, "fala_ms": fala_ms,
                "curto": fala_ms < self.config.min_fala_ms,
                "barge_in": self._turno_barge}
        self._stat["cancelados"] += 1
        if self._turno_barge and self._fala_extra_ms() < self.config.min_fala_ms:
            self._stat["barge_in_falso"] += 1
        self._reset_turno()
        return info

    def flush(self) -> list[TurnEvent]:
        """Fecha o turno aberto (fim do stream / desligamento do socket)."""
        if self._estado is Estado.OCIOSO:
            return []
        return self._fechar_turno("flush")

    def fala_ms_atual(self) -> int:
        """Quanto a fala ABERTA já durou (ms); 0 sem turno.

        A decisão pelo PRAZO (`barge_decisao_ms`) só tem o retrato do onset, e o
        `barge_suspeito` não carrega `fala_ms` (o motor o põe no `speech_end`):
        sem isto toda decisão pelo prazo chegava com 0, o `fala_sustentada`
        (>=1200 ms) nunca disparava e a decisão saía pelo TEXTO de um fragmento
        de 200 ms — medido: "Eu preciso resolver" cortando a MESMA fala quatro
        vezes seguidas no bloco `misto`."""
        return int(self._fala_frames * FRAME_MS)

    def playback_cortado(self) -> None:
        """O cliente DESCARTOU a fila de playback (barge/cancel): o backlog não toca.

        A `interrupted` manda o cliente chamar `lxCancelaPlayback`, mas o motor
        continuava achando que TUDO que já saiu ainda vai ser ouvido. A JANELA
        ficava aberta pelo backlog inteiro depois do corte (medido no bloco
        `misto`: `restante_ms` de 9,5 a 19,4 s) e, dentro dela, a fala seguinte
        era lida como BARGE em vez de onset: cada fala virava suspeita, decidia
        `interrupcao` e matava a resposta que acabara de nascer — 17 decisões e
        24 `interrupted` em 6 rodadas, em cascata. Pior, no regime de barge o
        limiar usa o piso de ECO (alto, de um TTS que já não toca) e a fala não
        passava: o motor ficava `ocioso` recebendo frame de voz e o turno não
        abria (a rodada sem áudio por 26 s).

        Zera o que o cliente ia tocar, a cauda de `_frames_playback` (não há áudio
        no fio para cobrir) e a calibração em curso (ela mede o eco do playback
        que morreu — e, enquanto `_calibrando`, `_avaliar_abertura` nem roda). O
        `_speaking` cai junto: o chunk em voo foi cancelado no pipeline."""
        self._playback_restante = 0
        self._playback_frames = 0
        self._speaking = False
        self._cortado = True
        self._calibrando = False
        self._calibracao_vals = []
        self._barge_frames = 0
        self._barge_sem_voz = 0
        self._barge_pico = 0
        self._barge_no_turno_emitido = False

    def playback_restante_ms(self) -> int:
        """Quanto o CLIENTE ainda vai tocar (backlog do último chunk), em ms."""
        return int(self._playback_restante * FRAME_MS)

    def reset(self) -> None:
        self._reset_turno()
        self._playback_frames = 0
        self._playback_restante = 0
        self._cortado = False
        self._turno_aberto = False
        self._calibrando = False
        self._calibracao_vals = []
        self._env.clear()
        self._pendente = np.zeros(0, dtype=np.float32)

    # -- entrada de áudio ------------------------------------------------
    def feed(self, pcm: bytes | np.ndarray) -> list[TurnEvent]:
        """Consome PCM16 mono 16 kHz (bytes) ou float32 em [-1, 1] e devolve os
        eventos do pedaço. Tamanho arbitrário: o alinhamento de 512 amostras é
        feito aqui."""
        self._pendente = np.concatenate([self._pendente, self._para_float(pcm)])
        eventos: list[TurnEvent] = []
        while len(self._pendente) >= FRAME_SAMPLES:
            frame = self._pendente[:FRAME_SAMPLES]
            self._pendente = self._pendente[FRAME_SAMPLES:]
            eventos.extend(self._processar(frame))
        return eventos

    @staticmethod
    def _para_float(pcm: bytes | np.ndarray) -> np.ndarray:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            if len(pcm) % 2:
                raise ValueError("PCM16 precisa de número par de bytes")
            return np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32) / 32768.0
        arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if arr.size and float(np.max(np.abs(arr))) > 1.5:   # veio em escala int16
            arr = arr / 32768.0
        return arr

    # -- núcleo ----------------------------------------------------------
    def _processar(self, frame: np.ndarray) -> list[TurnEvent]:
        amostra = self._proxima_amostra
        self._proxima_amostra += FRAME_SAMPLES
        rms = float(np.sqrt(np.mean(frame * frame)))
        # envelope suavizado: a decisão NÃO pode ser por frame cru. Medido nos
        # wavs do app, o nível de UM frame de fala varia 34 dB entre vogal e
        # consoante — com esse espalhamento o eco do TTS passa por cima de
        # qualquer limiar calibrado. Numa janela de ~200 ms o espalhamento cai
        # para ~10 dB: o humano, mesmo só +6 dB acima do eco, aparece
        # sustentado, enquanto o eco sozinho não cruza o limiar.
        self._env.append(rms * rms)
        env_dbfs = _dbfs(math.sqrt(sum(self._env) / len(self._env)))

        prob = float(self._scorer(frame) if self._scorer is not None
                     else scorer_silero(frame))
        self._historico.append(_Quadro(amostra, frame, prob, env_dbfs))
        if not self._ref_audio():
            self._pre_env.append(env_dbfs)
        if not self._speaking and self._playback_frames:
            self._playback_frames -= 1
            if self._playback_frames == 0:
                # herda o eco no piso: o que sobrou dele não pode virar "fala"
                # fantasma (senão o STT transcreve o próprio TTS)
                self._noise_dbfs = float(np.clip(max(self._noise_dbfs,
                                                     self._eco_dbfs - 3.0),
                                                 self.config.piso_ruido_dbfs,
                                                 self.config.teto_ruido_dbfs))
        if (not self._speaking and self._playback_restante
                and (self.config.eco_so_tocando or not self._turno_aberto)):
            # #167: o backlog só escoa DEPOIS do turno. Com o turno aberto o cliente
            # está atrasado (ele toca em 1x o que o servidor produziu mais rápido),
            # então descontar durante a geração consumia o backlog antes do
            # `turn_complete` e a janela fechava com áudio ainda na fila do cliente.
            # Com `eco_so_tocando` ele escoa em TEMPO REAL (é a estimativa do que o
            # cliente ainda tem para tocar — é ela que diz se há áudio audível).
            self._playback_restante -= 1
        if self._turno_aberto and not self._speaking:
            # trava de segurança (#167): turno "aberto" que o handler esqueceu de
            # fechar não pode segurar a janela para sempre em vão sem áudio
            janela_antes = self._playback_ativo()
            self._turno_aberto_frames -= 1
            if self._turno_aberto_frames <= 0:
                self._turno_aberto = False
                self._janela_log("trava de segurança", janela_antes)

        if self._estado is Estado.FALANDO:
            # turno já aberto: o áudio dele NÃO pode ser pulado pela calibração
            eventos = self._avaliar_turno(frame, env_dbfs, prob)
            if self._calibrando:
                self._calibrar(env_dbfs)
            # #167: o braço do barge NÃO pode ficar parado durante a calibração do
            # eco — com a calibração a cada início de áudio, o onset do humano que
            # já estava falando pagaria `eco_calibracao_ms` inteiro antes de contar
            eventos += self._barge_com_turno_aberto(env_dbfs, prob)
            return eventos
        if self._calibrando:
            self._calibrar(env_dbfs)
            if self._debug:
                self._calib_n = getattr(self, "_calib_n", 0) + 1
                if self._calib_n % 10 == 1:
                    print(f"[CALIBRA] abertura BLOQUEADA (calibrando, "
                          f"dbfs={env_dbfs:.1f} prob={prob:.2f})", file=sys.stderr,
                          flush=True)
            # NA FUSÃO a calibração segue acumulando mas NÃO segura a abertura: a
            # resposta nova é fundo, a pessoa já está falando e esse é justamente o
            # caso em que a janela de calibração (~1,3 s) engolia o onset inteiro.
            if not self._fundindo():
                return []
        self._adaptar(env_dbfs, prob)
        return self._avaliar_abertura(amostra, env_dbfs, prob)

    def _barge_com_turno_aberto(self, env_dbfs: float, prob: float) -> list[TurnEvent]:
        """O playback começou com o turno do usuário ABERTO (ele já falava).

        Aqui não há turno novo a abrir — o áudio anterior continua no buffer do
        turno — mas o assistente passou a falar por cima de quem já falava: isso
        é barge-in e o handler tem de derrubar o playback. Emitir só o
        `barge_in` (sem `speech_start`) é o que evita reabrir o turno e perder o
        começo da fala do usuário, que veio antes do playback."""
        # turno aberto PELO barge já sinalizou o barge: não repete o evento
        if (time.monotonic() >= self._funde_ate
                and self._playback_ativo() and not self._barge_no_turno_emitido
                and not self._turno_barge):
            voz = self._voz(env_dbfs, prob, self.limiar_energia_dbfs)
            self._conta_barge(voz)
            if self._barge_frames >= self.config._frames_barge:
                self._barge_frames = 0
                self._barge_no_turno_emitido = True
                self._stat["barge_in_com_turno_aberto"] += 1
                return [self._evento("barge_in", self._t_ms(),
                                     self._proxima_amostra, b"", prob=prob,
                                     rms_dbfs=env_dbfs, barge_in=True,
                                     detalhe="playback com turno aberto")]
            return []
        self._barge_frames = 0
        return []

    def _calibrar(self, env_dbfs: float) -> None:
        """Acha o nível do eco no começo do playback.

        Sem isto o palpite de `eco_perda_inicial_db` faz o PRÓPRIO eco passar do
        limiar e disparar barge-in sozinho no começo de toda fala do TTS.

        A janela é de duração variável de propósito: o wav do TTS pode começar
        em silêncio (medido: 21 wavs do app, vários abrem com pausa) e uma
        janela fixa calibraria o SILÊNCIO como eco. Então ela só fecha quando
        houver `eco_calibracao_ms` de áudio de verdade — ou `eco_calibracao_teto`
        janelas, para um TTS que abre com pausa longa não travar o detector.
        A estatística da janela é um quantil ALTO (`eco_calibracao_quantil`, 90
        por padrão) e não o máximo: a mediana pura afundava em wav com trecho
        quieto (o eco depois estourava o próprio limiar) e o máximo capturava o
        humano que fala em cima."""
        self._calibracao_vals.append(env_dbfs)
        piso_audio = self._noise_dbfs + self.config.eco_faixa_morta_db
        com_audio = [v for v in self._calibracao_vals if v > piso_audio]
        # O TETO é INCONDICIONAL (#674): antes ele exigia já ter visto áudio, e se
        # o piso de ruído subisse acima da voz (eco do turno anterior) a janela
        # nunca fechava — `_calibrando` ficava ligado para sempre, `_avaliar_abertura`
        # nunca rodava e o turno seguinte ao corte só abria ~14 s depois, quando o
        # TTS parava. No teto sem áudio a amostra cai nas próprias janelas (o
        # `com_audio or _calibracao_vals` logo abaixo já cobre isso).
        esgotou = len(self._calibracao_vals) >= \
            self.config.eco_calibracao_teto * self.config._frames_calibracao
        if len(com_audio) < self.config._frames_calibracao and not esgotou:
            return
        self._calibrando = False
        amostra = com_audio or self._calibracao_vals
        obs = float(np.percentile(amostra, self.config.eco_calibracao_quantil))
        self._eco_ausente = self._eco_sumiu(obs)
        alvo = self._noise_dbfs if self._eco_ausente else obs
        self._eco_dbfs = float(np.clip(alvo, self.config.piso_ruido_dbfs,
                                       self.config.teto_ruido_dbfs + 20.0))
        self._calibracao_vals = []
        # se o humano já estava falando, conta o que ele já sustentou (o eco não
        # entra: estes frames são reavaliados com o limiar já convergido)
        # #167: o braço do barge conta DESDE o início do áudio (não é zerado pela
        # recalibração por chunk) — aqui só se garante o mínimo que o histórico
        # sustenta, sem descartar o que já contou
        self._barge_frames = max(self._barge_frames,
                                 min(self._fala_no_historico(),
                                     self.config.eco_calibracao_teto * self.config._frames_calibracao))

    def _eco_sumiu(self, obs_dbfs: float) -> bool:
        """O playback acrescentou eco de verdade?

        Este é o ponto cego que a bancada do frontend achou: quando o humano JÁ
        está falando no começo do playback (mic falso do Chromium, fone de
        ouvido, eco muito baixo), a calibração come o humano como se fosse eco —
        e aí ele teria de superar o PRÓPRIO p90 + margem, o que uma fala
        contínua quase nunca faz (e amplificar o estímulo não resolve: o limiar
        é relativo, então é invariante à escala).

        Dois sinais independentes de que o que se ouve não é eco:
        (a) o nível durante o playback é ~igual ao de ANTES dele (o eco somaria);
        (b) o mic está bem ACIMA do nível do payload — alto-falante só atenua,
            então mais alto que a fonte não é eco (a folga cobre a diferença
            entre o RMS do payload e o pico do envelope, que em wav esparso
            passa de 8 dB).

        Sem histórico e sem `nivel_dbfs` não há como saber: mantém o regime de eco.
        """
        if (self._nivel_payload is not None
                and obs_dbfs > self._nivel_payload + self.config.eco_folga_payload_db):
            return True
        if self._pre_env:
            # MESMA estatística dos dois lados: comparar p75 de cá com p90 de lá
            # dava falso "o playback somou eco" quando a janela de antes pegava
            # um trecho com pausa (e aí o regime de eco voltava a comer a fala)
            pre = float(np.percentile(self._pre_env, self.config.eco_calibracao_quantil))
            return (obs_dbfs - pre) < self.config.eco_contrib_min_db
        return False

    def _fala_no_historico(self) -> int:
        """Frames de voz consecutivos no fim do histórico (exclui o atual)."""
        limiar = self.limiar_energia_dbfs
        total = 0
        for quadro in reversed(list(self._historico)[:-1]):
            if not self._voz(quadro.env_dbfs, quadro.prob, limiar):
                break
            total += 1
        return total

    def _voz(self, env_dbfs: float, prob: float, limiar_dbfs: float) -> bool:
        return prob >= self.config.fala_threshold and env_dbfs >= limiar_dbfs

    def _conta_barge(self, voz: bool) -> int:
        """Conta o burst do barge COM folga (#707).

        O contador zerava no primeiro frame abaixo do limiar: uma fala com pausa
        entre palavras nunca somava `barge_in_ms` (o pico ficava em 2–3 de 10) e
        nenhuma suspeita saía — o turno caía com 64 ms e o VAD do STT recusava.
        Agora só zera depois de `barge_folga_ms` SEGUIDOS sem voz; o eco é plano,
        então não acumula. `_barge_pico` guarda o máximo do episódio para o log."""
        if voz:
            self._barge_frames += 1
            self._barge_sem_voz = 0
            self._barge_pico = max(self._barge_pico, self._barge_frames)
        else:
            self._barge_sem_voz += 1
            if self._barge_sem_voz > self.config._frames_barge_folga:
                self._barge_frames = 0
                self._barge_sem_voz = 0
        return self._barge_frames

    def _env_antes(self) -> float:
        """Envelope uma janela atrás (~200 ms). É o que separa salto de rampa."""
        n = self.config._frames_envelope
        hist = self._historico
        if len(hist) <= n:
            return self._eco_dbfs
        return float(hist[-1 - n].env_dbfs)

    def _degrau_sobre_eco(self, env_dbfs: float, prob: float) -> bool:
        """Fala por cima do TTS que não alcança eco + 4 dB.

        O Silero alto sozinho é o próprio eco (é fala). Conta quando o
        envelope sobe `barge_degrau_db` dentro de uma janela (~200 ms) e
        fica acima do piso travado nesse instante. Eco plano e rampa lenta
        não passam. Sem eco declarado, o limiar de ocioso já cobre.
        """
        if (prob < self.config.barge_prob_alta or self._eco_ausente
                or not self._ref_audio()):
            self._degrau_base = None
            return False
        degrau = self.config.barge_degrau_db
        if self._degrau_base is None:
            if env_dbfs < self._eco_dbfs + degrau:
                return False
            if env_dbfs < self._env_antes() + degrau:
                return False
            self._degrau_base = self._eco_dbfs
        elif env_dbfs < self._degrau_base + degrau:
            self._degrau_base = None
            return False
        return True

    def _adaptar(self, dbfs: float, prob: float) -> None:
        """Pisos adaptativos (ruído de fundo e eco). Só fora de voz — senão a
        EMA persegue a própria fala — e congelados no modo `hold`."""
        if self._hold:
            return
        if self._voz(dbfs, prob, self.limiar_energia_dbfs):
            return
        # #273: candidato a degrau não pode deixar o piso subir atrás da voz.
        # Senão os frames entre +2 e +4 dB nunca sustentam os 300 ms.
        if self._degrau_sobre_eco(dbfs, prob):
            return
        if self._ref_audio() and not self._eco_ausente:
            # O eco NÃO pode seguir o envelope para baixo: nas pausas entre
            # chunks do TTS o envelope cai e o estimador escorregava até o
            # ruído — aí o próprio eco voltava a passar do limiar. Faixa morta
            # de ±`eco_faixa_morta_db` e descida 5x mais lenta que a subida.
            if dbfs > self._eco_dbfs + self.config.eco_faixa_morta_db:
                self._eco_dbfs = min(self._eco_dbfs + self.config.adaptacao_eco_db, dbfs)
            elif dbfs < self._eco_dbfs - self.config.eco_faixa_morta_db:
                self._eco_dbfs = max(self._eco_dbfs - self.config.adaptacao_eco_db * 0.2,
                                     dbfs)
        else:
            alvo, passo = self._noise_dbfs, self.config.adaptacao_ruido_db
            self._noise_dbfs = (min(alvo + passo, dbfs) if dbfs > alvo
                                else max(alvo - passo * 3.0, dbfs))
        self._noise_dbfs = float(np.clip(self._noise_dbfs, self.config.piso_ruido_dbfs,
                                         self.config.teto_ruido_dbfs))
        self._eco_dbfs = float(np.clip(self._eco_dbfs, self.config.piso_ruido_dbfs,
                                       self.config.teto_ruido_dbfs + 20.0))

    def _avaliar_abertura(self, amostra: int, dbfs: float, prob: float) -> list[TurnEvent]:
        if self.config.hold_to_talk and not self._hold:
            self._onset_frames = 0
            self._barge_frames = 0
            return []

        if self._debug and self._onset_frames == 0 and self._barge_frames == 0:
            print(f"[ABERTURA] ativo={self._playback_ativo()} speaking={self._speaking}"
                  f" frames={self._playback_frames} restante={self._playback_restante}"
                  f" turno_aberto={self._turno_aberto} dbfs={dbfs:.1f} prob={prob:.2f}",
                  file=sys.stderr, flush=True)
        # FUSÃO DEPOIS DO CORTE: `_funde_ate` vale também aqui, não só no
        # `_barge_com_turno_aberto`. Logo depois de um `interrupted` a resposta
        # nova mal começou e o turno da fala que interrompeu já FECHOU (o motor
        # está ocioso) — sem esta guarda a continuação da fala abre OUTRA
        # suspeita, decide `interrupcao` e mata a resposta que acabou de nascer.
        # Medido no bloco `misto` antes dela: 17 decisões e 24 `interrupted`
        # para 6 falas, e o turno novo morria antes do 1º áudio.
        if self._playback_ativo() and time.monotonic() >= self._funde_ate:
            voz = (self._voz(dbfs, prob, self.limiar_energia_dbfs)
                   or self._degrau_sobre_eco(dbfs, prob))
            self._conta_barge(voz)
            self._onset_frames = 0
            if self._barge_frames >= self.config._frames_barge:
                if self.config.interrupcao == "imediata":
                    return self._abrir_turno(amostra + FRAME_SAMPLES, self._barge_frames,
                                             barge=True, prob=prob, dbfs=dbfs)
                # #501: abre o turno já (para o áudio ficar bufferizado), mas NÃO
                # anuncia o corte ainda — o cliente só abaixa o volume. A decisão
                # vem depois, por `decidir_barge`.
                evs = [e for e in self._abrir_turno(
                    amostra + FRAME_SAMPLES, self._barge_frames,
                    barge=True, prob=prob, dbfs=dbfs) if e.tipo != "speech_start"]
                self._suspeito = True
                self._suspeito_audio = b""
                self._decisao_frames = 0
                for e in evs:
                    if e.tipo == "barge_in":
                        e.tipo = "barge_suspeito"
                        # o áudio vai NO EVENTO da suspeita: é dele que o handler
                        # transcreve. Sem isto o `speech_start` (que é filtrado
                        # aqui) era o único a carregar áudio e o trecho chegava
                        # vazio no STT — causa do corte 1/6 (#503)
                        e.audio = b"".join(_pcm16(f) for f in self._turno)
                        e.detalhe = f"onset durante playback ({self.config.interrupcao})"
                return evs
            return []

        voz = self._voz(dbfs, prob, self.limiar_energia_dbfs)
        self._onset_frames = self._onset_frames + 1 if voz else 0
        if self._onset_frames >= self.config._frames_onset:
            return self._abrir_turno(amostra + FRAME_SAMPLES, self._onset_frames,
                                     prob=prob, dbfs=dbfs)
        return []

    def _avaliar_turno(self, frame: np.ndarray, dbfs: float, prob: float) -> list[TurnEvent]:
        self._turno.append(frame)
        # histerese: manter o turno é mais permissivo que abri-lo
        voz = self._turno_hold or self._voz(dbfs, prob, self.limiar_energia_turno_dbfs)
        if voz:
            self._fala_frames += 1
            self._fala_extra_frames += 1
            self._silencio_frames = 0
        else:
            self._silencio_frames += 1

        if len(self._turno) >= self.config._frames_turno_max:
            return self._fechar_turno("turno_max")
        if self._silencio_frames >= self.config._frames_silencio:
            return self._fechar_turno("silencio")
        return []

    def _abrir_turno(self, fim: int, frames_confirmados: int, *,
                     barge: bool = False, hold: bool = False,
                     prob: float = 0.0, dbfs: float = -120.0) -> list[TurnEvent]:
        """`fim` é a amostra logo após o último frame confirmado. O início é
        retroagido: os frames confirmados + `prefix_ms` de pré-roll."""
        faltam = (frames_confirmados + self.config._frames_prefix
                  + self.config._frames_envelope)
        inicio = max(0, fim - faltam * FRAME_SAMPLES)
        pre = [q.pcm for q in self._historico if q.amostra >= inicio]
        if not pre:
            # fora do `feed` normal o frame atual pode não ter entrado — e no
            # `set_hold` (botão, #314) pode não haver frame NENHUM (mic negado ou
            # lento): aí `_historico[-1]` estourava `IndexError` em deque vazio e o
            # erro subia pelo `_live_loop`, fechando o WS. Turno sem áudio é
            # legítimo neste caminho; o evento sai com `audio` vazio.
            if self._historico:
                pre = [self._historico[-1].pcm]
                inicio = self._historico[-1].amostra
            else:
                inicio = fim
        audio = b"".join(_pcm16(f) for f in pre)

        if not barge:
            self._suspeito = False
            self._suspeito_audio = b""
        self._estado = Estado.FALANDO
        self._turno = list(pre)
        self._turno_amostra = inicio
        self._turno_barge = barge
        self._turno_hold = hold
        self._fala_frames = max(frames_confirmados, 0)
        self._silencio_frames = 0
        self._onset_frames = 0
        self._barge_frames = 0
        self._stat["turnos"] += 1
        if barge:
            self._stat["barge_in"] += 1

        # o áudio é anterior à decisão: o timestamp retroage junto
        t_ms = self._t_ms() + round((inicio - fim) * 1000 / SAMPLE_RATE)
        eventos: list[TurnEvent] = []
        if barge:
            eventos.append(self._evento("barge_in", t_ms, inicio, b"", prob=prob,
                                        rms_dbfs=dbfs, barge_in=True,
                                        detalhe=f"energia sustentada {self.config.barge_in_ms}ms"))
        eventos.append(self._evento("speech_start", t_ms, inicio, audio, prob=prob,
                                    rms_dbfs=dbfs, barge_in=barge,
                                    detalhe="hold" if hold else ("barge_in" if barge else "")))
        return eventos

    def audio_suspeito(self) -> bytes:
        """Áudio acumulado da suspeita ABERTA (pré-roll + o que já veio depois).

        O evento `barge_suspeito` carrega o áudio do instante do onset, mas ele é
        um retrato: a fala continua chegando DEPOIS dele e fica no motor. As
        passadas de comando precisam do acumulado — lendo só o retrato, a janela
        "incremental" saía do mesmo buffer congelado nas passadas seguintes e a
        passada 1 (só pré-roll, ~250 ms) não tinha o comando (achado do #547).
        Vazio quando não há suspeita."""
        if not self._suspeito:
            return b""
        return self._suspeito_audio + b"".join(_pcm16(f) for f in self._turno)

    def decidir_barge(self, tipo: str, texto: str = "", fala_ms: int = 0,
                      decisao_ms: int = 0, p_cortar: float | None = None,
                      modelo_ms: int | None = None,
                      filtro: str | None = None) -> list[TurnEvent]:
        """Fecha a SUSPEITA de barge (#501). Chamado pelo handler quando chega o
        `speech_end`, quando a janela de `barge_decisao_ms` estoura, ou direto com
        `tipo` decidido.

        `interrupcao` mantém o turno aberto e emite o `barge_in` que o cliente já
        conhece (corta). Os demais tipos emitem `barge_retoma` e DESCARTAM o turno
        bufferizado — a resposta do assistente continua e o backchannel não entra
        no histórico. Devolve vazio se não havia suspeita."""
        if not self._suspeito:
            return []
        self._suspeito = False
        self._decisao_frames = 0
        if self._turno:
            self._suspeito_audio += b"".join(_pcm16(f) for f in self._turno)
            self._turno = []
        audio = self._suspeito_audio
        self._suspeito_audio = b""
        decisao = self._evento("barge_decisao", self._t_ms(), self._turno_amostra,
                               b"", texto=texto, tipo_barge=tipo, p_cortar=p_cortar,
                               modelo_ms=modelo_ms, filtro=filtro,
                               detalhe=f"decisao em {decisao_ms} ms")
        if tipo == "interrupcao":
            self._funde_ate = time.monotonic() + self.config.barge_fusao_ms / 1000.0
            if self._estado is Estado.OCIOSO:
                # a decisão chegou DEPOIS de o turno fechar (janela do handler):
                # reabre para os frames seguintes entrarem no turno certo
                self._estado = Estado.FALANDO
                self._turno_barge = True
                self._silencio_frames = 0
            self._stat["barge_in"] += 1
            abrir = [self._evento("barge_in", self._t_ms(), self._turno_amostra, audio,
                                  barge_in=True, detalhe=f"{tipo} (conteúdo)"),
                     self._evento("speech_start", self._t_ms(), self._turno_amostra, audio,
                                  barge_in=True, fala_ms=fala_ms, detalhe="barge_in")]
            return [decisao, *abrir] if decisao else abrir
        # retoma: solta o turno e devolve o áudio para o handler decidir (ele pode
        # anotar a fala sem abrir turno)
        self._stat["barge_retomado"] = self._stat.get("barge_retomado", 0) + 1
        retoma = self._evento("barge_retoma", self._t_ms(), self._turno_amostra, audio,
                              tipo_barge=tipo, texto=texto, fala_ms=fala_ms)
        self._reset_turno()
        return [decisao, retoma]

    def _fechar_turno(self, motivo: str) -> list[TurnEvent]:
        if self._suspeito and self._turno:
            # o áudio da suspeita não pode morrer no fechamento: a decisão ainda
            # vai chegar (o handler chama `decidir_barge` com o transcript)
            self._suspeito_audio += b"".join(_pcm16(f) for f in self._turno)
            self._turno = []
        fala_ms = self._fala_frames * FRAME_MS
        curto = fala_ms < self.config.min_fala_ms
        sobra = max(0, self._silencio_frames - self.config._frames_cauda)
        corte = max(1, len(self._turno) - sobra)      # guarda `cauda_ms` de silêncio
        audio = b"".join(_pcm16(f) for f in self._turno[:corte])
        fim = self._turno_amostra + corte * FRAME_SAMPLES

        if curto:
            self._stat["turnos_curtos"] += 1
        falso = self._turno_barge and self._fala_extra_ms() < self.config.min_fala_ms
        if falso:
            self._stat["barge_in_falso"] += 1
        ev = self._evento("speech_end", self._t_ms(), fim, audio, detalhe=motivo,
                          fala_ms=fala_ms, curto=curto, barge_in=self._turno_barge,
                          barge_falso=falso)
        self._reset_turno()
        return [ev]

    def _fala_extra_ms(self) -> int:
        """Fala contada DEPOIS de o turno abrir. Num barge-in, 'nenhuma fala
        além da janela de confirmação' é o sinal de que o disparo foi eco."""
        return self._fala_extra_frames * FRAME_MS

    def _reset_turno(self) -> None:
        self._estado = Estado.OCIOSO
        self._turno = []
        self._turno_barge = False
        self._turno_hold = False
        self._fala_frames = 0
        self._fala_extra_frames = 0
        self._silencio_frames = 0
        self._onset_frames = 0
        self._barge_frames = 0
        self._barge_sem_voz = 0
        self._barge_pico = 0
        self._degrau_base = None
        self._barge_no_turno_emitido = False
        self._decisao_frames = 0

    def _evento(self, tipo: str, t_ms: int, amostra: int, audio: bytes, **kw) -> TurnEvent:
        t_ms = max(0, int(t_ms))
        delta = None if self._ultimo_evento_ms is None else t_ms - self._ultimo_evento_ms
        self._ultimo_evento_ms = t_ms
        return TurnEvent(tipo=tipo, t_ms=t_ms, ts=self._agora(), amostra=int(amostra),
                         t_decisao_ms=self._t_ms(), audio=audio,
                         ms_desde_evento_anterior=delta, **kw)

    def _t_ms(self) -> int:
        return int((self._relogio() - self._t0) * 1000)


def gerar_relatorio(engine: TurnEngine, eventos: Iterable[TurnEvent]) -> dict:
    """Resumo pronto para o log de sessão: contadores + linha de tempo."""
    linhas = [{"type": e.tipo, "t_ms": e.t_ms, "detalhe": e.detalhe,
               "curto": e.curto, "barge_falso": e.barge_falso,
               "fala_ms": e.fala_ms} for e in eventos]
    return {"stats": engine.estatisticas(), "eventos": linhas,
            "backend": BACKEND or "nao-carregado"}