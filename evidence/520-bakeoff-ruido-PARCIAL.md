# Bake-off de redução de ruído (#520) — parcial com recomendação

Estado: **medido em parte**, com os quatro casos recomendados abaixo. Corpus
público em tmp (o mesmo do #521), nunca `voices/`/`outputs/`.

## Números (DNSMOS 1–5; timbre = similaridade do locutor antes/depois; WER do Whisper)

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

## O que a medição ainda NÃO sustenta

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
