# Locutor-alvo (#532) — primeiro corte

Estado: **começando**. O que já se sabe e o que falta.

## A distinção que o card faz, confirmada na prática

Denoise não resolve fala de terceiro e identidade não limpa a mistura. Medido:

- **Sobreposição total** (alvo + concorrente somados do começo ao fim): selecionar
  janelas por similaridade **não corta nada** — todas as janelas contêm as duas
  vozes. WER fica igual antes e depois da seleção (0,88 → 0,88). Só um modelo de
  **separação** resolve este caso.
- **Sobreposição parcial** (blocos alternando): aí a seleção por similaridade
  funciona — com o ECAPA do R2, o bloco do alvo pontua **0,88** e o do concorrente
  **0,66**, e o corte relativo descarta **62 %** do áudio.

Ou seja: o caso "outra pessoa falando" tem dois regimes e eles pedem camadas
diferentes. A recomendação final precisa separar os dois.

## O que ainda falta

- A **separação** propriamente dita (MossFormer2_SS_16K está no HF; o `funasr` tem
  módulo `mossformer`) e o TSE condicionado no embedding.
- SI-SDRi, taxa de inserção de terceiros, turnos falsos com só terceiros em 10 min,
  perda de turno do alvo, latência em streaming, memória e a curva por duração de
  cadastro (10/30/60 s).
- SIR 0/10 e as misturas com os ruídos do R1.

## Bug aberto no instrumento (declarado)

O runner (`tools/bakeoff/alvo.py`) registra **0 % de corte** no cenário alternado,
enquanto a chamada isolada da mesma função corta 62 % com os mesmos arquivos. É
defeito do runner, não do método; a evidência acima usa a medição isolada. A
suspeita é o reaproveitamento dos arquivos temporários entre iterações do laço.

## Reprodução

```bash
./.venv-mlx/bin/python tools/bakeoff/alvo.py --sirs 5 --n 4
```
