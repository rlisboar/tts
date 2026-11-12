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
