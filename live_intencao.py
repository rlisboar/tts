"""Classificador de intenção de interrupção do Live (#507) — lado do app.

Decide se a fala do usuário por cima do assistente quer CORTAR ou CONTINUAR. É a
peça de MODELO; a ordem dela dentro da cadeia de filtros é do speech-pipeline.

Por que um modelo aqui: a cadeia de regras acerta o óbvio ("para", "espera",
pergunta com `?`) mas não separa o ambíguo — "entendi, obrigado" e "mas e o
preço?" têm a mesma forma. O modelo lê o conteúdo.

Três invariantes que valem como contrato:

- **Processo próprio** (`intencao_worker.py`), como o `tts_worker`: crash do Metal
  mata o filho, não a sessão Live.
- **Prazo curto e duro** (`timeout_ms`, 400 por padrão): estourou, deu erro, o
  filho não subiu ou não está carregado → `None`, e o chamador cai no
  `sem_decisao`. Nunca segura o turno.
- **Sem egress**: o modelo está no cache local do HF; nada sai da máquina.

Um processo por sessão, com `aquece()` no começo da sessão (quando o filtro está
ligado) e `fecha()` na saída. `None` é resposta legítima — não é exceção.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
WORKER = BASE / "intencao_worker.py"

#: prazo padrão de UMA decisão. É o teto que o turno paga no pior caso.
TIMEOUT_MS = int(os.environ.get("TTS_INTENCAO_TIMEOUT_MS", "400"))

#: abaixo disto a probabilidade é "não sei" e o chamador fica com a regra
LIMIAR = float(os.environ.get("TTS_INTENCAO_LIMIAR", "0.5"))


class Intencao:
    """Ponte com o filho. Idempotente no `fecha`; `decide` nunca levanta."""

    def __init__(self, python: str | None = None, timeout_ms: int | None = None):
        self._py = python or sys.executable
        self._timeout = (timeout_ms or TIMEOUT_MS) / 1000.0
        self._proc: subprocess.Popen | None = None
        self._fila: queue.Queue = queue.Queue()
        self._n = 0
        self._lock = threading.Lock()
        self._morto = False
        self.erro = ""

    # -- ciclo de vida ----------------------------------------------------
    def aquece(self, espera_s: float = 30.0) -> bool:
        """Sobe o filho e espera o `pronto`. False = sem modelo; `decide` dá None."""
        with self._lock:
            if self._proc is not None or self._morto:
                return self._proc is not None
            try:
                self._proc = subprocess.Popen(
                    [self._py, str(WORKER)], cwd=str(BASE), text=True,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=1, start_new_session=True)
            except OSError as exc:
                self._morto, self.erro = True, f"não subiu ({exc})"
                return False
            threading.Thread(target=self._ler, args=(self._proc,), daemon=True).start()
        try:
            aviso = self._fila.get(timeout=espera_s)
        except queue.Empty:
            self._caiu("timeout no aquecimento")
            return False
        if aviso.get("tipo") != "pronto":
            self._caiu("protocolo inesperado no aquecimento")
            return False
        return True

    def fecha(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write('{"tipo": "close"}\n')
                    proc.stdin.flush()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._morto = True

    @property
    def ativo(self) -> bool:
        return self._proc is not None and not self._morto

    # -- decisão ----------------------------------------------------------
    def decide(self, ref_assistente: str, fala_usuario: str, idioma: str = "") -> dict | None:
        """`{"decisao", "p_cortar", "ms"}` ou None (sem modelo, erro, prazo)."""
        if not self.ativo or not (fala_usuario or "").strip():
            return None
        with self._lock:
            self._n += 1
            n = self._n
            try:
                self._proc.stdin.write(json.dumps(
                    {"id": n, "ref": ref_assistente or "", "fala": fala_usuario,
                     "idioma": idioma or ""}) + "\n")
                self._proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                self._caiu(f"escrita falhou ({type(exc).__name__})")
                return None
        try:
            r = self._fila.get(timeout=self._timeout)
        except queue.Empty:
            return None                       # prazo estourado: o chamador decide
        if r.get("id") != n:
            return None                       # resposta fora de ordem: descarta
        if r.get("erro"):
            self.erro = str(r["erro"])
            return None
        p = float(r.get("p_cortar") or 0.0)
        return {"decisao": "cortar" if p >= LIMIAR else "continuar",
                "p_cortar": round(p, 4), "ms": int(r.get("ms") or 0)}

    # -- interno ----------------------------------------------------------
    def _ler(self, proc: subprocess.Popen) -> None:
        for linha in proc.stdout:
            linha = linha.strip()
            if not linha:
                continue
            try:
                self._fila.put(json.loads(linha))
            except Exception:  # noqa: BLE001
                continue
        self._caiu("EOF no protocolo")

    def _caiu(self, motivo: str) -> None:
        self._morto = True
        self.erro = motivo


__all__ = ["Intencao", "LIMIAR", "TIMEOUT_MS"]
