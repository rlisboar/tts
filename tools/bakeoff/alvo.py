"""Corpus e primeira medição do locutor-ALVO (#532).

Duas falas simultâneas: o ALVO e um CONCORRENTE, em SIR fixos. A pergunta do card
é se dá para entender só o alvo; a média é o que vaza do concorrente para a
transcrição e o WER do alvo.

Primeira camada medida aqui: **seleção por janela** com o extrator do R2 (ECAPA),
sem modelo de separação — é o piso contra o qual um TSE de verdade tem de ganhar.

Uso: python tools/bakeoff/alvo.py [--sirs 0,5,10] [--n 4]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum  # noqa: E402
import metricas  # noqa: E402

VENV = Path("/tmp/tts-bakeoff/venv312/bin/python")
JANELA_S = 1.5


def _le(p: Path) -> np.ndarray:
    x, _ = sf.read(str(p), dtype="float32")
    return x if x.ndim == 1 else x.mean(axis=1)


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.float64(x) ** 2)))


def junta(arquivos: list[Path], alvo_s: float) -> tuple[np.ndarray, str]:
    """Concatena clipes do MESMO falante até ~`alvo_s` e devolve (amostras, texto).

    O corpus público é de clipes curtos (1,3–7 s): sem isso a janela de 1,5 s
    engole o arquivo inteiro e a medição por janela não diz nada."""
    amostras: list[np.ndarray] = []
    textos: list[str] = []
    dur = 0.0
    for p in arquivos:
        x = _le(p)
        txt = p.with_suffix(".txt")
        if not x.size or not txt.exists():
            continue
        amostras.append(x)
        amostras.append(np.zeros(int(0.15 * comum.SR), dtype=np.float32))
        textos.append(txt.read_text().strip())
        dur += x.size / comum.SR + 0.15
        if dur >= alvo_s:
            break
    if not amostras:
        return np.zeros(comum.SR, dtype=np.float32), ""
    return np.concatenate(amostras).astype(np.float32), " ".join(textos)


def _falantes() -> dict[str, list[Path]]:
    por: dict[str, list[Path]] = {}
    for p in sorted(comum.FALA.glob("pub_*.wav")):
        if p.with_suffix(".txt").exists():
            por.setdefault(p.stem.split("_")[1], []).append(p)
    return por


def mistura(alvo: np.ndarray, conc: np.ndarray, sir_db: float) -> np.ndarray:
    n = min(alvo.size, conc.size)
    a, c = alvo[:n], conc[:n]
    c = c * (_rms(a) / (_rms(c) or 1e-9) / 10 ** (sir_db / 20))
    return (a + c).astype(np.float32)


def alternado(alvo: np.ndarray, conc: np.ndarray, sir_db: float,
             blocos_s: float = 1.5) -> np.ndarray:
    """Sobreposição PARCIAL: blocos alternando alvo e concorrente (o caso de turno
    de fala, não de conversa cruzada).

    Cada fonte tem PONTEIRO PRÓPRIO. A linha do tempo é a do alvo (assim o texto de
    referência corresponde exatamente ao que o alvo diz, uma vez cada) e o
    concorrente dá a volta quando acabam os seus blocos — antes o mesmo índice `i`
    fatiar as duas fontes fazia o concorrente curto morrer no 2º bloco e o
    `break` devolvia uma mistura só de alvo (0 % de corte na seleção)."""
    n = int(blocos_s * comum.SR)
    ganho_conc = _rms(alvo) / (_rms(conc) or 1e-9) / 10 ** (sir_db / 20)
    pedacos: list[np.ndarray] = []
    pos_alvo, pos_conc, usar_alvo = 0, 0, True
    while pos_alvo < alvo.size:
        if usar_alvo:
            pedaco = alvo[pos_alvo: pos_alvo + n]
            pos_alvo += n
        else:
            if pos_conc + n > conc.size:  # acabou o concorrente: recomeça
                pos_conc = 0
            pedaco = conc[pos_conc: pos_conc + n] * ganho_conc
            pos_conc += n
        if not pedaco.size:
            break
        pedacos.append(pedaco)
        usar_alvo = not usar_alvo
    return np.concatenate(pedacos).astype(np.float32) if pedacos else alvo.copy()


def _eca():
    """Extrator do R2 (ECAPA) rodando no venv de bancada."""
    codigo = """
import os, sys, json, numpy as np, torch
from speechbrain.inference.speaker import EncoderClassifier
# `source`/`savedir` locais: sem o `savedir` o load resolve pelo cache do HF e
# quebra offline (#598)
enc = EncoderClassifier.from_hparams(source=os.environ['TTS_ECAPA_DIR'],
                                     savedir=os.environ['TTS_ECAPA_DIR'],
                                     run_opts={'device':'cpu'})
out = []
for p in sys.argv[1:]:
    import soundfile as sf
    x,_ = sf.read(p, dtype='float32')
    with torch.no_grad(): e = enc.encode_batch(torch.from_numpy(x)[None]).squeeze().numpy()
    out.append((e/np.linalg.norm(e)).tolist())
print(json.dumps(out))
"""
    def _roda(caminhos: list[Path]) -> list[np.ndarray]:
        r = subprocess.run([str(VENV), "-c", codigo, *[str(c) for c in caminhos]],
                           capture_output=True, text=True,
                           env={**os.environ, "TTS_ECAPA_DIR": str(comum.MODELO_ECAPA)})
        return [np.array(v) for v in json.loads(r.stdout)]
    return _roda


def seleciona(mix: np.ndarray, sr: int, ref: np.ndarray, eca,
              tag: str = "sel", corte_abs: float | None = None,
              margem: float = 0.12) -> tuple[np.ndarray, list[float]]:
    """Guarda só as janelas cujo escore contra o alvo passa do limiar calibrado.

    É a ideia da diarização por segmento: janelas de outro falante são descartadas
    em vez de irem ao STT. Devolve também os escores, para o chamador conferir que
    a mistura realmente tem as duas vozes.

    `tag` isola os temporários de cada chamada: antes o diretório `_sel` era um só
    e os arquivos `j<n>.wav` eram reescritos por cima a cada iteração do laço."""
    tmp = comum.BASE / "resultados" / "_sel" / tag
    tmp.mkdir(parents=True, exist_ok=True)
    passo = int(JANELA_S * sr)
    pedacos = []
    for i in range(0, max(mix.size - passo + 1, 1), passo):
        # arquivo mais curto que uma janela: a janela é o arquivo inteiro (antes o
        # laço não rodava e a "seleção" devolvia a mistura com escores vazios)
        p = tmp / f"j{i // passo}.wav"
        sf.write(str(p), mix[i:i + passo], sr, subtype="PCM_16")
        pedacos.append(p)
    if not pedacos:
        return mix, []
    pr = tmp / "ref.wav"
    sf.write(str(pr), ref, sr, subtype="PCM_16")
    embs = eca([pr, *pedacos])
    e_ref, resto = embs[0], embs[1:]
    # limiar de FAR 1% do R2 (2,43 em escore normalizado) — aqui sem AS-norm,
    # então se usa o cosseno cru: calibra-se pelo próprio par alvo/concorrente
    escores = [float(e @ e_ref) for e in resto]
    if corte_abs is None:
        # corte RELATIVO ao melhor bloco. O cosseno absoluto do ECAPA varia com o
        # canal e com a duração, então um número fixo colhido de outro corpus
        # erraria. A margem de 0,12 vem da distância medida (0,88 x 0,66).
        corte = max(escores) - margem
    else:
        # corte ABSOLUTO. É o que serve quando as janelas do terceiro NÃO são as
        # piores por acaso: no fluxo separado o alvo pontua 0,55–0,78 e o terceiro
        # 0,00–0,13, mas o relativo ainda comia bloco de alvo de 0,55 porque o
        # máximo estava em 0,78. No sobreposto, onde todas as janelas são do alvo,
        # o absoluto quase não corta — que é o desejado.
        corte = corte_abs
    saida = [mix[i * passo:(i + 1) * passo] for i, s in enumerate(escores) if s >= corte]
    return (np.concatenate(saida) if saida else mix), escores


def _transcreve(x: np.ndarray, sr: int, idioma: str, tag: str) -> list[str]:
    sys.path.insert(0, str(comum.RAIZ_REPO))
    comum.isola_estado()
    import app

    tmp = comum.BASE / "resultados" / f"_wer_{tag}.wav"
    sf.write(str(tmp), x, sr, subtype="PCM_16")
    try:
        r = app._transcribe(tmp, language=idioma)
    except Exception:  # noqa: BLE001
        return []
    return (r.get("text") or "").lower().split()


def _separa(mono: np.ndarray, tag: str) -> list[Path]:
    """Roda o MossFormer2_SS_16K na venv de bancada e devolve os dois fluxos.

    A separação precisa de torch e do modelo vendorizado; o runner roda na .venv-mlx
    (onde está o Whisper do app). Mesmo padrão do extrator ECAPA: processo filho."""
    pasta = comum.RESULTADOS / "_sep" / tag
    if pasta.exists():
        shutil.rmtree(pasta)
    pasta.mkdir(parents=True)
    entrada = comum.RESULTADOS / "_sep" / f"{tag}.wav"
    sf.write(str(entrada), mono, comum.SR, subtype="PCM_16")
    r = subprocess.run([str(VENV), str(Path(__file__).with_name("separa.py")),
                        str(entrada), "--saida", str(pasta)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"separação falhou: {r.stderr.strip()[-400:]}")
    return sorted(pasta.glob("fluxo*.wav"))


def _wer(ref: str, x: np.ndarray, sr: int, idioma: str, tag: str = "wer") -> dict:
    """WER do alvo + vazamento de terceiros, do mesmo alinhamento."""
    import metricas

    alvo = ref.lower().split()
    obtido = _transcreve(x, sr, idioma, tag)
    if not alvo or not obtido:
        return {"wer": float("nan"), "vazamento": float("nan")}
    a = metricas.alinhamento(obtido, alvo)
    return {"wer": a["wer"], "vazamento": metricas.vazamento(obtido, alvo),
            "ins": a["ins"], "sub": a["sub"], "del": a["del"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sirs", default="0,5,10")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--seg", type=float, default=20.0,
                    help="duração-alvo de cada lado, montada juntando clipes do falante")
    ap.add_argument("--separa", action="store_true",
                    help="liga o MossFormer2_SS_16K (2 fluxos, escolhidos pelo ECAPA)")
    ap.add_argument("--corte-abs", dest="corte_abs", type=float, default=None,
                    help="escore mínimo absoluto para a janela sobreviver "
                         "(sem isso o corte é relativo ao melhor bloco)")
    ap.add_argument("--somente-limpo", action="store_true",
                    help="só o WER do alvo limpo (âncora das metas do card)")
    a = ap.parse_args()
    comum.garante()
    por = _falantes()
    falantes = [f for f, ps in sorted(por.items()) if len(ps) >= 3]
    eca = _eca()
    modelo = a.separa
    linhas = []
    for f_alvo in falantes[: a.n]:
        f_conc = next((f for f in reversed(falantes) if f != f_alvo), None)
        if f_conc is None:
            continue
        xa, ref = junta(por[f_alvo], a.seg)
        xc, _ = junta(por[f_conc], a.seg)
        if min(xa.size, xc.size) < 3 * comum.SR:
            print(f"falante {f_alvo}: material curto — pulado", flush=True)
            continue
        refwav = comum.RESULTADOS / f"_ref_{f_alvo}.wav"
        if a.somente_limpo:
            m = _wer(ref, xa, comum.SR, "en", tag=f"limpo-{f_alvo}")
            linhas.append({"alvo": f_alvo, "modo": "limpo", "wer_limpo": m["wer"],
                           "vaz_limpo": m["vazamento"]})
            print(f"falante {f_alvo} LIMPO: WER {m['wer']:.2f}", flush=True)
            continue
        sf.write(str(refwav), xa, comum.SR, subtype="PCM_16")
        for sir in (float(s) for s in a.sirs.split(",")):
            base = f"fal{f_alvo}-sir{sir:g}"
            for modo, mono in (("sobreposto", mistura(xa, xc, sir)),
                               ("alternado", alternado(xa, xc, sir))):
                sel, escores = seleciona(mono, comum.SR, xa, eca, tag=f"{base}-{modo}",
                                         corte_abs=a.corte_abs)
                linha = {"alvo": f_alvo, "concorrente": f_conc, "sir": sir, "modo": modo,
                         "reducao": round(1 - sel.size / mono.size, 3)}
                for chave, audio, suf in (("mistura", mono, "mix"), ("selecao", sel, "sel")):
                    m = _wer(ref, audio, comum.SR, "en", tag=f"{base}-{modo}-{suf}")
                    linha[f"wer_{chave}"] = m["wer"]
                    linha[f"vaz_{chave}"] = m["vazamento"]
                if modelo:
                    fluxos = _separa(mono, f"{base}-{modo}")
                    embs = eca([refwav, *fluxos])
                    e_ref = embs[0]
                    pont = [float(e @ e_ref) for e in embs[1:]]
                    i = int(np.argmax(pont))
                    sep = _le(fluxos[i])
                    linha["sep_escores"] = [round(p, 3) for p in pont]
                    linha["si_sdri"] = round(metricas.si_sdri(sep, xa, mono), 3)
                    m = _wer(ref, sep, comum.SR, "en", tag=f"{base}-{modo}-sep")
                    linha["wer_separacao"] = m["wer"]
                    linha["vaz_separacao"] = m["vazamento"]
                    # separação E veto no fluxo escolhido, com corte ANCORADO no
                    # locutor: só cai a janela que ainda ficou longe da referência
                    sel3, _ = seleciona(sep, comum.SR, xa, eca, tag=f"{base}-{modo}-sepsel",
                                        corte_abs=a.corte_abs)
                    m = _wer(ref, sel3, comum.SR, "en", tag=f"{base}-{modo}-sepsel")
                    linha["wer_sep_selecao"] = m["wer"]
                    linha["vaz_sep_selecao"] = m["vazamento"]
                linhas.append(linha)
                # sanidade: as duas vozes TÊM de estar na mistura. Sem isso um
                # "0 % de corte" passa silencioso como resultado do método.
                abertura = (max(escores) - min(escores)) if escores else 0.0
                if len(escores) < 2 or abertura < 0.12:
                    linha["suspeita"] = f"escores sem as duas vozes (abertura {abertura:.2f})"
                    print(f"  !! {base}-{modo}: suspeita — escores "
                          f"{[round(s, 2) for s in escores]}", flush=True)
                print(f"SIR {sir:.0f} falante {f_alvo} {modo.upper()}: "
                      f"WER mistura {linha['wer_mistura']:.2f} "
                      f"-> seleção {linha['wer_selecao']:.2f} "
                      f"(cortou {linha['reducao']:.0%})"
                      + (f" | separação {linha['wer_separacao']:.2f}"
                         f" (SI-SDRi {linha['si_sdri']:+.1f} dB, escolha {linha['sep_escores']})"
                         f" -> sep+sel {linha['wer_sep_selecao']:.2f}"
                         if modelo else ""), flush=True)
    destino = comum.RESULTADOS / ("limpo.json" if a.somente_limpo else
                                 "alvo_sep.json" if a.separa else "alvo.json")
    destino.write_text(json.dumps(linhas, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
