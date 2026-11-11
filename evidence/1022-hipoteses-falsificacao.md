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
  PSEUDO-CARIMBO (`first_audio_ms + max(0, abre − decisão)`), então o `max(0, …)` zera
  `fio_rx` no caso `abre < decisão` e DESCARTA a parcela — não a joga no `fio_cliente`
  (este continua igual ao `fio`).
- **`fio_cliente` negativo** é `fio_rx` grande ou `resto` negativo (dois relógios), não
  transporte. Não leia o sinal como "o cliente demorou": leia como "as duas pontas não
  são comparáveis neste turno".

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

## Hipótese 5 — marca a frio no 1º trecho (PRÉ-REGISTRO, 2026-11-10, antes de medir)

**Base:** `7cb4618` (main, no ar no mini desde 16:28) · **Card:** #1022 · **Aceita pelo PM** antes da medida.

**Por que nasceu.** No A/B do piso (`tools/mede_pos_llm.sh`, mini, bloco `piso10a`, n=3) a
`marca` deu p50 **62 ms** e p95 **4323 ms**: uma fala pagou o aquecimento do filho do AudioSeal.
O log do mesmo bloco tem `[marca] aquecido em 4311 ms` e, depois, um SEGUNDO
`carregado` + `aquecido em 3401 ms` dentro da mesma sessão. A hipótese 3 (marca inline) nunca foi
testada com a marca ligada: na janela de 9/11 e na de 2026-11-10 16:31 o estado sintético da 7890
tinha `tts_marca_dagua=False`, e o 0 ms era ausência.

**O caminho, lido no código (`7cb4618`).** O PCM do 1º trecho passa por `emit_audio`, e o app
aplica o AudioSeal ANTES do `send` (`live_pipeline.py:1262-1280`; `marca_ms` é carimbado antes
de `first_audio_ms`). Ou seja, a marca está no S2. O filho tem `TTL_S=600` (`marca_dagua.py:57`)
e é aquecido no boot (`tts_marca_prewarm_boot`) e na abertura do WS. Produção hoje:
`tts_marca_dagua=True`, `live_marca_primeiro_trecho=True`, `tts_marca_prewarm_boot=True`, e o log
da 7860 tem `aquecido em 3387–3717 ms`.

| # | hipótese | MATA-SE se | Se sobreviver |
|---|---|---|---|
| 5 | a marca **a frio** no 1º trecho é a cauda do S2 p95 | com o filho **quente** (aquecido antes do 1º turno e TTL não vencido), `marca` p95 ≤ 100 ms **e** o S2 p95 **não** cai contra o braço a frio | (a) engenharia nossa: filho quente durante a sessão Live, com a marca LIGADA em todos os trechos; (b) só como opção quantificada para o dono: `live_marca_primeiro_trecho=False` ("custa X ms, compra Y") |

**Desenho, fixado antes do número.**
- Estado sintético (#446 §C), `tts_marca_dagua=True`, LLM falso, semente 875, no mini, em série e sem
  bancada nem `pytest` concorrentes. `TTS_LIVE_DUMP_DIR` fora do env.
- Braço **frio**: o 1º turno chega com o filho recém-subido (sessão aberta e fala imediata). Braço
  **quente**: o 1º turno só depois de `[marca] aquecido` no log. **n ≥ 10 primeiros turnos por braço.**
- Publicar por braço: `marca` p50/p95, S2 p50/p95, `tts` p50/p95 e a memória (swap, RSS 7860) no
  início de cada bloco.
- Leitura: se os blocos do mesmo braço divergirem entre si mais do que os braços, o veredito é
  "ruído do ambiente", não "marca".

**Pergunta aberta, fechada na mesma medida (é defeito por si, com ou sem a hipótese):** por que o
filho foi **recarregado dentro da sessão** (`aquecido` 2× no mesmo bloco). Há três candidatos, a
separar pelo log: o prazo curto do Live (1,5 s) matando o filho "mudo" durante a carga; o TTL; ou
uma reabertura do WS.

**Fora de escopo até o número:** qualquer mudança em produção ou em código. Desligar a marca, ou deixar
algum trecho sem ela, é decisão do dono (2026-10-28: marca "on").

### Readout da hipótese 5 (2026-11-10, critério do `1b3775d` intocado)

**Rodada:** mini, 20:06:47 → 21:24:36, `tools/mede_pos_llm.sh` (2 ciclos por amostra), LLM falso
(TTFT 300 ms), semente 875, estado sintético, sem `TTS_LIVE_DUMP_DIR`. Dois clones descartáveis do
`7cb4618`: **frio** = código como está; **quente** = + `_ultima = time.monotonic()` no `_pega` (a
linha que virou o `76fbe8c`, #1114). Foram 10 pares alternados, com uma instância nova e uma sessão nova por
amostra. A amostra é o 1º turno da sessão.

| braço (n=10) | `marca` do 1º turno p50 / p95 | S2 do 1º turno p50 / p95 | subidas do filho por amostra |
|---|---|---|---|
| frio | **5309 / 5876 ms** | **10925 / 13795 ms** | 2 em 10/10 (boot + dentro do turno) |
| quente | **739 / 907 ms** | **6265 / 6590 ms** | 1 em 10/10 (só o boot) |

Faixas: frio 4763–6015 ms; quente 682–963 ms. Os braços não se sobrepõem, e a dispersão dentro de cada braço é
muito menor que a distância entre eles.

**Veredito: SOBREVIVEU.** O critério de morte (`marca` p95 ≤ 100 ms com o filho quente **e** S2 p95 sem
queda) não se cumpriu nas duas pontas: a `marca` quente fica em 907 ms e o S2 p95 do 1º turno cai 7,2 s.

**A causa, achada na mesma rodada:** `_ultima = 0.0` + `prewarm()` sem tocar `_ultima` faziam o vigia do
TTL fechar todo filho de prewarm ~5 s depois de nascer (consertado no `76fbe8c`, #1114). O caso do Live
aberto e ocioso > `TTL_S` vai para o #1115 (`prewarm()` no `speech_start`).

**O que o número não fecha:**
- Mesmo quente, o 1º pedido de marca custa ~0,74 s (contra 58–71 ms dos turnos seguintes). É
  recompilação do `torch._dynamo` no 1º PCM real: o worker aquece com `zeros(1, 1, 16000)`, que não é a
  forma do trecho do Live. Esse ponto ficou com o audio-ml.
- O aquecimento precisa começar **≥ ~4,6 s** antes do 1º pedido (`carregado` 105–125 ms +
  `aquecido` 3,3–4,7 s + import do filho, sem carimbo).
- O S2 p95 sem o 1º turno (A/B do piso, n=17) é 4842 ms, e quem domina agora é o `tts_gen` (1445 / 3234).

## Hipótese 6 — GPU/trava disputada entre o Live e os jobs de `/api/tts` (PRÉ-REGISTRO, 2026-11-10, antes de medir)

**Base:** main `76fbe8c` · **Card:** #1022 · Aceita pelo PM para pré-registro, **ainda sem ordem de rodar**.

**Por que nasceu.** Na H5 a 7860 de produção atendia jobs de `/api/tts` em sequência pelo túnel
(narração), e o `tts` do 1º turno da bancada ficou em 2,6–3,2 s nos dois braços. No par 10, quando os
jobs pararam, o `tts` caiu para **1,2 s** nos dois braços. A observação não foi controlada: a carga dependia
da narração acontecer por acaso. Em produção o cenário é pior que o da H5, porque o Live e os jobs correm no
**mesmo processo** (7860), e aí a disputa é de GPU **e** do `_gen_lock`.

| # | hipótese | MATA-SE se | Se sobreviver |
|---|---|---|---|
| 6 | carga de `/api/tts` concorrente é a cauda do `tts`/S2 do Live | com carga, nos turnos do Live sem o 1º: `tts` p95 ≤ 1,2 × o p95 sem carga **e** `lock` + `fila_tts` p95 < 100 ms **e** S2 p95 ≤ 1,2 × sem carga | saída de engenharia = **prioridade/fila** (o Live na frente do lote no `_gen_lock`/GPU). Limitar a narração do dono é decisão **dele**, nunca nossa |

**Desenho (não depende da narração do dono).**
- Instância de **bancada** (`tools/mede_pos_llm.sh`, 7895/7899, estado sintético #446 §C, voz FLEURS,
  sem dump) e LLM falso. O 1º turno de cada sessão fica fora da conta, para a marca a frio da H5 não contaminar.
- **Carga sintética e controlada na MESMA instância de bancada**, que é o caso da produção (mesmo processo):
  um gerador fecha o laço `POST /api/tts` → espera o job → repete, com 1 job por vez, texto fixo de
  ~200 caracteres e voz do corpus FLEURS. O gerador só começa depois do `health` e só para no fim do bloco.
  Condição de validade: **0 respostas 429** (o `/api/tts` está em `_RATE_HEAVY_EXATAS`). Se o limite morder,
  o bloco é descartado e a carga é reduzida antes de repetir, sem relaxar o limite no código.
- Braços **sem carga × com carga**, alternados (ABAB…), **n ≥ 12 turnos por braço** fora do 1º. Por bloco:
  `tts`, `tts_gen`, `lock`, `fila_tts`, `marca` e S2 p50/p95, jobs concluídos durante o bloco, swap e
  RSS da 7860 no início.
- A 7860 de produção fica **ociosa de `/api/tts`** durante a rodada. Se aparecer job de narração no log da
  7860 num bloco, o bloco é marcado e repetido: é carga fora do controle e entra nos dois braços por acaso.
- Leitura: se os blocos do mesmo braço divergirem entre si mais do que os braços, o veredito é
  "ruído do ambiente", não "carga".

**Variante, só se a principal sobreviver:** a carga em OUTRO processo (como foi na H5) separa a disputa
de GPU da disputa do `_gen_lock`. Fica de fora até o número da principal.

**Fora de escopo até o número:** qualquer mudança em código ou em produção.

### Readout da hipótese 6 (2026-11-11, critério do `1f5f3cf` intocado)

**Rodada:** mini, 6 blocos alternados em duas janelas sem a narração do dono:
`sem-a/com-a/sem-b/com-b` (17:33–19:05) + `sem-c/com-c` (19:50–20:32, bloco extra para fechar n ≥ 12).
- **Bancada:** clone `13716e3`, o SHA no ar depois do deploy de 17:20. Esse deploy só acrescentou
  telemetria (log persistente e campos do #1129) e **nenhuma decisão do Live mudou**.
- **Ambiente:** LLM falso (TTFT 300 ms), corpus e voz FLEURS em `~/tts-bench`, estado sintético, sem dump.
- **Carga dos blocos `com`:** `/tmp/h6_carga.py` → `~/tts-bench/h6_carga.py` na mesma instância, com 1 job
  `/api/tts` por vez, texto fixo de ~200 caracteres e ≥ 3,5 s entre POSTs. Foram 78, 85 e 76 POSTs.
- **Validade, conferida bloco a bloco:**
  - 0 POST `/api/tts` na 7860;
  - 0 amostra de CI (`Runner.Worker` ou `pytest`), com o detector incluído depois do achado do #1149;
  - 0 respostas 429.
- **Incidentes declarados:** o `sem-a` da 1ª tentativa (2026-11-10) se perdeu com o `/tmp` no reboot
  da 01:00, e a H6 foi refeita inteira. Uma 2ª instância órfã do roteiro largou um `sem-a` às 17:30 com o CI
  ativo; foi morta por PID, sem saída gravada, e **não conta**.

Turnos fora do 1º da sessão (o 1º paga a marca a frio, H5), p50 / p95 em ms:

| braço | n | S2 | `tts` | `tts_gen` | `lock` + `fila_tts` | `marca` |
|---|---|---|---|---|---|---|
| sem carga (sem-a, sem-b, sem-c) | 13 | 2356 / **2886** | 1249 / **1261** | 1247 / 1259 | 0 / **1** | 66 / 99 |
| com carga (com-a, com-b, com-c) | 12 | 2870 / **5563** | 1250 / **1266** | 1181 / 1264 | 0 / **2654** | 75 / 96 |

Por bloco, o `lock+fila` p95 sem carga foi 1 / 0 / 1 ms e com carga **1629 / 718 / 2769** ms. Os
braços não se sobrepõem, então não é ruído do ambiente.

**Veredito: SOBREVIVEU.** O critério de morte pedia as três condições juntas, e duas falham:
- `tts` p95 ≤ 1,2×: vale (1,00×);
- `lock+fila` p95 < 100 ms: **falha** (2654);
- S2 p95 ≤ 1,2×: **falha** (1,93×).

**Mecanismo:** o `tts_gen` é igual nos dois braços, então **não é disputa de GPU**. É **espera na fila do
`_gen_lock`**, porque o job local de `/api/tts` segura a trava pelo job inteiro (load + todos os trechos)
e o trecho do Live espera o job acabar. A variante pré-registrada (carga em outro processo) fica
dispensada: o mecanismo já está separado pelos estágios.

**Saída de engenharia, alçada nossa:** prioridade/fila, com o Live na frente do lote entre trechos e
sem cancelar o trecho em curso. Desenho no card do #1022, código em `wt/speech-pipeline-genlock`. Limitar a
narração do dono é decisão dele, e não foi proposto.

**Observações, sem critério:**
- na 1ª janela o swap do mini foi de 3,3 GB para **8,5 GB** a partir do `com-a`, e ficou nesse nível até o
  fim; na janela do bloco extra ficou em ~2,4 GB;
- o RSS da 7860 caiu de ~3,9 GB, no início, para 70–150 MB durante a carga (a produção foi paginada);
- o `com-c` teve um S2 p95 de 7103 ms. A cauda acompanha o `lock+fila` do mesmo bloco (p95 2769).

## Medida da prioridade do Live no `_gen_lock` (PRÉ-REGISTRO, 2026-11-11, antes de medir)

**Código:** `wt/speech-pipeline-genlock` (`_GenArbitro`: o lote cede entre trechos e o Live passa na frente,
sem cancelar o trecho em curso, com teto anti-starvation de 3 s ou 4 trechos). **Desenho:** o MESMO da H6
(roteiro `h6c/h6x`, clone com o código novo, LLM falso, FLEURS, carga `/api/tts` na mesma instância, os mesmos
guardas), com **n ≥ 12 por braço** fora do 1º turno.

**O que o desenho promete, declarado antes:** o Live espera **no máximo um trecho de lote**. Com
`chunk_max_chars=140` e o texto de carga de ~200 caracteres, um trecho leva **~1,2 s** (`tts_gen` p95 da H6:
~1,26 s). Não promete < 100 ms. Esse é o alvo da alavanca seguinte (trecho menor para o lote com Live aberto).

| critério | passa se | se falhar |
|---|---|---|
| **M1, a promessa do desenho** | braço com carga: `lock+fila` p95 **≤ 1300 ms**. Era 2654 ms na H6 | o árbitro não entrega o que promete. Volta ao código |
| **M2, efeito no S2** | braço com carga: S2 p95 **< 5563 ms** (o da H6 com carga) **e** ≤ 1,5× o S2 p95 do braço sem carga da MESMA rodada | o ganho no lock não chega ao S2. Investigar o resto |
| **M3, o lote não passa fome** | jobs de carga concluídos por bloco `com` ≥ **55** (a H6 teve 75–84) e 0 `jobs_erro` | o anti-starvation não segura. Rever o teto |
| **M4, alvo esticado (só dado)** | `lock+fila` p95 < 100 ms | esperado não passar. Decide se a alavanca seguinte entra |

A exclusividade do Metal (nunca dois `_generate_chunk` juntos) é provada pelas células do código, não por esta medida.
