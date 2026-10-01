# Live local — guia do modo conversa em tempo real

Como o Live funciona por dentro: sessão WebSocket, motor de turnos, pipeline de
resposta e o que foi **medido** (e não só escolhido) em cada limiar. O objetivo
é uma conversa estilo Gemini Live rodando 100% local: **nenhum áudio sai da
máquina** e o texto só sai pelo provedor de IA que estiver escolhido. As exceções
são a checagem de metadados do Whisper na primeira transcrição do processo, o
provedor de chat remoto (quando configurado) e o backend `dsh`, cuja rota é a do
harness — ver *Egress* abaixo.

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
| janela de barge INTRA-TURNO (#167/#216, ligada — ver medição abaixo) | `TTS_LIVE_BARGE_JANELA_TURNO` | 1 |
| janela de barge pela DURAÇÃO REAL do chunk (#167/#216, ligada — idem) | `TTS_LIVE_PLAYBACK_DURACAO` | 1 |
| eco só como referência com áudio TOCANDO (#167, desligada — idem) | `TTS_LIVE_ECO_SO_TOCANDO` | 0 |
| cadência do evento `stats` de telemetria | `TTS_LIVE_STATS_MS` | 250 ms (0 desliga) |
| traço de depuração do turno | `LIVE_DEBUG_TTS=1` | off |

O teto de sessões é decidido NO REGISTRO (depois do `setup`, atômico com a inserção):
antes disso a checagem ficava no connect e duas conexões que passassem juntas furam o
limite — e o cliente só descobre que o `session_id` retoma depois de mandar o `setup`,
então `busy` chega nesse ponto (uma retomada SUBSTITUI a entrada e não conta).

`session_id` REPETIDO com a sessão ainda VIVA (reconexão que não fechou o socket
antigo, ou dois clientes com o mesmo id) segue a mesma regra — quem nasce depois
manda — e a antiga é fechada pelo SERVIDOR com `error{session_substituida}` + close
1000. Sem isso ela continuava viva e FORA do registro (o teto é `len(_live_sessions)`):
fora do sweep por TTL (nunca vencia, só morria se o cliente fechasse) e fora da
contagem — com teto 1, N sockets com o mesmo id conviviam. Id NOVO com o teto cheio
continua levando `busy` (#222).

Protocolo (o contrato completo está no comentário da seção no `app.py`):
cliente → `setup` (1º frame; `session_id` opcional retoma), PCM16 16 kHz, `end_of_speech`,
`cancel`, `ping`; servidor → `ready{resumed}`, `speech_start`/`speech_end`, áudio PCM16
24 kHz, `transcript_user`, `assistant_text`, `turn_complete`, `interrupted`, `error`,
`prewarm{ok}`, `pong` e `stats` (telemetria, abaixo). Turno marcado `curto`/`barge_falso`
**abre igual** — quem descarta por eco é o pipeline (por texto, não por tempo).

Corte do playback no ONSET (#225): o cliente corta o áudio do assistente ao receber
`speech_start` **se ainda tiver fila** (`LX.ativos`), e não só no `interrupted`. A
janela de playback do servidor é uma ESTIMATIVA do que o cliente ainda vai tocar;
quando ela fecha antes (cliente atrasado/regime do #216), o onset vira turno NOVO sem
`barge_in` e nenhum `interrupted` vem — sem o corte, o assistente seguia falando por
cima do usuário. O `interrupted` do barge é o outro caminho de corte (idempotente).

Fala que chega com turno em curso NÃO é mais descartada (#126): os trechos se
ACUMULAM num único pendente (frase completada em dois pedaços vira um turno só) e
abrem quando o pipeline liberar, precedidos do evento aditivo
`turno_pendente{buffer_bytes, trechos, descartados_ms, barge_in, truncado}`
("anotei, respondo já"): `trechos` é QUANTOS pedaços já se acumulam no pendente e
`descartados_ms` é quanto de áudio saiu no teto (`truncado: true`) — o booleano
`substituido` do primeiro desenho não dizia se o corte fora de 20 ms ou de 20 s.
Teto de `TTS_LIVE_PENDENTE_MAX_S` (30 s): estourou, sai o trecho MAIS ANTIGO e o
evento vem com `truncado: true`. O turno em si vem na sequência normal
(`speech_start` → `turn_complete`), inclusive quando o turno anterior termina em
`error` (não herda) — o caminho de erro acorda o pendente como o `turn_complete`.
O `cancel` do cliente limpa o pendente; barge-in mantém (a fala que interrompeu abre
turno próprio). O `stats.turno` ganha `pendente`, `pendentes_trechos`,
`pendentes_descartados_ms` e `pendentes_descartados`.

### Telemetria da sessão (`stats` + log de metadados)

Enquanto a sessão está aberta, o servidor emite um JSON `stats` a cada
`TTS_LIVE_STATS_MS` (250 ms; 0 desliga) pela MESMA fila do resto do fio — o evento
é **aditivo**: nenhum evento existente mudou. O painel da UI (#119) se alimenta
dele; contrato completo no comentário da task #118. Formato:

```json
{"type": "stats", "t_ms": 41200,
 "mic":      {"frames": 431, "bytes": 1379200, "desde_ultimo_ms": 42, "dbfs": -31.4, "prob": 0.87},
 "motor":    {"estado": "ouvindo", "limiar_dbfs": -45.2, "limiar_turno_dbfs": -48.1,
              "barge_ativos": 0, "barge_falsos": 0},
 "turno":    {"stage": "llm", "ms": 812, "n": 7, "buffer_bytes": 95232, "t_decisao_ms": 486},
 "playback": {"speaking": true, "chunks": 12, "bytes": 284160},
 "sessao":   {"idade_s": 41, "ocioso_ms": 130, "ttl_s": 300, "criadas": 2, "historicos": 1},
 "erro":     {"code": "pipeline", "stage": "tts", "idade_ms": 2100},
 "provedor": {"estado": "ok", "http": 200, "idade_ms": 3400}}
```

- `mic.dbfs` é o RMS do áudio do cliente medido **no servidor** e
  `mic.desde_ultimo_ms` a idade do último frame — é o par que distingue "o mic
  parou de mandar" (bug #117) de "o servidor parou de processar";
- `motor.estado` deriva `ouvindo`/`fechando` além dos estados da FSM; os
  `limiar_*` são os VIGENTES (com eco do playback eles sobem — é o número que
  explica um barge-in não disparar);
- `turno.stage` (`idle|stt|llm|tts`) com `ms` no estágio e `t_decisao_ms` do
  orçamento de latência; `playback.speaking` é a MESMA janela que alimenta o
  limiar de eco (`set_speaking`);
- `erro` e `provedor` só aparecem quando há ocorrência; `provedor` é do
  PROCESSO (não da sessão) e mostra `ok|erro{http}|timeout` da última chamada
  ao provedor de chat.

O log da sessão vai no logger `live` (INFO; `LIVE_LOG=0` desliga), uma linha
`chave=valor` por evento de metadados: abre/resume/fecha, speech_start/speech_end
(com `fala_ms`, `curto`, `barge_falso`), stage_inicio/stage_fim com ms, barge_in,
interrupted, cancel, erros. **Invariante de privacidade: nunca áudio nem texto
transcrito/gerado no log** — o teste `test_live_ws_log_só_metadados` prova (o
texto do transcript NÃO aparece). Foi a ausência desse log que deixou o #117 sem
diagnóstico.

### Painéis de monitoramento no cliente (OBS)

O painel do Live nasceu na #119; a #120 extraiu o núcleo dele para um componente
ÚNICO, reusado pelas telas que captam ou tocam áudio — sem cópia por tela:

- `OBS` — malha **única** de `requestAnimationFrame` para todas as barras de áudio
  do app (`OBS.barras`: id→callback; o loop só existe enquanto há barra registrada
  e para sozinho quando a última sai). O Live registra/desregistra na mesma engine.
- `obsLog(el, txt, cls)` — log com timestamp e teto de linhas (textNode: sem
  injeção de HTML).
- `criaObsMonitor(mount, opts)` — fábrica do painel (medidor de entrada e/ou saída
  opcionais, chip de estado, contadores por `setInfo` e log em `<details>`);
  `micFilteredStream(base, obs)` é o hook único: com `obs`, sempre há analyser
  (reusa o da cadeia filtrada; no modo transparente levanta um ctx mínimo só para o
  tap, sem tocar no áudio gravado) e `close()` desanexa.

Painéis: ditado/STT (`#obsStt`), gravação de amostra e biometria (`#obsVozes`),
Conversa (`#obsCv`, sem medidor — o mic ali é do MicVAD, declarado no painel) e
progresso dos jobs de TTS (`#obsTts`, contadores de trecho/status/tempo).
Verificação: `tests/obs_ui.sh` (Playwright, mic falso real, console sem erro de
script) + `evidence/120-obs-*.png`.

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

#### Com o backend de IA `dsh`

O egress de texto acima supõe o provedor HTTP do app. Com
`chat_backend: "dsh"` o texto do turno (e, no modo histórico, o histórico
renderizado) não vai para o `chat_base_url`: sai pela ROTA que estiver
configurada no harness — que, no default de `chat_dsh_model`, é um provedor
remoto. Quem fala com o provedor é o processo `dsh` da sessão, com a credencial
dele (`~/.dsh/.credentials.yaml`; o app não lê nem copia). Trocar o modelo pela
UI troca a rota; um modelo apontando para um provedor da sua rede deixa o caminho
local. É o mesmo material que já iria para o `chat_base_url` quando o provedor é
remoto — muda quem pergunta, não o conteúdo.

O que **não** muda: **o áudio continua com zero egress** nos dois backends (nem
PCM do cliente, nem áudio do TTS saem da máquina) e o HEAD de metadados do
Whisper na primeira transcrição de cada processo vale igual. Com o dsh fora (ver
o fallback acima), o turno volta ao `chat_base_url` e o egress é o de sempre.

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

**DE QUAL CENÁRIO SÃO ESTES NÚMEROS (#173):** são do cenário LOCAL — o `live_ws.sh` sobe
um stub de chat SSE na própria máquina e é contra ele que o alvo de 1,5 s é aferido. Com
provedor de chat REMOTO o alvo NÃO vale: o `first_token` sozinho já custa segundos (medido
direto no provedor do dono, sem app, uma pergunta de uma linha: **TTFT 4,3 / 7,9 / 4,5 s**,
total 5,1–8,9 s; o smoke de release viu 7,1 / 8,1 / 16,5 s e o Live fechou o 1º áudio em
~31 s). Ali o esperado é `STT + TTFT + 1º chunk` — **segundos, não 1,5 s** — e não é defeito
do app: o turno não tem fallback, retry nem erro (STT 389 ms, TTS normal). O provedor é
escolha do dono, então "o Live está lento" com provedor remoto é o comportamento publicado
aqui, não regressão.

Bimodalidade medida no mesmo provedor (mesma família do #160, mas na rota do provedor de
chat do dono, não no dsh): em parte das chamadas ele devolve a resposta INTEIRA num único
delta — ignorando `stream` — e nas outras ~60 deltas. Com um delta só, `first_chunk_ms` é o
FIM da geração e empata com `first_token_ms`: o `SentenceChunker`/playback aguentam, mas
quem lê o painel não pode tratar `first_token_ms` como "fluidez" quando o provedor responde
em bloco.

#### TTFT por rota — quanto cada escolha custa no Live (#175)

Tudo aqui é `1º token` do LLM (o resto do turno soma STT ~0,3 s + 1º chunk de TTS ~0,25 s).
Medições de 29/09, método e procedência por linha; **nenhuma promessa de latência, é
orientação de escolha**:

| rota / modelo | 1º token | como foi medido |
| --- | --- | --- |
| **stub local** (o do `live_ws.sh`) | ~0,01 s | SSE na própria máquina; é o cenário do alvo de 1,5 s |
| **`dsh` / rota `dsflash`** | **0,38–0,47 s** | cliente ACP real, 6 turnos (api-backend #147/#162 e minha rodada); o infra confirmou no probe cru |
| OpenRouter (`z-ai/glm-5.3-flash`) | 5,6–12,6 s | infra, #160; em troca, NÃO preempta no meio (gaps < 0,4 s) |
| provedor do dono, `glm-5.3-flash` (hoje) | **4,3–11,0 s** (mediana ~7,6 s) | API direta, sem app, 2 rodadas × 3 amostras; o smoke de release viu 7,1 / 8,1 / 16,5 s |
| mesmo provedor, `glm-5-turbo` | 4,2–9,9 s (mediana 4,6 s) | idem, 3 amostras |
| mesmo provedor, `glm-4.5-air` | 4,3–7,9 s (mediana 6,6 s) | idem, 3 amostras |
| mesmo provedor, `glm-5.3-flashx` | não medido | a chave devolveu 429 em todas as tentativas de hoje |

Leitura: a rota do `dsh` é **uma ordem de grandeza** melhor no 1º token (0,4 s contra
segundos) — e é o que o Live precisa, porque o Live vive do 1º áudio; a ressalva é a cauda
do `dsflash` (preempção do provedor, seção acima), que atrasa o MEIO da resposta mas não o
começo. Trocar de MODELO dentro do provedor atual não muda a ordem de grandeza (4–10 s em
três modelos diferentes): não é "troque de fornecedor", é "provedor remoto de propósito
geral não é bom para o 1º áudio".

**RECOMENDAÇÃO:** para o LIVE, use o backend `dsh` (`chat_backend: "dsh"` em Configurações →
IA) — 1º token sub-segundo; para a CONVERSA, o provedor atual serve bem (lá ninguém espera
1,5 s). Tudo isso depende da conta/rota do dono: as linhas de provedor são da chave dele, e a
rota do `dsh` também é escolha dele (`chat_dsh_model`).

**LIMITE ATUAL da recomendação:** `chat_backend` é GLOBAL — escolher `dsh` para melhorar o
Live também põe a Conversa no `dsh`. O campo por caminho (`chat_backend_live`, herdando do
global) está na **task_b5223fc6 (#176)**; até ele existir, quem quiser a melhor config do Live
aceita o `dsh` também na Conversa.

Três decisões fazem o alvo caber (no cenário local), todas por medição:

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

### TTS do turno: in-process — exceto família isolada, que usa worker PERSISTENTE

O `tts_worker` (processo filho) existe para isolar crash nativo de famílias
pesadas, mas CARREGA O MODELO A CADA JOB — medido com `kokoro` (família isolada) e
o mesmo texto curto: **8,5 s** de 1º áudio por pedido contra 121 ms no in-process
quente. Com o alvo de 1,5 s, família isolada ficava fora do Live.

A #152 resolveu com um worker PERSISTENTE POR SESSÃO: o filho sobe no pre-warm
(fora do turno, junto do aquecimento dos outros modelos), carrega o modelo UMA vez
e atende N turnos por NDJSON em stdin/stdout (`tts_worker.py --serve`), com
`close`/kill quando a sessão morre. Medido no app real
(`smoke_worker_persist.py`, 1º áudio do turno, pre-warm fora da conta):

| caminho | 1º áudio do turno |
| --- | --- |
| worker por job (o que existia) | **8515–9114 ms** |
| in-process, 1º chunk depois do pre-warm | 2518–2783 ms |
| in-process, 2º turno (mesmo texto) | 83–121 ms |
| **worker persistente, 1º chunk** | **487–507 ms** |
| worker persistente, 2º turno | 84–87 ms |

Escopo: SÓ o caminho do Live. `/api/tts/jobs` continua UM processo por job (lá a
isolação por job é desejada). `TTS_LIVE_WORKER=0` desliga o worker do Live; ele só
liga para família de `_ISOLATED_FAMILIES` (omnivoice — o default — segue
in-process).

**Exclusividade de Metal** (o desenho da task): o árbitro é o `app._gen_lock` — o
MESMO lock da geração in-process e do spawn do worker de lote (que o segura pelo
job inteiro). O cliente do worker toma esse lock no `init` e em CADA pedido, então
nunca há duas gerações no Metal. Com o worker vivo a sessão NÃO gera in-process;
na 1ª falha (filho morto, timeout, erro do filho) ela marca `worker_indisponivel`,
regera aquele chunk in-process e segue assim até o fim — mesmo padrão do fallback
do dsh. O modelo do pai é liberado quando o filho assume (`_unload_local_tts`),
para não carregar o mesmo modelo duas vezes. Consequência medida: um turno do Live
espera o job de lote terminar (8,4 s no teste D do smoke) — sem atropelo, os dois
saem com áudio (RMS 0,0333).

Cobertura: `tests/test_live_worker.py` (rápido, filho STUB — protocolo, reuso do
processo, crash → fallback, lock, ciclo de vida; RODA no `pytest tests/ -q`) e a
suíte opcional do caminho LIGADO com modelo real, marcada como **`worker_real`**:

```bash
TTS_TEST_WORKER=1 ./.venv-mlx/bin/python -m pytest -m worker_real -q
```

(os tests `worker_real` — worker isolado por job, worker persistente `--serve` e o
pipeline do Live usando o worker — ficam SKIPPED sem o env. O cabeçalho do pytest
avisa em todo run que esse caminho está fora do default, senão `pytest tests/ -q`
vira falso verde aqui.) Evidência de latência: `smoke_worker_persist.py`.

**Bug de terceiro achado no caminho** (patch em
`backends._patch_kokoro_interpolate`): o `interpolate` do `mlx_audio` calcula
`size = ceil(n * scale)`, e `34200 * (1/300)` em float dá 114.00000000000001 — o
ceil vira 115, o ida-e-volta do `_f02sine` devolve 34500 e o `uv` continua 34200,
estourando `Shapes (1,34200,1) and (1,34500,9) cannot be broadcast`. Atinge ~1/4
dos textos curtos ("Ok.", "Oi", "Bom dia", "Certo.") — justamente os primeiros
chunks do Live. O patch (arredondar o produto antes do ceil) é aplicado no
namespace do istftnet, já que o venv é reinstalado pelos pinos do requirements.

**PATCH DE TERCEIRO EM RUNTIME — não "conserte" o venv.** Se o Kokoro voltar a
estourar `cannot be broadcast` em texto curto e você for olhar o
`.venv-mlx/lib/python3.12/site-packages/mlx_audio/tts/models/kokoro/istftnet.py`,
vai encontrar o `ceil` ORIGINAL: quem conserta é o app, em runtime
(`backends._patch_kokoro_interpolate`, marcador `istftnet._rod_interp_seguro`,
idempotente e só nesse namespace). É inofensivo se a lib corrigir antes — o `size`
resultante é o mesmo. Fórmula/invariantes: `tests/test_kokoro_interpolate.py`
(rápido, sem modelo).

### Barge-in nos VÃOS de geração (#167/#216) — o que ficou ligado por default

O motor armava o barge pela JANELA de playback, que era dimensionada pela FILA de
chunks (`playback_janela_ms` = 900 ms depois do último envio). Nos vãos de LLM/TTS
(medidos até 20 s) a janela fechava e o onset virava turno NOVO em vez de
`interrupção`.

O que ficou LIGADO por default (e é o produto): calibração de eco a cada INÍCIO de
áudio (e não só quando a janela estava fechada), braço do barge contando DURANTE a
calibração, mínimo de contagem preservado quando a calibração fecha, mais DUAS
alavancas de `Config` que o handler aciona (`set_turno_aberto` no 1º áudio e
`set_speaking(..., duracao_ms=…)` no envio):

- `barge_janela_turno` (`TTS_LIVE_BARGE_JANELA_TURNO`, default **1**): a janela
  segue o TURNO do assistente e não fecha nos vãos de geração;
- `playback_por_duracao` (`TTS_LIVE_PLAYBACK_DURACAO`, default **1**): a janela
  cobre a duração REAL de cada chunk + o backlog em voo — é ela que responde pelo
  caso "injeção logo APÓS o `turn_complete` com o cliente ainda com áudio na fila".

O que ficou DESLIGADO: `eco_so_tocando` (`TTS_LIVE_ECO_SO_TOCANDO`, default **0**),
que faz a referência de ENERGIA olhar "toca agora?" em vez da janela. Ela baixa o
limiar do vão (regime de ocioso em vez do eco), mas na medição da COMBINAÇÃO custa
nos dois sentidos — inclusive no caso da fala BAIXA no vão, que era a razão de ela
existir (20/20 sem ela contra 13/20 com ela). Ver abaixo.

#### A medição que virou o default (#216)

As três alavancas nasceram desligadas e cada uma tinha uma medição EM ISOLADO que
a justificava; o defeito do #167 (o dono não recebia o fix) era exatamente esse
default. A medição da COMBINAÇÃO está em `evidence/216-DECISAO.md` (script
`evidence/216-decisivo.sh`, harness `tests/live_barge_rep.sh`, REP=20 por célula,
dois sentidos). Resumo, com a célula que DECIDE (`MODO=vao`: injeção 900 ms depois
do `turn_complete`, com o cliente ainda tocando áudio):

| configuração | vão (o defeito) | eco (cortes falsos, menor melhor) | sentido verdadeiro |
| --- | --- | --- | --- |
| default antigo (nenhuma) | **0/20** | 34 e 20 | 20/20 |
| A+B (novo default) | **20/20** e 20/20 | **20** | 20/20 |
| A+B+C | 15/20 e 20/20 | 44 e 45 | 18/20 |

Com fala BAIXA no vão (`AMP=0.2`, ~−31 dBFS — o caso para o qual C foi inventada) o
resultado é o mesmo sentido: A+B **20/20**, A+B+C **13/20**.

O que sustenta a decisão é o ECO: toda configuração com `eco_so_tocando` ficou em
36-45 cortes falsos (C=36, A+C=40, B+C=38, A+B+C=44 e 45) e toda configuração sem
ela ficou em 20-35 (baseline=34 e 20, A=35, A+B=20, o mínimo medido). O VÃO não
separa A+B de A+B+C: cada uma foi medida duas vezes e as duas chegaram a 20/20
(A+B+C fez 15/20 numa das rodadas, com a assinatura `speech_start` SEM barge — o
mic abre turno no vão e o áudio do assistente, ainda na fila do cliente, segue
tocando por cima).

`tests/live_ui.sh` passa com o novo default (o corte do barge é exigência dura
desde o #179). Aviso para quem for reproduzir: a matriz foi medida com a máquina
carregada e o harness é sensível a carga — o número robusto é o do eco (margem
grande), não a diferença de 34 para 20 no baseline.

Quem quiser o comportamento antigo tem os três envs (`=0` desliga A e B, `=1`
liga C) — o A/B do harness depende disso.

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

### Backend de IA do Live: `openai` (padrão) ou `dsh`

`chat_backend` (Configurações → IA, ou `TTS_CHAT_BACKEND`) escolhe o provedor do
turno. O **default continua `openai`** (endpoint + chave, caminho intacto); com
`dsh` o texto vai para o harness local via ACP (`dsh_client.py`), que sustenta um
primeiro token de ~0,4 s **porque o contexto vive na sessão ACP** — o turno manda
poucas dezenas de tokens em vez de re-subir a conversa inteira.

Como o Live usa isso (por sessão do WS, nada compartilhado com a Conversa):

- **um `DshClient` por sessão** (processo + sessão ACP próprios), fechado no fim da
  sessão — o `cancel` do barge-in não vaza de uma sessão para outra;
- **`prewarm` no `start()`** do pipeline, junto do aquecimento do whisper/TTS e na
  mesma thread (fora do handshake do WS). O boot a frio é caro — medido aqui:
  **23,7 s só do dsh** e **26,5 s** no `prewarm` do pipeline inteiro (dsh + TTS +
  whisper); quente, o prompt curto do prewarm custa ~0,4 s. Quem falar ANTES do
  evento `prewarm` paga o boot no turno. Um turno que chega DURANTE o prewarm NÃO
  espera por ele: o `session/cancel` cancela o prompt de aquecimento e o turno
  segue (o prewarm já entregou o que importa — processo, sessão e rota) — é o que
  impede um provedor lento de transformar o aquecimento em atraso (#172);
- **só o 1º turno manda histórico renderizado** (persona + resumo + últimas falas),
  o que abre a sessão ACP; os seguintes mandam só o texto novo;
- **persona OBRIGATÓRIA** no primeiro prompt de cada sessão ACP (inclusive na
  reaberta pelo teto): fala curta ("1 a 3 frases, sem markdown, sem código, tom de
  conversa") — é o que encurta a resposta e, o que define de fato o tempo até o 1º
  áudio, é a projeção dos deltas (patch DSH-4a; sem ele, um chunk no fim). O `system`
  da sessão, quando existe, entra DEPOIS da persona;
- **teto de contexto** igual ao do LIVE-5 (`TTS_DSH_CTX_MAX_MSGS` 24 /
  `TTS_DSH_CTX_MAX_CHARS` 12000): estourou, o próximo turno reabre sessão ACP nova
  com o contexto renderizado — reusa o resumo que o `_live_hist_comprime` já
  produziu, sem LLM extra. O resumo sai pelo backend do LIVE (não pelo da Conversa):
  com o Live no dsh ele usa o POOL do dsh, em processo separado do cliente da sessão
  (o resumo roda em thread no fim do turno e o cliente da sessão já pode estar no
  prompt seguinte). Antes ele ia por `_chat_llm` — o caminho da Conversa —, o que
  mandava o resumo para o provedor REMOTO quando os dois caminhos divergem e
  falhava 400 (e o Live nunca comprimia) quando não havia endpoint configurado;
- **barge-in** chama `cancel(turno_id)` no harness e ainda descarta delta de turno
  abandonado ANTES do chunker (defesa em profundidade: o cliente já barra o chunk
  tardio, o pipeline não confia só nisso);
- **erro do dsh** vira `error{pipeline}` com a sessão VIVA (padrão F1): o turno
  seguinte volta a tentar;
- **pipeline que não NASCE** (exceção na montagem, antes do 1º frame de áudio) também
  deixa a sessão viva: sai `error{pipeline}` e o `ready` já foi mandado — a sessão
  continua respondendo `ping`/`stats` (sem STT/TTS) em vez de morrer calada; o
  processo dsh já criado é fechado junto;
- **dsh fora NÃO deixa a sessão muda** (#146): se o boot/handshake morre ANTES do
  1º token (binário errado, `~/.dsh` sem permissão de escrita, Node antigo…), o
  turno é REFEITO no backend openai, o dsh fica marcado como indisponível (com o
  processo liberado), sai o evento aditivo
  `dsh_indisponivel{fallback:"openai",message}` e os turnos seguintes já nascem no
  openai. **A marcação é sem volta DEPOIS de uma repetição curta** (#162): o harness
  recusa um 2º prompt na sessão com `-32602 "a prompt is already in flight"` e essa
  corrida é transitória (acontecia em ~30% dos primeiros turnos), então o cliente
  repete UMA vez com 250 ms antes de considerar o dsh fora — o `dsh_client` também
  serializa o prewarm com o 1º turno pelo mesmo motivo. Só o retry esgotado cai na
  política sem volta. Falha no MEIO do stream não dá para refazer sem repetir o que já foi
  dito: aí o turno fecha com `error{pipeline}` e o dsh fica marcado só para o
  próximo. Custo do caminho degradado (medido no `tests/live_ws.sh` com o dsh
  quebrado de propósito): o turno que descobre o dsh fora paga ~1 s a mais
  (1877 ms contra ~870 ms).

**ANTES do patch do bridge (chunk único por ACP)** — medido aqui com persona, pipeline
real (STT real + TTS real + dsh real), 5 turnos na mesma sessão: fim-de-fala → 1º áudio
**mediana 2,49 s** (amostras 1,93 / 3,26 / 18,37 / 2,49 / 1,88 s). O `first_token_ms`
fica ~0,25 s abaixo do 1º áudio. Causa: o bridge `dsh-acp` só projetava o evento
DURÁVEL (`assistant/message`), então o "1º token" era o FIM da geração — resposta de
1,4 k chars levava 21,9–32,3 s. Reprodução: `evidence/dsh-chunks-spike.py` e
`evidence/dsh-provider-sse-spike.py` (o provedor streama bem: 1º delta em 372–376 ms).

**DEPOIS do patch (#149, `scripts/dsh-acp-stream-patch.mjs` aplicado)** — o bridge passa
a projetar `agent/assistant-stream` em `session/update`, então os deltas EXISTEM. O número
de referência é o do **veredito do gate #125** (6 rodadas do `tests/live_ws.sh` com backend
dsh, na mesma máquina, lado a lado com o app do dono de pé): fim-de-fala → 1º áudio
**1312 / 1330 / 1524 / 1636 / 19 864 / 34 131 / 47 793 ms** — ou seja, o alvo de 1,5 s é
ALCANÇADO, mas **não sustentado** (2 de 7; o resto estoura por ordens de grandeza). O
veredito classifica como aprovado com essa ressalva. Ambiente do veredito, para quem
repetir: shell sandboxed com `DSH_HOME=/tmp/qa-home/.dsh` (o `~/.dsh` real não é gravável),
bridge **PATCHADO**, `effort=off`, perfil `tts-studio`, instância própria em porta livre;
escrever em `/opt/homebrew` (apply/revert) exigiu escalar o sandbox.

**RE-MEDIÇÃO pós-#160, com a máquina QUIETA** (`load average` 2,5–4,9 — sem as suítes, app
do dono apenas de pé), pipeline real, 5 turnos, **rota CARIMBADA** (bridge `patched`
sha256-8 `64c0d1cc`, zero `dsh_indisponivel`, sem nada de fallback): primeiro áudio de
**1563 / 19 518 / 198 490 / 2673 / 51 635 ms** (mediana 19,5 s), `first_token_ms` sempre em
**1,3–1,8 s**. Reprodução: `evidence/dsh-live-latency-spike.py` (carimba estado da rota e
sai 1 se o turno caiu no fallback — para o número não ser confundido com o do openai, que
responde em ~0,87 s e se disfarça de "dsh rápido").

**A CAUSA É EXTERNA e o dono tem nome: o PROVEDOR da rota `dsflash`** — ele PREEMPETA no meio
da resposta (provado no #160, não suposto): o mesmo gap aparece no TCP CRU contra o provedor
(3 de 6 rodadas com um único vão de 26,6 / 55,5 / 36,0 s, primeiro `recv()` em 0,85–2,07 s e
depois fio mudo), o CONTROLE sem stream (mesma pergunta, sem flush envolvido) levou
29,9–36,1 s, e o bridge × cliente no mesmo relógio dá atraso de 1 ms (recebe em bloco e
repassa na hora). Nenhuma das suspeitas do diagnóstico (flush do `notify`, coalescência do
harness, stdout, concatenação do patch, MLX) se sustenta — o NOSSO lado sai limpo, patch
inclusive. Prova completa: `evidence/dsh-acp-DSH-4b-EVIDENCIA.md`.

**E trocar de rota não é de graça** (também medido no #160, e é decisão do dono): a
alternativa (OpenRouter, `z-ai/glm-5.3-flash`) NÃO preempta no meio — gaps < 0,4 s — mas
paga `1º token` de **5,6–12,6 s**, o que é pior para o Live, que vive do 1º áudio (`muse-spark`:
8,6–43,2 s). Portanto: `dsflash` continua sendo a escolha certa pelo 1º áudio; o que falta é
o provedor parar de preemptar. Sem preempção numa janela, o número desta seção fica
CONDICIONAL ao provedor; com ela, é o que está publicado.

Medições minhas, na mesma família (rodadas **sob carga**, `load average` 4,6–11,4) — ficam
como contraste, não como número final:

- **no cliente, sem modelos de áudio no processo** (6 turnos, sessão quente): 1º delta em
  **0,35–0,47 s** e resposta inteira em **menos de 1,5 s em todos os turnos**;
- **no pipeline real** (STT real + TTS real + dsh, 5 turnos): 1º áudio de **1,2–2,6 s** nos
  turnos em que o stream corre, e cauda de **18–44 s** em parte deles (medianas 30,6 s e
  32,4 s nas duas rodadas). Com STT real e **TTS falso**: 3 de 4 turnos em 1,2–1,8 s — a
  cauda aparece mais junto do **TTS real**, cuja geração MLX disputa com o stream. Sem
  nenhuma mudança no `live_pipeline` para consumir os deltas (o laço do turno já era por
  delta; o chunker por sentença passa a soltar o 1º áudio mais cedo quando o stream corre).

**DIAGNÓSTICO (bateu nas duas pontas — gate e minha):** o gargalo NÃO é o pipeline nem o
chunker, é a CHEGADA do texto. No trace do gate, o `stage_fim llm` empata com o
`first_chunk_ms` (o pipeline entrega no primeiro texto que recebe) e o 1º delta varia de
1,4 s a 37,8 s. A assinatura medida nos INTERVALOS entre deltas (4 turnos): não é "stream
lento", é **1 delta adiantado e o resto em bloco** — em cada turno com cauda há UM único
intervalo acima de 1 s e ele é enorme (16,6 / 22,4 / 42,2 s), enquanto os ~30 deltas
seguintes saem com ~5 ms entre si; proporção que chega em ≤1 s: **de 1 para 36**. Quando
isso acontece, o 1º áudio fica preso à primeira SENTENÇA completa, não ao 1º token.

**CONDIÇÃO do número acima:** ele exige o patch na máquina. Um `npm install -g` que recrie
o `node_modules` global desfaz a projeção e o comportamento VOLTA ao chunk único — aí vale
o número "ANTES" e o alvo de 1,5 s deixa de ser cobrável no dsh. Rollback explícito:
`node scripts/dsh-acp-stream-patch.mjs --revert`. Para saber em que estado a máquina está
— inclusive depois de um `npm install` — sem abrir o `node_modules`, o
`GET /api/chat/dsh/models` devolve `bridge: "patched" | "clean" | "unknown"` (e
`bridge_detalhe`): quem medir latência do dsh deve registrar esse campo junto do número.

**ALVO no caminho dsh (só na condição incremental):** volta a ser os 1,5 s do épico — o
pedaço que o patch destrava cabe e sobra (1º delta 0,35–0,47 s + TTS do 1º chunk ~0,25 s),
e o `first_token` medido fica em 1,3–1,8 s mesmo com a máquina quieta. O que estoura é a
CHEGADA do resto da resposta, e a causa está fora daqui (provedor da rota): enquanto ela
não mudar, o estado é **alcançado e não sustentado** — número de referência o do veredito do
gate #125, acima.

### Executar

```bash
./.venv-mlx/bin/python -m pytest tests/test_live_pipeline.py -q   # dublês, sem MLX
./tests/live_ws.sh                    # ciclo completo + latência + barge-in
LIVE_LLM=config ./tests/live_ws.sh    # usa o provedor de chat configurado
BASE=http://127.0.0.1:7860 ./tests/live_ws.sh   # contra um servidor já de pé
TTS_CHAT_BACKEND=dsh ./tests/live_ws.sh         # turno pelo harness dsh
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
    live_pipeline.py smoke_worker_persist.py tests/*.py client/mic_router.py
```

### Suítes de modelo: rodar em paralelo agora é seguro (#134)

As suítes que carregam Whisper/Kokoro (`live_ws.sh`, `live_ui.sh`, `obs_ui.sh`)
disputavam Metal/CPU quando a bateria rodava junto e o alvo de ponta do
`live_ws.sh` dava **falso vermelho** (medido pelo QA: 1792 ms em paralelo, 884 ms
sozinho, segundos depois). Agora elas se serializam sozinhas por uma trava de
arquivo (`tests/serial.sh`): espera com aviso de quem é a dona, rouba lock órfão
por PID morto/TTL e libera no exit via `trap`.

- `TTS_SERIAL=0` desliga a trava (escape para quem sabe que está sozinho);
- `TTS_SERIAL_ESPERA=Ns` e `TTS_SERIAL_TTL=Ns` ajustam espera e roubo de órfão;
- a trava tem teste próprio e **sem modelo**: `tests/serial_lock.sh`.

Com isso o alvo de 1,5 s segue sendo medido honesto em qualquer ordem de bateria
— não é preciso lembrar de rodar em série.