"""Marca d'água AudioSeal no áudio GERADO e detector para o gate (#657).

O app clona voz: sem marca, uma saída dele é indistinguível de fala humana para
qualquer verificador. O AudioSeal (MIT, Meta) embute uma marca imperceptível e o
detector a lê de volta — medido no R3 (#522): 100 % de detecção no áudio marcado,
0 % no cru, e sobrevive a codec, reamostragem e ganho (o limite honesto é corte
grande: 83 % com 60 % do trecho cortado).

Roda em PROCESSO FILHO, em CPU (`marca_worker.py`): torch dentro do app brigaria
com o Metal do MLX e pagaria setup no caminho do primeiro áudio. Sem os pesos, o
áudio sai sem marca e o chamador recebe o aviso — nada quebra.

O detector é a outra ponta: `detecta_prob()` serve o gate de locutor (anti-clone)
e o filtro de eco do Live. O limiar é do CHAMADOR (0,5 é o que o R3 usou).

DOIS LIMITES DECLARADOS (#845) — não são furo, são desenho:

1. AMOSTRA DE VOICE DESIGN não é marcada. Ela é REFERÊNCIA: condiciona o clone e o
   modelo copia o timbre dela, então marcar a referência enfia a marca na linhagem
   da voz sem melhorar detecção nenhuma (a saída já sai marcada). O gancho está no
   `sf.write` de `save_design_voice` (app.py).
2. O SERVIDOR REMOTO (`remote/omni_server.py`) não marca: quem marca é o CLIENTE,
   no app, ao gravar (`_marca_arquivo` no trecho; `_marca_saida_isolada` na saída
   do filho isolado, depois que ele sai; `_marca_pcm` no Live). Consumidor DIRETO
   do servidor recebe áudio cru — é contrato do cliente, e o aviso está lá.

Nenhum dos dois vale para a saída normal do app, que sai marcada em todos os
caminhos: job local, família isolada, retorno remoto e Live. A chave
`live_marca_primeiro_trecho` (default True; dono, 2026-10-28: marca em toda
saída) mantém o AudioSeal também no 1º PCM do Live. False é o atalho do #901:
só esse PCM sai sem marca; do 2º chunk em diante a marca volta.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
WORKER = RAIZ / "marca_worker.py"
#: pesos (contrato do `models/README.md`); ausente = marca indisponível
MODELO_DIR = Path(os.environ.get("TTS_MARCA_MODELO_DIR") or RAIZ / "models" / "audioseal")
GERADOR = "gerador.pt"
DETECTOR = "detector.pt"
#: teto por operação — áudio longo não pode pendurar o pedido
TIMEOUT_S = float(os.environ.get("TTS_MARCA_TIMEOUT_S", "60"))
#: TTL de ociosidade do filho (mesma política do #507/#612)
TTL_S = float(os.environ.get("TTS_MARCA_TTL_S", "600"))
INTERVALO_VIGIA_S = 5.0

_trava = threading.Lock()
_proc: subprocess.Popen | None = None
_ultima = 0.0
_vigia_thread: threading.Thread | None = None
_contador = 0


def motivo() -> str:
    """Por que a marca está fora ("" = disponível). Acionável."""
    try:
        if not GERADOR and not DETECTOR:      # pragma: no cover — legível abaixo
            return "pesos ausentes"
        faltam = [a for a in (GERADOR, DETECTOR) if not (MODELO_DIR / a).is_file()]
        if faltam:
            return (f"pesos ausentes em {MODELO_DIR} (falta {', '.join(faltam)}) "
                    f"— rode ./tools/baixa_modelos.sh")
        import importlib.util

        if importlib.util.find_spec("audioseal") is None:
            return "audioseal não instalado no venv"
    except Exception as exc:  # noqa: BLE001
        return f"não deu para conferir a marca: {exc}"
    return ""


def disponivel() -> bool:
    return not motivo()


def _vigia() -> None:
    global _proc
    while True:
        time.sleep(INTERVALO_VIGIA_S)
        with _trava:
            proc = _proc
            if proc is None:
                return
            vencido = (time.monotonic() - _ultima) > TTL_S
            if vencido:
                _proc = None
        if vencido:
            _fecha(proc)


def _fecha(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.stdin.write('{"tipo": "close"}\n')
        proc.stdin.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _pega() -> subprocess.Popen:
    global _proc, _vigia_thread
    with _trava:
        if _proc is not None and _proc.poll() is None:
            return _proc
        _proc = subprocess.Popen([sys.executable, str(WORKER)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True, cwd=str(RAIZ))
        if _vigia_thread is None or not _vigia_thread.is_alive():
            _vigia_thread = threading.Thread(target=_vigia, name="marca-ttl", daemon=True)
            _vigia_thread.start()
        return _proc


def prewarm() -> None:
    """Sobe o filho em segundo plano (o load custa ~2,6 s e o 1º áudio não paga).

    Chamado na abertura da sessão do Live: o `first_audio_ms` não pode incluir a
    carga do modelo."""
    if not disponivel():
        return
    threading.Thread(target=lambda: _pega(), name="marca-prewarm", daemon=True).start()


def fecha() -> None:
    """Fecha o filho agora (shutdown do app)."""
    global _proc
    with _trava:
        proc, _proc = _proc, None
    _fecha(proc)


def _pede(pedido: dict, prazo_s: float | None = None) -> dict:
    """Manda um pedido e devolve a resposta dele (protocolo serial sob trava).

    `prazo_s` None usa `TIMEOUT_S` (o job, 60 s). O Live passa o prazo curto.
    """
    global _ultima, _contador
    prazo = TIMEOUT_S if prazo_s is None else float(prazo_s)
    if not disponivel():
        raise RuntimeError(motivo() or "marca indisponível")
    proc = _pega()
    _contador += 1
    pedido["id"] = _contador
    with _trava:
        _ultima = time.monotonic()
        try:
            proc.stdin.write(json.dumps(pedido) + "\n")
            proc.stdin.flush()
            linha = _le_ate(proc, _contador, prazo)
        finally:
            _ultima = time.monotonic()
    dados = json.loads(linha)
    if dados.get("erro"):
        raise RuntimeError(dados["erro"])
    return dados


def _fd_leitura(proc: subprocess.Popen) -> int:
    saida = proc.stdout
    if saida is None or saida.closed:
        raise RuntimeError("worker da marca sem stdout")
    fd = saida.fileno()
    # readline no TextIOWrapper não aceita prazo. O fd solto + select acorda
    # no teto; os bytes não passam pelo buffer do wrapper (ele não é lido).
    os.set_blocking(fd, False)
    return fd


def _casa_id(buf: bytearray, ident: int) -> str | None:
    while True:
        nl = buf.find(b"\n")
        if nl < 0:
            return None
        linha = bytes(buf[:nl]).decode("utf-8", "replace").strip()
        del buf[: nl + 1]
        if not linha:
            continue
        try:
            dados = json.loads(linha)
        except Exception:  # noqa: BLE001
            continue
        if dados.get("id") == ident:
            return linha


def _mata_filho(proc: subprocess.Popen) -> None:
    """Mata e esquece. Já corre sob `_trava` — não pega a trava de novo.

    Não sobe outro aqui: o load do AudioSeal (~2,6 s) seguraria o `_gen_lock`
    de novo. O próximo `_pega` é quem re-sobe.
    """
    global _proc
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
        for stream in (proc.stdin, proc.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if _proc is proc:
            _proc = None


def _filho_mudo(proc: subprocess.Popen, prazo: float) -> None:
    pid = getattr(proc, "pid", None)
    print(
        f"[marca] filho mudo (pid={pid}) não respondeu em {prazo:g}s; "
        "matei e o áudio sai sem marca",
        flush=True,
    )
    _mata_filho(proc)
    raise RuntimeError(f"marca passou de {prazo:g}s (filho mudo)")


def _le_ate(proc: subprocess.Popen, ident: int, prazo: float | None = None) -> str:
    """Linha JSON deste id. Cada espera no fd respeita o que falta do prazo.

    O `readline` bloqueava sem prazo. No job local essa leitura corre dentro
    do `_gen_lock`: filho mudo travava o servidor. No estouro o filho morre,
    `_proc` zera e a exceção sobe — o chamador entrega o áudio sem marca.
    `prazo` None é o teto do job (`TIMEOUT_S`).
    """
    if prazo is None:
        prazo = TIMEOUT_S
    limite = time.monotonic() + prazo
    fd = _fd_leitura(proc)
    buf = bytearray()
    while True:
        achada = _casa_id(buf, ident)
        if achada is not None:
            return achada
        restante = limite - time.monotonic()
        if restante <= 0:
            _filho_mudo(proc, prazo)
        # Filho já morto: drena o que houver no pipe e não espera o teto.
        espera = 0.0 if proc.poll() is not None else restante
        try:
            pronto, _, _ = select.select([fd], [], [], espera)
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                f"worker da marca morreu (rc={proc.returncode})") from exc
        if not pronto:
            if proc.poll() is not None:
                raise RuntimeError(f"worker da marca morreu (rc={proc.returncode})")
            _filho_mudo(proc, prazo)
        try:
            pedaco = os.read(fd, 65536)
        except BlockingIOError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"worker da marca morreu (rc={proc.returncode})") from exc
        if not pedaco:
            raise RuntimeError(f"worker da marca morreu (rc={proc.poll()})")
        buf.extend(pedaco)


def marca_wav(entrada: Path, prazo_s: float | None = None) -> Path:
    """Marca o wav NO LUGAR. Devolve o caminho.

    `prazo_s` None é o teto do job (60 s). O Live passa um prazo curto para
    o turno não ficar mudo se o filho não responder.
    """
    saida = Path(tempfile.mkstemp(prefix="marca-", suffix=".wav")[1])
    try:
        _pede({"op": "marca", "wav": str(entrada), "saida": str(saida)},
              prazo_s=prazo_s)
        saida.replace(entrada)
    finally:
        saida.unlink(missing_ok=True)
    return entrada


def detecta_prob(entrada: Path) -> float | None:
    """Probabilidade da marca (None = marca indisponível/falhou)."""
    try:
        return float(_pede({"op": "detecta", "wav": str(entrada)})["prob"])
    except Exception as exc:  # noqa: BLE001 — sem detector, o chamador decide
        print(f"[marca] detecção falhou: {exc}", flush=True)
        return None


__all__ = ["disponivel", "motivo", "marca_wav", "detecta_prob", "prewarm",
           "fecha", "MODELO_DIR"]
