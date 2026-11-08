# #876 — STT do 1º turno frio: o prewarm do Live o deixa igual ao quente (0,23 s × 1,25 s sem)

Data: 2026-11-08. Máquina: MacBook Pro (dev), **não** o mini. SHA `0343e36` (o prewarm
do `978b50a` já está nele). Script: `evidence/876-stt-frio-prewarm.py`. Sem servidor,
sem LLM e sem TTS; produção intocada.

## O que isola

Cada medida é um **processo novo** (`import app`). Depois dele, opcionalmente o
prewarm, e então dois `live_pipeline._stt_julga` seguidos num turno de fala real
de 2,5 s (`voices/5d214dbbff.wav`, −26 dBFS). O caminho é o real: Silero, o Whisper
do Live e o juiz. O repo do Live resolvido é `mlx-community/whisper-large-v3-turbo`.

- `off`: sem prewarm.
- `on`: só `_stt_aquece` (o Whisper do Live, dentro de `_stt_do_live()`).
- `full`: `_vad_load()` + `_stt_aquece`, o lado STT do `_prewarm_app` de produção.

## Resultado (3 processos por modo, alternados)

| modo | prewarm (s) | 1º turno (s) | 2º turno, quente (s) |
|---|---|---|---|
| off | — | 1,281 / 1,269 / 1,226 | 0,237 / 0,236 / 0,235 |
| on | 0,777 / 0,658 / 0,664 | 1,021 / 0,743 / 0,757 | 0,246 / 0,239 / 0,234 |
| **full** | 1,164 / 1,193 / 1,242 | **0,226 / 0,228 / 0,282** | 0,236 / 0,229 / 0,304 |

Antes da série, o 1º processo da máquina, com o cache de disco frio, mediu
`off` = **17,47 s** no 1º turno (a carga do turbo do disco) e 0,256 s no 2º.

## O que isto fecha

1. Com o prewarm de produção, o 1º turno frio custa no STT o mesmo que um turno quente:
   ~1 s de ganho por sessão nova, e ~17 s logo depois de um boot com o cache frio. Esse
   custo vai para o handshake.
2. A metade que o `_stt_aquece` sozinho não cobre (~0,5 s) é a carga do Silero. O
   `_prewarm_app` já a faz (`app._vad_load()` antes do aquece), e por isso o `full`
   empata com o quente.
3. Se o 1º turno do mini ainda sai mais lento que os seguintes, a diferença **não**
   está no STT. Sobram o 1º chunk do TTS (forma ainda não compilada) e o 1º token do
   LLM. O S2 frio do #875 deve ser decomposto por `latency` (`llm_ttft` × `pos_llm`)
   antes de mexer no STT.

## Limites

- Os números absolutos são desta máquina. No mini (16 GB), o estágio `stt` sai ~4×
  maior (`evidence/876-stt-live.md`). O que se transfere é a proporção:
  frio sem prewarm ≈ 5× o quente, e com prewarm = quente.
- Sem TTS em paralelo. A disputa de GPU com o OmniVoice está medida à parte no
  `876-stt-live.md` (p90 do turbo de 252–313 ms).
- Se o dono falar ANTES do prewarm terminar (handshake de ~1,2 s no lado STT), o
  1º turno espera o `_stt_lock`. Isso não foi medido aqui.
