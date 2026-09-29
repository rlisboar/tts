# #216 — DECISÃO: as alavancas do #167 nascem LIGADAS (e por quê, com número)

**Decisão:** `TTS_LIVE_BARGE_JANELA_TURNO` e `TTS_LIVE_PLAYBACK_DURACAO` passam a
`1` por default (app e `Config`); `TTS_LIVE_ECO_SO_TOCANDO` fica em `0`.

**Critério declarado ANTES de medir** (comentário na task, 18:55): virar default só
se a COMBINAÇÃO não regredir NENHUM dos dois sentidos contra o baseline da mesma
sessão; e com `tests/live_ui.sh` verde (o corte do barge é exigência dura, #179).

Resultado: a combinação A+B (as duas alavancas que resolvem o vão) passa o critério
— cria o comportamento no vão (0/20 → 20/20) e não regride o sentido do eco. A
terceira alavanca (C) reprova com folga no sentido do eco (todas as células com C
ficaram em 36-45 cortes falsos contra 20-35 das sem C) e não entrega nada medível
no vão nem no sentido verdadeiro, então fica fora. O vão em escala cheia, medido
duas vezes em cada célula, NÃO separa A+B de A+B+C — a separação principal é do
eco, e é essa a razão da decisão (não uma suposta derrota de C no vão, que a
repetição não confirmou).

ADENDO (rodada posterior, `vao-quieto-*`): no vão com fala BAIXA — o caso para o
qual C foi inventada — A+B faz **20/20** e A+B+C faz **13/20**. Ou seja, no único
regime em que C tinha vantagem teórica (limiar de ocioso no vão) ela também custa,
pelo mesmo mecanismo do dreno do backlog. Ver a seção "Fala BAIXA no vão".

## Protocolo

- `tests/live_barge_rep.sh`, `REP=20` por célula, três regimes:
  - **`vao`** (o defeito do #167): injeta 900 ms DEPOIS do `turn_complete`, com o
    cliente ainda com áudio na fila. É a célula que decide — no modo padrão o
    harness injeta com playback ativo e satura em 20/20 em TODAS as configurações.
  - **`eco`** (`MIC_FILE=1`): o mic falso toca o wav em loop; não há humano, então
    TODO corte é falso. Métrica: `interrupted` cumulativo (menor é melhor).
  - **`true`** (sem `MIC_FILE`): barge verdadeiro; satura perto do teto.
- Alavancas: `A=playback_por_duracao` · `B=barge_janela_turno` ·
  `C=eco_so_tocando`. Células: baseline, A, B, C, AB, AC, BC, ABC.
- Scripts: `evidence/216-combos*.sh` (matriz) e `evidence/216-decisivo.sh` (as
  células que faltaram, uma shell só e com espera longa — os 4 shells concorrentes
  da matriz morriam na trava de modelo com arquivo vazio).
- Leitura da tabela: `./.venv-mlx/bin/python evidence/216-analise.py`.

## Resultado (as rodadas que valem)

| regime | baseline | A | C | AB | ABC |
| --- | --- | --- | --- | --- | --- |
| vão (`ok`, alvo) | **0/20** | — | — | **20/20** e 20/20 (2 rodadas) | 15/20 e 20/20 (2 rodadas) |
| vão com fala BAIXA (`AMP=0.2`, `ok`) | — | — | — | **20/20** | 13/20 |
| eco (`interrupted`, menor melhor) | 34 e 20 (2 rodadas) | 35 | 36 | **20** | 44 e 45 (2 rodadas) |
| verdadeiro (`ok`) | 20/20 | 19/20 | 20/20 | 20/20 | 18/20 |

As células de eco com C que completam o quadro: AC=40 e BC=38 (rodadas de 17:03 e
17:06) — é o que permite atribuir o custo a C, e não a A ou B.

- A+B **cria** o comportamento que o ticket pede: no vão, 0/20 → 20/20, e a
  repetição (17:11) confirma 20/20.
- No vão A+B+C **não perde de forma limpa**: deu 15/20 numa rodada e 20/20 na
  repetição (17:09). Ou seja, o vão sozinho NÃO separa A+B de A+B+C — quem separa
  é o sentido do eco.
- No eco a separação é por PRESENÇA de C, e é robusta a carga: **toda** célula com
  C ficou em 36-45 cortes falsos (C=36, AC=40, BC=38, ABC=44 e 45) e **toda**
  célula sem C ficou em 20-35 (baseline=34 e 20, A=35, A+B=20). A+B é o MÍNIMO
  global — mas o número que sustenta a decisão é "C custa ≥36", não "A+B melhora
  o baseline" (o baseline tem duas leituras, 34 e 20, e só uma rodada de controle
  pareada resolve qual é a dele).
- No sentido verdadeiro tudo empata dentro da variância (17-20/20).

## Por que C perde (mecanismo, não só o número)

1. `_playback_restante` acumula, no ENVIO, a duração real de cada chunk (A) e é
   escoado 1 frame por frame (o cliente toca em 1x) — é a estimativa do áudio que
   ainda está no alto-falante.
2. C muda QUANDO esse escoamento acontece (`live_turns.py`: o dreno só roda com
   `not _turno_aberto` **ou** com `eco_so_tocando`). Com C o backlog escoa durante
   o turno, então ele chega zerado ao `turn_complete` e a janela fecha — justo no
   ponto em que o harness (e o cliente) ainda têm áudio para interromper.
3. C também baixa o LIMIAR no vão (regime de ocioso em vez de eco). No sentido do
   eco isso é o que dispara o barge falso em série: 44 cortes contra 20.
4. Assinatura das 5 falhas de A+B+C no vão (rodada de 16:44): `speech_start` SEM
   barge e estado `falando` ANTES da injeção — o mic abriu turno no vão e o áudio
   do assistente (ainda na fila do cliente) seguiu tocando por cima. A repetição
   de 17:09 não reproduziu as 5 falhas, então a taxa real de A+B+C no vão está
   entre 15/20 e 20/20 — não é um empate com A+B, mas também não é a derrota
   limpa que a primeira rodada sugeria.

## Fala BAIXA no vão: o caso em que C deveria ganhar — e não ganha

C existe para um caso real: fala BAIXA no vão. Com C o limiar do vão é o de
ocioso (~−53 dBFS) e não o do eco (~−24 dBFS); a injeção em escala cheia passa dos
dois, então é a célula `vao-quieto-*` (`AMP=0.2`, ~−31 dBFS RMS) que mediria a
diferença. Ela foi rodada e o resultado é o CONTRÁRIO do esperado:

| célula (`MODO=vao AMP=0.2`, REP=20) | barge |
| --- | --- |
| A+B | **20/20** |
| A+B+C | **13/20** |

Ou seja: com fala baixa, A+B já entrega o vão inteiro e C **custa 7 pontos** — a
mesma direção da injeção em escala cheia (15 × 20), pelo mesmo mecanismo do dreno
do backlog. A leitura inicial ("C entrega fala baixa no vão") era plausível pelo
limiar e NÃO se confirmou na medição; o que sobra a favor de C é só o limiar de
ocioso no vão, que não se converteu em barge a mais em nenhum dos dois níveis.

Cuidado com as rodadas: `evidence/216-vao-quieto-ab.txt` e `-abc.txt` têm só a
linha da trava de modelo (morreram esperando; o 0/20 delas é espúrio) — as válidas
são as repetições `...abB` (20/20, 17:32) e `...abcB` (13/20, 17:37), de
`evidence/216-limpo.sh`. São uma rodada cada: a célula decisiva continua sendo o
eco, onde a separação por presença de C é robusta e grande.

## O que fica fora e como voltar atrás

- C continua disponível: `TTS_LIVE_ECO_SO_TOCANDO=1`. Se o dono reclamar de fala
  baixa não interromper no vão, é a primeira alavanca a tentar.
- Voltar ao comportamento antigo: `TTS_LIVE_BARGE_JANELA_TURNO=0
  TTS_LIVE_PLAYBACK_DURACAO=0`.
- Guard: `tests/test_api.py::test_engine_do_live_nasce_com_as_alavancas_do_167_ligadas`
  pina o default (um `os.environ.get(..., "0")` reintroduzido não passa silencioso).

## Limites desta medição (para quem for revisar)

- O harness não tem caminho acústico real: o "eco" é o mic falso tocando o wav.
- **A máquina esteve CARREGADA durante toda a matriz** (vários agentes rodando
  `pytest`/suítes em paralelo) e o harness é sensível a carga. As células com C no
  eco (36-45) são robustas porque se separam por margem grande; a comparação
  baseline×A+B no eco NÃO é: o baseline deu 34 numa janela e 20 em outra. Por isso
  o número que sustenta a decisão é a presença de C, não o ganho sobre o baseline.
- `MODO=vao` injeta num ponto fixo (900 ms depois do `turn_complete`); o vão DURANTE
  a geração não é injetado separadamente — é o que o `AMP` tenta cobrir de outro
  jeito (limiar), não a janela.
- Achado colateral: quando o turno do usuário abre com o áudio do assistente ainda
  na fila do cliente e a janela já fechou, o playback NÃO é derrubado (o áudio do
  assistente segue por cima da fala). Vale um ticket próprio.