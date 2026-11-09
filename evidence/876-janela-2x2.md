# Janela 2×2 do #876 (motor do STT) × #870 (duck rápido) — mini de produção

Rodada ÚNICA, em série, no mini (16 GB), instância de bancada na 7890 e produção
na 7860 **intocada** durante toda a janela. `--sha 48b11d5` (o deploy do dia),
estado sintético em `/tmp/bench-mini/.state-local`, corpus de 36 arquivos em
`/tmp/live-bench-audio`, semente 875, 10 ciclos contados + 1 de aquecimento por
célula, ordem `f0-c0 → f0-c1 → f1-c1 → f1-c0` (só o motor inverte entre os
blocos de flag).

## Duas condições de contorno, declaradas antes de olhar número

1. **LLM falso** (`tools/llm_fake_sse.py` na 7898, TTFT fixo de 300 ms). O
   provedor do mini (`llm.glm.model.craton.app`) estava em HTTP 530 desde ~01:03.
   Consequência: **o S2 absoluto é PISO, não produção** — em produção o TTFT é
   0,8–1,3 s. O comparável é o `pos_llm` e o delta entre células.
2. **`app.py` com o fix do stream do MLX** (`de1ae52`, ainda não deployado) —
   sem ele o C1 não transcreve nada (`RuntimeError: There is no Stream(gpu, 0) in
   current thread`). O `codigo` no ar da produção seguia batendo com `48b11d5`.

## Resultado

```
# Janela 2×2 #876 × #870 — SHA 48b11d5 — /tmp/rodada-876-1794234572
# ORDEM EXECUTADA (do marcas.txt): f0-c0 f0-c1 f1-c1 f1-c0
# f0/f1 = TTS_LIVE_BARGE_DUCK_RAPIDO off/on · c0/c1 = whisper/parakeet · aquecimento descartado: 1 ciclo(s) por célula
# LIMITES DECLARADOS ANTES: pausa p95 ≤800 ms · duck p50 ≤300 ms · eco_falso_duck ≤5 % (PONTO) · eco_falso_corte ≤ C1/F0 · S1-S4 e RSS sem piora
# LLM do chat: http://127.0.0.1:7898/v1
# LLM FIXO (fake): o S2 ABSOLUTO é PISO, não produção — o veredito do eixo usa o `pos_llm` e o delta entre células; o `llm_ttft` fica constante.
# n<20 por célula: a p95 é frouxa — leia com a mediana e o máximo ao lado; a decisão NÃO sai só da p95.

  célula   n_falas  stt_p50  duck_p50  pausa_p50  pausa_p95  pausa_max  stop_p50  eco_duck   eco_corte   S4_simb%
  f0-c0    20       1045     754       —          —          —          4180      4/9         0/9          0.0
  f0-c1    20       289      754       —          —          —          2943      4/10        1/10         0.0
  f1-c0    20       1055     455       1103       1303       1303       4176      8/9         3/9          0.0
  f1-c1    20       343      455       811        1307       1307       2870      10/10        5/10         0.0

## Veredito da flag `TTS_LIVE_BARGE_DUCK_RAPIDO` (célula f1-c1)

  pausa p95 ≤ 800 ms           FALHA
  duck p50 ≤ 300 ms            FALHA
  eco_falso_duck (ponto)       NÃO LIGA — 10/10 = 100.0 % acima de 10 % (IC95 [72.2, 100.0] com n=10)
  eco_falso_corte ≤ C1/F0      FALHA
  S1 sem piora                 PASSA
  WER A sem piora (pareado)    PASSA
  S2 sem piora                 PASSA
  S3 duck sem piora            PASSA
  S4 sem piora                 PASSA
  RSS ≤ C1/F0                  PASSA

  >>> NÃO LIGA
  eco_falso_duck: NÃO LIGA — 10/10 = 100.0 % acima de 10 % (IC95 [72.2, 100.0] com n=10)
  eco_falso_corte: 5/10 na f1-c1 × 1/10 na C1/F0 · WER pareado em 10 fala(s)
  (pausa em 7 interrupção(ões): p50=811 ms · máx=1307 ms · n<20, p95 frouxa — a decisão não sai só da p95)

## Eixo do motor (#876): c0 × c1 dentro de cada flag state

  f0: stt p50 1045 → 289 ms (PASSA) · WER pareado 0.654 → 0.578 (-7.6 pp; PASSA; n=10) · S1 65.0% → 80.0% (PASSA)
  f1: stt p50 1055 → 343 ms (PASSA) · WER pareado 0.631 → 0.491 (-14.0 pp; PASSA; n=10) · S1 70.0% → 85.0% (PASSA)

## Falso turno por ruído (fases)

  f0-c0: eco 0/13 IC95 [0.0, 22.8] · ventoinha 0 falso(s) em 180 s (n=1)
  f0-c1: eco 3/9 IC95 [12.1, 64.6] · ventoinha 0 falso(s) em 180 s (n=1)
  f1-c0: eco 0/9 IC95 [0.0, 29.9] · ventoinha 0 falso(s) em 180 s (n=1)
  f1-c1: eco 3/19 IC95 [5.5, 37.6] · ventoinha 0 falso(s) em 180 s (n=1)

## S1–S5 por célula (amostra sem aquecimento)

  f0-c0: S1 {"n": 20, "aceitas": 65.0} S2 {"n": 17, "p50_ms": 3747} · S4 0.0% · RSS 1087 MB · S5 ws_vivo=True erro=None
  f0-c1: S1 {"n": 20, "aceitas": 80.0} S2 {"n": 18, "p50_ms": 2971} · S4 0.0% · RSS 1312 MB · S5 ws_vivo=True erro=None
  f1-c0: S1 {"n": 20, "aceitas": 70.0} S2 {"n": 18, "p50_ms": 3778} · S4 0.0% · RSS 2229 MB · S5 ws_vivo=True erro=None
  f1-c1: S1 {"n": 20, "aceitas": 85.0} S2 {"n": 18, "p50_ms": 2939} · S4 0.0% · RSS 628 MB · S5 ws_vivo=True erro=None

## Curva RSS/swap (85 amostras, 1/min)

  iso                       rss_mb   swap_mb
  2026-11-09T11:29:32-03:00     328      4953
  2026-11-09T11:30:32-03:00     307      2163
  2026-11-09T11:31:32-03:00     307      2163
  2026-11-09T11:32:32-03:00     314      2147
  2026-11-09T11:33:32-03:00     314      2147
  2026-11-09T11:34:32-03:00     314      2123
  2026-11-09T11:35:32-03:00     314      2123
  2026-11-09T11:36:33-03:00     322      2123
  2026-11-09T11:37:33-03:00     322      2115
  2026-11-09T11:38:33-03:00     330      2107
  2026-11-09T11:39:33-03:00     328      2107
  2026-11-09T11:40:33-03:00     328      2099
  2026-11-09T11:41:33-03:00     328      2099
  2026-11-09T11:42:33-03:00     328      2099
  2026-11-09T11:43:33-03:00     328      2083
  2026-11-09T11:44:33-03:00     328      2075
  2026-11-09T11:45:33-03:00     328      2075
  2026-11-09T11:46:33-03:00     328      2067
  2026-11-09T11:47:33-03:00     328      2067
  2026-11-09T11:48:33-03:00     328      2019
  2026-11-09T11:49:33-03:00     363      1196
  2026-11-09T11:50:33-03:00     363      1196
  2026-11-09T11:51:33-03:00     363      1196
  2026-11-09T11:52:33-03:00     363      1180
  2026-11-09T11:53:33-03:00     363      1180
  2026-11-09T11:54:33-03:00     363      1180
  2026-11-09T11:55:33-03:00     363      1180
  2026-11-09T11:56:33-03:00     363      1180
  2026-11-09T11:57:33-03:00     682      1140
  2026-11-09T11:58:33-03:00     414      2707
  2026-11-09T11:59:33-03:00     414      2683
  2026-11-09T12:00:33-03:00     414      2659
  2026-11-09T12:01:33-03:00     414      2659
  2026-11-09T12:02:33-03:00     414      2643
  2026-11-09T12:03:33-03:00     414      2619
  2026-11-09T12:04:34-03:00     414      2603
  2026-11-09T12:05:34-03:00     414      2603
  2026-11-09T12:06:34-03:00     414      2603
  2026-11-09T12:07:34-03:00     414      2571
  2026-11-09T12:08:34-03:00     366      2571
  2026-11-09T12:09:34-03:00     366      2563
  2026-11-09T12:10:34-03:00     366      2563
  2026-11-09T12:11:34-03:00     366      2555
  2026-11-09T12:12:34-03:00     366      2555
  2026-11-09T12:13:34-03:00     366      2555
  2026-11-09T12:14:34-03:00     366      2547
  2026-11-09T12:15:34-03:00     366      2539
  2026-11-09T12:16:34-03:00     366      2539
  2026-11-09T12:17:34-03:00     366      2531
  2026-11-09T12:18:34-03:00     366      2523
  2026-11-09T12:19:34-03:00     366      2515
  2026-11-09T12:20:34-03:00     366      2499
  2026-11-09T12:21:34-03:00     366      2491
  2026-11-09T12:22:34-03:00     366      2491
  2026-11-09T12:23:34-03:00     366      2475
  2026-11-09T12:24:34-03:00     366      2475
  2026-11-09T12:25:34-03:00     366      2467
  2026-11-09T12:26:34-03:00     326      1274
  2026-11-09T12:27:34-03:00     508      1589
  2026-11-09T12:28:34-03:00     518      1565
  2026-11-09T12:29:34-03:00     518      1549
  2026-11-09T12:30:34-03:00     518      1541
  2026-11-09T12:31:34-03:00     518      1541
  2026-11-09T12:32:34-03:00     518      1517
  2026-11-09T12:33:34-03:00     518      1501
  2026-11-09T12:34:34-03:00     518      1389
  2026-11-09T12:35:34-03:00     518      1389
  2026-11-09T12:36:34-03:00     518      1389
  2026-11-09T12:37:34-03:00     516      1381
  2026-11-09T12:38:34-03:00     370      1381
  2026-11-09T12:39:34-03:00     370      1381
  2026-11-09T12:40:34-03:00     370      1381
  2026-11-09T12:41:35-03:00     370      1381
  2026-11-09T12:42:35-03:00     370      1373
  2026-11-09T12:43:35-03:00     370      1373
  2026-11-09T12:44:35-03:00     370      1373
  2026-11-09T12:45:35-03:00     370      1373
  2026-11-09T12:46:35-03:00     370      1373
  2026-11-09T12:47:35-03:00     370      1373
  2026-11-09T12:48:35-03:00     370      1349
  2026-11-09T12:49:35-03:00     370      1349
  2026-11-09T12:50:35-03:00     370      1349
  2026-11-09T12:51:35-03:00     370      1341
  2026-11-09T12:52:35-03:00     370      1341
  2026-11-09T12:53:35-03:00     370      1341

## Janelas de eco com falso turno (6)

  k=4 inicio_s=54.65 eventos=['interrupted', 'interrupted', 'speech_start']
  k=7 inicio_s=80.6 eventos=['speech_start', 'transcript_user', 'interrupted', 'interrupted', 'speech_start']
  k=9 inicio_s=112.03 eventos=['interrupted', 'interrupted', 'speech_start']
  k=4 inicio_s=55.25 eventos=['interrupted', 'interrupted', 'speech_start']
  k=12 inicio_s=119.78 eventos=['speech_start', 'transcript_user', 'speech_start', 'interrupted']
  k=14 inicio_s=131.64 eventos=['speech_start', 'transcript_user', 'speech_start', 'interrupted']
```

## Leitura

**Eixo do motor (C0 whisper × C1 parakeet) — o C1 ganha com folga, nas duas
flag states:**

| flag | stt p50 | WER pareado | S1 |
|---|---|---|---|
| off | 1045 → **289 ms** (−72 %) | 0,654 → 0,578 (−7,6 pp) | 65 % → 80 % |
| on | 1055 → **343 ms** (−67 %) | 0,631 → 0,491 (−14,0 pp) | 70 % → 85 % |

É o mesmo sentido do pré-registro (parakeet ~30 % mais rápido e ~1 pp mais WER
em EN); aqui, em PT e com o corpus de `voices/`, o parakeet é **melhor também em
WER**. Nenhuma célula C1 piorou S1, S2, S3, S4 ou RSS.

**Eixo da flag `TTS_LIVE_BARGE_DUCK_RAPIDO` — NÃO LIGA.** Três critérios duros
falham no C1: `eco_falso_duck` 10/10 (limite 5 %), pausa p95 1307 ms (limite
800) e duck p50 455 ms (limite 300). `eco_falso_corte` também piora (5/10 contra
1/10 na C1/F0). O que a flag compra é real mas insuficiente: o duck cai de 754
para 455 ms (−40 %), e é ela que faz o barge interromper (sem a flag não há
`pausa` nenhuma para medir). O preço é o eco virar turno em toda janela de
playback no C1.

## Limites desta medição

- n=10 por célula: a p95 de `pausa` tem 7 amostras na célula do veredito — é
  frouxa por construção e a decisão não sai dela sozinha.
- `pausa` só existe com a flag ligada (sem ela o barge não interrompe), então
  não há controle pareado para esse estágio.
- A fase de ventoinha deu **0 falso turno nas quatro células** (180 s cada).
- S4 saiu 0 % em tudo: o texto do fake não tem símbolo, então o estágio não foi
  exercitado (não é regressão).
- Curva RSS/swap: produção estável durante a janela (RSS 307–682 MB, mediana
  366; swap caiu de 4953 MB para 1341 MB conforme os órfãos das tentativas
  anteriores foram morrendo).

## Conclusão operacional

1. **C1 (parakeet) pode ir para frente** — mas só depois de deployar `de1ae52` e
   de o plist do serviço ganhar `/opt/homebrew/bin` no PATH (hoje não tem
   `EnvironmentVariables` de PATH e o `parakeet_mlx` depende de `ffmpeg`).
2. **A flag do #870 fica desligada** e volta para a bancada com o alvo no eco:
   nenhuma janela de playback pode abrir turno no C1.
3. Repetir a janela com provedor real quando houver chave válida, para o S2
   absoluto deixar de ser piso.
