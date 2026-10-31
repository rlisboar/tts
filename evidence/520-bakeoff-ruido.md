# Bake-off de redução de ruído (#520) — medido e decidido

Corpus público em tmp (o mesmo do #521), nunca `voices/`/`outputs/`.

**Como ler este arquivo:** as seções estão em ordem cronológica e a conclusão
MUDOU no meio do caminho. A primeira rodada (inglês) elegeu DFN3 na amostra e
RNNoise no Live; a rodada em pt com **fala real** mostrou que nenhum filtro
melhora o WER do STT. O veredito final está no fim — **leia a última seção antes
de tirar conclusão das tabelas do começo**.

| pergunta | resposta final |
|---|---|
| limpar a AMOSTRA de referência | **DFN3** (timbre 0,97–0,99, PESQ e STOI ganham) — virou o motor do #523, com guarda de timbre |
| filtro antes do STT | **não compra acerto** em fala real; o caminho do I2 é híbrido (RNNoise só no VAD/barge) |
| import com música | **Demucs** sozinho (WER 0,63 → 0,45); empilhar DFN3 depois não paga |

## Primeira rodada — inglês (DNSMOS 1–5; timbre = similaridade do locutor antes/depois; WER do Whisper)

| cenário | candidato | OVRL | timbre | WER | RTF |
|---|---|---|---|---|---|
| ventoinha 10 dB | atual (gating) | 2,82 | 0,822 | 0,913 | **0,002** |
| ventoinha 10 dB | RNNoise | 2,82 | 0,918 | **0,859** | 0,026 |
| ventoinha 10 dB | DeepFilterNet3 | **3,15** | **0,974** | 0,933 | 0,447 |
| ventoinha 5 dB | atual | 2,72 | 0,810 | 0,913 | **0,002** |
| ventoinha 5 dB | RNNoise | 2,77 | 0,895 | **0,859** | 0,027 |
| ventoinha 5 dB | DeepFilterNet3 | **3,12** | 0,944 | 0,933 | 0,459 |
| babble 10 dB | atual | 2,95 | 0,812 | 0,913 | **0,002** |
| babble 10 dB | RNNoise | 2,91 | 0,928 | 0,906 | 0,026 |
| babble 10 dB | DeepFilterNet3 | **3,05** | **0,953** | 0,913 | 0,453 |

O DFN3 limpa mais em OVRL; o RNNoise **preserva mais o timbre** em babble e tem o
**melhor WER** em ventoinha. O gating atual perde nos dois e só ganha em custo.

## Recomendação por caso

| caso | recomendação | por quê |
|---|---|---|
| (a) limpar a amostra de referência | **DeepFilterNet3** | é offline e o que mais preserva timbre em ruído de ventoinha (0,974) — o clone copia dessa amostra |
| (b) Live VAD/barge | **RNNoise (o de hoje)** | 17× mais barato (RTF 0,027), timbre melhor em babble e não atrasa o frame |
| (c) Live STT | **RNNoise** | melhor WER medido; o DFN3 piora o WER e custa 0,45 de RTF |
| (d) import com música | **Demucs → DFN3** | separação é outro problema; Demucs ainda não medido aqui |

Ou seja: **não trocar o filtro do Live**; o ganho real está na amostra (a) e no
import (d).

## O que a primeira rodada ainda NÃO sustentava (as seções seguintes cobrem)

- **MossFormer2 e Resemble-Enhance** não foram medidos (entram no mesmo runner,
  dependem de venv próprio).
- **Demucs** também não: a recomendação (d) é desenho, não número.
- PESQ/STOI e o custo no VAD (`smoke_live_turns.py` sob ventoinha) pendentes.
- Só ventoinha e babble, SNR 5 e 10 — falta 0 e 20 e as demais famílias.

## Ressalvas

- **O WER foi medido com `language="pt"` em áudio INGLÊS** (LibriSpeech): o valor
  absoluto (~0,9) está inflado; a comparação entre candidatos vale, o número não.
- Ruído sintético; a ventoinha do dono não foi gravada.
- Faltam SNR 0/20, teclado/música/rua, Demucs e o custo no VAD com
  `smoke_live_turns.py`, além de PESQ/STOI.
- Amostra pequena (3 arquivos por célula).

## Reprodução

```bash
./.venv-mlx/bin/python tools/bakeoff/corpus.py publico && ./.venv-mlx/bin/python tools/bakeoff/corpus.py monta
./.venv-mlx/bin/python tools/bakeoff/ruido_bakeoff.py --familias ventoinha,babble --snrs 5,10 --limite 3
```

---

# WER em pt-BR (ressalva do gate #529) — decide o I2

O R1 mediu WER em inglês; o produto é pt-BR. Fala em pt com a frase conhecida
(referência exata), ruído do R1, SNR fixo, 6 falantes por célula. Carga da máquina
no momento: **loadavg 4,92** — RTF aqui é de máquina ocupada, não de produção.

**WER (menor é melhor):**

| cenário | cru | RNNoise | DFN3 |
|---|---|---|---|
| ventoinha 5 dB | 0,600 | **0,533** | 0,622 |
| ventoinha 10 dB | 0,600 | **0,556** | 0,589 |
| babble 5 dB | 0,667 | **0,533** | 0,667 |
| babble 10 dB | 0,711 | **0,622** | 0,689 |

RTF: RNNoise 0,015–0,017; DFN3 0,062–0,104.

**RNNoise ganha nas quatro células**, por 0,03 a 0,14 de WER, e é 4–6× mais barato.
O DFN3 **não melhora o WER em pt** em nenhuma célula — empata no babble 5 dB e
piora no resto contra o cru. É o mesmo sentido do achado em inglês, agora com
número.

Consequência para o I2: **o filtro que fica ANTES do STT é o RNNoise**. O DFN3
segue sendo o certo para a AMOSTRA (preserva timbre, que é o que o clone copia),
não para o reconhecedor.

Ressalva do corpus: as vozes pt são sintéticas do sistema e o WER absoluto é alto
(0,53 no melhor caso); a comparação relativa é o que vale, e o mesmo texto é usado
nos três candidatos.

---

# Import com música (caso d) — Demucs medido

Música não é ruído estacionário: o denoise tira ventoinha e deixa o acompanhamento.
Hipótese testada: separar a voz PRIMEIRO (Demucs, stem vocals) e só depois, se for
o caso, denoisar. Música do R1 em SNR 5 e 10, fala em pt, 4 falantes por célula.
Carga no momento: loadavg 5,13.

**Controle da música:** a música sozinha, passada pelo Demucs, cai de rms 0,05049
para **0,00005** — mil vezes menos. A separação tira mesmo o acompanhamento, o
número adiante não é sorte.

| SNR | candidato | OVRL | timbre | WER | RTF |
|---|---|---|---|---|---|
| 5 | cru | 2,384 | 0,816 | 0,633 | 0,00 |
| 5 | só DFN3 | **2,609** | 0,945 | 0,583 | 0,01 |
| 5 | **só Demucs** | 2,533 | **0,987** | **0,450** | 0,63 |
| 5 | Demucs + DFN3 | 2,558 | 0,988 | 0,450 | 0,50 |
| 10 | cru | 2,514 | 0,854 | 0,617 | 0,00 |
| 10 | só DFN3 | **2,598** | 0,951 | 0,617 | 0,01 |
| 10 | **só Demucs** | 2,506 | **0,996** | **0,450** | 0,47 |
| 10 | Demucs + DFN3 | 2,542 | 0,996 | 0,467 | 0,50 |

**Recomendação: Demucs sozinho, como etapa PRIMÁRIA do import com música.** Ele
derruba o WER em 0,18 e leva o timbre a 0,99; é a única camada que mexe na música.
O DFN3 ganha no OVRL, mas **empilhar DFN3 depois do Demucs não paga**: WER igual a
SNR 5 e pior a SNR 10 (0,467 × 0,450), por +0,03 a 0,5 de RTF. Quem escolhe pelo
OVRL está escolhendo a métrica que menos importa aqui — o que se quer do import é
texto certo e voz preservada.

Custo: RTF 0,47–0,63 em CPU ocupada. É offline; para o Live não serve.

Licença: Demucs MIT (código e pesos), linha no `LICENCAS.md`. Roda na venv de
bancada — traz torch, então **não** entra no caminho do app por enquanto.

O que fica de fora: música com voz cantada (o stem `vocals` do Demucs leva o
cantor junto) e instrumento com timbre próximo da fala.

---

# SNR extremo (0 e 20 dB) com PESQ e STOI — e a tensão com o WER

As pontas que faltavam. O `cru` não é candidato aqui (as duas métricas comparam
contra a referência limpa) — ele é a linha de base de leitura.

| cenário | candidato | PESQ | STOI |
|---|---|---|---|
| ventoinha 0 dB | cru | 1,15 | 0,960 |
| ventoinha 0 dB | RNNoise | 1,41 | 0,810 |
| ventoinha 0 dB | **DFN3** | **2,53** | **0,980** |
| ventoinha 20 dB | cru | 2,86 | 1,000 |
| ventoinha 20 dB | RNNoise | 2,84 | 0,860 |
| ventoinha 20 dB | **DFN3** | **2,98** | **1,000** |
| babble 0 dB | cru | 1,08 | 0,960 |
| babble 0 dB | RNNoise | 1,16 | 0,810 |
| babble 0 dB | **DFN3** | **2,30** | **0,980** |
| babble 20 dB | cru | 2,31 | 1,000 |
| babble 20 dB | RNNoise | 2,42 | 0,860 |
| babble 20 dB | DFN3 | 2,42 | 1,000 |

**O DFN3 domina nas duas métricas, inclusive a 20 dB** (onde o filtro só podia
estragar, ele não estraga). O RNNoise **derruba o STOI para 0,81–0,86 em TODOS os
SNR**, inclusive no quase limpo — é degradação estrutural, não só de ruído.

## A tensão, declarada

Isto **contradiz o WER em pt**, que elegeu o RNNoise (0,533 contra 0,622 do DFN3).
Duas leituras possíveis, e o dado não decide sozinho:

1. STOI/PESQ punem o processamento característico do RNNoise, mas o reconhecedor
   se dá bem com ele — nesse caso o WER é que manda em STT, e o STOI só está
   medindo outra coisa.
2. O WER em pt com 6 falantes sintéticos tem ruído de amostragem e o ganho do
   RNNoise é menor do que parecia.

**Como fica a recomendação do I2:** WER continua sendo o critério para o STT
(é o que o produto entrega), mas a força dele caiu para "1 experimento contra 2
métricas". Antes de o filtro virar default, vale um terceiro eixo: WER com fala
espontânea em pt, ou WER com mais falantes. Não declaro o caso fechado.

O que **não** muda: para a AMOSTRA, o DFN3 segue vencedor em todas as métricas
(OVRL, timbre, PESQ, STOI).

---

# WER com fala pt REAL (R1b) — e as duas conclusões anteriores caem

Corpus **real**: FLEURS pt_br (`google/fleurs`, CC BY 4.0), 30 locutores DIFERENTES,
uma fala cada, transcrita no corpus, fala inteira (mediana 12 s). Nada de voz do
dono. Carga no momento: loadavg registrado no json.

| cenário | cru | RNNoise | DFN3 |
|---|---|---|---|
| ventoinha 0 dB | 0,146 | 0,146 | 0,148 |
| ventoinha 5 dB | 0,145 | 0,143 | 0,149 |
| ventoinha 20 dB | 0,144 | 0,146 | 0,144 |
| babble 0 dB | **0,142** | 0,147 | 0,149 |
| babble 5 dB | **0,142** | 0,147 | 0,149 |
| babble 20 dB | **0,142** | 0,148 | 0,147 |

**Nenhum filtro melhora o WER em fala real.** O cru empata nas ventoinhas e **ganha
em todo o babble**. Diferenças de 0,002–0,007 estão dentro do ruído de amostragem
com 30 locutores.

## As duas conclusões anteriores caem

1. **Vozes sintéticas do sistema** diziam que o RNNoise ajudava (0,533 × 0,622). Em
   fala real ele não ajuda.
2. **PESQ/STOI** diziam que o DFN3 era melhor em tudo — inclusive o RNNoise
   derrubando o STOI a 0,81–0,86. Em WER real, o DFN3 também não ajuda.

O que os três experimentos concordam: **o DFN3 não piora** o reconhecimento. O que
só o experimento real mostra: **filtrar antes do STT não compra acerto** — compra
outra coisa (menos ruído audível, timbre melhor na amostra) e cobra RTF.

## Consequência para o I2

O filtro antes do STT deixa de ser ganho de acerto e passa a ser escolha de custo e
de experiência: se for para ter um, é o **RNNoise** (0,015 de RTF, neutro no WER);
o DFN3 não se paga nesse ponto da cadeia (0,5–1,0 de RTF para WER igual ou pior).
E vale reavaliar se o I2 precisa de filtro antes do reconhecedor **ou só atrás do
VAD**, que é onde o R1 já o tinha.

Ressalvas: FLEURS é leitura de frases de Wikipédia (não é fala espontânea), os
ruídos são sintéticos e o piso do Whisper nesse corpus já é 0,14 — um ganho
pequeno teria pouco espaço para aparecer.
