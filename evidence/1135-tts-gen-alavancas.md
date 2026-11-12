# #1135 — de onde vem o `tts_gen` do 1º trecho do Live e o que dá para cortar

**Data:** 2026-11-11 · **Base:** `2fc0de8` · **Autor:** audio-ml · **Card:** task_46ba09f4 (#874)
**Natureza:** análise de código + micro-bench LOCAL (MBP, MLX, load 6–12). Sem mini, sem código de produção.
**Script:** `evidence/1135-prefixo-cfg-quant.py` (9 rodadas embaralhadas, mediana, 1 aquecimento por condição).

## 1. Decomposição (do código do `mlx_audio` 0.4.4, `omnivoice/generation.py`)

O OmniVoice é difusão mascarada, NÃO autorregressivo. Cada passo roda o backbone (Qwen3-0,6B, 28
camadas, atenção BIDIRECIONAL, sem KV-cache) **duas vezes**:

| passada | sequência | custo |
|---|---|---|
| condicionada | `style + texto + [ref_text] + prefixo de ref (25 tokens/s) + T alvo` | domina |
| incondicionada (CFG, `guidance_scale=2.0`) | só os `T` alvo | menor, mas não grátis |

`T` = ~25 tokens/s do áudio gerado (1º trecho de 18 chars ≈ 1 s → ~25–40). O **prefixo de referência**
tem até 250 tokens (10 s) e é reprocessado em TODO passo. O vocoder (`audio_tokenizer.decode`) e o
`argsort` do revelado são desprezíveis: o tempo é linear em passos (33 ms/passo no MBP; intercepto ~7 ms).

**Achado de código:** `omni_ref_max_s` (padrão 10, "quanto da amostra usar") **não corta nada ≤ 20 s**:
`create_voice_clone_prompt` só corta acima de 20 s (`_trim_long_audio`, `trim_threshold=20`). Medido: ref de
10 s com `max_duration_s` de 10/6/4/3/2 → sempre 250 tokens. Hoje o prefixo é a amostra INTEIRA (menos silêncio).

## 2. Medidas (MBP, 10 passos, texto "Claro, posso ajudar.", ms, mediana de 9)

| condição | ms | vs `ref10s` |
|---|---|---|
| ref 10 s (250 tokens) — base | **328** | — |
| ref 6 s | 278 | −15 % |
| ref 4 s | 260 | **−21 %** |
| ref 3 s | 259 | −21 % |
| ref 2 s | 232 | −29 % |
| ref 10 s + transcrição no prefixo | 350 | +7 % |
| ref 4 s + transcrição | 261 | ≈0 |
| ref 10 s, **CFG desligado** (`guidance_scale=0`) | 231 | **−30 %** |
| ref 4 s, CFG desligado | **172** | **−48 %** |
| ref 10 s, 8 passos | 263 | −20 % |
| ref 10 s, 5 passos | 171 | −48 % |
| ref 10 s, 16 passos | 517 | +58 % |
| backbone **q8** (ref 10 s) | 401 | **+22 % (MAIS LENTO)** |
| backbone **q4** (ref 10 s) | 389 | **+19 % (MAIS LENTO)** |

Leitura: o passo é limitado por COMPUTE no prefixo (linear no comprimento da sequência), não por banda de
memória. Por isso quantizar NÃO ajuda (a de-quantização sobe o custo, e a curva por prefixo some); por isso cortar
prefixo e cortar a passada de CFG ajudam. Escala para o mini: o `gen` de ~1,25 s do 1º trecho é ~3,8× o MBP; os
percentuais são os que se projetam (o ganho absoluto depende do tamanho real da amostra de cada voz).

## 3. Alavancas (ganho = fração do `tts_gen`; projeção sobre 1,25 s)

| # | alavanca | ganho medido (MBP) | projeção mini | risco de qualidade | código |
|---|---|---|---|---|---|
| A | **Prefixo de ref do Live cortado a ~4 s** (janela densa de fala, cache próprio) | −21 % (4 s) a −29 % (2 s) | ~−0,26 s | médio: timbre/prosódia (o prefixo É o condicionamento do clone) | pequeno: `_cond_for` com `ref_max` do Live (a chave do cache já inclui `ref_max`) + janela por energia |
| B | **CFG desligado no 1º trecho** (`guidance_scale=0` só no 1º trecho) | −30 % | ~−0,37 s | médio: CFG reforça texto E locutor; risco de WER e de timbre | nenhum para o liga/desliga (parâmetro existente) |
| A+B | os dois | **−48 %** | ~**−0,6 s** (1,25 → ~0,65 s) | soma dos dois; medir junto | — |
| C | CFG só nos primeiros k passos | ~−15 % (estim.) | ~−0,19 s | baixo-médio | médio: fork do `iterative_unmask` (60 linhas) preso ao pino 0.4.4 |
| D | 8 passos no 1º trecho | −20 % (sobre 10) | ~−0,25 s | ALTO (já medido no #1079: dobra a perda de timbre, 0,127) | nenhum |
| E | Quantização q8/q4 | **−22 %/−19 % (PIORA)** | negativo | — | **REFUTADA, não fazer** |
| F | Omitir a transcrição do prefixo | ≤ −7 % (só com ref longa) | ≤ −0,09 s | médio: a transcrição melhora a clonagem | pequeno; vira nada junto com A |
| G | KV-cache do prefixo | até ~−35 % (estim.) | — | ALTO: atenção bidirecional, o prefixo depende do alvo; cache exato é impossível, o aproximado muda a saída | pesquisa |
| H | Streaming por chunk do modelo | n/a | — | — | **não se aplica**: difusão mascarada refina TODOS os frames juntos; só há áudio depois do último passo |
| I | Trecho menor | n/a | — | — | já no mínimo (`first_max_chars=18`); o custo é prefixo + passos, não `T` |
| J | Outro backend do catálogo | não medido | — | ALTO: identidade da voz é decisão do dono | bake-off próprio, fora deste card |

Ordem por ganho/risco: **B** (sem código, −30 %), **A** (−21 %, código pequeno), depois **C**. D fica
descartada pelo #1079; E refutada; G só como pesquisa.

## 4. PRÉ-REGISTRO (feito ANTES de medir; vale para o mini, estado sintético, FLEURS, em série após a fila)

**Instrumento:** o `latency` do turno já publica `tts_gen` (sub-marcas do #1022). Acrescentar ao registro
de cada fala `ref_tokens` (comprimento do prefixo) e `cfg` usados — leitura, sem mudar comportamento.
**Corpus/voz:** FLEURS pt_br público + voz da sessão da bancada (`flvoz`), nunca `voices/` do dono.
**Desenho:** blocos alternados controle × tratamento (como no A/B do piso: `10a 12a 10b 12b …`), mesma semente,
≥ 4 blocos por braço, `n ≥ 20` primeiros trechos por braço, LLM falso, marca LIGADA, sem dump.

### B — CFG desligado no 1º trecho (`guidance_scale=0`, 10 passos)
- **Ganho:** `tts_gen` p50 cai ≥ **25 %** (controle: CFG 2.0). Menos que 15 % → B morre por falta de ganho.
- **Guarda de timbre:** cosseno ECAPA contra o áudio do CONTROLE (mesmo texto/semente), buckets curto/longo
  como no #1079, em ≥ 24 pares. Passa se a queda de B sobre o controle de ruído (outra semente) for
  ≤ **0,06** no curto (o 10 passos já custou 0,051) e o longo ficar ≥ 0,80.
- **Guarda de inteligibilidade:** volta ao STT do Live; WER médio ≤ **0,03** acima do controle, **0** transcrições
  vazias, pior caso ≤ 0,25.
- **Escuta:** ABX do dono 8 pares curtos (CFG 2.0 × 0). Morre se ele distinguir ≥ 7/8 E apontar B como pior.
- **Critério de morte:** qualquer guarda falha → B fica fora; vai para C (CFG parcial) como variante.

### A — prefixo de referência do Live cortado a 4 s
- **Ganho:** `tts_gen` p50 cai ≥ **15 %** nas vozes cuja amostra passa de 4 s (relatar por `ref_tokens`; voz com
  amostra ≤ 4 s fica de fora do denominador, ganho 0 por construção).
- **Janela:** os 4 s de maior energia de fala (sem silêncio de borda), derivada em RAM no cache do Live — sem
  gravar nada em `voices/` e sem tocar na ref do app (o `omni_ref_max_s` do dono não muda).
- **Guarda de timbre:** cosseno ECAPA entre o áudio gerado e a AMOSTRA ORIGINAL do locutor, ref inteira × ref 4 s,
  em ≥ 24 pares; passa se a queda média for ≤ **0,05** (curto) e o p10 não ficar abaixo do p10 do controle de ruído.
- **Guarda de WER/ABX:** as mesmas de B.
- **Critério de morte:** queda de timbre > 0,08 ou ABX ≥ 7/8 contra A → o corte sobe para 6 s (−15 %) e re-mede.

### A+B juntos
Só roda depois de A e B passarem sozinhos. Meta: `tts_gen` p50 ≤ **0,75 s** no mini (de ~1,25 s) com as guardas
de A e B somadas (timbre ≤ 0,08 no curto). S2 p50 esperado ≈ 2,36 − 0,5 ≈ **1,85 s** (meta #874: 1,5 / 2,5).

### Fora de escopo e regras
- Nenhum pino (`mlx-audio`, `mlx-lm`, `transformers`) sobe. C exigiria fork do laço do pino 0.4.4 e só entra se A/B não bastarem.
- Dado do dono intocável; só sintético/FLEURS; mini só sem `~/.tts-studio/bancada.lock`.
- A fila do `_gen_lock` tem conserto próprio em medição (`50e352f`): não entra nestas contas.

---

## 5. READOUT das guardas objetivas de A e B (mini, 2026-11-12 13:57–14:07, estado sintético, voz FLEURS `flvoz` de 227 tokens)

Script `evidence/1135-guardas-ab.py`; dados `evidence/1135-guardas-ab-rodada1.json` (todas as condições) e
`…-rodada2-ctrlnr.json` (controle da transcrição). 12 textos × 2 repetições; 1º trecho, 10 passos; `gen` = `generate` do modelo quente.

| condição | `gen` p50 / curto (ms) | timbre vs ctrl (curto · longo) | WER médio | vazios |
|---|---|---|---|---|
| ctrl (hoje) | 1574 / 1346 | — | 0,16 | 0 |
| ctrlb (outra semente = piso de ruído) | 1460 / 1346 | 0,54 · 0,77 | 0,13 | 2 |
| **B** (CFG 0) | 1184 / 1141 (**−25 % / −15 %**) | 0,52 · **0,52** | **0,245** | 2 |
| **A4** (ref 4 s, sem transcrição) | 1006 / 813 (**−36 % / −40 %**) | 0,36 · 0,72 | **1,10** | 0 |
| A6 (ref 6 s) | 1163 / 986 | 0,34 · 0,68 | 1,30 | 0 |
| AB | 713 / 591 (−55 %) | 0,25 · 0,34 | 0,71 | **10** |
| ctrlnr (ref inteira, SEM transcrição) | 1351 / 1153 | 0,54 · 0,73 | 0,386 | 4 |

**Veredito de B: MORRE** pelo critério pré-registrado. Ganho no limite (−25 % no p50, mas só −15 % no curto que é o
tamanho real) e duas guardas falham: timbre no longo 0,52 contra 0,77 do controle de ruído, e WER +0,085 (limite +0,03).
O CFG é o que mantém o locutor e o texto: sem ele o modelo desliza para outra voz.

**Veredito de A: MORRE como desenhado** (WER 1,10 e 1,30 contra o critério de morte; AB com 10 transcrições vazias). O
confundidor foi separado: **descartar a transcrição sozinha** (ctrlnr) já leva o WER de 0,16 para 0,39 e a 4 vazios, e
**cortar o prefixo a 4 s** leva de 0,39 para 1,10 — o modelo alucina palavras ("Nosso papai é com a Lisa" para "Claro,"). Dois
motivos somados: o corte cai no meio de palavra e o `ref_text` deixa de casar com os tokens. O ganho de custo é real
(−40 % no curto), então o caminho que sobra NÃO é cortar às cegas e sim: **ref de ~4 s que termina em fronteira de palavra e vem
com a transcrição alinhada** (STT com timestamps no cadastro da voz, derivando o recorte e o texto juntos). Isso é um card
novo, com bancada própria, e só vale se o WER voltar a ≤ ctrl + 0,03.

Ressalvas: o controle de ruído (ctrlb) já mostra 2 vazios e 0,77 no longo, então a régua de timbre desta voz é folgada;
n=24 por célula; uma voz só. As conclusões de B e A não dependem dessa folga (as diferenças são de 0,25 a 0,9).

## 6. Próxima alavanca: 1º trecho do Live mais curto (análise + pré-registro; nada rodado no mini)

**Medida local** (MBP, `evidence/1135-gen-vs-tamanho-texto.py`, 10 passos, ref 250 tokens, mediana de 9):

| texto | chars | áudio | `generate` (ms) |
|---|---|---|---|
| "Claro," | 6 | 1,32 s | 583 |
| "Sim, claro." | 11 | 1,56 s | 629 |
| "Deixa eu ver isso." (hoje, `first_max_chars=18`) | 18 | 1,84 s | 530 |
| "Claro, posso te ajudar." | 23 | 2,00 s | 753 |
| 38 chars | 38 | 2,60 s | 591 |
| 75 chars | 75 | 5,04 s | 919 |

O custo é **plano de 6 a 38 chars** (530–750, dentro do ruído de ±100): o passo é dominado pelo prefixo de 250 tokens
e pelo piso de 10 passos, e o alvo (T = 25 tokens/s, 33–65 tokens) é pequeno ao lado dele. Cortar o 1º trecho de 18 para 6 chars
**não reduz o `gen`** (o "Claro," saiu até mais lento que o de 18). Só a partir de ~40 chars o custo cresce.

**Onde sobra ganho:** na `llm_frase` (1º token → 1ª cláusula fechada). Com LLM real a ~40 tok/s e ~3–4 chars/token,
18 chars ≈ 5 tokens ≈ 125 ms e 6 chars ≈ 2 tokens ≈ 50 ms: **~−75 ms no S2**, no melhor caso. Com a regra de vírgula/N palavras
do #942 muitas respostas já fecham a 1ª cláusula antes dos 18 chars ("Claro,"), então o ganho médio é menor.

**Veredito de análise: lever pequeno, ≤ ~75 ms e sem ganho no `gen`.** Risco de prosódia: trecho de 1–2 palavras
("Claro,") vira uma interjeição isolada, o modelo entona como frase completa (queda final) e a emenda com o 2º trecho
fica audível; o ECAPA em trecho curto também comprime a medida (#1079). Não vale o risco antes de A/B.

**Pré-registro (se mesmo assim for medido, depois do C2):** braço `first_max_chars` 18 × 10 (controle × tratamento), blocos
alternados, ≥ 4 blocos, n ≥ 20 por braço, LLM real pela exceção do #446 §C (o ganho está no LLM, o falso esconde-o).
- **Ganho:** `llm_frase` p50 −40 ms ou mais; `tts_gen` p50 **inalterado** (±10 %), senão a hipótese do custo plano morre.
- **Guarda de emenda:** ABX do dono no par "1º trecho + 2º trecho" (8 pares); morre se ≥ 7/8 distinguirem e apontarem o curto como pior.
- **Guarda de WER/timbre:** as do #1079 (timbre ≥ controle − 0,06 no curto; WER ≤ +0,03; 0 vazios).
- **Critério de morte:** ganho de `llm_frase` < 40 ms ou qualquer guarda falha.

## 7. Tamanho real do 1º trecho do Live × `gen` do mini (esclarecimento pedido pelo PM) e A′

**O 1º trecho do Live é curto por construção.** `SentenceChunker` (`live_pipeline.py:156`): o 1º trecho sai na 1ª vírgula, nas
6 palavras (se couberem em `first_max`) ou no último separador antes de **`first_max = 18` chars**. O "140" é o `chunk_max_chars` do
JOB do app, não do Live. Simulação do chunker com 12 respostas em pt (`SentenceChunker.push` em deltas de 2, 4 e 20 chars):
**1º trecho = 4–18 chars, p50 8**. Só com um provedor SEM streaming (resposta inteira numa delta) o 1º trecho passa de 18
(26–43 chars em 3 de 12). Logo o `tts_gen` de ~1,25 s do mini é o custo de um trecho de ≤ 18 chars: **piso fixo** de prefixo + passos.

**`gen` no mini por tamanho** (medido no #1079, `evidence/1079-piso-passos.md`, mini, 10 passos, ref bf16): trecho
curto (1,4–2,2 s, ~6–25 chars) **1183 ms**; trecho longo (4,1–5,2 s, ~55–75 chars) **1664 ms** → ~+160 ms por segundo de áudio. Para **30 chars (~2,2 s)** o
mesmo ~1,2–1,3 s. Para **140 chars (~9–10 s)** a extrapolação linear dá ~2,3–2,5 s (+1,1–1,3 s); **não foi medido no mini** (bancada ocupada) e
fica como medida de 5 min depois do C2 se alguém quiser confirmar. Conclusão: o trecho curto NÃO ganha ~500 ms no Live, porque o 1º trecho já tem ≤ 18 chars; só o caso
"provedor sem streaming" teria 1º trecho de 26–43 chars, e aí o corte para 18 pouparia ~40–150 ms. **Descartada.**

### A′ — prefixo de ref de ~4 s em fronteira de palavra, com transcrição alinhada (pré-registro)

Por que A falhou (§5): o corte caiu no meio de palavra e o `ref_text` deixou de casar. Os tokens de referência NÃO mapeiam o tempo do
arquivo original: `create_voice_clone_prompt` reamostra a 24 kHz, normaliza o RMS e **remove silêncios** (`_remove_silence`) antes de tokenizar; os
25 tokens/s valem para o áudio JÁ processado. O alinhamento tem de ser feito sobre esse mesmo áudio processado.

**Desenho (sem tocar `voices/` nem o cadastro):** derivado e recalculável, em cache no estado do app (`state/ref-live/<voice_id>-<mtime>-<params>.json|npy`):
1. replicar o pré-processamento do `create_voice_clone_prompt` em RAM → áudio processado `x'`;
2. STT com timestamps por palavra sobre `x'` (mlx_whisper `word_timestamps=True`, motor já carregado) → `[(palavra, t0, t1)]`;
3. escolher o maior prefixo de palavras com `t1 ≤ ~4,0 s` (parar no fim de palavra, de preferência após pausa);
4. `tokens' = tokens[: round(t1 × 25)]` e `ref_text' = palavras até lá`;
5. o Live usa `(tokens', ref_text')` só no 1º trecho; a chave inclui mtime e parâmetros, então voz regravada invalida o cache; falha de STT → cai no prefixo inteiro (comportamento de hoje).

**Guardas e critérios (antes de medir; mini, estado sintético, FLEURS, em série após o C2):**
- **Ganho:** `tts_gen` p50 do 1º trecho −30 % ou mais nas vozes com prefixo > 4 s (hoje 227 tokens ≈ 9 s); morre abaixo de 15 %.
- **WER:** ≤ controle + 0,03 e **0 vazios** (A deu 1,10); pior caso ≤ 0,25.
- **Timbre:** ECAPA contra a amostra original, queda ≤ 0,05 no curto; p10 ≥ p10 do controle de ruído.
- **Alinhamento:** em ≥ 95 % das vozes sintéticas o corte cai em fronteira de palavra (conferir `t1` contra o silêncio mais próximo); `ref_text'` tem de ser prefixo literal do original.
- **ABX do dono:** só se passar tudo acima (8 pares).
- **Morte:** WER > ctrl + 0,03 ou ABX ≥ 7/8 contra A′. Variante: 6 s.
- **Restrições:** nenhum arquivo de `voices/` nem do cadastro é escrito; teste unitário só com voz sintética (fixture `voz_sintetica`/FLEURS); cache fora de `voices/`.
