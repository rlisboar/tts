# Readout P1–P5 #1157 — rodada 2026-11-13

Árvore medida: `6c23f647c76ab901b464088ca2a4eec9348e2958`.
Baseline: `f8d53361ca58de37338d16f0b4f5138c3f402bbe`.

O tree medido contém `953ae62` (a pausa entre trechos) e `41dcc2c` (só solta o lock com Live, teto do turno preso). Não contém `7f93fa5` (#1166, família em subprocesso recusa a pausa) nem `4237ed2` (recusa de porta ocupada nas duas portas). Resultado deste arquivo não se atribui a esses dois.

Fim 09:40:41. Screen 68883, lock pid 68886, solto no EXIT. Seis blocos rc=0, `prod_tts_no_bloco=0`, `ci=0`. 7860 idle no fechamento desta leitura (11:21). Portas 7890/7895/7899 livres. Lock ausente.

Arquivos nesta pasta, os que o texto cita: `p5-base.json`, `p5-off.json`, `p5-on.json`, `pos-llm-p1157-{sem,com}-{a,b,c}-*.json`, `carga-com-{a,b,c}.json`.

## Ressalva de build

`6c23f64` contém `b04c40c`. `_voz_recorta_fala` herdou o piso de 1,0 s da verificação; o recorte inclui 0,3 s de eco; o dono é recusado e o barge não corta.

- Teste: `tests/test_live_turns.py::test_barge_curto_do_dono_sobre_o_eco_passa_com_a_fala_recortada`
- Card: `task_5436a136` (#1183 / #984), audio-ml

P1 (lock/fila), P2 (S2) e P5 (vazão) não passam por `_voz_do_dono`. S3 (interrupção, duck, stop, eco) de TODOS os braços do ABAB é **não atribuível** até a correção: os seis braços rodaram com `interrompe_apos_ms=1500` e eco −15 dB / 100 ms. Não atribuir duck/stop/eco à pausa do lote.

Braço off: `{"live_pausa_lote": false}` escrito antes do boot. Default do código é true. Baseline `f8d5336`: chave ausente, aceita por `h6_exige_flag` como a literal "ausente".

## P5 — régua, sem Live (12 min)

| braço | posts | ok | erro | 429 | timeout | jobs/min | janela |
|---|---:|---:|---:|---:|---:|---:|---:|
| base f8d5336, chave ausente | 49 | 49 | 0 | 0 | 0 | 4,01 | 734,0 s |
| off, false escrito | 49 | 49 | 0 | 0 | 0 | 4,01 | 733,2 s |
| on, true | 49 | 49 | 0 | 0 | 0 | 4,01 | 732,8 s |

Off / base = 0 % (dentro de ±10 %). A flag, com ou sem o default, não move a vazão sem Live.

## P1 — lock e fila (estágio do pos_llm, n=5)

Régua pré-registrada: p95 de lock+fila com carga ≤ 300 ms.

| braço | lock p95 ms | fila p95 ms |
|---|---:|---:|
| sem-a | 0,0 | 0,8 |
| sem-b | 0,0 | 0,0 |
| sem-c | 0,0 | 0,8 |
| com-a | 344,8 | 0,8 |
| com-b | 366,8 | 0,8 |
| com-c | 1831,0 | 0,8 |

A fila cabe. A soma dos dois p95 com carga fica acima de 300 ms nos três pares (345, 368, 1832). Não há p95 conjunto por amostra; n=5.

## P2 — S2 p95 com carga contra o sem carga do mesmo par

Régua: ≤ 1,2×.

| par | sem p95 ms | com p95 ms | razão |
|---|---:|---:|---:|
| a | 3527,7 | 4059,7 | 1,15 |
| b | 3577,9 | 4612,7 | 1,29 |
| c | 3506,2 | 6199,7 | 1,77 |

Só o par a cabe. pos_llm p95 (TTS+fila), à parte: sem 1652 / 1566 / 1556, com 1612 / 1660 / 3365.

## P3 — carga dos braços com Live

| braço | posts | ok | fim | erro | 429 | timeout | viu_pausa | jobs/min |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| com-a | 31 | 31 | 30 | 0 | 0 | 2 | 10 | 1,85 |
| com-b | 31 | 31 | 30 | 0 | 0 | 2 | 10 | 1,82 |
| com-c | 31 | 31 | 30 | 0 | 0 | 2 | 10 | 1,83 |

Um job por braço ficou sem status. Nenhum `t_fim` é ≥ `t_live_fim`. A régua "todo job termina depois que o Live fica ocioso" não se verifica com esse carimbo: os jobs fecharam com a sessão ainda aberta.

## P4

`t_soltou − t_live_fim` nos 9 jobs com pausa e `t_soltou`: p50 cerca de −520 s, o mais tardio cerca de −89 s. Soltaram antes do fim da sessão. Não há carimbo separado de "ficou ocioso", então a régua "retomada ≤ 60 s" não fecha com este JSON.

## S3 — não atribuível

Nos seis braços: 5 interrupções, duck_ok_300 = 0, stop_ok_800 = 0, eco_respostas = 10, recusadas_identidade = 0. Número de barge/eco, não de `live_pausa_lote`.

## Nota sobre o 1831 ms (par c)

O p95 publicado não é uma espera que aconteceu. `pct` interpola: n=5, p=0,95, k=3,8. No par c os cinco `lock_espera_ms` são 0, 619, 0, 2134, 0. Ordenados [0, 0, 0, 619, 2134] dão 619 + 0,8×(2134−619) = 1831.

A espera real é 2134 ms, na fala 12 (papel A, duração da fala do usuário 28,36 s). `fila_tts` nesse turno = 1 ms. `tts_gen` depois de pegar o lock = 1154 ms, igual aos outros turnos. Não é fila e não é o teto `_LOTE_TURNO_PRESO_S` (30 s, e esse teto é o lote esperando um turno do Live, o sentido oposto).

`lock_espera_ms` é o tempo até entrar no `_gen_lock` (`live_pipeline.py`, antes do `with trava`). 2134 ms é resto de um trecho do lote já em curso. Os outros não-zero da rodada: 431, 428, 122, 619 ms. Todos abaixo de um trecho (~3,5 s).

## Revisão do critério P1 (decisão do PM, não conveniência)

O teto de 300 ms media o resíduo do trecho em curso, que é unpreemptível por desenho (sem mexer na difusão). O critério novo mede a mesma espera com a régua certa. O p95 sai do veredito: com n=5 e vários zeros ele interpola e não descreve espera nenhuma (o 1831 é 619 + 0,8×(2134−619)).

- **P1a (bloqueia):** nenhuma `lock_espera_ms` passa de um trecho do lote. Referência medida: 3,5 s. Máximo da rodada: 2 134 ms. Passa.
- **P1b (reporta, não bloqueia):** a distribuição. Seis braços × 5 turnos A = 30 amostras com `lock_espera_ms`. 25 zeradas. Não-zero, só nos braços com carga: 122, 428, 431, 619 e 2 134 ms.

## P2 — resultado, critério mantido

S2 p95 com/sem: 1,15 / 1,29 / 1,77 contra 1,2×. Reprova nos pares (b) e (c). Não é critério errado.

A fala 12 do par (c) concentra o excesso: S2 6 290 ms contra 2 977 ms na mesma fala do sem-c. Desses 3 313 ms, 2 134 são lock. Os outros 1 179 ms não são a pausa. O veredito abaixo fecha isso: o P2 reprova.

## P4 — vazia

Não há número de retomada. Esta seção fica vazia até o `task_7533b217`.

`t_live_fim` não serve: é o `touch` de `p1157.livefim` depois que o `mede_pos_llm.sh` volta, o fim do processo da bancada, não o instante em que `_live_ativo_para_lote` ficou falso. `t_soltou` cai 89–956 s antes desse carimbo, e `retomada_s` sai negativo. Isso mostra o instrumento errado. Não é medida de retomada e não prova a promessa (retomada ≤ 60 s).

## Veredito

O card **reprova no P2**. P1a, P3 e P5 passam. P2 falha em (b) e (c) e a régua de 1,2× não mudou. A feature fica na main porque é melhoria estrita no lock e não piora nada — não é vitória. P4 continua não medido (`task_7533b217`).

O lock cai ao resíduo de um trecho, e isso é o ganho.

Cerca de 1,2 s do excesso daquele turno não é lock, e o card reprova por isso. Na fala 12 o excesso de S2 é 3 313 ms (6 290 no com-c contra 2 977 na mesma fala do sem-c) e só 2 134 ms são `lock_espera_ms`. Os outros 1 179 ms não são a pausa. Mesmo com o P1 novo, o P2 não passaria. O resíduo tem outra causa.

Build medido: `6c23f647c76ab901b464088ca2a4eec9348e2958` (contém `953ae62` e `41dcc2c`; não contém `7f93fa5` nem `4237ed2`). Baseline: `f8d53361ca58de37338d16f0b4f5138c3f402bbe`.
