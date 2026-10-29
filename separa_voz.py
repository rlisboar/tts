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


#: janela de decisão. Separar um bloco que já é de UM falante atrapalha (R4:
#: SI-SDRi negativo), então a separação só vale onde há sobreposição — a decisão é
#: POR JANELA e o resto é veto por perfil, que é o que o R4 mediu como melhor.
JANELA_S = 1.5
#: correlação original x fluxo escolhido acima da qual a separação NÃO mudou a
#: janela (medido: 0,95 em janela de um falante, 0,82 em sobreposta) — é o sinal de
#: sobreposição mais barato e estável que achei; os escores de perfil sozinhos não
#: separam, porque o modelo também "separa" um falante só em dois fluxos
CORR_SEPAROU = 0.90
#: os dois fluxos casando com o perfil = a MESMA voz partida em dois (não duas
#: pessoas): margem curta entre os dois escores
MARGEM = 0.05


def separa_alvo(wav: Path, vecs: list, modelo: str) -> tuple[Path, dict]:
    """Áudio só com a voz do perfil: separa nas sobreposições, veta janela no resto.

    `vecs` são as amostras do perfil e `modelo` é o extrator QUE AS GEROU — os dois
    andam juntos (perfil do Resemblyzer pontuado pelo ECAPA dá 0,055: dimensões
    256 x 192 e o `zip` truncava calado).

    Por JANELA de `JANELA_S`:
    - os DOIS fluxos casam com o perfil (escores próximos, ambos acima do limiar) →
      é um falante só: fica o áudio ORIGINAL da janela (separar piora);
    - só um fluxo casa → há sobreposição: fica esse fluxo;
    - nenhum casa com folga → janela do concorrente: vira silêncio (veto).

    Devolve o wav resultante e o que foi descartado (`trechos`/`ms`), além dos
    escores por janela para o log."""
    import app                       # só para reusar o extrator já carregado
    import numpy as np
    import soundfile as sf

    with tempfile.TemporaryDirectory(prefix="separa-") as pastinha:
        dir_saida = Path(pastinha)
        fluxos_p = separa(wav, dir_saida)
        x, sr = sf.read(str(wav), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        fluxos = [sf.read(str(f), dtype="float32")[0] for f in fluxos_p]
        centroide = app._speaker_centroide(vecs)
        limiar = float(app._speaker_limiar(modelo)[0])

        _conta = [0]

        def _sim(sinal) -> float:
            if sinal.size < 1600:            # < 0,1 s não gera embedding confiável
                return -1.0
            _conta[0] += 1
            # arquivo PRÓPRIO por janela: reusar um nome só já deu leitura do
            # conteúdo anterior (todos os escores voltando -1 na mesma rodada)
            tmp = dir_saida / f"jan{_conta[0]}.wav"
            sf.write(str(tmp), sinal, sr, subtype="PCM_16")
            emb = app._speaker_embed(tmp, modelo)
            return app._cos_sim(emb, centroide) if emb else -1.0

        janela = int(JANELA_S * sr)
        saida = np.zeros(x.size, dtype=np.float32)
        descartados_ms = 0
        trechos_descartados = 0
        escores_janelas = []
        sobrepostas = 0
        for ini in range(0, x.size, janela):
            fim = min(ini + janela, x.size)
            sim_orig = _sim(x[ini:fim])
            sims = [(_sim(f[ini:fim]), i) for i, f in enumerate(fluxos)]
            sims.sort(reverse=True)
            (melhor_sim, melhor_i), (seg_sim, _) = sims[0], sims[1]
            # os dois fluxos casam com o perfil: um falante só, separar atrapalha
            um_falante = (sim_orig >= limiar and seg_sim >= limiar
                          and (melhor_sim - seg_sim) <= MARGEM)
            escolhido = fluxos[melhor_i][ini:fim]
            k = min(escolhido.size, fim - ini)
            corr = float(np.corrcoef(x[ini:ini + k], escolhido[:k])[0, 1]) \
                if k > 1 else 0.0
            if corr != corr:                  # NaN: sinal constante, sem correlação
                corr = 0.0
            # separar num bloco de UM falante atrapalha: a separação só vale quando
            # mudou mesmo a janela (sobreposição) — e nunca quando o perfil já está
            # satisfeito sem ela
            separou = corr < CORR_SEPAROU and melhor_sim >= limiar
            if melhor_sim < 0 and sim_orig < 0:
                pass                       # embedding falhou: fica o original
            elif (melhor_sim < limiar * 0.5 and sim_orig < limiar * 0.5):
                # janela do concorrente: ATENUA (-30 dB) em vez de zerar. Silêncio
                # digital picado faz o Whisper alucinar (medido: vazamento 464 % e
                # WER 5,0 com as janelas zeradas) e -20 dB deixava vazar demais
                # (14,3 %); -30 dB dá 7,1 % mantendo o contexto
                saida[ini:fim] = x[ini:fim] * 0.03
                trechos_descartados += 1
                descartados_ms += int((fim - ini) / sr * 1000)
            elif separou and not um_falante:
                saida[ini:fim] = escolhido
                sobrepostas += 1
            else:
                saida[ini:fim] = x[ini:fim]
            escores_janelas.append([round(sim_orig, 3), round(melhor_sim, 3),
                                    round(seg_sim, 3), round(corr, 3)])
            escores_janelas.append([round(sim_orig, 3), round(melhor_sim, 3),
                                    round(seg_sim, 3)])

        final = Path(tempfile.mkstemp(prefix="alvo-", suffix=".wav")[1])
        sf.write(str(final), saida, sr, subtype="PCM_16")
        info = {"escores_janelas": escores_janelas[:40], "sobrepostas": sobrepostas,
                "limiar": round(limiar, 3)}
        if trechos_descartados:
            info["descartados"] = {"trechos": trechos_descartados, "ms": descartados_ms}
        return final, info


def _duracao_ms(wav: Path) -> int:
    try:
        import soundfile as sf

        info = sf.info(str(wav))
        return int(info.frames / max(1, info.samplerate) * 1000)
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["disponivel", "motivo", "separa", "separa_alvo", "fecha",
           "MODELO_DIR", "JANELA_S"]
