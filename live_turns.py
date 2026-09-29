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
precisa passar `eco + barge_in_margin_db` por 300 ms seguidos. O pré-roll do
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
    barge_in_margin_db: float = 4.0               # margem sobre o nível de eco (medido)
    fala_threshold: float = 0.5                   # prob mínima do Silero
    energia_margin_db: float = 6.0                # margem p/ ABRIR turno
    energia_margin_turno_db: float = 3.0          # margem p/ MANTER turno (histerese)
    min_fala_ms: int = 250                        # turno mais curto que isto é "curto"
    cauda_ms: int = 200                           # silêncio guardado no áudio do turno
    turno_max_ms: int = 30000
    piso_ruido_dbfs: float = -60.0
    teto_ruido_dbfs: float = -30.0
    eco_perda_inicial_db: float = 12.0            # palpite ao abrir o playback
    eco_calibracao_ms: int = 400                  # janela que acha o nível do eco
    eco_calibracao_teto: int = 3                  # ...repetida se o TTS abre em silêncio
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
        for nome in ("prefix_ms", "silence_ms", "onset_ms", "barge_in_ms", "min_fala_ms",
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
        self._barge_no_turno_emitido = False
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
        return self._tocando() if self.config.eco_so_tocando else self._playback_ativo()

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
            self._playback_restante = min(
                self.config._frames_audio_max,
                self._playback_restante
                + int(math.ceil(float(duracao_ms) / FRAME_MS))
                + self.config._frames_cauda)
        if borda_audio:
            palpite = (nivel_dbfs - self.config.eco_perda_inicial_db
                       if nivel_dbfs is not None else self._noise_dbfs + 6.0)
            self._eco_dbfs = float(np.clip(palpite, self.config.piso_ruido_dbfs,
                                           self.config.teto_ruido_dbfs + 20.0))
            self._eco_ausente = False
            self._calibrando = True
            self._calibracao_vals = []
            if not self._playback_ativo():
                # só com a janela FECHADA (episódio novo de playback) o acumulador
                # de barge zera e o aviso de "barge com turno aberto" rearma —
                # assim o par True/False por chunk não repete o evento (#115)
                self._barge_frames = 0
                self._barge_no_turno_emitido = False
        self._speaking = bool(ligado)
        self._nivel_payload = nivel_dbfs if ligado else None
        if not ligado:
            # o stop não fecha a janela na hora: o cliente ainda tem áudio na fila
            # E ainda vai TOCAR o chunk que acabou de chegar (duração real dele)
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
        if not aberto:
            # fechou o turno: vale a cauda normal (pode haver áudio no cliente)
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

    def reset(self) -> None:
        self._reset_turno()
        self._playback_frames = 0
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
        if (self._playback_ativo() and not self._barge_no_turno_emitido
                and not self._turno_barge):
            voz = self._voz(env_dbfs, prob, self.limiar_energia_dbfs)
            self._barge_frames = self._barge_frames + 1 if voz else 0
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
        # o teto só vale depois de ver áudio: TTS que abre com pausa longa não
        # pode fechar a calibração no silêncio (o seed viraria o eco)
        esgotou = bool(com_audio) and len(self._calibracao_vals) >= \
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

    def _adaptar(self, dbfs: float, prob: float) -> None:
        """Pisos adaptativos (ruído de fundo e eco). Só fora de voz — senão a
        EMA persegue a própria fala — e congelados no modo `hold`."""
        if self._hold:
            return
        if self._voz(dbfs, prob, self.limiar_energia_dbfs):
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

        if self._playback_ativo():
            voz = self._voz(dbfs, prob, self.limiar_energia_dbfs)
            self._barge_frames = self._barge_frames + 1 if voz else 0
            self._onset_frames = 0
            if self._barge_frames >= self.config._frames_barge:
                return self._abrir_turno(amostra + FRAME_SAMPLES, self._barge_frames,
                                         barge=True, prob=prob, dbfs=dbfs)
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
        if not pre:                       # não deve ocorrer: o frame atual já entrou
            pre = [self._historico[-1].pcm]
            inicio = self._historico[-1].amostra
        audio = b"".join(_pcm16(f) for f in pre)

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

    def _fechar_turno(self, motivo: str) -> list[TurnEvent]:
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
        self._barge_no_turno_emitido = False

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