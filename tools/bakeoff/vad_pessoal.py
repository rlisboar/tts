"""VAD PESSOAL: o turno do Live só abre se a voz for do dono (#532).

Pergunta medida: com a TV (ou outra pessoa) falando sem parar por 10 min, quantos
turnos FALSOS o portão abre? E com o dono falando, quantos turnos ele PERDE? As
duas coisas andam juntas: baixar o limiar reduz turno falso perdido e aumenta
turno falso aberto; o relatório mostra a curva inteira para a escolha ser explícita.

Regra implementada (é a do card, "VAD comum + verificação em janela deslizante"):
um VAD de energia abre candidato, e cada janela candidata só vira turno se o ECAPA
contra o cadastro do dono passar do limiar em `--confirmacoes` janelas seguidas.
O VAD de energia entra porque sem ele uma pausa longa com ruído de fundo
pontuaria parecido com fala.

Uso:
    python tools/bakeoff/vad_pessoal.py --minutos 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alvo  # noqa: E402
import comum  # noqa: E402

PASSO_S = 0.25
JANELA_S = 1.0


def _fala_continua(arquivos: list[Path], minutos: float) -> np.ndarray:
    """Emenda clipes do falante até encher `minutos`, dando a volta se precisar."""
    x, _ = alvo.junta(arquivos, minutos * 60)
    if not x.size:
        return x
    falta = int(minutos * 60 * comum.SR) - x.size
    while falta > 0:
        pedaco = x[: min(falta, x.size)]
        x = np.concatenate([x, pedaco])
        falta -= pedaco.size
    return x


def pontua(x: np.ndarray, ref: np.ndarray, eca, pasta: Path) -> list[float]:
    """Escore do ECAPA de cada janela deslizante contra a referência do dono."""
    pasta.mkdir(parents=True, exist_ok=True)
    passo = int(PASSO_S * comum.SR)
    janela = int(JANELA_S * comum.SR)
    caminhos = [pasta / "ref.wav"]
    sf.write(str(caminhos[0]), ref, comum.SR, subtype="PCM_16")
    for i, ini in enumerate(range(0, max(x.size - janela + 1, 1), passo)):
        p = pasta / f"j{i}.wav"
        sf.write(str(p), x[ini: ini + janela], comum.SR, subtype="PCM_16")
        caminhos.append(p)
    if len(caminhos) == 1:
        return []
    embs = eca(caminhos)
    e_ref = embs[0]
    return [float(e @ e_ref) for e in embs[1:]]


def energia(x: np.ndarray) -> list[float]:
    """RMS por janela — o VAD comum. Normalizado pelo percentil 90 da própria
    gravação para o limiar não depender do ganho do microfone."""
    passo = int(PASSO_S * comum.SR)
    janela = int(JANELA_S * comum.SR)
    vals = [float(np.sqrt(np.mean(x[i: i + janela].astype(np.float64) ** 2)))
            for i in range(0, max(x.size - janela + 1, 1), passo)]
    if not vals:
        return []
    p90 = float(np.percentile(vals, 90)) or 1.0
    return [v / p90 for v in vals]


def turnos(escores: list[float], ener: list[float], limiar: float,
           confirmacoes: int, piso_energia: float = 0.15) -> list[int]:
    """Início (em janelas) de cada turno aberto.

    É CONTAGEM DE CONFIRMAÇÃO, não histerese: exige `confirmacoes` janelas seguidas
    acima do limiar para abrir, e basta uma janela abaixo para zerar a contagem.
    Não há limiar de saída separado — o nome "histerese" prometia mais do que há
    (achado P3 do gate), e a métrica de abertura não muda com um ou com outro."""
    abertos, seguidos = [], 0
    for i, (e, en) in enumerate(zip(escores, ener, strict=False)):
        if en >= piso_energia and e >= limiar:
            seguidos += 1
            if seguidos == confirmacoes:
                abertos.append(i - confirmacoes + 1)
        else:
            seguidos = 0
    return abertos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutos", type=float, default=10.0)
    ap.add_argument("--limiares", default="0.20,0.25,0.30,0.35,0.40,0.45,0.50,0.55,0.60")
    ap.add_argument("--confirmacoes", type=int, default=2)
    ap.add_argument("--cadastros", default="10,30,60",
                    help="durações de cadastro do dono, em segundos")
    a = ap.parse_args()
    comum.garante()
    por = alvo._falantes()
    falantes = [f for f, ps in sorted(por.items()) if len(ps) >= 3]
    f_alvo, f_conc = falantes[0], falantes[-1]
    eca = alvo._eca()

    dono = _fala_continua(por[f_alvo], a.minutos)
    terceiro = _fala_continua(por[f_conc], a.minutos)
    ruidoso = _fala_continua(por[f_conc], a.minutos)
    ruidoso = (ruidoso * 0.35 + np.random.default_rng(3).normal(
        0, 0.01, ruidoso.size).astype(np.float32)).astype(np.float32)

    ref, _ = alvo.junta(por[f_alvo], 20)   # cadastro do dono, 20 s
    casos = {"so_dono": dono, "so_terceiro": terceiro, "terceiro_com_ruido": ruidoso}
    pontos, eners = {}, {}
    for nome, x in casos.items():
        eners[nome] = energia(x)
        pontos[nome] = pontua(x, ref, eca, comum.RESULTADOS / "_vad" / nome)
        print(f"{nome}: {len(pontos[nome])} janelas, escore mediano "
              f"{np.median(pontos[nome]):.3f}", flush=True)

    linhas = []
    for lim in (float(s) for s in a.limiares.split(",")):
        # turnos do dono: cada 10 min de fala contínua vira ~1 turno longo; conta
        # PEDAÇOS (o portão não fecha enquanto o escore sustenta) e a fração do
        # tempo do dono que passou no portão
        tp = turnos(pontos["so_dono"], eners["so_dono"], lim, a.confirmacoes)
        fp = turnos(pontos["so_terceiro"], eners["so_terceiro"], lim, a.confirmacoes)
        fr = turnos(pontos["terceiro_com_ruido"], eners["terceiro_com_ruido"], lim,
                    a.confirmacoes)
        cobertura = float(np.mean([e >= lim for e in pontos["so_dono"]]))
        linha = {"limiar": lim, "confirmacoes": a.confirmacoes,
                 "turnos_falsos_10min": round(len(fp) / a.minutos * 10, 2),
                 "turnos_falsos_ruido_10min": round(len(fr) / a.minutos * 10, 2),
                 "cobertura_dono": round(cobertura, 3),
                 "turnos_dono": len(tp)}
        linhas.append(linha)
        print(f"limiar {lim:.2f}: falso {linha['turnos_falsos_10min']:.1f}/10min "
              f"(com ruído {linha['turnos_falsos_ruido_10min']:.1f}), "
              f"cobertura do dono {cobertura:.0%}", flush=True)

    # curva por duração de cadastro, no limiar do meio da faixa
    lim_curva = 0.35
    curva = []
    for dur in (float(s) for s in a.cadastros.split(",")):
        ref, _ = alvo.junta(por[f_alvo], dur)
        e_dono = pontua(dono, ref, eca, comum.RESULTADOS / "_vad" / f"cad{dur:.0f}-dono")
        e_ter = pontua(terceiro, ref, eca, comum.RESULTADOS / "_vad" / f"cad{dur:.0f}-ter")
        linha_c = {"cadastro_s": dur,
                   "mediana_dono": round(float(np.median(e_dono)), 3),
                   "mediana_terceiro": round(float(np.median(e_ter)), 3),
                   "cobertura_dono": round(float(np.mean([e >= lim_curva for e in e_dono])), 3),
                   "turnos_falsos_10min": round(len(turnos(
                       e_ter, eners["so_terceiro"], lim_curva, a.confirmacoes))
                       / a.minutos * 10, 2)}
        curva.append(linha_c)
        print(f"cadastro {dur:.0f} s: mediana dono {linha_c['mediana_dono']:.3f} x "
              f"terceiro {linha_c['mediana_terceiro']:.3f}, cobertura "
              f"{linha_c['cobertura_dono']:.0%}, falso "
              f"{linha_c['turnos_falsos_10min']:.1f}/10min", flush=True)

    (comum.RESULTADOS / "vad_pessoal.json").write_text(json.dumps(
        {"alvo": f_alvo, "concorrente": f_conc, "minutos": a.minutos,
         "linhas": linhas, "curva_cadastro": curva,
         "escores": {k: [round(v, 3) for v in vs] for k, vs in pontos.items()}},
        indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
