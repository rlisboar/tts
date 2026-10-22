# #442 — decisão do default do RNNoise: o filtro serve ao VAD, não ao STT

Medição com as duas métricas, na mesma bancada. Fecha o intervalo do F5 e responde
o que o #278 prometia. **Nenhum default mudado** — a troca depende de OK do dono.

Leitura de uma linha: o filtro **salva o VAD no ruído alto e estraga o STT**; por
isso a recomendação é **híbrida** — filtro **só no VAD**, PCM cru no STT.

## 1. Métrica do objetivo (VAD e barge), motor real `live_turns`

Ventoinha sintética (passa-baixa 900 Hz + tom de 118 Hz), 60 s só de ruído para os
falsos inícios e estímulo de fala sobre o ruído para a abertura de turno.

| ventoinha | condição | falsos inícios/min | abre o turno | `fala_ms` |
| --- | --- | --- | --- | --- |
| −27 dBFS | sem filtro | 0 | sim | 672 |
| −27 dBFS | **com filtro** | 0 | sim | 1216 |
| −17 dBFS | sem filtro | 1 | **NÃO** | 0 |
| −17 dBFS | **com filtro** | 0 | **SIM** | 992 |
| −7 dBFS | sem filtro | 1 | não | 0 |
| −7 dBFS | com filtro | 1 | não | 0 |

Barge (eco da IA tocando + humano por cima): dispara nos DOIS lados em −27.

**É aqui que o filtro ganha a vida dele**: com ventoinha alta (−17) o motor SEM
filtro não abre turno nenhum — a fala do humano é engolida. Com filtro, abre e
detecta 992 ms. Em −27 (ventoinha baixa) não há diferença de abertura, e o
`fala_ms` sobe (1216 contra 672) porque o VAD enxerga mais fala.

## 2. WER (2 vozes × 5 janelas, 3 s cada)

| ventoinha | cru | filtrado |
| --- | --- | --- |
| −27 dBFS | 0 % (baseline) | **52,6 %** |
| −17 dBFS | 0 % (baseline) | **50,0 %** |

Metade das palavras muda quando o filtro entra no caminho do STT — em qualquer dos
níveis. Confirma e aperta o F5 (que media outro baseline).

## 3. Por que a híbrida, e o que ela custa

O filtro melhora o que DECIDE (VAD) e degrada o que TRANScreve (STT). Separar os
caminhos pega os dois ganhos:

- **custo de código**: o evento de `speech_start`/`speech_end` carrega o áudio do
  pré-roll — hoje ele vem do próprio stream que o motor consumiu. Na híbrida é
  preciso manter um ring buffer do PCM **cru** alinhado por índice de amostra e
  fatiar por `evento.amostra` (o motor continua recebendo o filtrado). Não é
  trivial, mas é local ao `_live_pcm_limpo` + `_live_trata_eventos`.
- **custo de CPU/memória**: o filtro já roda; o buffer cru extra é 32 kB/s.
- **risco novo**: o filtro pode furar frame (`faltas`) — medido **0 em 60 s** de
  processamento contínuo agora, então o buraco atinge o VAD quase nunca; e atinge
  só o VAD, nunca o STT (é o oposto do desenho atual).
- **o que NÃO resolve**: o `speech_end`/barge continuam decididos no sinal
  filtrado — é o desejado.

## 4. Recomendação

1. **Híbrida como default** (filtro no VAD, cru no STT). É a única das quatro que
   não tem lado perdedor: mantém a abertura de turno em ventoinha alta e devolve as
   palavras ao STT.
2. Se a híbrida não couber agora: **desligar** o filtro do caminho do STT inteiro
   (`TTS_LIVE_RNNOISE=0`) é melhor que o estado atual, que perde metade das
   palavras em troca de VAD — mas aí se perde a abertura em ventoinha alta.
3. **Não** manter o default atual (filtro em tudo) sem mudança: é o pior dos dois
   em qualidade de transcrição.

## 5. Roteiro manual de 2 minutos (metade do navegador, que não roda offline)

Para o dono, com ventoinha ligada e na mesma distância de uso:

1. Abrir o Live (Configurações → Live) e iniciar uma sessão.
2. Falar 5 frases curtas de conteúdo conhecido (ex.: "o prazo é sexta", "me manda
   o relatório", "não, o outro").
3. Anotar quantas voltaram certas no `transcript_user` da tela.
4. Repetir com o NS do navegador desligado — hoje é preciso editar
   `noiseSuppression` em `static/index.html` ou usar o `chrome://flags` de captura
   com NS off.
5. Comparar as duas listas de transcrição e a coluna `stats.mic.dbfs` (o piso de
   ruído deve cair quando o NS volta).

Se as duas rodadas derem o mesmo resultado, o NS do navegador pode ficar como está;
se a rodada com NS perder palavras, o NS sai **só no Live** (decisão do frontend).

## 6. Limites

- Ventoinha SINTÉTICA (não é a do dono), 2 vozes, modelo `sh.rnnn` sem garantia
  para PT, sem repetição estatística — a direção dos dois efeitos foi consistente
  em todos os níveis medidos, que é o que a decisão precisa.
- O WER acima é contra o próprio transcript cru (0 % por construção), não contra
  texto de referência: mede DESVIO, não acerto absoluto.
