"""Extração do locutor-alvo antes do STT (VOZ-I6, #534).

Por que existe: fala de terceiro SOBREPOSTA não é ruído para denoiser e não some
com seleção por janela — só um modelo de SEPARAÇÃO tira o concorrente de dentro da
janela (`evidence/532-bakeoff-locutor-alvo.md`; é o vencedor do R4). O caminho é:
separa em dois fluxos, EMBEDA os dois com o extrator do perfil e fica com o mais
próximo do centróide do dono — o outro é o que se descarta.

O modelo (MossFormer2_SS_16K, 670 MB, Apache-2.0) roda em PROCESSO FILHO e em MPS
(`separa_worker.py`): se estourar, morre só ele e o chamador segue com o áudio
original. Enquanto o filho roda, o pai segura a trava de Metal (`app._gen_lock`) —
é a mesma arbitragem que a geração local usa.

Sem os pesos (`disponivel()` False), nada muda: o chamador recebe
`separacao="indisponivel"` e transcreve o áudio original. O contrato só aparece
quando o cliente PEDE `speaker_alvo`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
WORKER = RAIZ / "separa_worker.py"
#: pesos (contrato do `models/README.md`); ausente = indisponível, nunca download
MODELO_DIR = Path(os.environ.get("TTS_SEPARA_MODELO_DIR")
                  or RAIZ / "models" / "mossformer2-ss-16k")
CHECKPOINT = "last_best_checkpoint.pt"
VENDIDO = RAIZ / "tools" / "separa_vend"
#: teto por separação — acima disso o worker é dado como morto e o chamador segue
TIMEOUT_S = float(os.environ.get("TTS_SEPARA_TIMEOUT_S", "120"))
#: TTL de ociosidade do filho (mesma política do #507/#612)
TTL_S = float(os.environ.get("TTS_SEPARA_TTL_S", "600"))
INTERVALO_VIGIA_S = 5.0

_trava = threading.Lock()
_proc: subprocess.Popen | None = None
_ultima = 0.0
_vigia_thread: threading.Thread | None = None
_contador = 0


def motivo() -> str:
    """Por que a separação está fora ("" = disponível). Acionável, como no ECAPA."""
    try:
        if not (VENDIDO / "mf2_ss" / "mossformer2.py").is_file():
            return f"código vendorizado ausente em {VENDIDO}"
        if sys.version_info < (3, 10):
            return "python antigo demais para o modelo"
        pesos = MODELO_DIR / CHECKPOINT
        if not pesos.is_file():
            return (f"pesos ausentes em {MODELO_DIR} — rode ./tools/baixa_modelos.sh")
        if pesos.stat().st_size < 100_000_000:
            return f"peso truncado em {MODELO_DIR} — rode ./tools/baixa_modelos.sh --forca"
    except OSError as exc:
        return f"não deu para conferir os pesos: {exc}"
    return ""


def disponivel() -> bool:
    return not motivo()


def _vigia() -> None:
    """Fecha o filho quando o TTL vence (mesma política do #507/#612)."""
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
    """Encerra o filho (best effort: ele também morre com o app)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.stdin.write('{"tipo": "close"}\n')
        proc.stdin.flush()
    except Exception:  # noqa: BLE001 — cai no terminate
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _pega() -> subprocess.Popen:
    """Sobe o filho (uma vez) e devolve o processo; ociosidade fecha sozinha."""
    global _proc, _vigia_thread
    with _trava:
        if _proc is not None and _proc.poll() is None:
            return _proc
        _proc = subprocess.Popen(
            [sys.executable, str(WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            cwd=str(RAIZ))
        if _vigia_thread is None or not _vigia_thread.is_alive():
            _vigia_thread = threading.Thread(target=_vigia, name="separa-ttl", daemon=True)
            _vigia_thread.start()
        return _proc


def fecha() -> None:
    """Fecha o filho agora (shutdown do app)."""
    global _proc
    with _trava:
        proc, _proc = _proc, None
    _fecha(proc)


def separa(wav: Path, dir_saida: Path) -> list[Path]:
    """Dois fluxos (16 kHz mono) do áudio em `wav`. Levanta em erro/timeout."""
    global _ultima, _contador
    if not disponivel():
        raise RuntimeError(motivo() or "separador indisponível")
    proc = _pega()
    _contador += 1
    pedido = json.dumps({"id": _contador, "wav": str(wav), "dir": str(dir_saida)})
    with _trava:                        # um pedido por vez (o protocolo é serial)
        _ultima = time.monotonic()
        try:
            proc.stdin.write(pedido + "\n")
            proc.stdin.flush()
            linha = _le_ate_resposta(proc, _contador)
        finally:
            _ultima = time.monotonic()
    dados = json.loads(linha)
    if dados.get("erro"):
        raise RuntimeError(dados["erro"])
    return [Path(p) for p in dados["fluxos"]]


def _le_ate_resposta(proc: subprocess.Popen, ident: int) -> str:
    """Lê até a resposta de `ident` (ignora linhas de aquecimento/outras)."""
    limite = time.monotonic() + TIMEOUT_S
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"worker da separação morreu (rc={proc.returncode})")
        if time.monotonic() > limite:
            raise RuntimeError(f"separação passou de {TIMEOUT_S:.0f}s")
        linha = proc.stdout.readline()
        if not linha:
            continue
        try:
            dados = json.loads(linha)
        except Exception:  # noqa: BLE001 — lixo no stdout não é resposta
            continue
        if dados.get("id") == ident:
            return linha


def separa_alvo(wav: Path, vecs: list) -> tuple[Path, dict]:
    """Fluxo do ALVO e o que foi descartado.

    `vecs` são as amostras do perfil; o escolhido é o fluxo de maior cosseno contra
    o centróide delas (o R2 mostrou o centróide vencendo o máximo). Devolve o caminho
    do fluxo escolhido e `{"trechos": 1, "ms": <duração do descartado>}`, além dos
    escores para o log/observabilidade."""
    import app                       # só para reusar o extrator já carregado

    with tempfile.TemporaryDirectory(prefix="separa-") as pastinha:
        dir_saida = Path(pastinha)
        fluxos = separa(wav, dir_saida)
        centroide = app._speaker_centroide(vecs)
        escores = []
        for f in fluxos:
            emb = app._speaker_embed(f, app._speaker_modelo_efetivo())
            escores.append(app._cos_sim(emb, centroide) if emb else -1.0)
        escolhido = max(range(len(fluxos)), key=lambda i: escores[i])
        alvo = dir_saida / f"alvo{escolhido}.wav"
        fluxos[escolhido].replace(alvo)
        descartado = [f for i, f in enumerate(fluxos) if i != escolhido]
        info = {"escores": [round(e, 3) for e in escores], "escolhido": escolhido}
        if descartado:
            info["descartados"] = {"trechos": 1, "ms": _duracao_ms(descartado[0])}
        # o temporário morre com o `with`: devolve um arquivo que sobrevive
        final = Path(tempfile.mkstemp(prefix="alvo-", suffix=".wav")[1])
        alvo.replace(final)
        return final, info


def _duracao_ms(wav: Path) -> int:
    try:
        import soundfile as sf

        info = sf.info(str(wav))
        return int(info.frames / max(1, info.samplerate) * 1000)
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["disponivel", "motivo", "separa", "separa_alvo", "fecha", "MODELO_DIR"]
