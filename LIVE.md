# Live local — guia do modo conversa em tempo real

Como o Live funciona por dentro: sessão WebSocket, motor de turnos, pipeline de
resposta e o que foi **medido** (e não só escolhido) em cada limiar. O objetivo
é uma conversa estilo Gemini Live rodando 100% local, sem egress de rede.

| Módulo | Papel |
| --- | --- |
| `live_turns.py` | Motor de turnos: VAD híbrido, prefix padding, barge-in, eco do TTS |
| `live_pipeline.py` | STT → LLM em stream → TTS por chunk |
| `app.py` (`/api/live/ws`) | Handler da sessão: auth, TTL, tradução dos eventos para o fio |
| `static/live-worklet.js` | Captura PCM16 no navegador (AudioWorklet) e playback cancelável |

## Turnos (motor)

### De onde vem a decisão de "começou a falar"

VAD **híbrido**: um frame só conta como voz quando as DUAS coisas valem —
probabilidade do Silero acima do limiar **e** energia acima de um limiar
adaptativo. As duas são necessárias:

- Silero sozinho aceita tom puro (medido: 200 Hz dá prob 0.68) e aceita o eco do
  próprio TTS (o eco é fala de verdade);
- energia sozinha aceita ruído impulsivo e o eco.

O Silero é carregado **explicitamente em ONNX** (`load_silero_vad(onnx=True)`) —
em `silero-vad` 6.x o default virou `onnx=False`, então pedir sem argumento
carrega o jit do torch *silenciosamente*. O fallback para o jit existe, mas sai
logado (`[live_turns] ONNX indisponível…`) e fica visível em `live_turns.BACKEND`.
O `.onnx` é lido de dentro do pacote: nenhum download, nenhuma saída de rede.

A energia decide sobre um **envelope suavizado** (~220 ms) e não sobre o frame
cru: medido nos wavs do app, o nível de um frame de fala varia até 34 dB entre
vogal e consoante — com esse espalhamento o eco passa por cima de qualquer
limiar calibrado. Com o envelope o espalhamento cai para ~10 dB.

### Prefix padding

`prefix_ms = 100`. Quando o detector confirma o início, o `speech_start` é
**retroagido** para o começo do pré-roll e o evento leva o áudio desse prefixo.
Sem isso a primeira sílaba ficaria fora do buffer de STT — o humano é justamente
quem fala primeiro.

O pré-roll cobre `prefixo + janela de confirmação + rampa do envelope`, então um
barge-in não perde o começo da fala que o disparou.

### Fim de fala por silêncio: `silence_ms = 600`

**Não use abaixo de 500 ms.** O turno fecha com o falante ainda respirando no
fim da frase e o Whisper come a última sílaba — o STT degrada de um jeito que
parece erro de transcrição, não de VAD. Acima de 600 ms a resposta começa a
demorar e o preset do Live (≤ 1,5 s até o primeiro áudio, ver `live_pipeline.py`)
não fecha. O módulo avisa no stdout se alguém configurar < 500.

O silêncio que sobra no áudio do turno é cortado, guardando `cauda_ms` (200 ms)
de cauda para o STT não receber uma frase decapitada.

### Barge-in (falar por cima do TTS)

O mic continua sendo consumido durante o playback. O eco do alto-falante é
tratado no MVP por **limiar adaptativo + energia sustentada**, sem AEC:

1. ao começar o playback o handler chama `set_speaking(True, nivel_dbfs=…)`;
2. o motor **calibra** o nível do eco na primeira janela de áudio de verdade
   (~400 ms de áudio, estatística de quantil alto): o wav do TTS pode abrir em
   silêncio — uma janela de duração fixa calibraria o silêncio como eco — e a
   mediana pura afundava quando o trecho era quieto (o eco depois estourava o
   próprio limiar);
3. durante o playback, um candidato precisa de `barge_in_ms = 300` de energia
   sustentada acima de `eco + 4 dB` (barge_in_margin_db) — o que derruba
   transiente de chunk e estalo;
4. o stop do playback não some com o eco na hora: `eco_cauda_ms` (800 ms) mantém
   a referência enquanto o cliente ainda tem áudio na fila, e ao acabar o nível
   herdado vai para o piso de ruído para não virar "fala fantasma" (senão o STT
   transcreve o próprio TTS).

Se o humano começa a falar durante a calibração, o motor já conta o que foi
sustentado e dispara o barge-in no fim da janela — com o áudio inteiro no
pré-roll.

### Números medidos (`smoke_live_turns.py`)

Bench offline com o Silero real, 21 wavs do app, 96 rodadas, eco de −42 a
−24 dBFS, voz +6 a +24 dB acima do eco (comando no fim desta página):

| métrica | valor |
| --- | --- |
| barge-in verdadeiro detectado | **85 %** (82/96) |
| disparo **antes** de o humano falar | **0** em 97 disparos |
| rodada com eco puro sem nenhum disparo | **96 %** (92/96) |
| latência fala → `barge_in` (p50) | ~0,5 s |
| latência (p90, pior caso) | ~1,2 s |

A detecção cai exatamente no pior caso (eco a −24 dBFS com a voz só +6 dB acima
dele); acima disso é 100 %. A latência inclui os 300 ms de confirmação — o áudio
dessa janela **não** se perde (está no pré-roll).

### Fallback de UI: "segurar pra falar"

O motor conta os falsos barge-in (turno de barge-in que fecha sem fala nenhuma
além da janela de confirmação, ou que é cancelado). Com ≥ 5 amostras e taxa
acima de 30 %, `estatisticas()["hold_to_talk_sugerido"]` vira `True` e a UI
oferece o modo "segurar pra falar" (`set_hold`): o turno abre e fecha no botão,
ignorando os heurísticos. `Config(hold_to_talk=True)` já nasce nesse modo.

### API (consumida pelo handler do WS)

```python
eng = TurnEngine()                          # config default do protocolo
eng.set_speaking(True, nivel_dbfs=-18.0)    # começou o playback
for ev in eng.feed(pcm16_100ms):            # frames de ~100 ms do WS
    enviar(ev.to_json())                    # speech_start | speech_end | barge_in
eng.set_speaking(False)                     # playback acabou
```

- Todo evento tem `t_ms` (relógio monotônico da sessão), `ts` (parede),
  `amostra` (posição no stream @16 kHz) e `t_decisao_ms` (quando o detector
  decidiu — é a latência real; `t_ms` é retroagido ao começo do áudio);
- `speech_start` leva o áudio do pré-roll; `speech_end` leva o turno inteiro
  (`fala_ms`, `curto`, `barge_falso`);
- num barge-in saem **dois** eventos no mesmo `feed()`: `barge_in` (o handler
  mata playback/geração) e o `speech_start` correspondente com `barge_in=true` —
  um caminho só de abertura de turno;
- `end_of_speech` é **comando do cliente** (o humano apertou parar) → o handler
  chama `engine.flush()`, que fecha o turno e emite um `speech_end` normal;
- nomes no fio: `speech_start` / `speech_end` / `barge_in` (servidor→cliente) e
  `end_of_speech` / `cancel` (cliente→servidor).

### Executar

```bash
./.venv-mlx/bin/python -m pytest tests/test_live_turns.py -q     # sem MLX/Metal
./.venv-mlx/bin/python smoke_live_turns.py                       # mede falso barge-in
```

O bench sai 2 se não houver wav em `voices/` (grave uma voz na UI).