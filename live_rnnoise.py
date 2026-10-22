"""RNNoise no PCM do Live, antes do `TurnEngine.feed`.

Um ffmpeg por sessão, vivo o tempo todo: s16le 16 kHz entra, sobe para 48 kHz
(o `arnndn` só aceita 48 kHz), passa pelo modelo e volta para 16 kHz. Não há
spawn por frame de 100 ms — o processo nasce com a sessão e morre com ela.

O ffmpeg não devolve um frame por entrada: a saída sai em rajadas e o atraso
interno oscila (medido: ~10–80 ms). Uma reserva de silêncio absorve essa
oscilação, então cada `processa` devolve o MESMO número de bytes que entrou
sem esperar o frame inteiro (teto de 40 ms). O relógio do VAD não anda. O
atraso que fica é o da reserva mais o do filtro — da ordem de um frame.

Se o modelo ou o ffmpeg não abrirem, `processa` devolve o PCM cru e o Live
segue — uma linha no log, não um silêncio.

Modelo: `models/rnnoise/sh.rnnn` (ver NOTICE ao lado). Não baixa nada no boot.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("live")

RAIZ = Path(__file__).resolve().parent
MODELO_PADRAO = RAIZ / "models" / "rnnoise" / "sh.rnnn"
SR = 16000
# silêncio inicial: enche o atraso do arnndn e dos dois resamples
_PRIME_MS = 300
# o que fica na fila depois do prime. Medido na rajada do ffmpeg (16 kHz,
# frames de 100 ms): 40 ms já cobre o buraco; 50 ms deixa folga sem somar
# outro frame inteiro ao barge-in.
_RESERVA_MS = 50
# sem byte novo por este tempo, a rajada desta escrita acabou
_ESTANCA_S = 0.04
_PRIME_TETO_S = 0.6
# o que não saiu neste teto não sai esperando: está preso até a próxima
# entrada. Esperar 160 ms só atrasava o VAD e completava com zero.
_ESPERA_S = 0.04

_avisou: set[str] = set()
# stdin já fechado pelo `_mata` chega como ValueError ("I/O operation on
# closed file") quando o `fechar()` corre junto de uma escrita — não é OSError.
_ERRO_PIPE = (BrokenPipeError, OSError, ValueError)
_avisou_lock = threading.Lock()


def ligado() -> bool:
    """Default ligado. `TTS_LIVE_RNNOISE=0` desliga (a suíte usa isso)."""
    return os.environ.get("TTS_LIVE_RNNOISE", "1") != "0"


def _avisa(msg: str) -> None:
    """Uma linha por motivo, no processo — não por frame e não por sessão."""
    with _avisou_lock:
        if msg in _avisou:
            return
        _avisou.add(msg)
    log.warning("%s", msg)


def _ffmpeg_bin() -> str:
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — sem o pacote, o caminho histórico
        return "/opt/homebrew/bin/ffmpeg"


def _binario_ok(binario: str) -> bool:
    if not binario:
        return False
    return Path(binario).exists() or shutil.which(binario) is not None


def _caminho_filtro(path: Path) -> str:
    """Escapa o que o grafo do ffmpeg trata como separador."""
    s = str(path)
    return (s.replace("\\", "\\\\").replace(":", "\\:")
             .replace("'", "\\'").replace(",", "\\,"))


def _bytes_ms(ms: int) -> int:
    n = SR * 2 * ms // 1000
    return n - (n % 2)


class RnnoiseAoVivo:
    """Estado RNNoise de UMA sessão Live. `processa` é síncrono e preserva o tamanho."""

    def __init__(self, modelo: Path | None = None, ffmpeg: str | None = None):
        self.modelo = Path(modelo) if modelo is not None else MODELO_PADRAO
        self.ffmpeg = ffmpeg or _ffmpeg_bin()
        self._reserva = _bytes_ms(_RESERVA_MS)
        self._proc: subprocess.Popen | None = None
        self._out = bytearray()
        self._err = bytearray()
        self._cv = threading.Condition()
        self._lock = threading.Lock()
        self._pronto = threading.Event()
        self._ok = False
        self._vivo = False
        # `fechar()` pode correr com `abrir()` (a sessão cai no handshake). A flag
        # fecha a janela: ver `_abrir`/`processa`/`fechar` (#374).
        self._fechado = False
        self.faltas = 0          # bytes completados com zero (não deu tempo)
        self._leitor: threading.Thread | None = None
        self._erros: threading.Thread | None = None

    @property
    def ativo(self) -> bool:
        return self._ok

    def abrir(self) -> bool:
        try:
            return self._abrir()
        finally:
            self._pronto.set()

    def _abrir(self) -> bool:
        if self._ok:
            return True
        if not self.modelo.is_file():
            _avisa(f"rnnoise sem modelo ({self.modelo}) — Live segue sem o filtro")
            return False
        if not _binario_ok(self.ffmpeg):
            _avisa(f"rnnoise sem ffmpeg ({self.ffmpeg}) — Live segue sem o filtro")
            return False
        modelo = _caminho_filtro(self.modelo)
        af = f"aresample=48000,arnndn=m={modelo},aresample=16000"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-fflags", "nobuffer", "-probesize", "32", "-analyzeduration", "0",
               "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
               "-flush_packets", "1", "-af", af,
               "-f", "s16le", "-ar", str(SR), "-ac", "1", "pipe:1"]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
        except OSError as exc:
            _avisa(f"rnnoise não subiu ({exc}) — Live segue sem o filtro")
            return False
        # A ATRIBUIÇÃO é o ponto de encontro com o `fechar()` (#374/F1). Fora do
        # `_lock`, um `fechar` que caísse entre o `Popen` e esta linha lia `_proc`
        # como None, não matava nada e o ffmpeg ficava vivo para sempre, com o
        # objeto já fechado (só morria com o app). Sob o lock o `_fechado` é
        # rechecado: ou ele já levou o `_proc` embora — e nós matamos ESTE —, ou
        # ele acha o nosso aqui e mata.
        with self._lock:
            if self._fechado:
                self._mata(proc)
                return False
            self._proc = proc
            self._vivo = True
        self._leitor = threading.Thread(target=self._ler, args=(proc,),
                                        name="rnnoise-stdout", daemon=True)
        self._erros = threading.Thread(target=self._ler_err, args=(proc,),
                                       name="rnnoise-stderr", daemon=True)
        self._leitor.start()
        self._erros.start()
        # o modelo é lido no start; se o caminho ou o filtro forem inválidos,
        # o processo morre antes de aceitar o prime
        time.sleep(0.05)
        if proc.poll() is not None:
            self._falhou_abrir(proc)
            return False
        try:
            self._escreve(proc, b"\x00" * _bytes_ms(_PRIME_MS))
        except _ERRO_PIPE as exc:
            self._falhou_abrir(proc, extra=str(exc))
            return False
        if not self._garantir_reserva():
            self._falhou_abrir(proc, extra="reserva não encheu")
            return False
        if proc.poll() is not None:
            self._falhou_abrir(proc)
            return False
        # a MESMA janela no aquecimento (prime + reserva): o `fechar()` pode ter
        # caído aqui dentro. Sem esta rechecagem o `abrir` terminava em
        # `_ok = True` num objeto já fechado e `ativo` mentia.
        with self._lock:
            if self._fechado:
                self._mata(proc)
                self._proc = None
                self._vivo = False
                return False
            self._ok = True
        return True

    def _escreve(self, proc: subprocess.Popen, pcm: bytes) -> None:
        assert proc.stdin is not None
        proc.stdin.write(pcm)
        proc.stdin.flush()

    def _falhou_abrir(self, proc: subprocess.Popen, extra: str = "") -> None:
        err = bytes(self._err).decode("utf-8", "replace").strip().replace("\n", " ")
        detalhe = (err or extra or f"rc={proc.returncode}")[:180]
        _avisa(f"rnnoise não abriu ({detalhe}) — Live segue sem o filtro")
        self._mata(proc)
        self._proc = None
        self._vivo = False

    def _drenar_ate_estancar(self) -> None:
        """Espera a rajada desta escrita. O que não sair fica no ffmpeg."""
        fim = time.monotonic() + _PRIME_TETO_S
        ultimo = time.monotonic()
        with self._cv:
            visto = len(self._out)
        while time.monotonic() < fim:
            time.sleep(0.005)
            with self._cv:
                agora = len(self._out)
            if agora != visto:
                visto = agora
                ultimo = time.monotonic()
            elif visto and time.monotonic() - ultimo >= _ESTANCA_S:
                break

    def _garantir_reserva(self) -> bool:
        """Deixa `_reserva` bytes de silêncio na fila. False = o filtro não emite."""
        self._drenar_ate_estancar()
        proc = self._proc
        with self._cv:
            tem = len(self._out)
        if tem < self._reserva and proc is not None and proc.poll() is None:
            try:
                self._escreve(proc, b"\x00" * (self._reserva - tem + self._reserva))
            except _ERRO_PIPE:
                return False
            self._drenar_ate_estancar()
        if proc is None or proc.poll() is not None:
            return False
        with self._cv:
            if len(self._out) < _bytes_ms(10):
                return False
            sobra = len(self._out) - self._reserva
            sobra -= sobra % 2
            if sobra > 0:
                del self._out[:sobra]
        return True

    def _ler(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        try:
            while True:
                bloco = proc.stdout.read(4096)
                if not bloco:
                    break
                with self._cv:
                    self._out.extend(bloco)
                    self._cv.notify_all()
        except Exception:  # noqa: BLE001 — o pipe fechou no meio
            pass
        self._vivo = False
        with self._cv:
            self._cv.notify_all()

    def _ler_err(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                bloco = proc.stderr.read(256)
                if not bloco:
                    break
                if len(self._err) < 2000:
                    self._err.extend(bloco)
        except Exception:  # noqa: BLE001
            pass

    def processa(self, pcm: bytes) -> bytes:
        """PCM16 mono 16 kHz entra e sai com o mesmo tamanho. Cru se o filtro caiu."""
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            return pcm
        cru = bytes(pcm)
        if not self._pronto.is_set():
            self._pronto.wait(0.7)
        if not self._ok or len(cru) < 2 or len(cru) % 2:
            return cru
        with self._lock:
            if self._fechado:
                return cru
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                self._caiu()
                return cru
            try:
                self._escreve(proc, cru)
            except _ERRO_PIPE:
                self._caiu()
                return cru
            return self._tira(len(cru))

    def _tira(self, n: int) -> bytes:
        prazo = time.monotonic() + _ESPERA_S
        with self._cv:
            while len(self._out) < n and self._vivo and time.monotonic() < prazo:
                restante = prazo - time.monotonic()
                if restante <= 0:
                    break
                self._cv.wait(timeout=restante)
            if len(self._out) >= n:
                sai = bytes(self._out[:n])
                del self._out[:n]
                return sai
            tenho = len(self._out)
            self.faltas += n - tenho
            if self.faltas == n - tenho:
                _avisa("rnnoise atrasou um frame — completei com silêncio; o Live segue")
            sai = bytes(self._out) + b"\x00" * (n - tenho)
            self._out.clear()
            return sai

    def _caiu(self) -> None:
        if self._ok:
            err = bytes(self._err).decode("utf-8", "replace").strip().replace("\n", " ")
            _avisa(f"rnnoise caiu ({err[:160] or 'sem stderr'}) — Live segue sem o filtro")
        self._ok = False

    def fechar(self) -> None:
        """Idempotente e seguro contra um `abrir()` em andamento (ver `_abrir`)."""
        with self._lock:
            self._fechado = True
            proc = self._proc
            self._proc = None
            self._ok = False
            self._vivo = False
        self._pronto.set()
        if proc is not None:
            self._mata(proc)

    @staticmethod
    def _mata(proc: subprocess.Popen) -> None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=0.25)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                return
        try:
            proc.wait(timeout=0.2)
        except Exception:  # noqa: BLE001
            pass
