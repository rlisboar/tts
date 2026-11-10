"""Mede a NCC do eco com o mesmo correlacionador do duck (#870).

O wav do turno (`TTS_LIVE_DUMP_DIR/*.wav` + json com `motivo`, #869) é a
fala do usuário. Não tem a referência do playback: uma NCC baixa nesse
arquivo não é o eco que falhou. O par do eco real é a pasta `eco-*` que
`live_eco_dump` grava na mesma variável, com a sessão de diagnóstico:

    mic.wav        PCM16 mono 16 kHz, cru
    ref.f32        float32, o que `anota_referencia` guardou (preferido)
    ref.wav        alternativa; 16 kHz como está, 24 kHz no passo 1,5
    relogio.jsonl  mic, play, origem, ref_n, pausa

Sem relógio, o playhead é o índice do mic (um ponto por frame de 512).
A fórmula de 24 kHz é a de `app._live_ref_16k` (passo 1,5 exato, sem
scipy): este script não importa o `app`.

    python tools/ncc_eco.py PASTA
    python tools/ncc_eco.py --mic mic.wav --ref playback.wav [--relogio r.jsonl] [--json]

Código de saída sempre 0. É medida, não gate. Não liga a flag do duck.
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import live_turns as lt  # noqa: E402


_AVISO_SEM_RELOGIO = "sem relógio: playhead = índice do mic"
# Silero acima disto conta como VOZ na janela: a NCC daí é de mistura
# (fala por cima do eco), não de eco puro. 0,5 é o mesmo limiar de decisão do
# motor; aqui só classifica, não decide nada em tempo real.
_VOZ_PROB = 0.5
_AVISO_TURNO = (
    "dump do STT (#869): é o turno do usuário, sem a referência do playback. "
    "Uma NCC baixa aqui não é o eco que falhou."
)


def ref_24k_para_16k(a: np.ndarray) -> np.ndarray:
    """Float 24 kHz → 16 kHz. Igual a `app._live_ref_16k`.

    Passo 1,5 exato. Em pedaços de 2400 amostras (100 ms no fio) a posição
    cai no mesmo ponto da interpolação do arquivo inteiro — é por isso que
    não se usa `resample_poly`, que reinicia a fase a cada chunk.
    """
    a = np.ascontiguousarray(a, dtype=np.float32).reshape(-1)
    if a.size < 3:
        return a
    pos = np.arange(int(a.size * 16000 / 24000), dtype=np.float32) * (24000 / 16000.0)
    return np.interp(pos, np.arange(a.size, dtype=np.float32), a).astype(np.float32)


def ncc_no_instante(mic: np.ndarray, ref: np.ndarray, fim: int, play: int,
                    origem: int, ref_n: int | None = None):
    """|NCC| máxima e o lag, ou None se a busca não cabe.

    Mesma janela, mesmo lag e a mesma função (`live_turns.melhor_ncc`) que
    o motor. `origem` / `ref_n` são os do ring naquele instante: não se
    busca amostra que o motor já não tinha.
    """
    n = lt._CORR_N
    if ref_n is None:
        ref_n = int(ref.shape[0])
    if fim < n or fim > int(mic.shape[0]):
        return None
    if play - origem < n + lt._CORR_LAG_MAX:
        return None
    janela = np.asarray(mic[fim - n:fim], dtype=np.float32)
    teto = play - origem - n

    def trecho(lag: int, n_: int):
        if lag > teto:
            return None
        ini = play - lag - n_
        if ini < origem or ini + n_ > ref_n or ini + n_ > int(ref.shape[0]):
            return None
        return ref[ini:ini + n_]

    return lt.melhor_ncc(janela, trecho)


def _base(**kw) -> dict:
    d = {
        "tipo": "eco",
        "aviso": "",
        "sem_relogio": False,
        "medidas": 0,
        "silencio": 0,
        "busca_curta": 0,
        "fora": 0,
        "p10": None,
        "p50": None,
        "p90": None,
        "min": None,
        "max": None,
        "lag_p50": None,
        "lag_p50_ms": None,
        "frac_075": None,
        "pausado": 0,
        "puro_n": 0,
        "voz_n": 0,
        "indef_n": 0,
        "puro_p10": None,
        "puro_p50": None,
        "puro_p90": None,
        "eco": False,
        "pasta": "",
        "mic_amostras": 0,
        "ref_amostras": 0,
    }
    d.update(kw)
    return d


def relogio_pelo_indice(n_mic: int, n_ref: int) -> list[dict]:
    """Playhead = fim da janela do mic. Um ponto por frame, do primeiro que cabe."""
    linhas = []
    fim = lt._CORR_N
    while fim <= n_mic:
        linhas.append({
            "mic": int(fim),
            "play": int(fim),
            "origem": 0,
            "ref_n": int(n_ref),
            "pausa": False,
        })
        fim += lt.FRAME_SAMPLES
    return linhas


def medir(mic: np.ndarray, ref: np.ndarray, linhas: list[dict] | None = None,
          *, sem_relogio: bool | None = None, tipo: str = "eco", aviso: str = "",
          pasta: str = "") -> dict:
    """NCC nas linhas do relógio. Sem linhas, o playhead é o índice do mic.

    Abaixo de −45 dBFS conta como `silencio`, não como NCC baixa. Enquanto
    `play - origem` não cobre a janela mais o lag máximo, conta como
    `busca_curta` — o motor, nessa fase, também não declara voz.
    """
    mic = np.ascontiguousarray(mic, dtype=np.float32).reshape(-1)
    ref = np.ascontiguousarray(ref, dtype=np.float32).reshape(-1)
    if sem_relogio is None:
        sem_relogio = linhas is None
    if linhas is None:
        linhas = relogio_pelo_indice(int(mic.shape[0]), int(ref.shape[0]))
        if not aviso:
            aviso = _AVISO_SEM_RELOGIO
    if mic.size == 0 or ref.size == 0:
        return _base(tipo=tipo, aviso=aviso or "mic ou referência vazios",
                     sem_relogio=sem_relogio, pasta=pasta,
                     mic_amostras=int(mic.size), ref_amostras=int(ref.size))
    n = lt._CORR_N
    preciso = n + lt._CORR_LAG_MAX
    vals: list[float] = []
    vals_puro: list[float] = []
    vals_voz: list[float] = []
    vals_indef: list[float] = []
    lags: list[int] = []
    silencio = busca = fora = pausado = 0
    for linha in linhas:
        try:
            fim = int(linha["mic"])
            play = int(linha["play"])
            origem = int(linha.get("origem") or 0)
            ref_n = int(linha["ref_n"]) if linha.get("ref_n") is not None else int(ref.shape[0])
        except (KeyError, TypeError, ValueError):
            fora += 1
            continue
        if fim > int(mic.shape[0]) or play > int(ref.shape[0]) or play > ref_n:
            fora += 1
            continue
        # #870: referência PAUSADA (barge_pausa) não avança com o playback, então
        # a NCC contra ela não descreve o eco. Conta à parte e fica fora da conta.
        if linha.get("pausa"):
            pausado += 1
            continue
        if fim < n or play - origem < preciso:
            busca += 1
            continue
        janela = mic[fim - n:fim]
        rms = float(np.sqrt(float(np.dot(janela, janela)) / float(n)))
        if lt._dbfs(rms) < lt._CORR_ENERGIA_DBFS:
            silencio += 1
            continue
        par = ncc_no_instante(mic, ref, fim, play, origem, ref_n)
        if par is None or par[1] < 0:
            busca += 1
            continue
        # #870: eco PURO (ninguém falando) é a pergunta do diagnóstico. A mistura
        # tem valor próprio e não pode diluir a estatística do eco.
        voz = _tem_voz(janela)
        if voz is None:
            vals_indef.append(float(par[0]))
        elif voz:
            vals_voz.append(float(par[0]))
        else:
            vals_puro.append(float(par[0]))
        vals.append(float(par[0]))
        lags.append(int(par[1]))
    out = _base(tipo=tipo, aviso=aviso, sem_relogio=sem_relogio, pasta=pasta,
                medidas=len(vals), silencio=silencio, busca_curta=busca, fora=fora,
                pausado=pausado, puro_n=len(vals_puro), voz_n=len(vals_voz),
                indef_n=len(vals_indef),
                mic_amostras=int(mic.size), ref_amostras=int(ref.size))
    if not vals:
        return out
    a = np.asarray(vals, dtype=np.float64)
    lag = np.asarray(lags, dtype=np.float64)
    # o veredito é do ECO PURO: com voz por cima a NCC cai legitimamente e a
    # mistura não pode reprovar o eco. Sem janela pura, cai no total e avisa.
    if vals_puro:
        puro = np.asarray(vals_puro, dtype=np.float64)
        out.update({"puro_p10": float(np.percentile(puro, 10)),
                    "puro_p50": float(np.percentile(puro, 50)),
                    "puro_p90": float(np.percentile(puro, 90))})
    elif not vals_indef:
        # só afirma "mic sempre com voz" quando a separação DE FATO rodou e
        # achou voz em tudo. Com tudo indefinido, dizer "sempre com voz" é
        # afirmar o que não se mediu — sem Silero não se sabe o que havia no mic
        # (#1101). O aviso das indefinidas abaixo já nomeia o motivo.
        out["aviso"] = (out["aviso"] + " · " if out["aviso"] else "") + \
            "sem janela de eco puro (mic sempre com voz)"
    if vals_indef:
        # separação parcial: o modelo caiu em ALGUMAS janelas. Dizer quantas é o
        # que separa "medi tudo junto" de "medi quase tudo, com buraco".
        out["aviso"] = (out["aviso"] + " · " if out["aviso"] else "") + \
            (f"Silero indisponível em {len(vals_indef)} janela(s) — "
             "separação puro × voz degradada")
        if not vals_puro:
            indef = np.asarray(vals_indef, dtype=np.float64)
            out.update({"puro_p10": float(np.percentile(indef, 10)),
                        "puro_p50": float(np.percentile(indef, 50)),
                        "puro_p90": float(np.percentile(indef, 90))})
    p10 = float(np.percentile(a, 10))
    p50_lag = float(np.percentile(lag, 50))
    out.update({
        "p10": p10,
        "p50": float(np.percentile(a, 50)),
        "p90": float(np.percentile(a, 90)),
        "min": float(a.min()),
        "max": float(a.max()),
        "lag_p50": p50_lag,
        "lag_p50_ms": p50_lag * 1000.0 / lt.SAMPLE_RATE,
        "frac_075": float(np.mean(a >= lt._CORR_LIMIAR)),
        "eco": bool((out["puro_p10"] if out["puro_p10"] is not None else p10) >= 0.8),
    })
    return out


def _tem_voz(janela: np.ndarray) -> bool | None:
    """Silero na janela do mic; `None` = modelo indisponível (#1090).

    Antes a falha virava `False` (= puro), então sem Silero o relatório
    imprimia `puro n=` cheio como se tivesse separado — o mesmo tipo de mentira
    que o `captura` ausente fazia no `meta.json`. Agora a janela indefinida tem
    contador próprio (`indef_n`) e aviso, e não é evidência de eco puro.
    """
    try:
        return lt.scorer_silero(janela) >= _VOZ_PROB
    except Exception:  # noqa: BLE001 — sem Silero a separação some, a NCC fica
        return None


def _le_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        canais, largura, sr, n = (w.getnchannels(), w.getsampwidth(),
                                   w.getframerate(), w.getnframes())
        cru = w.readframes(n)
    if canais != 1 or largura != 2:
        raise ValueError(f"{path.name}: preciso de PCM16 mono (tem {canais} canais, {largura} bytes)")
    a = np.frombuffer(cru, dtype="<i2").astype(np.float32) / 32768.0
    return a, sr


def _le_ref(path: Path) -> tuple[np.ndarray, str]:
    if path.suffix == ".f32":
        return np.fromfile(path, dtype="<f4"), ""
    amostras, sr = _le_wav(path)
    if sr == 16000:
        return amostras, ""
    if sr == 24000:
        return ref_24k_para_16k(amostras), ""
    return np.zeros(0, dtype=np.float32), (
        f"taxa {sr} Hz fora do par 16 kHz/24 kHz — não medi")


def _eh_turno(caminho: Path) -> bool:
    js = caminho.with_suffix(".json")
    if not js.is_file():
        return False
    try:
        meta = json.loads(js.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(meta, dict) and "motivo" in meta


def _eh_pasta_eco(pasta: Path) -> bool:
    return (pasta / "mic.wav").is_file() or (pasta / "ref.f32").is_file() or (
        pasta / "ref.wav").is_file()


def _resultado_turno(caminho: Path) -> dict:
    return _base(tipo="turno", aviso=_AVISO_TURNO, pasta=str(caminho), eco=False)


def medir_pasta(pasta: Path) -> dict:
    pasta = Path(pasta)
    if not _eh_pasta_eco(pasta):
        return _base(tipo="vazio", aviso=f"sem mic.wav nem referência em {pasta}",
                     pasta=str(pasta))
    mic_path = pasta / "mic.wav"
    if not mic_path.is_file():
        return _base(aviso=f"sem mic.wav em {pasta}", pasta=str(pasta))
    try:
        mic, sr = _le_wav(mic_path)
    except (OSError, ValueError, wave.Error) as exc:
        return _base(aviso=str(exc), pasta=str(pasta))
    if sr != 16000:
        return _base(aviso=f"mic a {sr} Hz; o cru do Live é 16 kHz — não medi",
                     pasta=str(pasta), mic_amostras=int(mic.size))
    ref_path = pasta / "ref.f32" if (pasta / "ref.f32").is_file() else pasta / "ref.wav"
    if not ref_path.is_file():
        return _base(aviso=f"sem ref.f32 nem ref.wav em {pasta}", pasta=str(pasta),
                     mic_amostras=int(mic.size))
    try:
        ref, aviso = _le_ref(ref_path)
    except (OSError, ValueError, wave.Error) as exc:
        return _base(aviso=str(exc), pasta=str(pasta), mic_amostras=int(mic.size))
    if aviso:
        return _base(aviso=aviso, pasta=str(pasta), mic_amostras=int(mic.size),
                     ref_amostras=int(ref.size))
    rel_path = pasta / "relogio.jsonl"
    linhas = None
    if rel_path.is_file():
        linhas = []
        try:
            texto = rel_path.read_text(encoding="utf-8")
        except OSError as exc:
            return _base(aviso=str(exc), pasta=str(pasta))
        for cru in texto.splitlines():
            cru = cru.strip()
            if not cru:
                continue
            try:
                linhas.append(json.loads(cru))
            except json.JSONDecodeError:
                linhas.append({})
        if not linhas:
            return medir(mic, ref, [], sem_relogio=False, pasta=str(pasta),
                         aviso="relógio vazio: não houve playback com referência")
    return medir(mic, ref, linhas, pasta=str(pasta))


def medir_diretorio(pasta: Path) -> list[dict]:
    pasta = Path(pasta)
    if not pasta.is_dir():
        return [_base(tipo="vazio", aviso=f"não é diretório: {pasta}", pasta=str(pasta))]
    if _eh_pasta_eco(pasta):
        return [medir_pasta(pasta)]
    ecos = sorted(p for p in pasta.iterdir()
                  if p.is_dir() and (p.name.startswith("eco-") or _eh_pasta_eco(p)))
    turnos = sorted(p for p in pasta.glob("*.wav") if _eh_turno(p))
    saida = [medir_pasta(p) for p in ecos]
    saida.extend(_resultado_turno(w) for w in turnos)
    if not saida:
        saida.append(_base(
            tipo="vazio", pasta=str(pasta),
            aviso="nada para medir: sem pasta eco-* nem wav de turno"))
    return saida


def medir_arquivos(mic_path: Path, ref_path: Path | None,
                   relogio: Path | None = None) -> dict:
    mic_path = Path(mic_path)
    if _eh_turno(mic_path) and ref_path is None:
        return _resultado_turno(mic_path)
    if not mic_path.is_file():
        return _base(aviso=f"não achei o mic: {mic_path}", pasta=str(mic_path))
    try:
        mic, sr = _le_wav(mic_path)
    except (OSError, ValueError, wave.Error) as exc:
        return _base(aviso=str(exc), pasta=str(mic_path))
    if sr != 16000:
        return _base(aviso=f"mic a {sr} Hz; o cru do Live é 16 kHz — não medi",
                     pasta=str(mic_path), mic_amostras=int(mic.size))
    tipo = "turno" if _eh_turno(mic_path) else "eco"
    aviso = _AVISO_TURNO + " " if tipo == "turno" else ""
    if ref_path is None:
        return _base(tipo=tipo, aviso=(aviso + "falta --ref").strip(),
                     pasta=str(mic_path), mic_amostras=int(mic.size))
    ref_path = Path(ref_path)
    if not ref_path.is_file():
        return _base(tipo=tipo, aviso=f"não achei a referência: {ref_path}",
                     pasta=str(mic_path))
    try:
        ref, erro = _le_ref(ref_path)
    except (OSError, ValueError, wave.Error) as exc:
        return _base(tipo=tipo, aviso=str(exc), pasta=str(mic_path),
                     mic_amostras=int(mic.size))
    if erro:
        return _base(tipo=tipo, aviso=erro, pasta=str(mic_path),
                     mic_amostras=int(mic.size))
    linhas = None
    if relogio is not None:
        try:
            texto = Path(relogio).read_text(encoding="utf-8")
        except OSError as exc:
            return _base(tipo=tipo, aviso=str(exc), pasta=str(mic_path))
        linhas = []
        for cru in texto.splitlines():
            cru = cru.strip()
            if not cru:
                continue
            try:
                linhas.append(json.loads(cru))
            except json.JSONDecodeError:
                linhas.append({})
    out = medir(mic, ref, linhas, tipo=tipo, pasta=str(mic_path))
    if aviso:
        out["aviso"] = (aviso + out["aviso"]).strip()
    return out


def _fmt(x, casas: int = 3) -> str:
    if x is None:
        return "null"
    return f"{x:.{casas}f}"


def imprime(r: dict) -> None:
    nome = r.get("pasta") or r.get("tipo")
    print(f"{r.get('tipo')}  {nome}")
    if r.get("aviso"):
        print(r["aviso"])
    print(f"medidas={r['medidas']} silencio={r['silencio']} "
          f"busca_curta={r['busca_curta']} fora={r['fora']} pausado={r['pausado']}")
    print(f"p10={_fmt(r['p10'])} p50={_fmt(r['p50'])} p90={_fmt(r['p90'])} "
          f"min={_fmt(r['min'])} max={_fmt(r['max'])}")
    # #870: a separação é o ponto. Eco puro é a pergunta; a mistura é contexto.
    print(f"puro n={r['puro_n']} p10={_fmt(r['puro_p10'])} p50={_fmt(r['puro_p50'])} "
          f"p90={_fmt(r['puro_p90'])}")
    print(f"voz  n={r['voz_n']}")
    if r["indef_n"]:
        # linha própria: janela indefinida não é puro nem voz, e some se ficar
        # só no `aviso` quando alguém corta a saída (#1090)
        print(f"indef n={r['indef_n']} (Silero indisponível)")
    if r["lag_p50"] is None:
        print("lag_p50=null")
    else:
        print(f"lag_p50={r['lag_p50']:.0f} ({r['lag_p50_ms']:.0f} ms)")
    print(f"frac>=0.75={_fmt(r['frac_075'])}")
    print("p10(puro)>=0.8: sim" if r["eco"] else "p10(puro)>=0.8: não")


def _uso() -> None:
    print("uso: python tools/ncc_eco.py PASTA")
    print("     python tools/ncc_eco.py --mic MIC.wav --ref REF.wav "
          "[--relogio RELOGIO.jsonl] [--json]")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    como_json = False
    mic = ref = relogio = pasta = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            como_json = True
        elif a in ("--mic", "--ref", "--relogio"):
            if i + 1 >= len(args):
                print(f"falta o valor de {a}")
                _uso()
                return 0
            i += 1
            if a == "--mic":
                mic = args[i]
            elif a == "--ref":
                ref = args[i]
            else:
                relogio = args[i]
        elif a in ("-h", "--help"):
            _uso()
            return 0
        elif a.startswith("-"):
            print(f"opção desconhecida: {a}")
            _uso()
            return 0
        elif pasta is None:
            pasta = a
        else:
            print("um diretório basta")
            _uso()
            return 0
        i += 1
    if mic is not None:
        resultados = [medir_arquivos(Path(mic), Path(ref) if ref else None,
                                     Path(relogio) if relogio else None)]
    elif pasta is not None:
        resultados = medir_diretorio(Path(pasta))
    else:
        _uso()
        return 0
    if como_json:
        corpo = resultados[0] if len(resultados) == 1 else resultados
        print(json.dumps(corpo, ensure_ascii=False, indent=2))
    else:
        for j, r in enumerate(resultados):
            if j:
                print()
            imprime(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
