# #1129 — interrupção do dono não detectada com AEC ligado: PRÉ-REGISTRO (2026-11-11, antes de medir)

**Base:** main `44bd859` · **Cards:** #1129 (`task_1e04a126`), aponta para o #870 (`task_b0c01c27`)
· **Bloqueado por:** log de barge persistente fora do `/tmp` (`task_9253e121`, infra)

Este arquivo existe para que o número da sessão anotada não seja lido com a hipótese
escolhida depois. O readout entra no fim, com data e SHA, no mesmo commit da medida.

## De onde vem a hipótese

No replay offline das 18 sessões reais do dono com AEC ligado (#870, 2026-11-11, só números,
dump apagado depois por decisão do dono):

- o motor abriu **9** eventos de barge em **13,85 min** de playback;
- o critério que eu tinha pré-registrado para "o dono falou" (Silero ≥ 0,5, ≥ −45 dBFS e NCC com a
  referência < 0,5) **não separa** o dono do resíduo do eco, porque com AEC o resíduo não
  correlaciona com a referência (NCC p50 0,17). Por isso os "374 perdidos / 0 eco falso" foram
  declarados **inválidos**;
- na checagem exploratória, declarada como tal, apareceram ~7,6 min de frames com voz durante o
  playback, com p50 de **−27,6 dBFS**, contra **−23,5 dBFS** da fala do dono no turno dele. Resíduo de
  eco depois do AEC costuma ficar 10–20 dB abaixo disso.

Leitura mais provável, **ainda não provada**: em boa parte desse tempo o dono fala por cima e o
motor não abre barge. É o pedido do dono: "não interrompe direito".

## Hipótese

| # | hipótese | MATA-SE se | Se sobreviver |
|---|---|---|---|
| H1 | com AEC ligado, o motor **não detecta** a maior parte das interrupções reais do dono | na sessão anotada, **≥ 4 de 5** interrupções abrem evento de barge (`barge_suspeito` sem detalhe, `barge_in` ou `speech_start{barge_in}`) dentro de [início anotado, início + 1,5 s] | medir o `eco_dbfs` calibrado e o envelope do mic nas perdidas, e só então propor o limiar (ver abaixo) |
| H2 (concorrente) | a "voz" no playback é resíduo de eco, e não o dono | nos trechos **sem** interrupção anotada, frames com Silero ≥ 0,5 e ≥ −45 dBFS somam **< 5 %** do playback | se sobreviver, a leitura exploratória do #870 estava errada: o problema volta a ser (a)/(b), e não (c) |

As duas podem morrer juntas. Nesse caso o readout diz isso e não força uma leitura.

## Desenho (fixado antes do número)

**Instrumento:** o log persistente do `task_9253e121`, fora do `/tmp` (o `tmp_cleaner` e o reboot
apagam o `/tmp/tts-studio.log`). Precisa ter, por sessão e com carimbo de tempo do servidor:
- `speech_start` / `speech_end` do motor (com `barge_in`);
- `barge_suspeito`, `barge_decisao`, `barge_pausa`, `barge_in` e `interrupted`;
- `eco_dbfs` calibrado e nível do mic (dBFS) a cada ~500 ms de playback;
- bordas do playback (`set_speaking` com duração), para reconstruir a janela.

**Nada de áudio, transcrição nem nome de voz no log.**

**Sessão do dono (~5 min, AEC LIGADO, a condição de produção):**
1. o dono faz uma pergunta que gere uma resposta longa (≥ 30 s);
2. durante a resposta, fala por cima **5 vezes**, com ~5 s entre elas, frases curtas e normais
   ("espera", "para aí", "não, outra coisa" — ele escolhe);
3. **anota a hora de cada interrupção.** Pode ser o botão "marcar" da UI, se o infra/frontend
   expuserem, ou a hora de relógio dita em voz alta antes ("um", "dois"…). A anotação é a
   verdade de chão. Sem ela, a medida não vale;
4. ao fim, fica ~20 s calado com a resposta tocando, que é o trecho de controle da H2.

**Métricas** (das 5 interrupções):
- detectadas / perdidas;
- tempo do início anotado até o 1º evento de barge;
- tempo do evento até o `interrupted` (o áudio parou).

Do trecho de controle, sai a fração de frames com "voz" no playback sem o dono falando.

**Alavancas, na ordem, só depois do número** (nada disso muda antes):
1. **limiar do barge com AEC ligado.** O limiar (envelope > eco + 4 dB, ou Silero ≥ 0,85 com
   degrau de 2 dB) foi desenhado para eco **sem** cancelamento. A pergunta é se o `eco_dbfs`
   calibrado nas sessões com AEC está alto (calibrado em cima da própria voz do dono, ou de um piso
   alto) e por isso empurra o limiar acima da voz dele;
2. `barge_in_ms` / `barge_decisao_ms` mais curtos;
3. a pausa reversível (`barge_pausa`, ~480 ms), que já existe atrás da flag. Ela só entra depois
   de (1), porque o duck rápido por NCC curta foi desligado de vez no #870.

**Fora de escopo até o número:** qualquer mudança no motor, nos filtros de produção ou nos
settings do dono. Mexer em `live_interrupcao` ou nos filtros de barge do dono é decisão dele.

## Proxy sintético do H1 (PRÉ-REGISTRO, 2026-11-11, antes de rodar)

**Por que existe.** A sessão anotada do dono decide o H1 e o H2 no uso real, mas depende dele. O
mini ficou livre para a bancada, e o `live_bench` já injeta uma interrupção (fala B do FLEURS) **por
cima do playback real**, com o **onset exato** (`b_ini`) como verdade de chão, e mede duck, pausa e
stop. O stop é o `interrupted`, que passa pela decisão por conteúdo com STT real. Este proxy
responde uma parte do H1, o **mecanismo do limiar**, e não substitui a sessão.

**O que o proxy PROVA:** a curva de detecção e de tempo do motor em função da **margem voz − eco**, com
verdade exata, na config de produção. Ou seja: o limiar atual perde interrupções na margem que o replay
do #870 sugeriu (dono a −27,6 dBFS no playback contra −23,5 fora dele)?

**O que só a sessão do dono prova:**
- o **H2**: se a "voz" no playback é resíduo de eco ou o dono;
- o **AEC real do Chrome**, inclusive a atenuação em double-talk. O AEC abafa a voz próxima enquanto o
  alto-falante toca, o que explicaria os ~4 dB a menos;
- a sala, o nível e o jeito de falar do dono, e o número real de perdas no uso.

**Desenho:**
- **Bancada:** clone `13716e3` (o que está no ar), estado sintético, LLM falso, corpus e voz FLEURS
  (`~/tts-bench`), sem dump.
- **Settings NÃO sensíveis de produção**, aplicados por `POST /api/settings` (só valores, nenhum dado do
  dono): `live_stt_engine=parakeet`, `live_interrupcao=inteligente`, `live_barge_sem_decisao=continua`, e
  os filtros de barge como no ar (todos ligados menos `identidade`).
- **Condições:** `--eco-db` ∈ {−15, −6, −30, −10}, nesta ordem, para espalhar a deriva. Com a fala B
  normalizada a −20 dBFS e o TTS a ~−18, a margem nominal é ≈ −2 − `eco_db`: ~13, ~4, ~28 e ~8 dB.
  **A margem que vale é a medida** no log novo (`niveis`: `dbfs_pico` no onset de B contra `eco_dbfs`).
- **Amostra:** `--ciclos 10` por condição, o que dá n = 10 interrupções B por condição.
  `--interrompe-apos-ms 1500` (default).
- **Validade:** `bancada.lock` presente, CI parado e 7860 sem `/api/tts` em cada condição, como na H6.

**Critério, fixado antes:**
- **P1, o mecanismo do limiar:** na condição **−6 dB** (margem ~4 dB, a do dono), se **≥ 9 de 10**
  interrupções abrirem evento de barge (`speech_start` com barge, `barge_suspeito` ou `barge_pausa`) em
  até **1,5 s** do `b_ini`, **o limiar NÃO é o que perde as interrupções do dono** (no proxy). Nesse caso
  o H1, se sobreviver na sessão, tem outra causa: AEC, nível ou o próprio dono. Se forem **< 9/10**, o limiar
  fica apontado, e a curva das 4 condições diz qual margem ele exige.
- **P2, controle do eco falso:** em cada condição, a taxa de `eco_falso_duck` na fase A (playback sem
  fala B), como relatado pela bancada. Ela entra como dado, sem critério de morte.
- O **stop** (n, p50, p95) e a **pausa** por condição entram como dado. Não decidem o P1.

**Leitura por faixa de margem, fixada antes de rodar** (detectadas = barge em ≤ 1,5 s do `b_ini`):

| resultado | o que o H1 conclui | alavanca |
|---|---|---|
| ≥ 9/10 em **todas** as faixas, inclusive ~4 dB (eco −6) | o **limiar não** perde a interrupção do dono. Se o H1 sobreviver na sessão, a causa é outra: AEC do Chrome em double-talk, nível ou jeito de falar do dono | nenhuma no limiar. A sessão decide |
| ≥ 9/10 em ~8, ~13 e ~28 dB, **< 9/10 em ~4 dB** | o limiar exige **mais que ~4 dB** de margem sobre o eco calibrado, e perde a voz do dono na margem medida no replay | **limiar do barge com AEC**: margem sobre `eco_dbfs` e regra do degrau. A curva diz quanto |
| < 9/10 também em ~8 dB (e > ~4 dB) | o limiar exige margem **alta**, mais que ~8 dB. Pior que o esperado | idem, com prioridade maior |
| < 9/10 até em **~28 dB** (eco −30, quase sem eco) | **não é o limiar**. O motor perde fala limpa: a cadeia de decisão, os filtros ou o `min_fala` cortam | olhar a decisão (`barge_decisao`) e os filtros, não o limiar |

**O H2 não sai deste proxy.** Saber se a "voz" no playback é resíduo de eco ou o dono **só a sessão anotada
do dono decide**. O proxy não tem AEC real, não tem a sala e não tem o dono.

### Adendo ao pré-registro do proxy (2026-11-11 ~22:00, ANTES de abrir as condições −6, −30 e −10)

**O n por condição é 5, e não 10, por estrutura da bancada.** O `live_bench` alterna os ciclos: nos pares, a
fala A é longa e a fala B a interrompe (`tipo=interrupcao`); nos ímpares, a fala A é **curta** (0,9 s, mede
o S1) e a fala B não interrompe. Com `--ciclos 10`, cada condição tem **5 interrupções**. Isso apareceu na
condição −15, a única aberta até agora (`interrupcoes: 5`).

**O P1 com n = 5, declarado antes de ver as outras condições:**
- P1 conta interrupção **detectada em até 1,5 s do `b_ini`**, como estava pré-registrado. O critério inclui
  o tempo; detecção tardia não conta como detectada;
- "≥ 9/10" vira **proporção ≥ 0,9**. Com n = 5 isso só é atingido por **5/5**; 4/5 = 0,8 reprova;
- cada proporção sai com o **IC95 de Wilson**. Com n = 5 o teste tem pouco poder: **5/5 dá IC [56,6; 100] %**.
  Por isso uma aprovação vira só "o proxy não aponta o limiar", e não prova de que o limiar está certo.
  Uma reprovação com 0–2/5 é sinal forte;
- as faixas da tabela de leitura continuam as mesmas, com "≥ 9/10" lido como "5/5" e "< 9/10" lido como
  "≤ 4/5".

**Achado da condição −15, registrado como dado e sem mudar o critério:** as **5/5** interrupções acabaram
detectadas, mas com **duck em 1154–2354 ms (p50 1854)** e **stop em 3073–6640 ms (p50 3374)**. Pelo
critério de 1,5 s, só **2/5** contam (1254 e 1154). Mesmo com margem ~13 dB, o que pesa no proxy é a
**latência** da detecção e do corte, mais do que a falta de detecção. Isso entra no readout como achado
principal da condição −15.

## Medida do cancel imediato, antes × depois (PRÉ-REGISTRO, 2026-11-12, antes de medir)

**Código:** `wt/speech-pipeline-cancel`, sobre o `50e352f` (genlock). Muda três coisas:
- o `interrupted` sai NA HORA do cancel, com dedupe por turno;
- a fila do WS perde o áudio do turno cancelado;
- o PCM tardio do trecho em geração é descartado.

**Causa medida no proxy (eco −15):** o `interrupted` saía só quando o trecho de TTS em geração voltava.
O turno 4 teve `cancel` às :54 e `interrupted` às :56 (stop de 6640 ms), e o turno 7 teve `barge_in` repetido 4×.

**Desenho:** a mesma bancada S3 do proxy (`mede_s3.sh`), com os settings não sensíveis de produção,
corpus FLEURS e sem dump.
- Dois braços, alternados **antes, depois, antes, depois**: **antes** = clone no `50e352f`; **depois** = clone
  no SHA do cancel.
- Condição única **eco −15** (o −15 do proxy), `--ciclos 10` por bloco, o que dá 5 interrupções por bloco
  e **n = 10 por braço**.
- Guardas iguais aos do proxy: CI parado, 7860 sem `/api/tts`, `bancada.lock`, nada no `/tmp`.

| critério | passa se | se falhar |
|---|---|---|
| **C1, o stop melhora** | stop p50 do braço depois ≤ p50 do antes − **500 ms** **e** stop p95 do depois ≤ p95 do antes | o conserto não chega ao cliente. Investigar a entrega com o `enviado.fila_ms` (#1129) |
| **C2, nada de áudio depois do corte** | 0 interrupção com áudio recebido pelo cliente depois do `interrupted` no braço depois (pela bancada) | o descarte vaza. Volta ao código |
| **C3, sem regressão no turno seguinte** | S1 do braço depois ≥ S1 do antes − 1 fala, e 0 turno seguinte mudo | o dedupe ou o portão engoliu o turno novo. Volta ao código |
| **C4, um cancel por turno (só dado)** | contagem de `cancel` por turno interrompido = 1 no braço depois | dado; não decide |

O duck (abertura da suspeita) **não** muda com este conserto e entra só como dado. A lacuna de ~1 s entre
servidor e cliente no duck é medida à parte com o instrumento de ms (`enviado`).
