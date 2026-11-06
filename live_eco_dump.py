"""Dump opt-in do par mic/referência para a NCC do eco real (#870).

O dump do turno (#869, `live_pipeline._stt_dump`) grava só o PCM que o STT
recebeu. Isso é a fala do usuário, sem o que o servidor mandou tocar — não
dá para medir o eco da sala. Este módulo, no MESMO `TTS_LIVE_DUMP_DIR` e
também desligado por padrão, grava durante a sessão:

    eco-YYYYMMDD-HHMMSS-xxxxx/
      mic.wav       PCM16 16 kHz, cru, na ordem de chegada
      ref.f32       float32, o array exato que `anota_referencia` guardou
      relogio.jsonl uma linha por chunk com playback e referência
      meta.json

Teto de 15 min por fluxo. Passou disso, para de acrescentar E de registrar
o relógio: um playhead além do arquivo pareceria um eco que não casa.
Não cria pasta se nada foi escrito. Nunca levanta — falha de disco marca
o estado como quebrado e as chamadas seguintes não escrevem, para o índice
não ficar com um buraco silencioso.

O env é lido em cada chamada (não no import): o teste liga e desliga.
Quem chama a partir do `app` volta antes do import quando o env está vazio.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import wave
from pathlib import Path

import numpy as np

_SR = 16000                       # live_turns.SAMPLE_RATE; o cru do Live é 16 kHz
_CAP = 15 * 60 * _SR              # amostras; mic e referência, cada um
_NOTA = (
    "mic.wav é o cru na ordem de chegada (16 kHz). ref.f32 é o float32 que "
    "o motor anotou, não o PCM de 24 kHz do fio. origem no relógio é a "
    "origem do ring do motor: a ferramenta não busca antes dela. Teto de "
    "15 min por fluxo; depois o arquivo para e o relógio também."
)


def _dir() -> str:
    return (os.environ.get("TTS_LIVE_DUMP_DIR") or "").strip()


# Só a CRIAÇÃO da pasta é global. O miolo (mic no loop, referência no sender)
# usa o lock do próprio dump, e nunca segura o lock do motor ao mesmo tempo.
_CRIA = threading.Lock()


class _Dump:
    def __init__(self, pasta: Path, sessao: str) -> None:
        self.pasta = pasta
        self.sessao = sessao
        self.lock = threading.Lock()
        self.mic_n = 0
        self.ref_n = 0
        self.linhas = 0
        self.quebrado = False
        self.truncado = False
        self.fechado = False
        self._pasta_ok = False
        self._mic = None
        self._ref = None
        self._rel = None
        self._avisou = False

    def _falha(self, exc: BaseException) -> None:
        self.quebrado = True
        if not self._avisou:
            self._avisou = True
            print(f"[live] dump do eco falhou: {exc}", flush=True)

    def _abre_pasta(self) -> None:
        if self._pasta_ok:
            return
        self.pasta.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.pasta.mkdir(exist_ok=False)
        except FileExistsError:
            self.pasta = self.pasta.with_name(
                self.pasta.name + "-" + secrets.token_hex(2))
            self.pasta.mkdir(exist_ok=False)
        self._pasta_ok = True

    def _cabe(self, ja: int, quero: int) -> int:
        if self.truncado or self.quebrado or self.fechado or quero <= 0:
            return 0
        resto = _CAP - ja
        if resto <= 0:
            self.truncado = True
            return 0
        return quero if quero <= resto else resto

    def escreve_mic(self, pcm: bytes) -> None:
        with self.lock:
            if self.fechado or self.quebrado or self.truncado:
                return
            n_quero = len(pcm) // 2
            n_cabe = self._cabe(self.mic_n, n_quero)
            if n_cabe <= 0:
                return
            parcial = n_cabe < n_quero
            try:
                self._abre_pasta()
                if self._mic is None:
                    w = wave.open(str(self.pasta / "mic.wav"), "wb")
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(_SR)
                    self._mic = w
                self._mic.writeframes(pcm[:n_cabe * 2])
                self.mic_n += n_cabe
                if parcial:
                    self.truncado = True
            except Exception as exc:  # noqa: BLE001 — diagnóstico não derruba a sessão
                self._falha(exc)

    def escreve_ref(self, x: np.ndarray) -> None:
        with self.lock:
            if self.fechado or self.quebrado or self.truncado:
                return
            n_cabe = self._cabe(self.ref_n, int(x.shape[0]))
            if n_cabe <= 0:
                return
            parcial = n_cabe < int(x.shape[0])
            try:
                self._abre_pasta()
                if self._ref is None:
                    self._ref = open(self.pasta / "ref.f32", "wb")
                bloco = np.ascontiguousarray(x[:n_cabe], dtype="<f4")
                bloco.tofile(self._ref)
                self._ref.flush()
                self.ref_n += n_cabe
                if parcial:
                    self.truncado = True
            except Exception as exc:  # noqa: BLE001
                self._falha(exc)

    def escreve_linha(self, linha: dict) -> None:
        with self.lock:
            # truncado: o playhead passaria do arquivo e a NCC cairia à toa
            if self.fechado or self.quebrado or self.truncado or not self._pasta_ok:
                return
            try:
                if self._rel is None:
                    self._rel = open(self.pasta / "relogio.jsonl", "a", encoding="utf-8")
                self._rel.write(json.dumps(linha, ensure_ascii=False,
                                           separators=(",", ":")) + "\n")
                self._rel.flush()
                self.linhas += 1
            except Exception as exc:  # noqa: BLE001
                self._falha(exc)

    def fecha(self) -> None:
        with self.lock:
            if self.fechado:
                return
            self.fechado = True
            for handle in (self._mic, self._ref, self._rel):
                if handle is None:
                    continue
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
            self._mic = self._ref = self._rel = None
            if not self._pasta_ok:
                return
            if self.mic_n or self.ref_n or self.linhas or self.quebrado:
                try:
                    meta = {
                        "sr": _SR,
                        "sessao": self.sessao,
                        "mic": "mic.wav",
                        "ref": "ref.f32",
                        "relogio": "relogio.jsonl",
                        "mic_amostras": self.mic_n,
                        "ref_amostras": self.ref_n,
                        "linhas": self.linhas,
                        "truncado": self.truncado,
                        "quebrado": self.quebrado,
                        "nota": _NOTA,
                    }
                    (self.pasta / "meta.json").write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    self._falha(exc)
                return
            try:
                self.pasta.rmdir()
            except OSError:
                pass


def _novo(sess: dict) -> _Dump | None:
    base = _dir()
    if not base:
        return None
    with _CRIA:
        est = sess.get("_eco_dump")
        if est is not None:
            return None if est.quebrado or est.fechado else est
        pasta = Path(base) / (
            time.strftime("eco-%Y%m%d-%H%M%S-") + f"{secrets.randbelow(100000):05d}")
        est = _Dump(pasta, str(sess.get("id") or ""))
        sess["_eco_dump"] = est
        return est


def anota_mic(sess: dict, pcm: bytes) -> None:
    """Acrescenta o cru. No-op sem env, sem bytes, ou depois de uma falha."""
    if not pcm or not _dir():
        return
    try:
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if len(pcm) < 2:
            return
        est = _novo(sess)
        if est is None:
            return
        est.escreve_mic(bytes(pcm))
    except Exception as exc:  # noqa: BLE001
        est = sess.get("_eco_dump")
        if est is not None:
            est._falha(exc)
        else:
            print(f"[live] dump do eco falhou: {exc}", flush=True)


def anota_ref(sess: dict, amostras: np.ndarray) -> None:
    """Acrescenta o float que o motor acabou de anotar. Não reamostra."""
    if not _dir() or amostras is None:
        return
    try:
        x = np.ascontiguousarray(amostras, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        est = _novo(sess)
        if est is None:
            return
        est.escreve_ref(x.copy())
    except Exception as exc:  # noqa: BLE001
        est = sess.get("_eco_dump")
        if est is not None:
            est._falha(exc)
        else:
            print(f"[live] dump do eco falhou: {exc}", flush=True)


def marca(sess: dict, eng) -> None:
    """Uma linha de relógio. O lock do motor solta antes do lock do dump.

    Só com playback ativo e referência no ring — é a janela em que o motor
    correlaciona. Sem pasta (nada gravado ainda) não cria uma só por causa
    do relógio.
    """
    if eng is None or not _dir():
        return
    est = sess.get("_eco_dump")
    if est is None or est.fechado or est.quebrado or est.truncado:
        return
    try:
        with eng._lock:
            if not eng._playback_ativo():
                return
            origem = int(eng._ref_origem)
            ref_n = int(eng._ref_n)
            if ref_n <= origem:
                return
            linha = {
                "mic": int(eng._proxima_amostra),
                "play": int(eng._ref_play),
                "origem": origem,
                "ref_n": ref_n,
                "pausa": bool(eng._ref_pausado),
            }
    except Exception:  # noqa: BLE001 — sem relógio a ferramenta avisa e segue
        return
    try:
        est.escreve_linha(linha)
    except Exception as exc:  # noqa: BLE001
        est._falha(exc)


def fecha(sess: dict) -> None:
    """Fecha os arquivos e escreve `meta.json`. Idempotente."""
    est = sess.get("_eco_dump") if sess is not None else None
    if est is None:
        return
    try:
        est.fecha()
    except Exception as exc:  # noqa: BLE001
        print(f"[live] dump do eco falhou: {exc}", flush=True)
