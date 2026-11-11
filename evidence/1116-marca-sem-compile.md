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

`marca_worker.py`: `os.environ.setdefault("NO_TORCH_COMPILE", "1")` dentro de
`main()`, antes do import do audioseal, e o aquecimento passa a incluir
`detector.detect_watermark`. A chave fica restrita ao processo do worker. O app
só sobe esse processo como filho, e quem importa o módulo (os testes do
`embute_conferido`) não a herda; há uma célula que verifica isso. O único leitor
da chave no venv é `audioseal/libs/moshi/utils/compile.py`.
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

Nota: com a compilação ligada, o próprio dynamo avisa
`hit config.recompile_limit (8)` em `conv.py:248` e em `seanet.py:93`. O
`forward` da conv é a mesma função para todas as camadas, e cada contagem de
canais vira uma entrada de cache. Por isso parte das camadas já rodava em eager
depois do limite.

## Mini (7cb4618 no ar, 2026-11-10 ~21:39 no relógio do mini)

Condições:
- só o worker isolado, em `/tmp` (`mktemp`, apagado no fim), com o venv e os
  pesos da produção somente para leitura (`sha256sums` 27/27 depois);
- sem instância e sem porta;
- a 7860 intocada: `/health` 200, 1 uvicorn;
- load 0,5–1,1, swap 1,28/2 GB.

**Carga na 7860 durante a medida:** não havia narração gerando. O uvicorn ficou
em 2–7 % de CPU; o log mostrava só `GET /api/tts/jobs/<id>` (o cliente
consultando 3 jobs em polling), sem nenhum `POST` de geração.

`main_worker.py` = o `marca_worker.py` do mini (sha256 `19957c76…`, igual ao
`76fbe8c`). `fix_worker.py` = este commit. Modos alternados, n=5 cada.

| modo | `pronto` (s) | 1º trecho 24 kHz 1,3 s (ms) | 2,1 s | 0,7 s | 1,3 s (repetido) | 4,8 s | det 3 s | 16 kHz 1 s | 3,33 s |
|---|---|---|---|---|---|---|---|---|---|
| compile (main) | 3,61–4,50 | **696 / 713 / 813 / 703 / 704** | 103–110 | 34–36 | 61–68 | 243–247 | 55–57 | 49–51 | 164–178 |
| **eager (#1116)** | **1,07–1,16** | **68 / 72 / 70 / 66 / 73** | 107–117 | 35–38 | 66–71 | 264–268 | 52–57 | 49–54 | 170–185 |

- O 1º trecho do compile (p50 704 ms) reproduz os ~739 ms da H5 do #1022.
  Com o eager ele fica em **70 ms**, dentro da meta de ≤ 100 ms e na faixa dos
  trechos seguintes.
- **Custo no regime, no mini:** o eager sai 3–8 % mais lento por trecho que o
  compile quente. Em ms: +1–2 no de 0,7 s, +5 no de 1,3 s, +20 no de 4,8 s.
  O compile quente só ganha quando o formato do trecho já foi visto. Trecho
  do Live com 58–71 ms no regime fica com ~+4 ms.
- `pronto`: 3,6–4,5 s → **1,1 s**.

## Saída compile × eager: mesma mensagem fixa, 3 trechos de 16 kHz (`/tmp/b1116/cmp.py`)

| trecho | igual em bits (f32) | max abs f32 | amostras PCM16 diferentes | prob compile / eager | mensagem de 16 bits decodificada |
|---|---|---|---|---|---|
| mini 0,7 s | não | 4,8e-8 | 1 de 11 200 (1 LSB) | 1,0 / 1,0 | idêntica nos dois |
| mini 1,3 s | não | 3,9e-8 | 3 de 20 800 | 1,0 / 1,0 | idêntica nos dois |
| mini 3,33 s | não | 2,2e-8 | 2 de 53 280 | 1,0 / 1,0 | idêntica nos dois |
| MBP (os mesmos 3) | não | 2,0e-8–3,0e-8 | 3 / 3 / 2 | 1,0 / 1,0 | idêntica nos dois |

As saídas não são iguais bit a bit: o inductor reordena somas em float32 e a
diferença máxima é de ~1 ulp. Em PCM16, isso muda no máximo 3 amostras em 1 LSB.
A detecção é equivalente: prob 1,0 e a mesma mensagem decodificada nos dois
caminhos.
