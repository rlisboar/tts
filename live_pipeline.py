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
import os
import pathlib
import re
import threading
import time
import wave

import numpy as np
from queue import Empty, Queue

from dsh_client import DshError

# Backend de IA "dsh" (DSH-2): com ele o turno NÃO re-renderiza o histórico a cada
# vez — o contexto vive na sessão ACP e o turno manda só o texto novo (medido no
# DSH-1: ttft 384–449 ms quente contra 4,4–7,0 s no 1º prompt da sessão). O teto
# abaixo é o MESMO do LIVE-5 (`_LIVE_HIST_MAX_*` do app): estourou, o próximo turno
# volta ao modo histórico (persona + resumo + últimos turnos) numa sessão ACP NOVA,
# reusando o resumo que o `_live_hist_comprime` já produz — sem LLM extra.
_DSH_CTX_MAX_MSGS = max(2, int(os.environ.get("TTS_DSH_CTX_MAX_MSGS", "24")))
_DSH_CTX_MAX_CHARS = max(500, int(os.environ.get("TTS_DSH_CTX_MAX_CHARS", "12000")))

# Persona do 1º prompt de CADA sessão ACP — obrigatória no caminho dsh (DSH-2,
# decisão do PM). Sem ela o harness responde como agente de código e se alonga; e
# como o perfil entrega o parágrafo INTEIRO num único `agent_message_chunk`, o 1º
# áudio passa a esperar a geração toda. Fala curta é o que faz o alvo caber.
_DSH_PERSONA = (
    "Você conversa por voz com uma pessoa. Responda em 1 a 3 frases curtas, sem "
    "markdown e sem bloco de código, em tom de conversa."
)

# Teto do 1º delta FALÁVEL (#264). O cliente só solta o parágrafo do thought
# (≤400 chars) DEPOIS do settle; com effort=off ignorado isso foi medido em
# ~178 s e ~95k de reasoning — o mic ficava mudo dentro do `next()`, e o
# `tts_thread.join(180)` nem chegava a sintetizar. 12 s cancela o harness e
# cai no openai. O join de 180 da SÍNTESE não muda.
def _env_positivo(nome: str, padrao: float) -> float:
    bruto = os.environ.get(nome, "")
    if not bruto.strip():
        return padrao
    try:
        valor = float(bruto)
    except ValueError:
        return padrao
    return valor if valor > 0 else padrao


_DSH_FIRST_DELTA_S = _env_positivo("TTS_LIVE_DSH_FIRST_DELTA_S", 12.0)
#: estouros de relógio SEGUIDOS antes de dar o dsh como indisponível (#287)
_DSH_ESTOUROS_MAX = max(1, int(os.environ.get("TTS_LIVE_DSH_ESTOUROS_MAX", "2")))

# PCM do TTS no fio (o mesmo 24 kHz do catálogo). Cada chunk entra na fila
# como o lxToca — max(now, fim) + duração — porque o 1º já toca enquanto o
# seguinte sintetiza. Somar o PCM inteiro no fim contaria de novo o que já
# saiu do alto-falante (#269). O relógio do TurnEngine fecha a janela de
# barge cedo (#167); esta cauda é outra conta.
_TTS_SR = 24000


def _eco_cauda_s() -> float:
    """Cauda curta DEPOIS do playback estimado.

    É a janela que o TurnEngine já publica (`Config.playback_janela_ms`,
    default 900). Não é o cvEco de 12 s da tela Conversa."""
    try:
        import live_turns
        ms = float(live_turns.Config().playback_janela_ms)
    except Exception:                     # noqa: BLE001 — default do motor
        ms = 900.0
    return max(0.05, ms / 1000.0)


def _fechar_sessao_dsh(cliente, timeout: float = 5.0) -> None:
    """Fecha a SESSÃO ACP (mantém o processo) fora do turno."""
    try:
        cliente.fechar_sessao(timeout=timeout)
    except Exception:                     # noqa: BLE001
        pass


def _fechar_cliente_dsh(cliente) -> None:
    """`close()` fora do turno: com prompt ainda em voo o session/close pode
    levar o timeout de controle (60 s) e seguraria o fallback openai."""
    try:
        cliente.close()
    except Exception:                     # noqa: BLE001 — close é best-effort
        pass

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
                 detectar_eco: bool = True, dsh=None, ao_morrer_dsh=None,
                 teto_primeiro_delta=None, bridge_estado=None):
        self.emit_json = emit_json
        self.emit_audio = emit_audio
        self.voice_id = voice_id
        self.language = language
        self.system = system
        self.history = list(history or [])
        self._stt = stt or _stt_app
        self._llm = llm or _llm_stream_app
        # Backend dsh (DSH-2): cliente POR SESSÃO Live, com stream/cancel/prewarm.
        # Presente, ele substitui o `llm` no turno; ausente/none, o caminho openai
        # (ou o `llm` injetado pelos testes) fica IDÊNTICO ao de antes.
        self._dsh = dsh
        self._dsh_nova = True             # 1º turno manda o contexto renderizado
        self._dsh_turno = ""              # identidade do turno em voo no dsh
        self._dsh_indisponivel = False    # #146: dsh morto -> turnos no openai
        self._dsh_motivo = ""             # motivo do fallback (vai para o `stats`)
        #: avisa o app quando a morte foi FALHA (não relógio) — o latch de
        #: processo evita repetir 6 tentativas de handshake a cada sessão nova (#287)
        self._ao_morrer_dsh = ao_morrer_dsh
        #: teto do 1º delta: o app encurta quando o bridge está SEM o patch de
        #: streaming (`clean`) — ali não existe delta a esperar, a resposta vem
        #: inteira no fim e gastar 12 s é só atraso (#287)
        self._teto_primeiro = float(teto_primeiro_delta or _DSH_FIRST_DELTA_S)
        self.bridge_estado = bridge_estado
        #: estouros de relógio SEGUIDOS: o dsh segue vivo, mas insistir a cada turno
        #: custaria 12 s por turno — depois do teto de tentativas ele desiste
        self._dsh_estouros = 0
        # a voz vem do `setup` da sessão, não do default do app
        # Sem `tts` injetado, o turno tenta o worker PERSISTENTE da sessão (#152) e
        # cai no `_tts_app` in-process na 1ª falha (ver `_tts_live`).
        self._worker = None
        self._worker_indisponivel = False
        self._worker_motivo = ""
        # #313: a espera pelo `_gen_lock` (job de lote na frente) medida no chunk.
        # O dict é do TURNO e vai para o `latency`; com `tts` injetado pelos testes
        # ele simplesmente fica vazio (nenhuma assinatura muda).
        self._espera: dict = {}
        self._tts = tts or self._tts_live
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
        # Fila do playback, no mesmo relógio do lxToca: fim do último chunk
        # agendado. Não é a soma do PCM. `_render_ate` é esse fim + a cauda
        # do motor, para o turno que abre DEPOIS, quando `_barge_turno` já
        # é falso. `_fala_em_curso` muda no meio da síntese.
        self._play_fim = 0.0
        self._render_texto = ""
        self._render_ate = 0.0

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
        chunk sem isto, 0,36 s com).

        O worker persistente (#152) sobe AQUI, fora do turno, e o pre-warm pula o
        TTS in-process quando ele assumiu (senão o modelo do pai ficaria carregado
        à toa enquanto o filho tem o mesmo modelo).

        #207: quem chama isto é uma THREAD (`_live_pipe_start`), e o `finally` do
        WS fecha a sessão no caminho dele. Num abrir/fechar rápido o `close()` cai
        ANTES ou NO MEIO daqui; sem consultar `_saiu`, o `start()` subia worker e
        processo `dsh` para uma sessão já morta — ninguém mais chama `close()`, e
        o processo (com 2 threads leitoras) ficava órfão a cada conexão curta."""
        if self._saiu:                    # close() chegou antes: sessão já morreu
            return
        worker_ok = self._worker_sobe()
        if self._saiu:                    # close() no meio: não vale bootar o dsh
            self.close()                  # fecha o worker que nasceu depois dele
            return
        try:
            self._prewarm(voice_id=self.voice_id, tts_in_process=not worker_ok)
        except TypeError:                 # prewarm de teste sem os parâmetros
            self._prewarm()
        if self._dsh is not None:
            # boot + handshake + prompt curto do dsh: 17–18 s a frio (composição do
            # perfil no 1º uso do host) e ~0,5 s quente. Roda na thread do
            # `_live_pipe_start`, então NÃO entra no orçamento do turno.
            #
            # Falha AQUI é o caso real (#146): quem morre é o handshake (processo
            # rc=1, EPERM no perfil do dsh…), não a construção do cliente. Marcar a
            # sessão como indisponível já no pre-warm evita o turno pagar as
            # tentativas de backoff com o usuário esperando — o resto da sessão vai
            # de openai, que é o caminho testado.
            try:
                self._dsh.prewarm()
            except Exception as exc:      # noqa: BLE001
                self._dsh_morreu(exc)
        if self._saiu:
            # #207: o `close()` caiu DURANTE o boot. O processo que ele matou não
            # é necessariamente o último: o backoff do `_garantir` (dsh_client)
            # não consulta `_stopping` e sobe um processo NOVO para terminar a
            # chamada. `close()` de novo é idempotente e leva esse junto.
            self.close()

    def close(self) -> None:
        self._saiu = True
        self.cancel()
        if self._worker is not None:
            # worker é POR sessão: morre com ela (sem ele o filho ficaria com o
            # modelo na RAM depois de o cliente ir embora)
            self._worker.fecha()
            self._worker = None
        if self._dsh is not None:
            try:
                self._dsh.close()         # cliente é POR sessão: fecha com ela
            except Exception:             # noqa: BLE001 — fechar é best-effort
                pass

    # -- TTS do turno: worker persistente da sessão (#152) ------------------
    def _worker_sobe(self) -> bool:
        """Sobe o worker persistente no pre-warm (fora do turno).

        False = segue in-process para sempre nesta sessão (família não isolada,
        knob desligado ou o filho não subiu)."""
        if not _worker_habilitado():
            return False
        # nome local (#207): o `close()` pode cair no meio e zerar `self._worker`
        # entre a atribuição e o `.start()` — com o atributo direto, o handler de
        # erro estourava em `None.fecha()` e o `start()` inteiro subia de exceção.
        w = _LiveWorker(voice_id=self.voice_id)
        self._worker = w
        try:
            w.start()
        except Exception as exc:            # noqa: BLE001 — pre-warm falho não derruba
            self._worker_indisponivel = True
            self._worker_motivo = f"{type(exc).__name__}: {exc}"
            print(f"[live] worker TTS não subiu ({self._worker_motivo}) — "
                  f"sessão no in-process", flush=True)
            w.fecha()
            if self._worker is w:           # close() já zerou: não ressuscitar
                self._worker = None
            return False
        if self._saiu:
            # #228: o `close()` caiu DURANTE o `start()` do worker. Ele viu
            # `self._worker`, chamou `fecha()` — que não tinha processo nenhum para
            # matar, o filho ainda não existia — e soltou a referência. O filho que
            # acabou de nascer não é de ninguém: fecha AQUI, que é imediato e não
            # depende de um segundo `close()` achar a referência (ele já não acha).
            w.fecha()
            if self._worker is w:
                self._worker = None
            return False
        return True

    def _tts_live(self, texto: str, omni: dict):
        """TTS do turno: worker persistente; in-process quando ele não serve.

        A 1ª falha do worker (processo morto, timeout, erro do filho) marca a
        sessão e o chunk é regerado in-process — o usuário não fica sem áudio e os
        turnos seguintes nem tentam o worker (mesmo padrão do fallback do dsh)."""
        if not self._worker_indisponivel and _worker_habilitado():
            # nome local (#241, família #207/#228): o `close()` da thread do WS
            # pode cair entre a leitura e o uso e zerar `self._worker` — o turno
            # anda com `w`, senão a 2ª leitura estoura `AttributeError` em None no
            # meio do turno em vez de cair no in-process.
            w = self._worker
            if w is None:
                w = self._worker = _LiveWorker(voice_id=self.voice_id)
            if w.ativo:
                try:
                    audio = w.gerar(texto, omni, espera=self._espera)
                    # o filho real já reprova mudo, mas o PAI confere o que voltou:
                    # vale para qualquer worker e não depende do contrato do filho
                    if not np.asarray(audio).size or _rms_audio(audio) < RMS_MUDO:
                        raise _WorkerMorto(
                            f"áudio mudo do worker (rms {_rms_audio(audio):.4f})")
                    return _pos_live(audio)
                except Exception as exc:    # noqa: BLE001
                    self._worker_indisponivel = True
                    self._worker_motivo = f"{type(exc).__name__}: {exc}"
                    print(f"[live] worker TTS caiu no turno ({self._worker_motivo})"
                          f" — in-process daqui em diante", flush=True)
                    w.fecha()
        # o pós roda nos DOIS caminhos (e é ele que faz o Live soar como o job)
        return _pos_live(_tts_app(texto, omni, voice_id=self.voice_id,
                                  espera=self._espera))

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
        """Barge-in/`cancel`: derruba geração e fila. Thread-safe e idempotente.

        No backend dsh derruba TAMBÉM o `session/prompt` em voo do harness: sem
        isso a sessão ACP segue gerando depois do barge-in e um chunk tardio
        entraria no TTS do turno seguinte. Casa por identidade de turno (o
        `cancel` de um turno velho não mata o novo)."""
        self._cancel.set()
        if self._dsh is not None and self._dsh_turno:
            try:
                self._dsh.cancel(self._dsh_turno)
            except Exception:             # noqa: BLE001 — cancelar é best-effort
                pass

    @property
    def cancelado(self) -> bool:
        return self._cancel.is_set()

    def _deltas(self, msgs: list, texto: str, turno_id: str):
        """Deltas de texto do turno — backend dsh quando presente, senão o `llm`.

        MODO SESSÃO: só o 1º turno (e o primeiro depois do teto de contexto) manda
        o histórico renderizado + persona, porque é ele que ABRE a sessão ACP; os
        seguintes mandam apenas o texto novo — o contexto fica do lado do harness,
        que é o que sustenta o ttft de ~0,4 s (medido no DSH-1) contra o reenvio
        da conversa inteira a cada turno.

        A identidade do turno fica gravada ANTES de qualquer chunk: com ela o
        `cancel` acha o que derrubar no harness e o laço do turno descarta delta
        de turno abandonado — o SentenceChunker é o último portão antes do TTS."""
        if self._dsh is None or self._dsh_indisponivel:
            # sem dsh: caminho openai. A identidade do turno fica gravada de
            # qualquer forma — o laço do turno compara com ela e o `cancel` do
            # barge-in continua achando o turno (o cliente morto ignora).
            self._dsh_turno = turno_id
            return self._llm(msgs)
        return self._deltas_dsh(msgs, texto, turno_id)

    def _deltas_dsh(self, msgs: list, texto: str, turno_id: str):
        """Deltas pelo dsh, com REDE DE SEGURANÇA para o openai (#146 / #259 / #264).

        O handshake/boot do harness pode morrer ANTES de qualquer token (binário
        errado, `~/.dsh` sem permissão de escrita, Node antigo…). Sem isto a sessão
        ficava MUDA: o pipeline plugava o cliente, o turno estourava em erro e o
        usuário não ouvia nada. Se o erro vier antes do 1º delta, marcamos o dsh
        como indisponível, avisamos e refazemos o turno no `llm` (openai) — o
        usuário é respondido. Falha no MEIO do stream não dá para refazer sem
        repetir texto: aí o turno fecha com `error{pipeline}`, mas o dsh também
        fica marcado e o PRÓXIMO turno já nasce no openai.

        O 1º delta tem teto (`_DSH_FIRST_DELTA_S`): o cliente de thought-only só
        entrega o parágrafo curto DEPOIS do settle, e o provedor pode gastar
        minutos nisso. Estourou sem texto → cancela o turno, `_dsh_morreu` uma
        vez e cai no openai. Settle vazio (StopIteration, sem thought) continua
        a rede da #259. Cancel no meio não mata o dsh. Delta que já chegou não
        é repetido pelo openai.

        O pull é UM delta por vez, na MESMA ordem do `for` do turno: puxar o
        stream inteiro numa thread adiantaria o 2º token e a guarda de
        identidade (`_dsh_turno`) veria o turno trocado antes de emitir o 1º."""
        self._dsh_turno = turno_id
        if self._dsh_nova:
            self._dsh_nova = False
            gen = self._dsh.stream(self._dsh_ctx(texto), turno_id)
        else:
            gen = self._dsh.stream(texto, turno_id)
        fila: Queue = Queue()
        pedido = threading.Event()
        parar = threading.Event()

        def _puxar() -> None:
            try:
                while not parar.is_set():
                    pedido.wait()
                    pedido.clear()
                    if parar.is_set():
                        return
                    try:
                        delta = next(gen)
                    except StopIteration:
                        if not parar.is_set():
                            fila.put(("fim", None))
                        return
                    if parar.is_set():
                        return
                    fila.put(("delta", delta))
            except Exception as exc:      # noqa: BLE001 — o turno decide o fallback
                if not parar.is_set():
                    fila.put(("erro", exc))
            finally:
                try:
                    gen.close()
                except Exception:         # noqa: BLE001
                    pass

        pedido.set()
        pull = threading.Thread(target=_puxar, name="live-dsh-delta", daemon=True)
        pull.start()
        try:
            yield from self._deltas_dsh_fila(
                fila, pedido, parar, pull, msgs, texto, turno_id)
        finally:
            parar.set()
            pedido.set()                  # acorda o pull se estiver no wait

    def _deltas_dsh_fila(self, fila: Queue, pedido: threading.Event,
                         parar: threading.Event, pull: threading.Thread,
                         msgs: list, texto: str, turno_id: str):
        """Consome a fila do pull. O teto vale SÓ para o primeiro item."""
        prazo = time.monotonic() + self._teto_primeiro
        primeiro = True
        while True:
            if self.cancelado:
                return
            if primeiro:
                restante = prazo - time.monotonic()
                if restante <= 0:
                    item = self._fila_ou_none(fila, 0)
                    if item is None:
                        if self.cancelado or not (texto or "").strip():
                            return
                        # para o pull antes do cancel: thought que settleia
                        # depois do teto não pode entrar na fila e virar fala
                        parar.set()
                        pedido.set()
                        self._dsh_teto_estourou(turno_id)
                        yield from self._llm(msgs)
                        return
                else:
                    item = self._fila_ou_none(fila, min(0.05, restante))
                    if item is None:
                        continue
            else:
                item = self._fila_ou_none(fila, 0.05)
                if item is None:
                    if not pull.is_alive() and fila.empty():
                        return
                    continue
            tipo, payload = item
            if tipo == "erro":
                if self.cancelado:
                    return
                if primeiro:
                    self._dsh_morreu(payload)
                    if (texto or "").strip():
                        yield from self._llm(msgs)
                    return
                self._dsh_morreu(payload, avisa=False)
                raise payload
            if tipo == "fim":
                if primeiro and not self.cancelado and (texto or "").strip():
                    self._dsh_morreu(DshError(
                        "dsh encerrou sem emitir nenhum token"))
                    yield from self._llm(msgs)
                return
            if self.cancelado:
                return                    # chunk tardio não passa do teto nem do barge-in
            if primeiro:
                self._dsh_estouros = 0    # veio delta: a contagem de estouros zera
            primeiro = False
            yield payload
            pedido.set()

    @staticmethod
    def _fila_ou_none(fila: Queue, timeout: float):
        try:
            if timeout <= 0:
                return fila.get_nowait()
            return fila.get(timeout=timeout)
        except Empty:
            return None

    def _dsh_teto_estourou(self, turno_id: str) -> None:
        """1º delta não veio a tempo: RELÓGIO, não morte (#287).

        O teto é de 12 s e o provedor do dono mede 7–31 s de TTFT — desligar o dsh
        para o resto da sessão por isso trocava de backend com o harness saudável.
        Aqui: cancela o turno, FECHA A SESSÃO ACP (a próxima nasce limpa, sem prompt
        preso) e mantém o processo. Só depois de `_DSH_ESTOUROS_MAX` estouros
        seguidos o dsh é dado como indisponível — sem isso cada turno pagaria 12 s.

        O `close`/`cancel` vão em thread: o processo está no thought de ~95k e o
        `session/close` síncrono esperaria até 60 s antes do openai falar."""
        cliente = self._dsh
        motivo = (f"dsh sem delta falável em {self._teto_primeiro:g}s "
                  f"(teto de relógio)")
        if cliente is None:
            return
        try:
            cliente.cancel(turno_id)
        except Exception:                 # noqa: BLE001 — cancelar é best-effort
            pass
        self._dsh_estouros += 1
        threading.Thread(target=_fechar_sessao_dsh, args=(cliente,),
                         name="live-dsh-fecha-sessao", daemon=True).start()
        print(f"[live] {motivo} — tentativa {self._dsh_estouros}/{_DSH_ESTOUROS_MAX}; "
              f"o turno segue no openai", flush=True)
        self.emit_json({"type": "dsh_lento", "fallback": "openai",
                        "tentativas": self._dsh_estouros, "message": motivo})
        if self._dsh_estouros >= _DSH_ESTOUROS_MAX:
            self._dsh_morreu(DshError(f"{motivo} — {self._dsh_estouros}x seguidas"),
                             fecha_async=True, relogio=True)

    def _dsh_morreu(self, exc: Exception, avisa: bool = True,
                    *, fecha_async: bool = False, relogio: bool = False) -> None:
        """Marca o dsh como indisponível nesta sessão (uma vez) e libera o processo.

        Sem volta: uma vez marcado, NENHUM turno tenta o dsh de novo (as tentativas
        de backoff dentro do turno são o que travava o 1º áudio). O motivo fica
        guardado para o `stats`/log."""
        if self._dsh_indisponivel:
            return
        self._dsh_indisponivel = True
        self._dsh_motivo = f"{type(exc).__name__}: {exc}"[:300]
        print(f"[live] dsh indisponível na sessão — turnos seguem no openai: "
              f"{self._dsh_motivo}", flush=True)
        try:
            cliente, self._dsh = self._dsh, None
            if cliente is None:
                pass
            elif fecha_async:
                threading.Thread(
                    target=_fechar_cliente_dsh, args=(cliente,),
                    name="live-dsh-close", daemon=True).start()
            else:
                cliente.close()           # órfão NÃO fica pendurado
        except Exception:                 # noqa: BLE001
            pass
        if avisa:
            self.emit_json({"type": "dsh_indisponivel", "fallback": "openai",
                            "message": self._dsh_motivo[:200]})
        if not relogio and self._ao_morrer_dsh is not None:
            # falha de verdade (binário/perfil/node): segura o caminho para as
            # próximas sessões também, não só para esta (#287)
            try:
                self._ao_morrer_dsh(self._dsh_motivo)
            except Exception:             # noqa: BLE001 — latch é otimização
                pass

    def _dsh_ctx(self, texto: str) -> list:
        """Prompt de ABERTURA de sessão ACP: persona + system da sessão + histórico.

        A persona é obrigatória e vai na FRENTE (o harness não tem system prompt no
        protocolo: quem abre a sessão é este prompt). Vale também para a sessão
        reaberta pelo teto de contexto — sessão nova, persona nova."""
        sistema = _DSH_PERSONA
        if self.system:
            sistema = f"{sistema}\n\n{self.system}"
        return ([{"role": "system", "content": sistema}] + list(self.history)
                + [{"role": "user", "content": texto}])

    def _dsh_pos_turno(self) -> None:
        """Teto de contexto do dsh, no FIM do turno (fora da latência).

        Estourou, o próximo turno reabre sessão ACP nova com o contexto
        RENDERIZADO. O resumo não é recalculado aqui: o `_live_hist_comprime` do
        app já reescreveu `self.history` com a linha "Resumo do que já foi dito",
        e é ela que entra no prompt da sessão nova (reuso, sem LLM extra)."""
        if self._dsh is None or self._dsh_nova:
            return
        if len(self.history) > _DSH_CTX_MAX_MSGS or \
                sum(len(m.get("content") or "") for m in self.history) > _DSH_CTX_MAX_CHARS:
            self._dsh_nova = True

    def _agendar_chunk(self, audio_bytes: int) -> None:
        """Um chunk que acabou de sair no fio, no relógio do lxToca.

        `proximo = max(agora, proximo) + duração`. O que já foi tocado não
        volta para a conta no fim do turno."""
        if audio_bytes <= 0:
            return
        dur = audio_bytes / float(_TTS_SR * 2)
        agora = time.monotonic()
        self._play_fim = max(agora, self._play_fim) + dur

    def _marcar_render(self, texto: str) -> None:
        """Fecha a referência de eco deste turno.

        A cauda começa quando a fila acaba (`_play_fim`), não agora + PCM
        inteiro. Sem chunk emitido não arma. No cancel o cliente já deu
        stop (lxCancelaPlayback no speech_start): o que ainda estava
        agendado não toca, então a janela é só a cauda a partir de agora.
        """
        texto = (texto or "").strip()
        if not texto or self._play_fim <= 0:
            return
        self._render_texto = texto
        if self.cancelado:
            agora = time.monotonic()
            self._play_fim = agora
            self._render_ate = agora + _eco_cauda_s()
            return
        self._render_ate = self._play_fim + _eco_cauda_s()

    def _parece_eco(self, texto: str) -> bool:
        """Eco da própria voz — no barge-in OU na cauda logo depois do playback.

        Fora dessas janelas não compara: o humano pode repetir a frase minutos
        depois. "Sim." continua passando (`_parece_eco` exige ≥2 palavras e
        sobreposição ≥0.6). Sem AEC."""
        if not self.detectar_eco:
            return False
        if self._barge_turno:
            ref = self._fala_em_curso
            if not ref and self.history and self.history[-1].get("role") == "assistant":
                ref = self.history[-1].get("content") or ""
            return _parece_eco(texto, ref)
        if not self._render_texto or time.monotonic() > self._render_ate:
            return False
        return _parece_eco(texto, self._render_texto)

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
        self._play_fim = 0.0          # speech_start no cliente zera a fila
        try:
            self.emit_json({"type": "speech_start", "turn": turno,
                            "buffer_bytes": bytes_entrada})
            # #314: veredito do STT do turno (`motivo`: "vad" ou "stt"). Só o
            # caminho REAL de produção o preenche; com `stt` injetado o dict fica
            # vazio e nada muda.
            julga: dict = {}
            if self._stt is _stt_app:
                texto = _stt_app(pcm, self.language, julga)
            else:
                texto = self._stt(pcm, self.language)
            lat["stt_ms"] = _ms(t0)
            if self.cancelado:
                return self._interrompido(lat, t0, turno, estado)
            motivo = str(julga.get("motivo") or "")
            if motivo:
                # fala julgada e RECUSADA (VAD do STT ou anti-alucinação): fecha
                # com o motivo, em vez de silêncio indistinguível de "não ouvi".
                # Não gasta LLM nem TTS e não entra no history.
                self.emit_json({"type": "turn_complete", "turn": turno,
                                "ms": _ms(t0), "audio_bytes": 0, "descartado": True,
                                "motivo": motivo,
                                "detalhe": str(julga.get("detalhe") or "")[:120],
                                "buffer_bytes": bytes_entrada, "transcript": texto,
                                "truncated": truncado})
                self._truncado = False
                return
            if not texto.strip():
                # nada transcrito e nenhum motivo declarado (stt injetado)
                self.emit_json({"type": "turn_complete", "turn": turno,
                                "ms": _ms(t0), "audio_bytes": 0,
                                "buffer_bytes": bytes_entrada, "transcript": "",
                                "truncated": truncado})
                self._truncado = False
                return
            if self._parece_eco(texto):
                # era o próprio TTS voltando pelo mic (barge ou cauda): não
                # gasta LLM/TTS, não entra no history
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
            turno_id = f"lt{turno}"
            inteiro = []
            for delta in self._deltas(msgs, texto, turno_id):
                if self.cancelado:
                    break
                if self._dsh is not None and self._dsh_turno != turno_id:
                    break                 # identidade: chunk de turno abandonado
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
            self._marcar_render(resposta)
            self.history += [{"role": "user", "content": texto},
                             {"role": "assistant", "content": resposta}]
            self._dsh_pos_turno()           # teto de contexto: fora da latência

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
        # #313: dict do turno onde o TTS registra a espera pelo `_gen_lock` (o
        # `_tts_live`/`_tts_app` escrevem nele). Vai para o `latency` do 1º chunk:
        # com um job de lote segurando o lock, o tempo deixa de ficar embutido no
        # `first_chunk_ms` sem nome. Com `tts` injetado nada escreve e o campo sai
        # ausente, que é honesto (não houve lock nosso para medir).
        espera: dict = {}
        self._espera = espera
        omni_rapido = _perfil_live(primeiro_chunk=True,
                                   max_steps=self.first_chunk_max_steps)
        # #315: 2º chunk também sai no perfil do Live (teto `live_num_steps`). Com
        # um chunk por vez e a geração serializada, o 1º só cobre o playback até o
        # 2º ficar pronto — mandar o 2º com o `num_steps` do setting (37, ~1,1 s)
        # abria buraco no alto-falante logo na virada. Do 3º em diante vale o
        # setting: aí a fila já tem folga e a qualidade volta a ser a do dono.
        omni_live = _perfil_live(primeiro_chunk=True,
                                 max_steps=_num_steps_live())
        omni_normal = _perfil_live(primeiro_chunk=False, max_steps=self.first_chunk_max_steps)
        n_chunk = 0
        try:
            while True:
                pedaco = fila.get()
                if pedaco is None or self.cancelado:
                    break
                if n_chunk == 0:
                    perfil = omni_rapido
                elif n_chunk == 1:
                    perfil = omni_live
                else:
                    perfil = omni_normal
                n_chunk += 1
                t_tts = time.perf_counter()
                audio = self._tts(pedaco, perfil)
                if primeiro:
                    lat["first_tts_ms"] = _ms(t_tts)
                if self.cancelado:              # caiu durante a síntese: não manda
                    break
                pcm = _pcm16(audio)
                self.emit_audio(pcm)
                estado["audio_bytes"] += len(pcm)
                self._agendar_chunk(len(pcm))
                if primeiro:
                    lat["first_chunk_ms"] = _ms(t0)
                    lat["first_audio_ms"] = _ms(t0)
                    if espera.get("lock_espera_ms") is not None:
                        lat["lock_espera_ms"] = int(espera["lock_espera_ms"])
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


# Palavras que o HUMANO usa e o eco (que repete o texto do assistente) não
# acrescenta: negação, contraste, pergunta e comando. Só derrubam a classificação
# quando a REFERÊNCIA não as tem — senão "então"/"e", que aparecem em qualquer
# frase, anulariam a janela. Ver `_parece_eco` (#301).
_MARCADORES_HUMANO = frozenset({
    "nao", "nem", "mas", "porem", "so", "cade", "quando", "onde", "quanto", "quanta",
    "quem", "como", "qual", "porque", "sim", "espera", "calma", "deixa", "repete",
    "outro", "outra", "contrario", "diferente", "errado", "corrige",
})


def _marcador_de_humano(transcript: str, dito: list[str], ultimo: str,
                        alvo: list[str]) -> bool:
    """O transcript traz algo que o ECO não traria?

    Duas portas, ambas medidas na matriz de `smoke_eco_regra.py`:

    · pontuação de PERGUNTA no transcript quando a referência não tem — o eco copia
      a prosódia do alto-falante, então "?" no eco de uma frase declarativa é raro;
    · palavra de `_MARCADORES_HUMANO` que a referência NÃO disse (o humano
      pergunta, nega ou manda repetir com vocabulário que ele mesmo escolheu).

    A regra é conservadora de propósito: descartar fala é pior que rodar um STT.
    """
    if "?" in (transcript or "") and "?" not in (ultimo or ""):
        return True
    return bool((set(dito) - set(alvo)) & _MARCADORES_HUMANO)


def _janela_ate(alvo: list[str], dito: list[str], divergencias: int) -> bool:
    """O transcript aparece CONTÍGUO na referência com até N palavras diferentes.

    Janela contígua e não "60 % das palavras em qualquer lugar": o eco é um trecho
    LITERAL da fala em curso, então o que o distingue é a ordem ser a da referência
    com quase nenhuma troca — a contagem solta aceitava frase humana que só
    reusasse o vocabulário.
    """
    n = len(dito)
    if not n or n > len(alvo):
        return False
    for i in range(len(alvo) - n + 1):
        erros = sum(1 for a, b in zip(alvo[i:i + n], dito) if a != b)
        if erros <= divergencias:
            return True
    return False


# Tolerância do casamento contíguo (#301). `minimo` de acerto vira `1 - minimo` de
# divergência por palavra, com TETO fixo: acima de 2 trocas a "janela" já não é um
# trecho literal do que está tocando e passaria a aceitar frase humana.
_ECO_DIVERGENCIA = 1.0 - 0.6          # 40 % de palavras diferentes, no máximo
_ECO_DIVERGENCIA_TETO = 2             # 2 palavras, sempre
_ECO_MIN_PALAVRAS = 2                 # 1 palavra nunca é eco


def _parece_eco(transcript: str, ultimo_assistente: str, minimo: float = 0.6) -> bool:
    """True se o transcript é a fala do PRÓPRIO assistente voltando pelo mic.

    O eco é um trecho LITERAL do que está tocando: mesma ordem, quase sem troca.
    `_normaliza` + janela contígua com `minimo` de acerto (`minimo=0.6` → até
    `_ECO_DIVERGENCIA` = 40 % de divergência, com teto de `_ECO_DIVERGENCIA_TETO`).
    Antes disso, três descartes: transcript de `_ECO_MIN_PALAVRAS` (o eco nunca é de
    1 — "Sim." é humano), transcript MAIOR que a referência (o eco não fala mais do
    que o assistente) e marcador de humano (`_marcador_de_humano`, que só vale
    contra referência sem o mesmo marcador).

    Medido em `smoke_eco_regra.py`: 0 falso-descarte nas falas humanas e eco puro
    preservado. **Limite conhecido:** eco que o Whisper fecha com um `?` que o
    assistente não falou é lido como humano (ex.: "o prazo de entrega é sexta-feira?"
    virando resposta à própria voz) — exigir palavra interrogativa junto do `?`
    devolveria metade dos falsos-descartes, então o custo fica. Sem AEC não dá para
    separar por energia."""
    alvo, dito = _normaliza(ultimo_assistente), _normaliza(transcript)
    if len(dito) < _ECO_MIN_PALAVRAS or len(alvo) < _ECO_MIN_PALAVRAS:
        return False
    if len(dito) > len(alvo) + 1:          # o humano falou mais: não é eco
        return False
    if _marcador_de_humano(transcript, dito, ultimo_assistente, alvo):
        return False
    fracao = (1.0 - minimo) if minimo else _ECO_DIVERGENCIA
    return _janela_ate(alvo, dito,
                       min(_ECO_DIVERGENCIA_TETO, int(round(len(dito) * fracao))))


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
# #315: teto do 2º chunk — o perfil do Live, configurável. Do 3º em diante volta o
# `num_steps` do dono (a fila já tem folga e a voz volta ao ajuste dele).
_LIVE_NUM_STEPS_PADRAO = 16
_DEBUG_TTS = __import__("os").environ.get("LIVE_DEBUG_TTS") == "1"

_prewarm_trava = threading.Lock()
_PREWARM_FEITO = False


def _num_steps_live() -> int:
    """Passos do perfil do Live (setting `live_num_steps`, teto próprio da máquina)."""
    import app
    try:
        passos = int(app._settings.get("live_num_steps") or _LIVE_NUM_STEPS_PADRAO)
    except (TypeError, ValueError):
        passos = _LIVE_NUM_STEPS_PADRAO
    return max(4, passos)


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


def _stt_aquece(app) -> None:
    """Uma transcrição curta p/ compilar o whisper (chama o mlx_whisper DIRETO).

    `app._transcribe` curto-circuita no VAD quando não há fala: com um tom (ou
    silêncio) o whisper nunca era chamado e o 1º turno pagava a compilação dele de
    qualquer forma (medido: 5 s no 1º turno do smoke). `_stt_lock`: sem ele o
    prewarm roda Metal junto com um STT em curso. Falha aqui não derruba nada."""
    try:
        import mlx_whisper
        tom = (np.sin(np.linspace(0, 2 * np.pi * 220, 8000)) * 0.3).astype(np.float32)
        with app._stt_lock:
            mlx_whisper.transcribe(
                tom, path_or_hf_repo=app._whisper_repo(), language="pt",
                temperature=0.0, condition_on_previous_text=False,
                no_speech_threshold=0.99, logprob_threshold=-3.0,
                compression_ratio_threshold=9.9)
    except Exception as exc:              # noqa: BLE001
        print(f"[live] pre-warm do STT falhou ({type(exc).__name__}) — segue", flush=True)


def _prewarm_app(voice_id: str | None = None, tts_in_process: bool = True) -> None:
    """Carrega os modelos E faz uma inferência de cada — uma vez por PROCESSO.

    Medido: só carregar não basta — o 1º turno pagava a compilação dos kernels
    (turno 1: STT 6,7 s e 1º áudio 21,5 s; turno 2, quente: 0,8 s e 1,4 s). Aqui
    a compilação é absorvida no handshake, com o cliente já conectado.

    A partir da 2ª sessão do processo só resta garantir os modelos carregados: as
    inferências de aquecimento já não têm o que compilar (e repeti-las por sessão
    custava ~15 s por teste na suíte).

    `tts_in_process=False` (#152): quem aquece o TTS é o worker PERSISTENTE da
    sessão (ele carrega e aquece no `init`, também fora do turno). Aqui o modelo
    TTS do pai é LIBERADO em vez de carregado — senão o mesmo modelo viveria duas
    vezes na RAM do M3. O STT/chat seguem iguais (o worker é só TTS)."""

    import app
    global _PREWARM_FEITO
    with _prewarm_trava:
        app._vad_load()
        if not tts_in_process:
            app._unload_local_tts()      # o worker tem o modelo; o pai não precisa
            if not _PREWARM_FEITO:
                _stt_aquece(app)
                _prewarm_chat()
                _PREWARM_FEITO = True
            return
        modelo = app._get_model()
        if _PREWARM_FEITO:
            return
        _stt_aquece(app)
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


def _stt_app(pcm16_le_16k: bytes, language: str | None,
             julga: dict | None = None) -> str:
    """STT do turno pelo caminho do app (VAD + anti-alucinação inclusos).

    Vem o `_stt_ok` junto (#314): o Live chamava `_transcribe` direto e o filtro
    anti-alucinação do projeto — blacklist, `no_speech_prob`, `avg_logprob`,
    `compression_ratio`, mínimos — só valia na Conversa; alucinação do Whisper ia
    reta ao LLM. Rejeitado, o texto NÃO entra no turno e `julga["motivo"]` diz por
    quê (`"stt"`), para o cliente e o `stats` verem o descarte.

    Turno que o VAD do próprio `_transcribe` recusa chega aqui como texto vazio
    com `language` vazio (é o formato daquele ramo) e vira motivo `"vad"` — é
    diferente de ruído qualquer e não pode sair indistinguível de "não ouvi".
    """
    import app
    destino = app.OUTPUTS_DIR / f".live-{threading.get_ident()}-{time.time_ns()}.wav"
    try:
        with wave.open(str(destino), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm16_le_16k)
        r = app._transcribe(destino, language=language)
        texto = (r.get("text") or "").strip()
        if not texto:
            if julga is not None:
                julga["motivo"] = "stt" if r.get("language") else "vad"
            return ""
        ok, motivo = app._stt_ok(r, texto)
        if not ok:
            if julga is not None:
                julga["motivo"] = "stt"
                julga["detalhe"] = motivo
            return texto            # o texto fica no `transcript`; quem descarta é o turno
        return texto
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


def _tts_app(texto: str, omni: dict, voice_id: str | None = None,
             espera: dict | None = None):
    """Gera UM chunk in-process (mesmo modelo/conds do app) e devolve float32 24 kHz.

    `voice_id` vem do `setup` da sessão; sem ele cai no default do app.

    Segura o `_gen_lock`, como o job normal faz: sem isso um turno do Live e uma
    geração da UI rodam MLX ao mesmo tempo e o Metal estoura — medido no smoke, o
    servidor MORREU com "Command buffer execution failed: GPU Timeout Error".
    Remoto não usa o lock (o servidor de lá paraleliza por conta dele).

    `espera` (#313) é o dict do turno que recebe `lock_espera_ms`: o lock é GLOBAL
    e um job de lote o segura pelo job inteiro, então esse tempo some dentro do
    `first_chunk_ms` e fica indistinguível de TTS lento. Só mede, não arbitra."""
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
    t_trava = _t.perf_counter()
    with trava:
        if espera is not None:
            espera["lock_espera_ms"] = _ms(t_trava)
        _marco("trava")
        r = app._generate_chunk(model, texto, _idioma_tts(),
                                conds, ref_text, omni, ref_audio=ref_audio,
                                family=be["family"], meta=be.get("meta"), sr=sr)
    _marco("generate")
    return r


def _pos_live(audio):
    """Pós do trecho do Live — a MESMA cadeia do job (app._pos_processa).

    Sem isto o Live saía ~2-3 dB mais baixo que o mesmo texto no job, com clique
    a cada borda (sem rampa) e aceitava áudio mudo. Cauda de 0,15 s: aqui não há
    silêncio inserido entre trechos, e o trim+pad é o que dá o respiro.
    """
    import app

    try:
        return app._pos_processa(audio, app._LIVE_AUDIO_SR, pad_s=0.15)
    except Exception as exc:             # noqa: BLE001 — nunca sem áudio por causa do pós
        # alguns dublês de teste devolvem sentinela em vez de PCM; degrada avisando
        print(f"[live] pós do trecho falhou ({type(exc).__name__}: {exc}) — áudio cru",
              flush=True)
        return audio


def _idioma_tts() -> str:
    """Idioma do texto p/ o TTS. O do app pode ser "auto" (deixa o modelo detectar)."""
    import app
    return app._settings.get("language") or "auto"


# ---------------------------------------------------------------------------
# Worker TTS PERSISTENTE por sessão (#152)
#
# POR QUE: o `tts_worker` (processo filho) isola o crash nativo do Metal, mas paga
# o load do modelo a cada job — medido com kokoro: 7,5 s de 1º áudio contra 0,24 s
# no in-process quente. No Live o alvo é 1,5 s, então família isolada ficava
# impossível. Aqui o filho carrega UMA vez por SESSÃO e atende N turnos pelo
# protocolo NDJSON do `tts_worker.py --serve`.
#
# EXCLUSIVIDADE DE METAL (o desenho): o árbitro continua sendo o `app._gen_lock`,
# o MESMO lock da geração in-process e do spawn do worker de lote (que o segura
# pelo job inteiro). `_LiveWorker.start()` e `_LiveWorker.gerar()` seguram esse
# lock durante toda a operação — logo jamais há duas gerações no Metal: Live
# (worker ou in-process), Conversa/UI e job de lote se serializam. Com o worker
# vivo a sessão do Live NÃO gera in-process; na 1ª falha ela marca
# `worker_indisponivel` e passa a gerar in-process (o filho morre), sem tocar o
# lock de dentro do lock. O modelo do pai é liberado quando o filho assume
# (`_unload_local_tts`), senão a RAM do M3 carregaria o mesmo modelo duas vezes.
# ---------------------------------------------------------------------------

_LIVE_WORKER_LIGADO = os.environ.get("TTS_LIVE_WORKER", "1") != "0"


#: abaixo disto o áudio do worker é mudo de fato e o turno é regerado in-process
RMS_MUDO = 0.01


def _rms_audio(audio) -> float:
    a = np.asarray(audio, dtype=np.float64).reshape(-1)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


class _WorkerMorto(RuntimeError):
    """Falha do worker persistente: o chamador cai no in-process."""


def _worker_habilitado() -> bool:
    """Worker só no caminho do Live E para família isolada (as que crasham Metal)."""
    if not _LIVE_WORKER_LIGADO:
        return False
    try:
        import app
        return app._current_backend()["family"] in app._ISOLATED_FAMILIES
    except Exception:                    # noqa: BLE001 — sem app, segue in-process
        return False


def _py_do_projeto() -> str:
    import sys

    import app
    py = app.BASE / ".venv-mlx" / "bin" / "python"
    return str(py) if py.exists() else sys.executable


class _LiveWorker:
    """Cliente do worker persistente — um por sessão do Live.

    Protocolo: NDJSON em stdin/stdout (`tts_worker.py --serve`). Leitor em thread
    própria porque o `gerar` é chamado da thread de TTS do turno; EOF do filho
    (crash) chega como `_WorkerMorto` em vez de travar o turno para sempre.
    """

    def __init__(self, voice_id: str | None = None, *, py=None, script=None,
                 timeout_s=None):
        self.voice_id = voice_id
        self._py = py
        self._script = script
        self._timeout = float(timeout_s or os.environ.get("TTS_LIVE_WORKER_TIMEOUT_S", "60"))
        self._proc = None
        self._log = None
        self._leitor = None
        self._fila: Queue = Queue()
        self._ativo = False
        self._n = 0

    @property
    def ativo(self) -> bool:
        return self._ativo

    def start(self) -> None:
        """Spawna o filho e espera o `ready` (load+aquece DENTRO do `_gen_lock`)."""
        import subprocess
        import uuid

        import app
        if self._ativo:
            return
        self._log = open(app.OUTPUTS_DIR /
                         f".live-tts-worker-{os.getpid()}-{uuid.uuid4().hex[:8]}.log",
                         "w", encoding="utf-8")  # nome único: sessões em paralelo
        cfg = {
            "voice_id": self.voice_id or "",
            "voice_path": "",
            "language": app._settings.get("language") or "auto",
            "omni": {},
            "model": app._settings.get("model"),
            "settings": {
                "chunk_max_chars": app._settings.get("chunk_max_chars", 140),
                "omni_ref_max_s": app._settings.get("omni_ref_max_s", 10.0),
                "omni_precision": app._settings.get("omni_precision", "bf16"),
                "audio_gain_db": app._settings.get("audio_gain_db", 0.0),
                "audio_eq_low_db": app._settings.get("audio_eq_low_db", 0.0),
                "audio_eq_mid_db": app._settings.get("audio_eq_mid_db", 0.0),
                "audio_eq_high_db": app._settings.get("audio_eq_high_db", 0.0),
            },
            "voices_dir": str(app.VOICES_DIR),
            "base_dir": str(app.BASE),
        }
        trava = app._NO_LOCK if app._use_remote_tts() else app._gen_lock
        try:
            with trava:
                self._proc = subprocess.Popen(
                    [self._py or _py_do_projeto(),
                     str(self._script or (app.BASE / "tts_worker.py")), "--serve"],
                    cwd=str(app.BASE), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self._log, text=True, bufsize=1,
                    start_new_session=True,     # grupo próprio: kill limpo
                )
                self._leitor = threading.Thread(target=self._le, daemon=True)
                self._leitor.start()
                self._envia({"type": "init", "config": cfg})
                r = self._espera(("ready", "fatal"),
                                 timeout=float(os.environ.get("TTS_LIVE_WORKER_INIT_S", "300")))
        except Exception:
            self.fecha()
            raise
        if r.get("type") == "fatal":
            erro = r.get("error") or "init falhou"
            self.fecha()
            raise _WorkerMorto(erro)
        self._ativo = True

    def _envia(self, obj: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise _WorkerMorto("worker não iniciado")
        try:
            self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _WorkerMorto(f"stdin: {exc}") from exc

    def _le(self) -> None:
        proc = self._proc
        try:
            for linha in proc.stdout:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    self._fila.put(json.loads(linha))
                except ValueError:
                    print(f"[live-worker] fora do protocolo: {linha[:120]}", flush=True)
        except Exception:                # noqa: BLE001 — leitura best-effort
            pass
        finally:
            self._fila.put({"type": "_eof"})

    def _espera(self, tipos: tuple, timeout: float) -> dict:
        limite = time.monotonic() + timeout
        while True:
            restante = limite - time.monotonic()
            if restante <= 0:
                raise _WorkerMorto(f"timeout de {timeout:.0f}s esperando {tipos}")
            try:
                msg = self._fila.get(timeout=restante)
            except Empty:
                raise _WorkerMorto(f"timeout de {timeout:.0f}s esperando {tipos}") from None
            if msg.get("type") == "_eof":
                raise _WorkerMorto("worker morreu (EOF no protocolo)")
            if msg.get("type") in tipos:
                return msg

    def gerar(self, texto: str, omni: dict, espera: dict | None = None):
        """Um chunk pelo filho. Levanta `_WorkerMorto` em qualquer falha.

        `espera` (#313) recebe `lock_espera_ms`: o mesmo lock global do caminho
        in-process, medido ANTES de adquirir (só mede, não arbitra)."""
        import base64

        import app
        if not self._ativo:
            raise _WorkerMorto("worker inativo")
        self._n += 1
        trava = app._NO_LOCK if app._use_remote_tts() else app._gen_lock
        t_trava = time.perf_counter()
        with trava:                      # Metal: uma geração por vez (ver topo)
            if espera is not None:
                espera["lock_espera_ms"] = _ms(t_trava)
            if not self._ativo:
                raise _WorkerMorto("worker inativo")
            self._envia({"type": "synth", "id": self._n, "text": texto,
                         "omni": omni or {}})
            r = self._espera(("ok", "err"), self._timeout)
        if r.get("type") == "err":
            raise _WorkerMorto(r.get("error") or "erro no worker")
        if r.get("id") != self._n:
            raise _WorkerMorto(f"resposta fora de ordem (id {r.get('id')} != {self._n})")
        audio = np.frombuffer(base64.b64decode(r.get("audio_b64") or ""),
                              dtype=np.float32)
        if audio.size == 0:
            raise _WorkerMorto("áudio vazio")
        # mudo do filho vira FALHA (não áudio aceito): o turno é regerado
        # in-process pelo chamador. Antes só `size == 0` reprovava e um trecho
        # silencioso atravessava o turno inteiro (#305, item 9 da auditoria).
        rms = r.get("rms")
        if rms is None:
            rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        if float(rms) < 0.01:
            raise _WorkerMorto(f"áudio mudo do worker (rms {float(rms):.4f})")
        return audio

    def fecha(self) -> None:
        """Encerra o filho (fim/TTL da sessão). Idempotente e best-effort."""
        self._ativo = False
        proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    try:
                        self._envia({"type": "close"})
                        proc.wait(timeout=5)
                    except Exception:    # noqa: BLE001
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:  # noqa: BLE001
                            proc.kill()
            except Exception:            # noqa: BLE001
                try:
                    proc.kill()
                except Exception:        # noqa: BLE001
                    pass
            for fluxo in (proc.stdin, proc.stdout):
                try:
                    if fluxo is not None:
                        fluxo.close()
                except Exception:        # noqa: BLE001
                    pass
        self._proc = None
        if self._log is not None:
            caminho = self._log.name
            try:
                self._log.close()
            except Exception:            # noqa: BLE001
                pass
            self._log = None
            # log vazio não deixa lixo (suítes criam dezenas de sessões): fica só
            # quando o filho de fato escreveu algo (warning/erro do modelo)
            try:
                p = pathlib.Path(caminho)
                if p.exists() and p.stat().st_size == 0:
                    p.unlink()
            except Exception:            # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Utilidade p/ o handler do WS (#91): casa o caminho do ws com o protocolo
# ---------------------------------------------------------------------------

def sessao_de_ws(pasta_base: pathlib.Path) -> pathlib.Path:
    """Diretório de trabalho dos WAV temporários do LIVE (usado como âncora)."""
    d = pasta_base / "live"
    d.mkdir(exist_ok=True)
    return d