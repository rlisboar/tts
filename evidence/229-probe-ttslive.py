"""#229 (família do #207/#228) — REPRO DETERMINÍSTICO da leitura dupla em `_tts_live`.

`_tts_live` faz:
    if self._worker is None:
        self._worker = _LiveWorker(...)   # atribuição
    if self._worker.ativo:                # SEGUNDA leitura do atributo
Um `close()` concorrente (thread do WS) zera `self._worker` entre as duas e a
segunda leitura estoura `AttributeError` em None.

Aqui a corrida é forçada com uma property `_worker` cujo SETTER chama `close()`
logo depois de guardar o valor — é exatamente a ordem "atribuiu → close() →
leu". Sai 0 se houver furo (AttributeError), 1 se não houver.

Uso: ./.venv-mlx/bin/python evidence/229-probe-ttslive.py
"""
import queue
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import app                                        # noqa: E402
import live_pipeline as lp                        # noqa: E402


class WorkerFalso:
    def __init__(self, voice_id=None):
        self._ativo = False

    @property
    def ativo(self):
        return self._ativo

    def fecha(self):
        pass

    def gerar(self, texto, omni):
        return None


lp._LiveWorker = WorkerFalso
lp._worker_habilitado = lambda: True
lp._tts_app = lambda texto, omni, voice_id=None: "in-process"

sess = {"id": "corrida", "voice_id": None, "system": None, "history": [],
        "fila": queue.Queue()}
pipe = app._live_pipe_novo(sess)
cls = type(pipe)


def _get(self):
    return self.__dict__.get("_worker")


def _set(self, v):
    self.__dict__["_worker"] = v
    # close() do outro lado, logo APÓS a atribuição e ANTES da 2ª leitura
    if v is not None and self.__dict__.pop("_arme", False):
        self.close()


cls._worker = property(_get, _set)

pipe.__dict__["_arme"] = True
try:
    pipe._tts_live("teste", {})
    print("  ✔ sem furo: a 2ª leitura achou o worker")
    sys.exit(1)
except AttributeError as exc:
    print(f"  ✘ FURO: AttributeError na 2ª leitura de `_worker` ({exc})")
    print("    fix de 1 linha: usar nome local — `w = self._worker = _LiveWorker(...)`;"
          " `if w.ativo:`")
    sys.exit(0)
