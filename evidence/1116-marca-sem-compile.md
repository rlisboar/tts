# #1116 — marca d'água sem `torch.compile` (1º trecho quente)

## Diagnóstico

O AudioSeal 0.2 (`audioseal/libs/moshi`) embrulha em `torch_compile_lazy`
(`torch.compile` na 1ª chamada) três coisas: `SEANetEncoder.forward`,
`SEANetDecoder.forward` e `apply_rope`. A chave `NO_TORCH_COMPILE` é lida no
IMPORT do pacote. O dynamo guarda o formato exato. Com `TORCH_LOGS=recompiles`,
o 1º trecho real mostra:

```
Recompiling function forward in audioseal/libs/moshi/modules/conv.py:248
  - 1/0: tensor 'x' size mismatch at index 1. expected 1, actual 32
Recompiling function forward in audioseal/libs/moshi/modules/seanet.py:93
```

O que dispara: canais por camada da conv (1 → 32 …), comprimento do trecho e
instância (o DETECTOR nunca era aquecido; o `embute_conferido` o chama em todo
trecho). Taxa e dtype não entram: o worker já desce tudo para 16 kHz float32.
Formatos reais: Live = PCM 24 kHz mono em trechos de tamanho variável; `/api/tts`
e `/v1` = wav do trecho (24 kHz, segundos); detecção = 16 kHz. Como cada
comprimento novo é um formato novo, aquecer "o formato real" não fecha a conta.
O compile é que sobra.

## Conserto

`marca_worker.py`: `os.environ.setdefault("NO_TORCH_COMPILE", "1")` antes do
import do audioseal, e o aquecimento passa a incluir `detector.detect_watermark`.
Intensidade, detector, alvo e `live_marca_primeiro_trecho` não mudam.

## Medida local (M3, load 8–10, worker real, n=5 processos por modo)

Sequência: marca 24 kHz de 1,3 / 2,1 / 0,7 / 1,3 / 4,8 s, detecta 16 kHz 3 s,
marca 16 kHz 1 s, marca 24 kHz 3,33 s. Script: `/tmp/b1116/bench.py`
(fora do repo).

| modo | `pronto` (s) | 1º trecho 1,3 s (ms) | 4º trecho, mesmo 1,3 s (ms) | prob da marca |
|---|---|---|---|---|
| compile (main) | 5,85–8,55 | 1144 / 1207 / 1188 / 1389 / 1544 | 112–129 | 1,0 |
| eager, sem aquecer o detector | 1,63–1,87 | 99–120 | 98–119 | 1,0 |
| **eager + detector aquecido (este commit)** | **1,35–1,48** (aquecido 627–697 ms) | **87–97** | 82–102 | 1,0 |

No regime, o eager empata com o compile (ex.: 4,8 s → 283–332 contra 317–375 ms).

## Mini

Pendente. Fica no card task_3d313618, em série com o speech-pipeline.
