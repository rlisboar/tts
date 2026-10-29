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


def _segmentos_de_fala(x, sr: int, maximo: int, piso: float = 0.15) -> list:
    """(ini, fim) por segmento de FALA, cortados em blocos de `maximo` amostras.

    Piso de energia adaptativo (0,15 do percentil 90 da própria faixa), quadros de
    20 ms e emenda de vãos curtos (< 0,2 s) — mesma régua do VAD do R4. Trechos de
    SILÊNCIO ficam fora da lista e o chamador os deixa como estão."""
    import numpy as np

    q = 320                          # 20 ms
    if x.size < q * 2:
        return [(0, x.size)]
    quadros = x[: x.size // q * q].reshape(-1, q)
    energia = np.sqrt((quadros.astype(np.float64) ** 2).mean(axis=1))
    referencia = float(np.percentile(energia, 90)) or 1e-9
    ativo = energia >= referencia * piso
    juntos = max(1, int(0.2 * sr / q))            # emenda vãos < 0,2 s
    for i in range(1, len(ativo) - juntos):
        if ativo[i - 1] and ativo[min(len(ativo) - 1, i + juntos)]:
            ativo[i:i + juntos] = True
    segmentos = []
    i = 0
    while i < ativo.size:
        if not ativo[i]:
            i += 1
            continue
        j = i
        while j < ativo.size and ativo[j]:
            j += 1
        ini, fim = i * q, min(j * q, x.size)
        while fim - ini > maximo:                 # segmento longo: corta em blocos
            segmentos.append((ini, ini + maximo))
            ini += maximo
        segmentos.append((ini, fim))
        i = j
    return segmentos or [(0, x.size)]


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

        passo = int(0.5 * sr)        # sub-bloco da medida: 1,5 s sozinho pontua baixo

        def _sim_forte(sinal) -> float:
            """Melhor escore entre sub-blocos de 0,5 s: um trecho com o dono em
            QUALQUER pedaço da janela não pode ser vetado (com o escore da janela
            inteira, o dono sumia em blocos de 2 s — medido)."""
            if sinal.size <= passo:
                return _sim(sinal)
            return max(_sim(sinal[k:k + passo])
                       for k in range(0, sinal.size - passo + 1, passo))

        janela = int(JANELA_S * sr)
        saida = np.zeros(x.size, dtype=np.float32)
        # as janelas seguem os SEGMENTOS DE FALA, não uma grade fixa: numa grade, a
        # fronteira cai meio a meio entre dois falantes, pontua baixo e o veto come
        # palavra do dono (#663). Segmento curto (< 0,25 s) não se separa.
        segmentos = _segmentos_de_fala(x, sr, janela)
        descartados_ms = 0
        trechos_descartados = 0
        escores_janelas = []
        sobrepostas = 0
        # PASSO 1: mede todas as janelas. O piso do veto é RELATIVO à mediana do
        # próprio áudio — o escore cru cai com a duração da janela (a mesma fala em
        # 1,5 s dá 0,26 e em 7 s dá ~0,9), então o limiar absoluto do gate vetava o
        # próprio dono.
        medido = []
        for ini, fim in segmentos:
            sim_orig = _sim(x[ini:fim])
            ocorre = _sim_forte(x[ini:fim])
            sims = [(_sim(f[ini:fim]), i) for i, f in enumerate(fluxos)]
            sims.sort(reverse=True)
            medido.append((ini, fim, sim_orig, ocorre, sims))
        validos = [m[4][0][0] for m in medido if m[4][0][0] > 0]
        mediana = float(np.median(validos)) if validos else 0.0
        piso = min(limiar * 0.5, mediana * 0.5)

        # PASSO 2: decide
        for ini, fim, sim_orig, ocorre, sims in medido:
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
                # embedding falhou: fica o ORIGINAL (sem isto a janela ficava em
                # zero no `saida` e o áudio saía picado de silêncio)
                saida[ini:fim] = x[ini:fim]
            elif melhor_sim < piso and ocorre < piso:
                # janela do concorrente SEM o dono: atenua forte
                saida[ini:fim] = x[ini:fim] * 0.03
                trechos_descartados += 1
                descartados_ms += int((fim - ini) / sr * 1000)
            elif melhor_sim < piso:
                # ambígua (o dono aparece em algum pedaço): atenua de leve, para a
                # palavra dele não sumir do transcript
                saida[ini:fim] = x[ini:fim] * 0.35
                trechos_descartados += 1
                descartados_ms += int((fim - ini) / sr * 1000)
            elif separou and not um_falante:
                saida[ini:fim] = escolhido
                sobrepostas += 1
            else:
                saida[ini:fim] = x[ini:fim]
            escores_janelas.append([round(sim_orig, 3), round(melhor_sim, 3),
                                    round(seg_sim, 3), round(corr, 3)])

        final = Path(tempfile.mkstemp(prefix="alvo-", suffix=".wav")[1])
        sf.write(str(final), saida, sr, subtype="PCM_16")
        info = {"escores_janelas": escores_janelas[:40], "sobrepostas": sobrepostas,
                "limiar": round(limiar, 3), "piso": round(piso, 3)}
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
