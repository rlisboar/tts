"""SANIDADE DE INTEGRAÇÃO dos extratores de identidade (#521, condição (a) do PM).

Por que existe: o Resemblyzer (2019) vencer o CAM++ contraria a literatura, e
depois de dois bugs de pré-processamento a hipótese mais provável era a
INTEGRAÇÃO, não o modelo. Antes de qualquer número entrar na tabela, o extrator
tem de reproduzir o par de exemplo do próprio autor — e o ECAPA é o recomendado,
então é quem mais precisa da prova.

Os pares são os do cartão oficial do SpeechBrain, com a expectativa publicada
junto: `spk1_snt1` × `spk1_snt2` é o MESMO locutor (o cartão diz predição 1) e
`spk1_snt1` × `spk2_snt1` são locutores DIFERENTES (predição 0). Se a integração
estiver errada (taxa de amostragem, decodificação, normalização, canal), o par
positivo afunda e o negativo sobe.

Três checagens, todas necessárias:
1. o par positivo passa e o negativo fica abaixo — a função discriminativa existe;
2. o nosso caminho de codificação dá o MESMO número que o `verify_files` oficial —
   é a igualdade que pega regressão de integração, não a coincidência de limiar;
3. controle com o nosso corpus (outro par de falantes diferentes).

ARMADILHA registrada: `example1.wav`/`example2.flac` do repositório NÃO são o par
do cartão — são falantes diferentes (0,18, predição 0). Quem usa esse par como
positivo conclui que a integração está quebrada.

Uso:
    /tmp/tts-bakeoff/venv312/bin/python tools/bakeoff/sanidade.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alvo  # noqa: E402
import comum  # noqa: E402

GH = ("https://raw.githubusercontent.com/speechbrain/speechbrain/develop"
      "/tests/samples/ASR")
POSITIVO = ("spk1_snt1.wav", "spk1_snt2.wav")
NEGATIVO = ("spk1_snt1.wav", "spk2_snt1.wav")
#: o cartão publica predição 1/0; o SpeechBrain 1.1.2 devolve 0,73 e 0,08 hoje.
#: As bandas são largas de propósito — o que se fixa é a separação, não o número
#: exato de uma versão
MIN_POS, MAX_NEG, TOLERANCIA = 0.55, 0.30, 0.01


def _baixa() -> Path:
    pasta = comum.BASE / "modelos" / "ecapa-exemplos"
    pasta.mkdir(parents=True, exist_ok=True)
    for nome in set(POSITIVO) | set(NEGATIVO):
        destino = pasta / nome
        if not destino.exists() or destino.stat().st_size < 1000:
            subprocess.run(["curl", "-sSL", "-o", str(destino), f"{GH}/{nome}"],
                           check=True)
    return pasta


def _oficial(pasta: Path, a: str, b: str) -> float:
    """Escore pelo caminho OFICIAL (`verify_files`), como referência."""
    codigo = (
        "import os,sys;"
        "from speechbrain.inference.speaker import SpeakerRecognition;"
        "r=SpeakerRecognition.from_hparams(source=os.environ['TTS_ECAPA_DIR'],"
        "savedir=os.environ['TTS_ECAPA_DIR'],run_opts={'device':'cpu'});"
        "print(float(r.verify_files(sys.argv[1],sys.argv[2])[0]))")
    r = subprocess.run([str(alvo.VENV), "-c", codigo, str(pasta / a), str(pasta / b)],
                       capture_output=True, text=True,
                       env={**os.environ, "TTS_ECAPA_DIR": str(comum.MODELO_ECAPA)})
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return float(r.stdout.strip())


def main() -> int:
    comum.garante()
    falhas: list[str] = []
    pasta = _baixa()
    eca = alvo._eca()

    def coss(a: str, b: str) -> float:
        e = eca([pasta / a, pasta / b])
        return float(e[0] @ e[1])

    pos = coss(*POSITIVO)
    neg = coss(*NEGATIVO)
    print(f"par do cartão, MESMO locutor (esperado > {MIN_POS}): {pos:.4f}", flush=True)
    print(f"par do cartão, DIFERENTES (esperado < {MAX_NEG}): {neg:.4f}", flush=True)
    if pos < MIN_POS:
        falhas.append(f"positivo {pos:.3f} < {MIN_POS}")
    if neg > MAX_NEG:
        falhas.append(f"negativo {neg:.3f} > {MAX_NEG}")
    if neg >= pos:
        falhas.append("negativo não ficou abaixo do positivo")

    ref_pos = _oficial(pasta, *POSITIVO)
    print(f"mesmo par pelo caminho OFICIAL do SpeechBrain: {ref_pos:.4f}", flush=True)
    if abs(ref_pos - pos) > TOLERANCIA:
        falhas.append(f"nosso caminho ({pos:.3f}) diverge do oficial ({ref_pos:.3f}) "
                      f"acima de {TOLERANCIA}: integração não é equivalente")

    por = alvo._falantes()
    falantes = [f for f, ps in sorted(por.items()) if len(ps) >= 2]
    import soundfile as sf
    tmp = comum.RESULTADOS / "_sanidade"
    tmp.mkdir(parents=True, exist_ok=True)
    a, b = falantes[0], falantes[-1]
    for nome, fonte in (("a.wav", por[a]), ("b.wav", por[b])):
        x, _ = alvo.junta(fonte, 4)
        sf.write(str(tmp / nome), x, comum.SR, subtype="PCM_16")
    e = eca([tmp / "a.wav", tmp / "b.wav"])
    corpus_neg = float(e[0] @ e[1])
    print(f"controle no nosso corpus ({a} x {b}): {corpus_neg:.4f}", flush=True)
    if corpus_neg > MAX_NEG:
        falhas.append(f"controle do corpus {corpus_neg:.3f} > {MAX_NEG}")

    print("SANIDADE: " + ("FALHOU — " + "; ".join(falhas) if falhas else "ok"), flush=True)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
