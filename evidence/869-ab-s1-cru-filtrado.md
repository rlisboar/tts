# #869 item 3 — S1 isolado do PCM cru: o `motivo=vad` só aparece com o filtrado no STT

Data: 2026-11-08. Máquina: MacBook Pro (dev), **não** o mini. Sem servidor, sem LLM,
sem rede; produção intocada. Script: `evidence/869-ab-s1-cru-filtrado.py`.

## O que isola

Mesmo SHA, mesmo áudio, mesma semente. Só muda o que o STT do turno recebe:
`TTS_LIVE_RNNOISE_STT=cru` × `filtrado`. O resto é o caminho real: ring
`_live_cru_guarda` → RNNoise real (ffmpeg `arnndn`) `_live_pcm_limpo` →
`TurnEngine.feed` → `speech_end` → `_live_audio_cru` → `live_pipeline._stt_julga`
(o Silero de `_transcribe`, o Whisper do Live e o juiz `_stt_ok`). Ficam fora o portão
"só a minha voz" e a separação (outros cards).

- Falas: as 16 vozes reais de `voices/` que têm `ref_text`. As `vd-*` são sintéticas
  e ficam fora. Reamostradas para 16 kHz.
- Por fala, duas formas: **longa** (o arquivo inteiro, 5–23 s, que o motor fatia em
  turnos) e **curta** (1,5 s a partir de 0,3 s, o perfil do relato do dono).
- Cenários (ruído gaussiano somado; fala normalizada em RMS):
  `limpo` (fala −26 dBFS, piso −60), `ventoinha` (−26 / −45), `baixo` (−38 / −50).
- Duas rodadas: `0343e36` (main, ring do cru 10 s) e `0343e36` + o ring de 34 s
  deste card.

## Resultado

Turnos aceitos (soma dos 3 cenários × longa + curta, 130 turnos por modo):

| | cru | filtrado |
|---|---|---|
| turnos aceitos | **129/130 (99,2 %)** | 121/130 (93,1 %) |
| `motivo=vad` (Silero recusa o turno que o motor fechou) | **0** | **5** |
| `motivo=stt` (baixa confiança) | 1 | 4 |

Os mesmos números saem nas duas rodadas, porque o ring não muda nenhuma decisão de
aceite. O que o ring muda é **quanto** do turno chega cru ao STT e o WER dos
turnos longos:

| cenário (longa) | WER mediano, filtrado | cru com ring 10 s (cru em 20/28 turnos) | cru com ring 34 s (cru em 28/28 turnos) |
|---|---|---|---|
| limpo | 0,032 | 0,031 | 0,031 |
| ventoinha | 0,083 | 0,076 | **0,047** |
| baixo | 0,169 | 0,099 | **0,088** |

Tabela crua: ver `resumo` abaixo.

## O que isto fecha

1. **O `motivo=vad` do relato** ("não reproduzido" em `869-live-vad-cru.md`) **aparece
   aqui só no caminho filtrado.** São 5 turnos que o motor fechou como fala e que o
   Silero recusou depois do RNNoise; no cru, nenhum. Em produção, até o `ee970d6`, o
   STT recebia o filtrado em 100% dos turnos (ACHADO 1 do card). Esse é o mecanismo
   do `motivo=vad`: o portão do turno lê o áudio que o RNNoise já comeu. O PCM cru
   remove esse mecanismo.
2. **Achado novo (consertado neste card):** o ring guardava 10 s e o recorte pede o
   turno inteiro. Todo turno de fala contínua acima de ~10 s ia calado ao filtrado
   (8 dos 28 turnos longos aqui). O motor fecha turnos de até `turno_max_ms` = 30 s,
   então o ring passou a 34 s (≈1,1 MB por sessão). A célula
   `test_turno_longo_tambem_vai_cru_ao_stt` morde: com 10 s, um turno de 15,4 s chega
   FILTRADO.

## Limites (não é o S1 do dono)

- O ruído é gaussiano, não a captura do Chrome (EC e NS ligados, AGC desligado). As
  vozes de `voices/` são, em parte, `denoised`. O efeito na captura real pode ser
  maior ou menor.
- n = 16 falas, 14–28 turnos por célula. As taxas são da bancada offline, não da
  sessão de 10 falas reais.
- WER médio alto (0,18–0,32) por 1–2 arquivos cujo `ref_text` não cobre o áudio
  inteiro; por isso a mediana.
- O antes/depois na captura real (S1 da sessão do dono) continua no #875, quando
  abrir a janela do mini. Este A/B atribui o ganho ao PCM cru **isolado**, que era o
  que faltava (a rodada `4b3ef88` misturava #870, #871 e a marca).

## resumo
```
== /tmp/ab869/ab_s1-0343e36.json
cenario   tipo  modo      falas_ok  turnos_ok  cru/turnos  WER_med  WER_media  recusas
baixo     curta cru       13/16     13/14      14/14         nan        nan  {'stt:baixa confiança (-1.31)': 1}
baixo     curta filtrado  13/16     13/14       0/14         nan        nan  {'vad:None': 1}
baixo     longa cru       16/16     28/28      20/28       0.099      0.270  {}
baixo     longa filtrado  16/16     26/28       0/28       0.169      0.323  {'vad:None': 1, 'stt:baixa confiança (-1.35)': 1}
limpo     curta cru       16/16     16/16      16/16         nan        nan  {}
limpo     curta filtrado  15/16     15/16       0/16         nan        nan  {'stt:baixa confiança (-1.08)': 1}
limpo     longa cru       16/16     28/28      20/28       0.031      0.178  {}
limpo     longa filtrado  16/16     27/28       0/28       0.032      0.192  {'vad:None': 1}
ventoinha curta cru       16/16     16/16      16/16         nan        nan  {}
ventoinha curta filtrado  14/16     14/16       0/16         nan        nan  {'stt:baixa confiança (-1.00)': 1, 'vad:None': 1}
ventoinha longa cru       16/16     28/28      20/28       0.076      0.220  {}
ventoinha longa filtrado  16/16     26/28       0/28       0.083      0.260  {'stt:baixa confiança (-1.48)': 1, 'vad:None': 1}
== /tmp/ab869/ab_s1-ring34.json
cenario   tipo  modo      falas_ok  turnos_ok  cru/turnos  WER_med  WER_media  recusas
baixo     curta cru       13/16     13/14      14/14         nan        nan  {'stt:baixa confiança (-1.31)': 1}
baixo     curta filtrado  13/16     13/14       0/14         nan        nan  {'vad:None': 1}
baixo     longa cru       16/16     28/28      28/28       0.088      0.260  {}
baixo     longa filtrado  16/16     26/28       0/28       0.169      0.323  {'vad:None': 1, 'stt:baixa confiança (-1.35)': 1}
limpo     curta cru       16/16     16/16      16/16         nan        nan  {}
limpo     curta filtrado  15/16     15/16       0/16         nan        nan  {'stt:baixa confiança (-1.08)': 1}
limpo     longa cru       16/16     28/28      28/28       0.031      0.180  {}
limpo     longa filtrado  16/16     27/28       0/28       0.032      0.192  {'vad:None': 1}
ventoinha curta cru       16/16     16/16      16/16         nan        nan  {}
ventoinha curta filtrado  14/16     14/16       0/16         nan        nan  {'stt:baixa confiança (-1.00)': 1, 'vad:None': 1}
ventoinha longa cru       16/16     28/28      28/28       0.047      0.206  {}
ventoinha longa filtrado  16/16     26/28       0/28       0.083      0.260  {'stt:baixa confiança (-1.48)': 1, 'vad:None': 1}
```
