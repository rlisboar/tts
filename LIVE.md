# Live local — guia do modo conversa em tempo real

Como o Live funciona por dentro: sessão WebSocket, motor de turnos, pipeline de
resposta e o que foi **medido** (e não só escolhido) em cada limiar. O objetivo
é uma conversa estilo Gemini Live rodando 100% local: nenhum áudio e nenhum
texto saem da máquina. A única exceção é a checagem de metadados do Whisper na
primeira transcrição do processo — ver *Egress* abaixo.

| Módulo | Papel |
| --- | --- |
| `live_turns.py` | Motor de turnos: VAD híbrido, prefix padding, barge-in, eco do TTS |
| `live_pipeline.py` | STT → LLM em stream → TTS por chunk |
| `app.py` (`/api/live/ws`) | Handler da sessão: auth, TTL, tradução dos eventos para o fio |
| `static/live-worklet.js` | Captura PCM16 no navegador (AudioWorklet) e playback cancelável |

## Handler da sessão (`/api/live/ws`)

Um WS por conversa, sem estado global de áudio: cada sessão tem fila própria e **uma
única task escreve no socket** (o pipeline só enfileira). Auth é a MESMA das rotas —
loopback dispensa, o resto precisa de credencial — e a credencial do navegador é um
**ticket de um uso** (a chave não vai na query string):

```bash
curl -sX POST localhost:7860/api/live/ticket -H "X-API-Key: $CHAVE"   # {ticket, expires_in: 60}
# depois:  new WebSocket("ws://host/api/live/ws?ticket=<ticket>")
```

`?key=<chave>` continua aceito (compat). **Em loopback o handshake nem consome o
ticket** (auth dispensada) — smoke que teste "reuso recusado" precisa conectar pelo
IP da LAN, não por `127.0.0.1`.

| Controle | Env | Padrão |
| --- | --- | --- |
| sessões simultâneas (acima → `error{busy}` + close 1013) | `TTS_LIVE_MAX_SESSIONS` | 4 |
| ociosidade da sessão (close 1000 + `error{session_ttl}`) | `TTS_LIVE_TTL_S` | 300 s |
| retomada do histórico por `session_id` | `TTS_LIVE_RESUME_TTL_S` | 1800 s |
| teto do histórico (mensagens / chars) | `TTS_LIVE_HIST_MAX_MSGS` / `TTS_LIVE_HIST_MAX_CHARS` | 24 / 12000 |
| teto de históricos guardados | `TTS_LIVE_MAX_HISTORICOS` | 32 |
| traço de depuração do turno | `LIVE_DEBUG_TTS=1` | off |

Protocolo (o contrato completo está no comentário da seção no `app.py`):
cliente → `setup` (1º frame; `session_id` opcional retoma), PCM16 16 kHz, `end_of_speech`,
`cancel`, `ping`; servidor → `ready{resumed}`, `speech_start`/`speech_end`, áudio PCM16
24 kHz, `transcript_user`, `assistant_text`, `turn_complete`, `interrupted`, `error`,
`pong`. Turno marcado `curto`/`barge_falso` **abre igual** — quem descarta por eco é o
pipeline (por texto, não por tempo).

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

1. ao começar o playback o handler chama `set_speaking(True, nivel_dbfs=…)`.
   O motor trabalha com uma **JANELA** de playback (`playback_janela_ms`), não
   com a flag instantânea: o handler marca no ENVIO do chunk e desmarca quando a
   fila esvazia, então no regime de um chunk por vez isso é um par
   `True`+`False` no mesmo instante e a flag fica ligada ~0 ms. Com a janela, o
   par não zera o contador de barge nem a calibração;
2. o motor **calibra** o nível do eco na primeira janela de áudio de verdade
   (~400 ms de áudio, estatística de quantil alto): o wav do TTS pode abrir em
   silêncio — uma janela de duração fixa calibraria o silêncio como eco — e a
   mediana pura afundava quando o trecho era quieto (o eco depois estourava o
   próprio limiar);
3. o motor **verifica se o playback acrescentou eco**: se o nível durante o
   playback é ~igual ao de antes dele, ou se o mic está bem acima do nível do
   payload (alto-falante só atenua), o eco é declarado ausente e vale o limiar de
   ocioso. Isso cobre mic falso de teste, fone de ouvido e eco muito baixo —
   sem essa verificação a calibração comia a fala do humano como eco e ele
   teria de superar o próprio p90 + 4 dB (fala contínua não supera);
4. durante o playback, um candidato precisa de `barge_in_ms = 300` de energia
   sustentada acima do limiar — o que derruba transiente de chunk e estalo;
5. o stop do playback não fecha a janela na hora: ela segue por
   `playback_janela_ms` (900 ms) — o cliente ainda tem áudio na fila — e ao
   acabar o nível herdado vai para o piso de ruído para não virar "fala
   fantasma" (senão o STT transcreve o próprio TTS).

Se o humano começa a falar durante a calibração, o motor já conta o que foi
sustentado e dispara o barge-in no fim da janela — com o áudio inteiro no
pré-roll.

Se o playback começa com o turno do usuário **já aberto** (ele já falava), o
motor emite `barge_in` **sozinho**, sem `speech_start`: o turno não reabre e o
áudio anterior a ele continua no buffer. O handler derruba o playback do mesmo
jeito.

### Quanto o estímulo precisa ter de nível

Quando o eco é declarado ausente, o limiar é o de ocioso: **piso de ruído +
4 dB** (`barge_in_margin_db`) — qualquer fala normal passa. Com eco presente, é
`eco_calibrado + 4 dB`. Amplificar a fala de teste **não** é o que decide: o
limiar acompanha o próprio sinal, então ganho puro é invariante de escala (o
`x3` do `live_ui.sh` não podia resolver um não-disparo). Medido: o mesmo
estímulo dispara igual a −17 dBFS e a −9 dBFS RMS.

### Números medidos (`smoke_live_turns.py`)

Bench offline com o Silero real, 21 wavs do app, 96 rodadas, eco de −42 a
−24 dBFS, voz +6 a +24 dB acima do eco (comando no fim desta página):

| métrica | valor |
| --- | --- |
| barge-in verdadeiro detectado (com eco) | **85 %** (82/96) |
| barge-in detectado **sem eco** (mic falso, fone) | **100 %** (24/24) |
| disparo **antes** de o humano falar | **0** em 97 disparos |
| rodada com eco puro sem nenhum disparo | **96 %** (92/96) |
| corte do playback no barge-in (`tests/live_ui.sh`, ponta a ponta) | **0 ms** (alvo <50 ms) |
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

### Egress (o que sai, se sai)

Auditado com `lsof -nP -a -p <pid> -i -sTCP:ESTABLISHED` durante uma sessão
completa (método e medição no gate do épico): **nenhum áudio e nenhum texto
saem**. O que aparece é uma coisa só, e só na primeira transcrição de cada
processo:

- o `mlx_whisper` chama `huggingface_hub.snapshot_download`, que resolve a
  revisão do repo e confere os metadados dos arquivos do modelo (medido: 2
  tentativas — DNS `huggingface.co` + um peer CloudFront:443). Com o cache
  quente, nenhum payload é transferido.

Para zerar de verdade (medido: **0 tentativas**, STT igual com o modelo em
cache):

```bash
export HF_HUB_OFFLINE=1
```

Sem o modelo em cache, a primeira carga falha com `LocalEntryNotFoundError`
("outgoing traffic has been disabled") — rode uma vez sem a variável para
baixá-lo. Provedor de chat remoto (`chat_base_url`) é egress de TEXTO por
desenho; com o provedor em loopback, sobra só o HEAD acima. O detalhe está
também em *Privacidade e uso responsável* no `README.md`.

### Executar

```bash
./.venv-mlx/bin/python -m pytest tests/test_live_turns.py -q     # sem MLX/Metal
./.venv-mlx/bin/python smoke_live_turns.py                       # mede falso barge-in
```

## Pipeline de resposta (`live_pipeline.py`)

Fim-de-fala → STT do turno → LLM em stream → chunking por sentença → TTS por
chunk → PCM16 24 kHz no fio, com cancelamento entre estágios. O turno roda em
thread própria (os helpers de STT/TTS do app são sync e disputam
`_stt_lock`/`_gen_lock`); o handler só enfileira os eventos e o áudio.

### Orçamento medido (mac, modelos quentes, `tests/live_ws.sh`)

| estágio | medido | o que o define |
| --- | --- | --- |
| fim-de-fala → STT do turno | **0,28–0,30 s** | whisper local no buffer; VAD/anti-ruído do app |
| 1º token do LLM | ~0,01 s (stub local) | provedor de chat; remoto soma rede |
| 1º chunk de TTS | **0,36–0,40 s** | 1º chunk curto (≤18 chars) a 12 passos |
| **fim-de-fala → 1º áudio** | **0,78–0,87 s** (alvo ≤ 1,5 s) | soma dos três + ~0,15 s de transporte |
| pre-warm (no `ready`) | 8–12 s, **1× por processo** | whisper + TTS com a voz da sessão + 1 request ao LLM |
| turno completo (5,9 s de fala) | ~4 s | chunk a 160 chars, passos do setting |

Três decisões fazem o alvo caber, todas por medição:

1. **1º chunk curto e rápido** (`first_max=18`, `first_chunk_max_steps=12`): a
   geração custa ~0,45 s a 16 passos e ~0,35 s a 12; com 48 chars o 1º chunk
   gastava ~1,1 s e o total batia 1,55 s. Os chunks seguintes ficam com o
   `omni_num_steps` do dono (37 hoje) — o alvo só olha o primeiro.
2. **Pre-warm com a voz da sessão**: o Metal compila **por forma**, e aquecer
   sem o clone prompt não aquece o caminho do turno. Medido: 1º áudio 1,47 s sem
   isso contra 0,79 s com. Ele roda no `setup` (cliente já conectado) e emite
   `prewarm` quando termina — quem mede o alvo precisa saber quando está quente.
   `app._transcribe` **não** serve para aquecer o STT: ele curto-circuita no VAD
   e nem chama o whisper (por isso o pre-warm chama `mlx_whisper` direto).
   O mesmo vale para o LLM: com Qwen local (mlx_lm.server) o 1º token custou
   **1162 ms** num turno sem aquecimento contra **173–350 ms** depois dele — sozinho
   isso estoura o alvo (1,92 s de 1º áudio); com o request mínimo de aquecimento o
   turno fecha em **1,13 s**.
3. **`_gen_lock` no TTS do turno**: sem ele o Live e uma geração da UI rodam MLX
   ao mesmo tempo e o servidor **morre** (`Command buffer execution failed: GPU
   Timeout Error`, exit 134) — medido, não teórico.

### Eco e turnos curtos (por que o descarte NÃO é por tempo)

O motor marca `curto` (fala abaixo de `min_fala_ms` = 250 ms) e `barge_falso`
(barge-in sem voz além da janela de confirmação). Medido com a FSM real e áudio
de TTS sintetizado:

| caso | medido | conclusão |
| --- | --- | --- |
| resposta curta legítima fora do playback ("Sim.") | 384 ms de fala | passa, mas com só 134 ms de margem → **`curto` não descarta** |
| a MESMA fala curta durante o playback | 84 ms de voz além da janela | vira `barge_falso` → descartar perderia "Sim."/"Ok." |
| eco puro (humano calado) | ~1000 ms além da janela | `barge_falso` **falso** → o eco passaria como turno |

Ou seja: o flag de tempo separa mal os dois lados. Quem decide é o **texto**:
`end_of_speech(barge_in=True)` + comparação do transcript com o texto do
assistente **em reprodução** — `_fala_em_curso`, atualizado a cada delta do LLM,
então vale também para o chunk EM SÍNTESE (barge-in no começo do turno, com o
`history` ainda vazio); o `history[-1]` é só fallback. Casou → é eco e o
turno fecha com `turn_complete{descartado:true, eco:true}` sem gastar LLM nem TTS;
diferiu → é o humano e o turno segue. `curto` fica como dica (o STT custa ~0,3 s e
o `_stt_ok` já barra lixo a jusante).

Matriz de aceite (em `tests/test_live_pipeline.py`, com os transcripts REAIS
medidos com a FSM + TTS do app):

| caso | transcript medido | classificação |
| --- | --- | --- |
| eco puro −30 dBFS | "Então o dia tá bonito hoje e a gente p…" | eco → descarta |
| eco puro −18 dBFS | idem | eco → descarta |
| "Não, obrigado, pode ser amanhã." | "Não, obrigado. Pode ser amanhã." | humano → segue |
| repetição de 1 palavra do assistente ("Bonito.") | "Bonito." | humano → segue |

No cancelamento, o que já foi dito fica no histórico (semântica do Live) e a
referência do eco acompanha o texto em voo — coberto por
`test_barge_durante_a_sintese_usa_o_texto_em_voo_como_referencia`.

**Tunáveis** (e limites): `_parece_eco(texto, referencia, minimo=0.6)` — `minimo` é
a fração de palavras que precisam casar em janela; transcript com menos de 2
palavras nunca é eco (conservador: descartar fala é pior que rodar um STT). Limite
conhecido: repetição de **2** palavras que existam no texto do assistente ("dia
está") casa a janela e é classificada como eco — ajustável baixando `minimo` ou
subindo o mínimo de palavras.

### Por que o TTS do turno é IN-PROCESS (e não pelo `tts_worker`)

O `tts_worker` existe para isolar crash nativo de famílias pesadas — mas ele
CARREGA O MODELO A CADA JOB. Medido com `kokoro` (família isolada) e o mesmo
texto curto:

| caminho | 1º áudio |
| --- | --- |
| worker isolado (subprocesso por job) | **7,5 s** |
| in-process, 1ª chamada (carrega o modelo) | 5,5 s |
| in-process, chamadas seguintes | **0,24 s** |

Com o alvo de 1,5 s, o worker só valeria como worker PERSISTENTE por sessão (P2);
até lá o turno gera in-process, segurando o `_gen_lock`. O pre-warm Absorve a
primeira carga. Troca assumida: nas famílias isoladas o modelo passa a viver no
processo do servidor e um crash nativo derruba o app (no omnivoice — o default —
isso já era o caso).

### Cancelamento

`cancel`/barge-in levanta um Event checado **entre** estágios: o stream do LLM
para, a fila de chunks é descartada, o chunk em voo termina e não é enviado (se
o cancel chegou durante a síntese) e o turno fecha com `interrupted` — sem
`turn_complete`, porque `interrupted` já é terminal. O que chegou ao cliente
fica (semântica do Live).

Eventos que o pipeline emite: `speech_start`, `transcript_user`,
`assistant_text` (delta), `latency` (orçamento por estágio), `turn_complete`
(`ms`, `audio_bytes`), `interrupted`, `error` e `prewarm`; o áudio sai como
frames BINÁRIOS PCM16 24 kHz.

### Executar

```bash
./.venv-mlx/bin/python -m pytest tests/test_live_pipeline.py -q   # dublês, sem MLX
./tests/live_ws.sh                    # ciclo completo + latência + barge-in
LIVE_LLM=config ./tests/live_ws.sh    # usa o provedor de chat configurado
BASE=http://127.0.0.1:7860 ./tests/live_ws.sh   # contra um servidor já de pé
```

O smoke é **auto-contido**: sobe o provedor de chat local (SSE) e um servidor
próprio numa porta livre, apontado para o stub por `TTS_CHAT_BASE_URL`/
`TTS_CHAT_MODEL` — então não escreve no `settings.json` do dono (nem quando morre
no meio) e não depende do provedor dele. Se o `settings.json` estiver com um stub
de smoke morto (o incidente de 2026-09-25), ele avisa ou aborta com o comando de
restauro. Falha se o 1º áudio passar de 1,5 s.

O bench sai 2 se não houver wav em `voices/` (grave uma voz na UI).

### Gate estático do épico (para o QA reusar)

Mesma lista do `.githooks/pre-commit` (ele roda isto + `pytest tests/` antes de
cada commit). Saída tem de ser **vazia** e o exit **0**:

```bash
./.venv-mlx/bin/python -m pyflakes app.py common.py tts_worker.py backends.py \
    smoke_sintese.py remote/*.py live_turns.py smoke_live_turns.py \
    live_pipeline.py tests/*.py client/mic_router.py
```