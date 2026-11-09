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

**Data:** 2026-11-09 · **SHA:** `48b11d5` · **Rodada:** `/tmp/rodada-876-1794234572`
(2×2 do #876 × #870, semente 875, 10 ciclos + 1 de aquecimento por célula) · **relatório:** `evidence/876-janela-2x2.md`

**Duas ressalvas que mudam a leitura, declaradas pelo relatório antes do número:**

1. **O provedor estava fora (HTTP 530) — rodou-se só com o LLM FALSO.** O critério da
   hipótese 1 pedia `llm_frase` baixo nas DUAS rodadas; houve uma. Onde isso pesa,
   está marcado abaixo.
2. O `pos_llm` só tem estágio nas falas de papel A **que abriram turno com áudio**
   (n=7–9 por célula), não nas 20 do S2.

### Medido (p50 / p95, ms, por célula)

| trecho | f0-c0 | f0-c1 | f1-c0 | f1-c1 |
|---|---|---|---|---|
| `llm_frase` | 34 / 35,6 | 34 / 35,6 | 29 / 34,7 | 31 / 34,1 |
| `tts` | **1437 / 1487** | **1429 / 1448** | **1431 / 1854** | **1448 / 1481** |
| `resto` | **870 / 2571** | **1014 / 2328** | **928 / 2979** | **656 / 1034** |
| `marca` | 0 / 0 | 0 / 1 | 0 / 0 | 0 / 0 |
| `fila_tts` · `lock` · `envio` | ~0 | ~0 | ~0 | ~0 |

### Veredito por hipótese

| # | veredito | número |
|---|---|---|
| 1 `llm_frase` | **INDETERMINADA** | 29–34 ms nas quatro células — 10× abaixo do limite de 300, mas só na rodada falsa; a real não existiu (provedor 530) |
| 2 `tts` no mini | **SOBREVIVEU** | 1429–1448 ms de p50, quatro células, contra limite de morte de 300 ms |
| 3 `marca` inline | **NÃO TESTADA** | 0,0 ms — a marca d'água estava DESLIGADA na janela (runbook, passo 5), então o 0 é ausência, não rapidez. Sonda local com a marca ligada: 61 ms |
| 4 `resto` como causa | **SOBREVIVEU — e é 2/3 VAD** | ver a abertura abaixo: 918 ms de `resto` = **608 de `vad` + 310 de `fio`** |

### O `resto` foi aberto (rodada local `vad5`, 2026-11-09)

O `t_decisao_ms` publicado pelo #1038 vinha como **carimbo de sessão** (78049 ms) e, subtraído
do `resto`, dava `fio` negativo em 78 s — campo inútil como estava. O motor passou a mandar
**duração** (o silêncio que o detector esperou) e a bancada a abrir o `resto` em `vad` + `fio`:

```
resto  p50 918  =  vad p50 608  +  fio p50 310
```

O `vad` bate com o `silence_ms` de 600 do config (608 = 19 frames de 32 ms), o que valida a
medida. Ou seja: **2/3 do `resto` é o hangover do VAD**; o terço restante é fio/fila. O `resto`
inteiro, porém, NÃO é o maior trecho — o `tts` é (1429–1448 ms de p50, contra 656–1014). Também
não é LLM (29 ms), não é `_cond_for` (52 ms), não é `marca` (0 ms com a marca desligada).

Cortar o `vad` tem piso declarado no próprio código: `SILENCE_MS_MINIMO_STT = 500` (abaixo
disso o STT come a última sílaba). São 100 ms disponíveis ali, contra 310 ms de `fio` que
ainda não têm dono.

**Como ler o `fio` (escrito ANTES da re-rodada, #1084).** Ele se parte em `fio_rx` (servidor:
decisão do VAD → turno abrir) e `fio_cliente` (mic, WS, playback). Dois cuidados, os dois
casuais de leitura errada:

- **`fio_rx` nunca sai negativo**, e não é mérito do servidor: `decisao_para_audio_ms` é
  PSEUDO-CARIMBO (`first_audio_ms + max(0, abre − decisão)`), então o `max(0, …)` engole o
  caso `abre < decisão` e joga a parcela negativa INTEIRA no `fio_cliente`.
- Logo, **`fio_cliente` negativo pode ser relógio cruzado, não transporte**. Não leia o sinal
  como "o cliente demorou": leia como "as duas pontas não são comparáveis neste turno".

### O que o número já derruba, e o que ele não sustenta

- **A hipótese do `_cond_for` caiu por medição direta**, não pela janela. Sonda isolada
  (`/tmp/probe_cond.py`, 2026-11-09): `create_voice_clone_prompt` frio custa **52 ms** com
  o modelo carregado, e as falas 14/16 da f0-c0 repetem vozes da 0/2 com o mesmo `tts`
  (1436/1438 ms) — a LRU de 4 não explica nada em 1430 ms. O `tts` é **síntese**, não cache.
- **A 1ª cláusula já é curta**: o chunker entrega `"Claro,"` (6 ch) e a síntese isolada
  disso a 16 passos custa **635 ms** — não 1430. Ou seja, metade do `tts` da janela é
  estado da máquina, não do texto (a mesma chamada a 8 passos custa 311 ms).
- **Rodada local de controle** (`tools/mede_pos_llm.sh base 5`, corpus FLEURS): `tts` p50
  **316 ms**, `resto` p50 **918 ms**. O `resto` se repete; o `tts` não.

### Consequência para o passo 2

A ordem de ataque é **`tts` → `fio` → VAD**, e não pelo tamanho do `resto`: o `tts` é o maior
trecho (1429–1448 ms) e o que mais rende; o `fio` (310 ms) vem depois e precisa de decomposição
própria; o VAD fica por último porque o piso dele é 500 ms e o S1 ainda está em 80–85 % — o
`silence_ms` fica em 600 e os 100 ms não pagam o risco de cortar sílaba.

O `resto` tinha razão de ser caixa preta até aqui: a bancada não gravava `t_decisao_ms` e o
campo, quando passou a ser publicado, vinha como carimbo de sessão. Com o `vad`/`fio` abertos,
cortar sem separar deixa de ser chute.
