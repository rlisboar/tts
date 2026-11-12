# #874 / task_e6abe331 — a narração pausa entre trechos enquanto houver Live ativo (opção b da H-trecho): PRÉ-REGISTRO (2026-11-12, antes do código)

Autor do código: api-backend. Pré-registro e apoio: speech-pipeline. Base: main atual, SEM o genlock (o árbitro do
#1022 fica arquivado: com a pausa, ele não tem o que ordenar).

## Por quê

glk3m (readout `eabfdda`): o trecho do lote dura p50 3560 / p95 4437 ms (100–108 chars), e o Live espera até um
trecho inteiro. Com narração em paralelo, o `lock+fila` do Live fica em p95 2876 ms e o S2 em p95 7683 ms. Decisão do
PM (opção b, ligada por padrão e configurável): com Live ativo, o lote não começa trecho novo.

## O sinal de "Live ativo" (recomendação do speech-pipeline)

**Não usar `sess["visto"]`.** Ele é renovado a cada mensagem do WS, e o cliente manda frame de mic o tempo todo,
inclusive em silêncio. Com ele, a sessão nunca fica "ociosa" e a narração nunca volta antes de a sessão fechar.

Live ATIVO = existe sessão Live aberta no registro (não `fechar`/`vencida`/`substituida`) **e** pelo menos um destes:
1. turno em curso: `_live_turno_em_curso(sess)` (pipeline ocupado, turno aberto no motor ou `audio_pendente`);
2. áudio do agente ainda tocando no cliente: `pb_fim_est > agora`;
3. atividade nos últimos `live_pausa_lote_ociosa_s` (60 s): carimbo NOVO `sess["atividade"]` (monotônico),
   renovado em `speech_start` do motor (fala detectada, não frame), início/fim de turno do pipeline e enfileiramento
   de áudio do agente (`_live_envia_audio`).

A retomada acontece quando nenhuma sessão satisfaz 1–3, ou quando a sessão fecha (`_live_libera_sessao`). A checagem
fica no laço do job, ENTRE trechos (o trecho em curso não é interrompido), e no início do job, antes de pegar o
`_gen_lock`.

## Critérios da medida (mini, fixados antes)

Desenho da H6/glk: clone com o código novo, LLM falso, FLEURS, carga `/api/tts` (`h6_carga.py`) na mesma instância,
ABAB com n ≥ 12 turnos por braço fora do 1º, mesmo corte (turnos com estágios, fora do 1º), `bancada_lock` novo,
`BENCH_TMP` fora do `/tmp`.

| critério | passa se |
|---|---|
| **P1, o Live não espera o lote** | braço com carga: `lock+fila` p95 ≤ **300 ms** (o trecho em curso no início da sessão fica no 1º turno, fora do corte) |
| **P2, o S2 com carga** | S2 p95 com carga ≤ **1,2 ×** o S2 p95 sem carga da MESMA rodada (o do card) |
| **P3, o lote não quebra** | 0 `jobs_erro`, 0 timeout e 0 429 a mais que no braço sem pausa; todo job postado durante o bloco chega a `done` depois que o Live fica ocioso |
| **P4, a retomada** | depois do fim da sessão Live do bloco, o 1º trecho de lote pendente começa em ≤ `live_pausa_lote_ociosa_s` + 5 s (o fechamento da sessão retoma na hora: aí, ≤ 5 s) |
| **P5, sem Live = hoje** | bloco só com carga, sem sessão Live: vazão de jobs/min dentro de ±10 % de um bloco equivalente na main |

Só dado, sem decidir: o tempo total de cada job (postagem → `done`), o tempo pausado por job e `jobs_fim` por bloco.
Com a pausa, o `jobs_fim` DURANTE o bloco com Live cai por desenho, e por isso não é critério.
