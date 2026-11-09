#1022 — hipóteses de causa do pos_llm e critérios de FALSIFICAÇÃO (pré-registro)

**Data:** 2026-11-09 · **Base:** `ea62125` (main) · **Instrumento:** `20131ea`/`2e92558`/`6aab19c` + `b40360f` (`wt/speech-pipeline-1022`, gates #1030 e #1040)
**Card:** #1034 · **Medição que fecha isto:** janela no mini (roteiro no #1036, conduzida pelo audio-ml)

Este arquivo existe porque comentário de board não é prova: a tabela foi escrita
**antes** da medição, para que o número da janela não seja lido com a hipótese
escolhida depois. O readout entra na seção final, com data e SHA, no mesmo commit
da rodada.

## Contexto

`pos_llm` (1º token do LLM → 1º áudio) é o maior elo do S2 em todas as rodadas do
#875: p50 2,6–3,4 s, p95 8–10 s. A medição local com LLM falso deu p50 324 ms —
o mini não se reproduz na bancada local, então o número que decide é o de lá.

## Critérios de falsificação (na régua do próprio instrumento)

| # | hipótese | MATA-SE se (na janela) | Se sobreviver |
|---|---|---|---|
| 1 | `llm_frase` (provedor espaça tokens) | `llm_frase` p50 < 300 ms nas 20 falas das DUAS rodadas | vira conserto de corte da 1ª cláusula |
| 2 | `tts` no hardware do mini / `_cond_for` miss | `tts` p50 < 300 ms **e** `llm_frase` já alto (senão o tempo migrou, não sumiu) | vira prewarm do `cond` + revisão de passos |
| 3 | `marca` inline | `marca` p50 < 50 ms nas falas com playback (não nas canceladas) | vira remoção do caminho crítico (ou `live_marca_primeiro_trecho`) |
| 4 | `resto` como causa | depois do #1038, `resto` ≈ `t_decisao_ms` (diferença < 100 ms) | se sobrar folga grande, há fio/cliente no meio e o item 2.2 do #1035 sobe de prioridade |

## Já mortas ANTES da janela (não voltam)

- **worker do #152** — `omnivoice` não está em `_ISOLATED_FAMILIES`; não há
  subprocesso no 1º áudio;
- **disputa de `_gen_lock` / `fila_tts`** — a bancada roda sozinha, sem job de
  lote (medição local: 0,5 ms e 0 ms);
- **`envio` como estágio** — é régua, ~0 por construção (`first_audio_ms` é
  carimbado DEPOIS do `emit_audio`, ou seja, já inclui a marca).

## Contraprova obrigatória

Se as duas rodadas (LLM real × LLM falso) derem `llm_frase` parecido, a hipótese 1
cai por construção e o gargalo é do app, não do provedor. A diferença entre as
rodadas é o que separa uma coisa da outra — é por isso que a rodada com o fake não
é redundante.

## Leitura

`resto` **não é estágio**: mistura dois relógios (`s2_ms` conta do fim da fala no
cliente; `first_audio_ms`, do início da thread, DEPOIS do VAD). O −121 ms medido
localmente é a decisão do VAD entrando negativa. Ler `resto` como tempo absoluto é
o erro mais fácil aqui.

## Readout da janela

<!-- preencher no commit da rodada: data, SHA, p50/p95 por trecho, quais
     hipóteses caíram e por qual número. -->
