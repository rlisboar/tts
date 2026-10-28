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
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
WORKER = BASE / "intencao_worker.py"

#: prazo padrão de UMA decisão. É o teto que o turno paga no pior caso.
TIMEOUT_MS = int(os.environ.get("TTS_INTENCAO_TIMEOUT_MS", "400"))

#: abaixo disto a probabilidade é "não sei" e o chamador fica com a regra
LIMIAR = float(os.environ.get("TTS_INTENCAO_LIMIAR", "0.5"))

#: COOLDOWN entre tentativas depois de falha (#595): sem isto, um aquecimento que
#: estourou o prazo fazia o próximo turno tentar de novo imediatamente (e outro
#: modelo de 2 GB subindo a cada 400 ms de turno). Dobra até o teto; zera no
#: primeiro `pronto`.
FALHA_ESPERA_S = float(os.environ.get("TTS_INTENCAO_ESPERA_S", "5"))
FALHA_ESPERA_MAX_S = 120.0


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
        self._falhas = 0
        self._tenta_em = 0.0              # monotonic: antes disso não sobe de novo

    # -- ciclo de vida ----------------------------------------------------
    def aquece(self, espera_s: float = 30.0, forcar: bool = False) -> bool:
        """Sobe o filho e espera o `pronto`. False = sem modelo; `decide` dá None.

        RE-ARMA (#595): `_morto` é marca de INSTANTE, não sentença. Filho vivo
        devolve True; filho morto/ausente sobe de novo — mas só depois do
        cooldown (`_tenta_em`), senão cada turno paga um `Popen` que falha.
        `forcar=True` ignora o cooldown (o dono ligando o filtro na tela)."""
        with self._lock:
            if self._proc is not None:
                if self._proc.poll() is None:
                    return True
                self._proc = None             # cadáver: limpa e cai no re-arme
            if not forcar and time.monotonic() < self._tenta_em:
                return False
            self._morto = False
            while not self._fila.empty():     # sobras do filho anterior
                self._fila.get_nowait()
            try:
                self._proc = subprocess.Popen(
                    [self._py, str(WORKER)], cwd=str(BASE), text=True,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=1, start_new_session=True)
            except Exception as exc:      # noqa: BLE001 — OSError e o que mais vier
                self._falhou(f"não subiu ({type(exc).__name__}: {exc})")
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
        self._falhas, self._tenta_em, self._morto = 0, 0.0, False
        self.erro = ""
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
        """O filho está VIVO agora (`poll`), não o que a última falha disse.

        Antes era `_proc is not None and not _morto` e `_morto` nunca limpava: um
        aquecimento estourado deixava `ativo` False e `decide` None para SEMPRE
        na sessão (#595)."""
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def morto(self) -> bool:
        """Última tentativa falhou (para o `stats`; não impede novo `aquece`)."""
        return self._morto

    @property
    def tenta_em_s(self) -> float:
        """Segundos até a próxima tentativa (0 = livre)."""
        return max(0.0, round(self._tenta_em - time.monotonic(), 1))

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
        # espera a PRÓPRIA resposta: com dois chamadores na mesma fila, quem
        # colhe um `id` alheio devolve ao início e continua esperando até o prazo
        limite = time.monotonic() + self._timeout
        r = None
        while True:
            restante = limite - time.monotonic()
            if restante <= 0:
                return None                   # prazo estourado: o chamador decide
            try:
                item = self._fila.get(timeout=restante)
            except queue.Empty:
                return None
            if item.get("id") == n:
                r = item
                break
            self._fila.put(item)              # resposta de outro chamador: devolve
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
        # só o DONO do `_proc` pode derrubar: o leitor do filho anterior termina
        # depois do re-arme e, sem esta guarda, matava a referência do novo (#595)
        if self._proc is proc:
            self._caiu("EOF no protocolo")

    def _falhou(self, motivo: str) -> None:
        """Falha com COOLDOWN exponencial (5 s, 10 s, 20 s … teto 120 s)."""
        self._falhas += 1
        self._tenta_em = time.monotonic() + min(
            FALHA_ESPERA_MAX_S, FALHA_ESPERA_S * (2 ** (self._falhas - 1)))
        self.erro = motivo

    def _caiu(self, motivo: str) -> None:
        """O filho morreu/calou: derruba o cadáver e arma o cooldown."""
        proc, self._proc = self._proc, None
        self._falhou(motivo)
        self._morto = True
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:       # noqa: BLE001 — já morreu
                pass


# ---------------------------------------------------------------------------
# Worker ÚNICO por processo do app, compartilhado pelas sessões Live (#507)
#
# O modelo custa ~2 GB de RSS: um por sessão estouraria a máquina com 2–3 sessões.
# Então: um só, com contagem de sessões que usam o filtro, e fechamento por TTL sem
# consulta. `pega()`/`solta()` são o contrato do chamador; ele NUNCA instancia
# `Intencao` direto no caminho da sessão.
# ---------------------------------------------------------------------------

#: TTL sem consulta nenhuma (env) — o dono pode baixar para devolver RAM
TTL_S = float(os.environ.get("TTS_INTENCAO_TTL_S", "600"))
#: cadência do vigia (o custo é um `monotonic` a cada N segundos)
INTERVALO_VIGIA_S = 5.0

_unico: Intencao | None = None
_refs = 0
_ultima = 0.0
_trava = threading.Lock()
_janitor: threading.Thread | None = None
_aquecendo = False


def _rss_mb(pid: int) -> float:
    """RSS do filho em MB (`ps`; portátil no macOS e no Linux)."""
    try:
        r = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                           capture_output=True, text=True)
        return round(int((r.stdout or "0").strip() or 0) / 1024.0, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _vigia() -> None:
    """Fecha o worker quando ninguém usa ou o TTL venceu (checa a cada 5 s)."""
    global _unico, _refs
    while True:
        time.sleep(INTERVALO_VIGIA_S)
        with _trava:
            inc = _unico
            if inc is None:
                return
            if _aquecendo:            # a carga leva segundos e não é ociosidade
                continue
            vencido = _refs <= 0 or (time.monotonic() - _ultima) > TTL_S
            if vencido:
                _unico = None
                _refs = 0
        if vencido:
            inc.fecha()


def pega() -> Intencao:
    """Sessão passa a usar o classificador (sobe o worker se preciso)."""
    global _unico, _refs, _ultima, _janitor  # noqa: PLW0603
    with _trava:
        _refs += 1
        _ultima = time.monotonic()
        if _unico is None:
            _unico = Intencao()
            # vigia novo a cada worker novo: o anterior pode estar saindo e um
            # `is_alive()` otimista deixaria o worker sem quem o feche
            _janitor = threading.Thread(target=_vigia, name="intencao-ttl", daemon=True)
            _janitor.start()
        inc = _unico
    if not inc.ativo:
        global _aquecendo
        _aquecendo = True
        try:
            # segunda tentativa: com outra sessão fechando, o 1º aquecimento pode
            # bater em memória/Metal e voltar False sem o modelo estar quebrado
            if not inc.aquece():
                inc._morto = False
                inc.aquece()
        finally:
            with _trava:
                _aquecendo = False
                _ultima = time.monotonic()   # o aquecimento conta como uso
    return inc


def solta() -> None:
    """A sessão deixou de usar o classificador (fecha quando a última sair)."""
    global _refs
    with _trava:
        _refs = max(0, _refs - 1)


def _rearma(inc: "Intencao | None") -> None:
    """Tenta ressuscitar o worker EM SEGUNDO PLANO (o turno não espera 3 s).

    Só depois do cooldown do próprio `aquece`, e uma tentativa por vez."""
    global _aquecendo
    if inc is None or inc.ativo:
        return
    with _trava:
        if _aquecendo:
            return
        _aquecendo = True

    def _roda():
        global _aquecendo, _ultima
        try:
            inc.aquece()
        finally:
            with _trava:
                _aquecendo = False
                _ultima = time.monotonic()
    threading.Thread(target=_roda, name="intencao-rearma", daemon=True).start()


def decide_compartilhado(ref: str, fala: str, idioma: str = "") -> dict | None:
    """`decide` no worker único, atualizando o relógio do TTL.

    Morto e fora do cooldown, o re-arme sai em thread: a próxima fala já pode ser
    decidida pelo modelo em vez de esperar o worker subir (#595)."""
    global _ultima
    with _trava:
        inc = _unico
        _ultima = time.monotonic()
    if inc is None:
        return None
    if not inc.ativo:
        _rearma(inc)
        return None
    return inc.decide(ref, fala, idioma)


def estado() -> dict:
    """Para o `stats`: worker vivo/morto e o custo em memória."""
    with _trava:
        inc = _unico
        refs = _refs
        ocioso = round(time.monotonic() - _ultima, 1) if _ultima else None
    vivo = bool(inc and inc.ativo)
    return {"vivo": vivo, "refs": refs, "ttl_s": TTL_S, "ocioso_s": ocioso,
            "rss_mb": _rss_mb(inc._proc.pid) if vivo else 0.0,
            # #595: por que está fora e quando volta a tentar (diagnóstico)
            "erro": (inc.erro if inc is not None else ""),
            "tenta_em_s": (inc.tenta_em_s if inc is not None else 0.0)}


def fecha_tudo() -> None:
    """Encerra o worker único (shutdown do app)."""
    global _unico, _refs, _janitor
    with _trava:
        inc, _unico, _refs, _janitor = _unico, None, 0, None
    if inc is not None:
        inc.fecha()


__all__ = ["Intencao", "LIMIAR", "TIMEOUT_MS", "TTL_S",
           "decide_compartilhado", "estado", "fecha_tudo", "pega", "solta"]
