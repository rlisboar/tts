# #1079 — piso de `num_steps` do 1º trecho do Live

Pergunta do card: até onde descer o número de passos de unmasking do PRIMEIRO
trecho sem perder timbre, sem piorar o WER e passando na escuta. Custo já medido
pelo speech-pipeline (16 = 1807 ms · 12 = 1362 · 8 = 915, isolado no mini).

**Resposta: o piso é 10.** 8 também passa no critério literal de 0,80 — mas é o
pior dos quatro no tamanho REAL do 1º trecho e dobra a perda de timbre em relação
ao 10. O ABX abaixo decide se 8 ainda se sustenta na escuta.

## Régua: por que 0,80 não se lê no cru

Duas coisas que a primeira tentativa mostrou e que mudaram o desenho:

1. **A geração é DETERMINÍSTICA por semente** — 16 passos contra 16 passos com a
   mesma semente dá cosseno **1,000 exato**. Repetir o mesmo nível não produz piso
   de ruído nenhum. O piso passou a vir de um **CONTROLE**: mesma condição (16
   passos), **outra semente**. O que a redução de passos faz é lido contra ele.
2. **O ECAPA comprime em trecho curto.** A própria voz de referência contra um
   pedaço dela mesma dá 0,56 (1,5 s) a 0,83 (2,2 s) — o 0,80 do card foi
   calibrado em 3 s e não atravessa para a faixa do 1º trecho. Por isso a medição
   tem dois buckets: `curto` (1,4–2,2 s, o tamanho real) e `longo` (4,1–5,2 s, a
   faixa em que o número 0,80 significa alguma coisa).

O áudio de cada nível é comparado ao de 16 passos **do mesmo texto e da mesma
semente**, depois do MESMO pós do Live (`_pos_live`: trim → normalize → EQ/fade —
sem ele o áudio cru sai ~40 dB baixo e o VAD do STT devolve vazio em tudo).

12 textos (6 curtos, 6 longos) × 2 sementes × 5 condições = **120 gerações**,
voz `7b620feaba`, `omni_precision` bf16, mini com a produção intacta na 7860.

## Custo (mediana do `generate`, ms)

| passos | curto (1,4–2,2 s) | longo (4,1–5,2 s) |
|---|---|---|
| 16 | 1859 | 2597 |
| 16b (controle) | 1866 | 2609 |
| 12 | 1410 (−24 %) | 1971 (−24 %) |
| 10 | 1183 (−36 %) | 1664 (−36 %) |
| 8 | **949 (−49 %)** | **1346 (−48 %)** |

## Timbre (cosseno ECAPA contra o áudio de 16 passos; média, p10, mínimo)

| condição | curto | longo | p (Wilcoxon pareado vs controle) |
|---|---|---|---|
| 16b (controle) | 0,603 · 0,501 · 0,494 | **0,808** · 0,755 · 0,751 | — |
| 12 | 0,534 · 0,421 · 0,421 | 0,821 · 0,755 · 0,750 | 0,047 · 0,202 |
| 10 | **0,552** · 0,472 · 0,465 | 0,799 · 0,760 · 0,760 | 0,062 · 0,281 |
| 8 | 0,476 · 0,316 · 0,308 | 0,812 · 0,778 · 0,777 | **0,018** · 0,830 |

Leitura: no áudio longo — a faixa do critério — **o número de passos não move o
timbre**: as três reduções ficam no nível do controle (p de 0,20 a 0,83) e todas
batem o 0,80. No tamanho real do 1º trecho a escala é comprimida e a ordem se
abre: 10 e 12 descem ~0,05–0,07 do controle, 8 desce **0,127** — o dobro.

## WER (o áudio volta ao STT; mesmo corpus)

| condição | WER médio | pior caso | transcrições vazias |
|---|---|---|---|
| 16 | 0,000 | 0,000 | 0 |
| 16b (controle) | **0,083** | 1,000 | **2** |
| 12 | 0,008 | 0,091 | 0 |
| 10 | 0,008 | 0,091 | 0 |
| 8 | 0,008 | 0,091 | 0 |

Cada redução erra **uma palavra em 24 falas** (12 e 8: "por" em "vamos por
partes"; 10: "tenta" → "entra"). O controle, com 16 passos e outra semente,
**derruba um texto inteiro** ("Claro," sai vazio nas duas repetições). Ou seja:
16 não é imune, e a diferença de WER entre 12/10/8 é de uma palavra — ruído.

## Escuta (ABX)

24 pares 16×10 e 16×8, A/B sorteados, `index.html` com player:
`/tmp/abx1079/abx8/index.html` (7 MB, fora do repo). Gabarito dentro do arquivo.

## Decisão

**Piso = 10.** O critério literal (0,80) é cumprido por todos no áudio longo, mas
o 1º trecho é curto por definição e é ali que a diferença aparece: o 10 fica a
0,051 do controle (dentro do ruído, p = 0,062) e já corta 36 % do `gen`; o 8
corta 49 % e cobra 0,127 de timbre no mesmo trecho.

Se a escuta não achar diferença nos pares 16×8, o 8 é defensável — mas então a
decisão é auditiva, não numérica, e deve estar escrita assim.

## Limites

- Uma voz (`7b620feaba`) e um locutor: timbre de outra voz pode se comportar
  diferente nos 8 passos.
- O `gen` foi medido in-process, sem a bancada: é o trabalho do modelo, sem fila
  nem lock, então o número é o piso do estágio, não o S2.
- Textos curtos são o pior caso do ECAPA; o bucket `longo` existe para o critério
  ter onde valer, não para maquiar o curto.
