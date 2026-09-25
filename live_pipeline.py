"""Pipeline de resposta do LIVE (task #93).

fim-de-fala → STT do turno → LLM com streaming de tokens → chunking por sentença
→ TTS por chunk → PCM16 mono 24 kHz para o cliente, com cancelamento entre
estágios e orçamento de latência instrumentado por estágio.

Costura com o WS (task #91): quem cria a sessão entrega dois callbacks
```
emit_json(dict)     # eventos do protocolo: speech_start, transcript_user,
                    # assistant_text (delta), turn_complete, interrupted, error, latency
emit_audio(bytes)   # PCM16 LE mono 24 kHz, na ordem
```
O turno roda em thread própria: os helpers do app (STT/TTS) são sync e disputam
`_stt_lock`/`_gen_lock`; o handler async só precisa enfileirar os callbacks numa
`asyncio.Queue` drenada por um sender — o receive loop nunca bloqueia.

Latência (medida no app real, mac, modelo quente): STT 0,32 s; 1º chunk de TTS
1,32 s com os `num_steps` do dono (37) e 0,60 s com o perfil rápido do LIVE
(16 passos + ref de clone 3 s). Por isso o PRIMEIRO chunk usa o perfil rápido
(min(setting, 16) e ref 3 s) e os seguintes ficam com o setting — o que define o
alvo "fim-de-fala → 1º áudio ≤ 1,5 s" é só o primeiro.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
import time
import wave

import numpy as np
from queue import Queue

# ---------------------------------------------------------------------------
# Chunking por sentença (puro — é o que decide quando o 1º áudio pode nascer)
# ---------------------------------------------------------------------------

_FIM_SENTENCA = re.compile(r"(?<=[.!?…])[\s\"')\]]+|\n+")
# Abreviações curtas que NÃO terminam sentença (evita picar "Sr. Silva").
_ABREV = {"sr", "sra", "dr", "dra", "etc", "ex", "p", "prof", "sta", "st", "vs", "obs"}


class SentenceChunker:
    """Acumula deltas do LLM e devolve sentenças COMPLETAS.

    Regra do primeiro chunk: sai assim que a 1ª sentença fecha; se ela passar de
    `first_max` chars, sai no último separador (vírgula/espaço) que couber — o
    primeiro áudio não espera uma frase inteira longa.

    `first_max` é 18 por medição: a geração de UM chunk custa ~0,45 s (16 passos)
    e o alvo de fim-de-fala→1º áudio é 1,5 s. Com 48 chars o
    primeiro chunk gastava ~1,1 s e o total batia 1,55 s (estourava por 50 ms).
    """

    def __init__(self, first_max: int = 18, max_chars: int = 160):
        self.first_max = first_max
        self.max_chars = max_chars
        self.buf = ""
        self.primeiro = True

    def push(self, delta: str) -> list[str]:
        self.buf += delta or ""
        saida = []
        pos = 0
        while True:
            limite = self.first_max if self.primeiro else self.max_chars
            m = _FIM_SENTENCA.search(self.buf, pos)
            while m and self._abreviacao(m.end()):   # "Sr. " não fecha sentença
                pos = m.end()
                m = _FIM_SENTENCA.search(self.buf, pos)
            if m and m.end() <= limite:              # sentença inteira que CABE
                sent = self.buf[: m.end()].strip()
                self.buf = self.buf[m.end():]
                pos = 0
                if sent:
                    saida.append(sent)
                    self.primeiro = False
                continue
            if len(self.buf) > limite:
                # passou do limite (1ª sentença longa, ou texto sem pontuação):
                # sai no último separador que couber — o 1º áudio não espera
                corte = max(self.buf.rfind(",", 0, limite), self.buf.rfind(" ", 0, limite))
                if corte > 0:
                    saida.append(self.buf[: corte + 1].strip())
                    self.buf = self.buf[corte + 1:]
                    self.primeiro = False
                    pos = 0
                    continue
            break
        return [s for s in saida if s]

    def flush(self) -> list[str]:
        """Fim do texto: o que sobrou vira um chunk (mesmo sem pontuação)."""
        resto, self.buf = self.buf.strip(), ""
        return [resto] if resto else []

    def _abreviacao(self, pos: int) -> bool:
        antes = self.buf[:pos].rstrip("\"')]").rstrip()
        if not antes.endswith("."):
            return False
        pedacos = antes[:-1].split()
        return bool(pedacos) and pedacos[-1].lower() in _ABREV


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class LivePipeline:
    """Um por sessão do WS. `start()` aquece os modelos antes do 1º turno.

    stt/llm/tts são injetáveis (testes sem modelo):
      stt(pcm16_le_16k: bytes, language: str|None) -> str
      llm(messages: list[dict]) -> Iterator[str]   (deltas de texto)
      tts(texto: str, omni: dict) -> np.ndarray float32 mono 24 kHz
    """

    def __init__(self, emit_json, emit_audio, *, voice_id: str | None = None,
                 language: str | None = "pt", system: str | None = None,
                 history: list | None = None, stt=None, llm=None, tts=None,
                 first_max_chars: int = 18, max_chars: int = 160,
                 first_chunk_max_steps: int = 12, prewarm=None,
                 detectar_eco: bool = True):
        self.emit_json = emit_json
        self.emit_audio = emit_audio
        self.voice_id = voice_id
        self.language = language
        self.system = system
        self.history = list(history or [])
        self._stt = stt or _stt_app
        self._llm = llm or _llm_stream_app
        # a voz vem do `setup` da sessão, não do default do app
        self._tts = tts or (lambda texto, omni: _tts_app(texto, omni,
                                                         voice_id=self.voice_id))
        self._prewarm = prewarm or _prewarm_app
        self.first_max_chars = first_max_chars
        self.max_chars = max_chars
        self.first_chunk_max_steps = max(1, int(first_chunk_max_steps))
        self.detectar_eco = detectar_eco
        self._barge_turno = False
        # texto do assistente EM REPRODUÇÃO — atualizado a cada delta do LLM,
        # então cobre também o chunk em voo durante a síntese. É a referência da
        # checagem de eco; o `history[-1]` só entra como fallback (num barge-in no
        # começo do turno ele ainda seria o do turno anterior).
        # Texto do assistente EM REPRODUÇÃO/SÍNTESE — atualizado a cada delta do
        # LLM, então cobre também o chunk em voo durante a síntese. É a referência
        # da checagem de eco; o `history[-1]` só entra como fallback (num barge-in
        # no começo do turno ele ainda seria o do turno anterior).
        self._fala_em_curso = ""

        self._cancel = threading.Event()
        self._fala = bytearray()          # PCM16 16 kHz do turno em curso
        self._em_turno = threading.Event()
        self._trava = threading.Lock()
        self._saiu = False
        self._turno_atual = 0
        self._truncado = False
        self.ultima_latencia: dict = {}

    # -- ciclo de vida ------------------------------------------------------
    def start(self) -> None:
        """Pre-warm: frio o 1º turno estoura o alvo (STT 5,1 s e TTS ~6,8 s).

        Passa a voz da sessão: o Metal compila por forma e uma geração SEM o
        clone prompt não aquece o caminho que o turno usa (medido: 0,9 s no 1º
        chunk sem isto, 0,36 s com)."""
        try:
            self._prewarm(voice_id=self.voice_id)
        except TypeError:                 # prewarm de teste sem o parâmetro
            self._prewarm()

    def close(self) -> None:
        self._saiu = True
        self.cancel()

    # -- entrada ------------------------------------------------------------
    def push_pcm(self, frames: bytes, substituir: bool = False) -> None:
        """Frames do cliente (PCM16 LE 16 kHz) — ou o turno INTEIRO de uma vez.

        `substituir=True` troca o que já estava bufferizado em vez de somar: o
        handler chama assim quando entrega o turno completo (evento do motor ou
        buffer da sessão), senão o mesmo áudio entraria duas vezes quando o
        cliente manda `end_of_speech` logo depois do `speech_end` do motor.

        Aplica o MESMO teto E A MESMA DIREÇÃO do handler (`app._LIVE_MAX_BUFFER`):
        estourou, descarta o FIM e mantém o INÍCIO do turno — é o começo da fala
        que o STT precisa (o handler faz `del buf[teto:]`; aqui era o contrário,
        achado do gate). O `buffer_bytes` continua batendo com o da sessão."""
        if not frames:
            return
        if substituir and self._fala:
            self._fala = bytearray()
        if _DEBUG_TTS:
            print(f"[live][dbg] push_pcm {len(frames)} (antes {len(self._fala)})", flush=True)
        self._fala.extend(frames)
        teto = self._teto_buffer()
        if teto and len(self._fala) > teto:
            del self._fala[teto:]              # mantém o INÍCIO (como o handler)
            self._truncado = True

    @staticmethod
    def _teto_buffer() -> int:
        try:
            import app
            return int(app._LIVE_MAX_BUFFER)
        except Exception:                                               # noqa: BLE001
            return 2 * 1024 * 1024

    def end_of_speech(self, inicia_thread: bool = True, barge: bool = False,
                      barge_in: bool | None = None) -> bool:
        """Dispara o turno com o que está no buffer. False se já havia turno.

        `barge`/`barge_in` (nome que o handler usa) marcam turno aberto por
        barge-in: com isso o pipeline checa no TRANSCRIPT se o áudio era o ECO do
        próprio TTS e descarta o turno — em vez de o app descartar por tempo.

        Por que decidir aqui: medido com a FSM real, durante o playback — fala
        humana CURTA ("Sim.") dá 84 ms de voz além da janela de confirmação,
        abaixo do limiar de 250 ms, então o corte por tempo a perderia; eco puro,
        ao contrário, pode passar como turno verdadeiro (extra ~1000 ms) e faria o
        modelo responder à própria fala. O flag de tempo sozinho separa mal; o
        texto separa bem. `curto` não descarta nada (decisão do PM, confirmada por
        medição: "Sim." em turno normal dá 384 ms de fala contra limiar de 250 —
        margem de 134 ms)."""
        with self._trava:
            if self._em_turno.is_set():
                return False
            pcm = bytes(self._fala)
            self._fala = bytearray()
            self._cancel.clear()
            if not pcm:
                # protocolo: sem áudio não há turno — o cliente sabe por quê
                self.emit_json({"type": "error", "code": "sem_audio",
                                "message": "nenhum frame de áudio recebido"})
                return True
            self._turno_atual += 1
            self._barge_turno = bool(barge if barge_in is None else barge_in)
            self._em_turno.set()
        t = threading.Thread(target=self._turno, args=(pcm,), daemon=True)
        if inicia_thread:
            t.start()
        else:
            self._turno(pcm)
        return True

    def cancel(self) -> None:
        """Barge-in/`cancel`: derruba geração e fila. Thread-safe e idempotente."""
        self._cancel.set()

    @property
    def cancelado(self) -> bool:
        return self._cancel.is_set()

    def _parece_eco(self, texto: str) -> bool:
        """Compara com o texto EM REPRODUÇÃO (fallback: último do assistente)."""
        if not self.detectar_eco:
            return False
        ref = self._fala_em_curso
        if not ref and self.history and self.history[-1].get("role") == "assistant":
            ref = self.history[-1].get("content") or ""
        return _parece_eco(texto, ref)

    @property
    def ocupado(self) -> bool:
        return self._em_turno.is_set()

    # -- turno --------------------------------------------------------------
    def _turno(self, pcm: bytes) -> None:
        t0 = time.perf_counter()
        turno = self._turno_atual
        bytes_entrada = len(pcm)
        # truncamento do buffer DESTE turno (o cliente tem de saber que o áudio
        # passou do teto e o fim foi descartado); zerado ao fechar o turno, para
        # o aviso valer por turno e não "grudar" no próximo
        truncado = self._truncado
        estado = {"audio_bytes": 0}
        fila: Queue = Queue()
        tts_thread = None
        erro_worker: list = []
        lat: dict = {"stt_ms": None, "first_token_ms": None, "first_chunk_ms": None,
                     "first_audio_ms": None}
        try:
            self.emit_json({"type": "speech_start", "turn": turno,
                            "buffer_bytes": bytes_entrada})
            texto = self._stt(pcm, self.language)
            lat["stt_ms"] = _ms(t0)
            if self.cancelado:
                return self._interrompido(lat, t0, turno, estado)
            if not texto.strip():
                # nada transcrito: não inventa `transcript_user` vazio
                self.emit_json({"type": "turn_complete", "turn": turno,
                                "ms": _ms(t0), "audio_bytes": 0,
                                "buffer_bytes": bytes_entrada, "transcript": "",
                                "truncated": truncado})
                self._truncado = False
                return
            if self._barge_turno and self._parece_eco(texto):
                # era o próprio TTS voltando pelo mic: não gasta LLM/TTS e avisa
                self.emit_json({"type": "turn_complete", "turn": turno,
                                "ms": _ms(t0), "audio_bytes": 0, "eco": True,
                                "descartado": True, "buffer_bytes": bytes_entrada,
                                "transcript": texto, "truncated": truncado})
                self._truncado = False
                return
            self.emit_json({"type": "transcript_user", "text": texto})

            # o TTS consome em paralelo: o stream do LLM não para para sintetizar
            tts_thread = threading.Thread(
                target=self._fala_worker,
                args=(fila, lat, t0, erro_worker, estado), daemon=True)
            tts_thread.start()

            chunker = SentenceChunker(first_max=self.first_max_chars,
                                      max_chars=self.max_chars)
            msgs = ([{"role": "system", "content": self.system}] if self.system else []) \
                + self.history + [{"role": "user", "content": texto}]
            inteiro = []
            for delta in self._llm(msgs):
                if self.cancelado:
                    break
                if lat["first_token_ms"] is None:
                    lat["first_token_ms"] = _ms(t0)
                inteiro.append(delta)
                # referência da checagem de eco acompanha o texto EM SÍNTESE: um
                # barge-in enquanto o 1º chunk toca precisa comparar com o que já
                # foi dito (o `history` ainda não tem nada deste turno)
                self._fala_em_curso = "".join(inteiro)
                self.emit_json({"type": "assistant_text", "delta": delta})
                for pedaco in chunker.push(delta):
                    fila.put(pedaco)
            if not self.cancelado:
                for pedaco in chunker.flush():
                    fila.put(pedaco)
            fila.put(None)                      # fim para o worker de TTS
            if tts_thread:
                tts_thread.join(180)
            if erro_worker:
                raise erro_worker[0]
            resposta = "".join(inteiro).strip()
            self._fala_em_curso = resposta      # é o que o cliente vai tocar
            self.history += [{"role": "user", "content": texto},
                             {"role": "assistant", "content": resposta}]

            if self.cancelado:
                return self._interrompido(lat, t0, turno, estado)
            lat["total_ms"] = _ms(t0)
            self.ultima_latencia = lat
            self.emit_json({"type": "latency", **lat})
            if _DEBUG_TTS:
                print(f"[live][dbg] turn_complete turno={turno} bytes={bytes_entrada} "
                      f"truncado={truncado}", flush=True)
            self.emit_json({"type": "turn_complete", "turn": turno, "ms": lat["total_ms"],
                            "audio_bytes": estado["audio_bytes"],
                            "buffer_bytes": bytes_entrada, "transcript": texto,
                            "truncated": truncado})
            self._truncado = False
        except Exception as exc:                                        # noqa: BLE001
            self.emit_json({"type": "error", "code": "pipeline",
                            "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._em_turno.clear()

    def _fala_worker(self, fila: Queue, lat: dict, t0: float, erro: list,
                     estado: dict) -> None:
        """Sintetiza na ordem e manda o áudio; o 1º chunk usa perfil rápido."""
        primeiro = True
        omni_rapido = _perfil_live(primeiro_chunk=True,
                                   max_steps=self.first_chunk_max_steps)
        omni_normal = _perfil_live(primeiro_chunk=False, max_steps=self.first_chunk_max_steps)
        try:
            while True:
                pedaco = fila.get()
                if pedaco is None or self.cancelado:
                    break
                t_tts = time.perf_counter()
                audio = self._tts(pedaco, omni_rapido if primeiro else omni_normal)
                if primeiro:
                    lat["first_tts_ms"] = _ms(t_tts)
                if self.cancelado:              # caiu durante a síntese: não manda
                    break
                pcm = _pcm16(audio)
                self.emit_audio(pcm)
                estado["audio_bytes"] += len(pcm)
                if primeiro:
                    lat["first_chunk_ms"] = _ms(t0)
                    lat["first_audio_ms"] = _ms(t0)
                    self.ultima_latencia = dict(lat)
                    primeiro = False
                # libera VRAM FORA do caminho crítico: o áudio acima já foi emitido
                _release_app()
        except Exception as exc:                                        # noqa: BLE001
            erro.append(exc)

    def _interrompido(self, lat: dict, t0: float, turno: int, estado: dict) -> None:
        lat = {**lat, "total_ms": _ms(t0), "cancelado": True}
        self.ultima_latencia = lat
        self.emit_json({"type": "latency", **lat})
        self.emit_json({"type": "interrupted", "turn": turno,
                        "audio_bytes": estado.get("audio_bytes", 0),
                        "truncated": self._truncado})
        self._truncado = False


_PONTUACAO = re.compile(r"[^\w\s]", re.UNICODE)


def _normaliza(t: str) -> list[str]:
    t = (t or "").lower()
    t = (t.replace("á", "a").replace("à", "a").replace("ã", "a").replace("â", "a")
         .replace("é", "e").replace("ê", "e").replace("í", "i").replace("ó", "o")
         .replace("ô", "o").replace("õ", "o").replace("ú", "u").replace("ç", "c"))
    return _PONTUACAO.sub(" ", t).split()


def _parece_eco(transcript: str, ultimo_assistente: str, minimo: float = 0.6) -> bool:
    """True se o transcript é a fala do PRÓPRIO assistente voltando pelo mic.

    Compara em janela deslizante de palavras (a captura pega um pedaço da fala em
    curso): se `minimo` das palavras do transcript aparecem em sequência no que o
    assistente acabou de dizer, é eco. Sem AEC não dá para separar por energia."""
    alvo, dito = _normaliza(ultimo_assistente), _normaliza(transcript)
    if len(dito) < 2 or len(alvo) < 2:
        return False
    if len(dito) > len(alvo) * 2:          # o humano falou bem mais: não é eco
        return False
    for i in range(len(alvo) - len(dito) + 1):
        igual = sum(1 for a, b in zip(alvo[i:i + len(dito)], dito) if a == b)
        if igual / len(dito) >= minimo:
            return True
    return False


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _pcm16(audio) -> bytes:
    import numpy as np
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    return (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


# ---------------------------------------------------------------------------
# Ligações com o app (import tardio: app importa este módulo pela #91)
# ---------------------------------------------------------------------------

_LIVE_MAX_STEPS = 12          # teto de passos do 1º chunk: 16 p=0,45 s, 12 p=0,35 s
_DEBUG_TTS = __import__("os").environ.get("LIVE_DEBUG_TTS") == "1"

_prewarm_trava = threading.Lock()
_PREWARM_FEITO = False


def _perfil_live(primeiro_chunk: bool, max_steps: int) -> dict:
    """Perfil de geração do LIVE: 1º chunk rápido, os seguintes com o setting."""
    import app
    omni = app._resolve_omni({}, family=app._current_backend()["family"])
    if primeiro_chunk:
        try:
            passos = int(omni.get("num_steps") or _LIVE_MAX_STEPS)
        except (TypeError, ValueError):
            passos = _LIVE_MAX_STEPS
        omni["num_steps"] = max(4, min(passos, max_steps))
    return omni


def _release_app() -> None:
    import app
    app._release_mlx_memory(aggressive=True)


def _prewarm_app(voice_id: str | None = None) -> None:
    """Carrega os modelos E faz uma inferência de cada — uma vez por PROCESSO.

    Medido: só carregar não basta — o 1º turno pagava a compilação dos kernels
    (turno 1: STT 6,7 s e 1º áudio 21,5 s; turno 2, quente: 0,8 s e 1,4 s). Aqui
    a compilação é absorvida no handshake, com o cliente já conectado.

    A partir da 2ª sessão do processo só resta garantir os modelos carregados: as
    inferências de aquecimento já não têm o que compilar (e repeti-las por sessão
    custava ~15 s por teste na suíte)."""


    import app
    with _prewarm_trava:
        app._vad_load()
        modelo = app._get_model()
        global _PREWARM_FEITO
        if _PREWARM_FEITO:
            return
        try:                              # STT: chama o whisper DIRETO
            # `app._transcribe` curto-circuita no VAD quando não há fala: com um
            # tom (ou silêncio) o whisper nunca era chamado e o 1º turno pagava a
            # compilação dele de qualquer forma (medido: 5 s no 1º turno do smoke).
            # `_stt_lock`: sem ele o prewarm roda Metal junto com um STT em curso.
            import mlx_whisper
            tom = (np.sin(np.linspace(0, 2 * np.pi * 220, 8000)) * 0.3).astype(np.float32)
            with app._stt_lock:
                mlx_whisper.transcribe(
                    tom, path_or_hf_repo=app._whisper_repo(), language="pt",
                    temperature=0.0, condition_on_previous_text=False,
                    no_speech_threshold=0.99, logprob_threshold=-3.0,
                    compression_ratio_threshold=9.9)
        except Exception as exc:                                        # noqa: BLE001
            print(f"[live] pre-warm do STT falhou ({type(exc).__name__}) — segue", flush=True)
        try:                              # TTS: um chunk curto compila o decoder
            omni = _perfil_live(primeiro_chunk=True, max_steps=_LIVE_MAX_STEPS)
            sr = int(getattr(modelo, "sample_rate", 24000) or 24000)
            be = app._current_backend()
            # MESMO caminho do turno: com o clone prompt da voz (senão o Metal
            # compila para uma geração sem conds e o 1º chunk real paga de novo)
            path = app.VOICES_DIR / f"{voice_id}.wav" if voice_id else None
            conds = ref_text = ref_audio = None
            if path and path.exists() and be["family"] == "omnivoice":
                conds = app._cond_for(modelo, voice_id, path)
                ref_text = app._voice_ref_text(voice_id)
            elif path and path.exists():
                ref_audio = str(path)
            trava = app._NO_LOCK if app._use_remote_tts() else app._gen_lock
            with trava:                   # mesmo lock do turno: Metal é serial
                app._generate_chunk(modelo, "Ok.", "pt", conds, ref_text, omni,
                                    ref_audio=ref_audio, family=be["family"],
                                    meta=be.get("meta"), sr=sr)
            # libera e gera de novo: o release esvazia o pool do Metal e o 1º
            # turno pagava a realocação (medido: ~0,7 s a mais no 1º turno
            # depois de um servidor recém-subido, e só nele)
            app._release_mlx_memory(aggressive=True)
            # 2º generate com ~18 chars: o Metal compila por forma, então aquecer
            # só com "Ok." (5 chars) não cobria o tamanho de um 1º chunk real
            for texto_pre in ("Ok.", "Tudo bem, entendi.", "Claro, o dia está"):
                with trava:
                    app._generate_chunk(modelo, texto_pre, "pt", conds, ref_text, omni,
                                        ref_audio=ref_audio, family=be["family"],
                                        meta=be.get("meta"), sr=sr)
        except Exception as exc:                                        # noqa: BLE001
            print(f"[live] pre-warm do TTS falhou ({type(exc).__name__}) — segue", flush=True)
        _prewarm_chat()
        _PREWARM_FEITO = True


def _prewarm_chat() -> None:
    """Um request mínimo ao provedor de chat — o 1º TOKEN do turno real também
    não pode pagar cold start.

    Medido com Qwen local (mlx_lm.server): 1162 ms de 1º token no 1º turno contra
    173-350 ms depois do aquecimento — sozinho, isso estoura o alvo de 1,5 s.
    Falha aqui não derruba nada (provedor fora do ar, chave ausente…)."""
    import ssl
    import urllib.error
    import urllib.request

    import app
    try:
        base, modelo, chave = app._chat_provider()
        headers = {"Content-Type": "application/json",
                   "User-Agent": "Mozilla/5.0 (compatible; tts-studio/1.0)"}
        if chave:
            headers["Authorization"] = f"Bearer {chave}"
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:                                               # noqa: BLE001
            ctx = ssl.create_default_context()
        corpo = json.dumps({"model": modelo, "stream": True, "max_tokens": 1,
                            "messages": [{"role": "user", "content": "oi"}]}).encode()
        req = urllib.request.Request(f"{base}/chat/completions", data=corpo,
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            r.read(1)
    except Exception as exc:                                            # noqa: BLE001
        print(f"[live] pre-warm do chat falhou ({type(exc).__name__}) — segue", flush=True)


def _stt_app(pcm16_le_16k: bytes, language: str | None) -> str:
    """STT do turno pelo caminho do app (VAD + anti-alucinação inclusos)."""
    import app
    destino = app.OUTPUTS_DIR / f".live-{threading.get_ident()}-{time.time_ns()}.wav"
    try:
        with wave.open(str(destino), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm16_le_16k)
        r = app._transcribe(destino, language=language)
        return (r.get("text") or "").strip()
    finally:
        destino.unlink(missing_ok=True)


def _llm_stream_app(messages: list):
    """Deltas do LLM (SSE). Cai no `_chat_llm` se o stream não abrir."""
    import ssl
    import urllib.error
    import urllib.request

    import app
    base, modelo, chave = app._chat_provider()
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (compatible; tts-studio/1.0)"}
    if chave:
        headers["Authorization"] = f"Bearer {chave}"
    try:
        extra = json.loads(app._settings.get("chat_extra") or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except (ValueError, TypeError):
        extra = {}
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:                                                   # noqa: BLE001
        ctx = ssl.create_default_context()

    corpo = json.dumps({"model": modelo, "messages": messages, "temperature": 0.4,
                        "stream": True, **extra}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=corpo,
                                 method="POST", headers=headers)
    recebeu = False
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            for linha in resp:
                if not linha.startswith(b"data:"):
                    continue
                payload = linha[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
                except (ValueError, KeyError, IndexError):
                    continue
                if delta:
                    recebeu = True
                    yield delta
    except urllib.error.HTTPError as e:
        if recebeu:
            raise
        # provedor sem SSE (ou 400/422 no corpo): cai no caminho não-stream
        print(f"[live] stream do chat indisponível (HTTP {e.code}) — usando _chat_llm",
              flush=True)
        yield app._chat_llm(messages)


def _tts_app(texto: str, omni: dict, voice_id: str | None = None):
    """Gera UM chunk in-process (mesmo modelo/conds do app) e devolve float32 24 kHz.

    `voice_id` vem do `setup` da sessão; sem ele cai no default do app.

    Segura o `_gen_lock`, como o job normal faz: sem isso um turno do Live e uma
    geração da UI rodam MLX ao mesmo tempo e o Metal estoura — medido no smoke, o
    servidor MORREU com "Command buffer execution failed: GPU Timeout Error".
    Remoto não usa o lock (o servidor de lá paraleliza por conta dele)."""
    import time as _t
    import app
    _m = [_t.perf_counter()]
    def _marco(rot):
        _m.append(_t.perf_counter())
        if _DEBUG_TTS:
            print(f"[live][dbg] {rot} {(_m[-1]-_m[-2])*1000:.0f} ms", flush=True)
    voice_id = (voice_id or app._settings.get("default_voice")
                or app.DESIGN_VOICE_ID)
    model = app._get_model()
    _marco("get_model")
    sr = int(getattr(model, "sample_rate", 24000) or 24000)
    be = app._current_backend()
    path = app.VOICES_DIR / f"{voice_id}.wav"
    ref_audio = None
    conds = None
    ref_text = None
    if be["family"] == "omnivoice" and path.exists():
        conds = app._cond_for(model, voice_id, path)
        _marco("conds(clone_prompt)")
        ref_text = app._voice_ref_text(voice_id)
    elif path.exists():
        ref_audio = str(path)
    trava = app._NO_LOCK if app._use_remote_tts() else app._gen_lock
    _marco("prep")
    with trava:
        r = app._generate_chunk(model, texto, _idioma_tts(),
                                conds, ref_text, omni, ref_audio=ref_audio,
                                family=be["family"], meta=be.get("meta"), sr=sr)
    _marco("generate")
    return r


def _idioma_tts() -> str:
    """Idioma do texto p/ o TTS. O do app pode ser "auto" (deixa o modelo detectar)."""
    import app
    return app._settings.get("language") or "auto"


# ---------------------------------------------------------------------------
# Utilidade p/ o handler do WS (#91): casa o caminho do ws com o protocolo
# ---------------------------------------------------------------------------

def sessao_de_ws(pasta_base: pathlib.Path) -> pathlib.Path:
    """Diretório de trabalho dos WAV temporários do LIVE (usado como âncora)."""
    d = pasta_base / "live"
    d.mkdir(exist_ok=True)
    return d