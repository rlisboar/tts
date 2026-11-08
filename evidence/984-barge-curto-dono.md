# #984 — barge curto do dono sobre o eco: pontuar só a fala nova leva o aceite de 0,4 s de 6 % a 60 % e baixa o falso aceite total de 4,2 % a 1,8 %

Data: 2026-11-08. Bancada offline no MacBook. `_voz_do_dono` real, ECAPA e coorte do
app, perfil "Dono E2E" (3 amostras ECAPA) copiado do estado do gate #980 para
`/tmp/ab984/state`. O estado do QA não foi tocado. Sem servidor.
Scripts: `evidence/984-barge-curto-dono.py`, `evidence/984-eco-sozinho.py`.

## Mudança

`_voz_do_dono(pcm, cfg, fala_ms=0)`: com `fala_ms`, o portão pontua só o pedaço do
trecho onde está a fala nova (`_voz_recorta_fala`). É o trecho de maior energia com
`fala_ms` + 0,2 s, com piso de 0,6 s, que é o mínimo do extrator. A regra das 2
confirmações passa a valer sobre esse pedaço, não sobre o trecho com o eco. As duas
chamadas do barge passam `fala_ms`: o I5 em `_live_decide_barge` e o filtro
`identidade` da cadeia. O turno do STT (`_stt_julga`) não passa `fala_ms` e segue
idêntico.

## Cenário

Trecho = eco da resposta do começo ao fim (as 3 saídas de TTS do E2E, nas vozes
Valentina, Bonner e Andrea) + a fala nova a −26 dBFS, com o eco a −12/−15/−18 dB
abaixo dela. Total de max(1,376 s, 0,4 + fala + 0,3 s). A fala entra no FIM (o caso
do E2E) ou logo depois do pré-roll de 0,4 s.
- Dono: `/tmp/e2e-dono-3.wav`, fora do cadastro; 8 recortes por célula.
- Terceiros: Valentina do E2E + 16 vozes PT de `voices/` (fica fora a que tem cosseno
  >0,5 com o dono); 3 recortes cada.
Limiar: o do portão (`rigor_far=1.8`, AS-norm).

## Resultado (somados os 3 níveis de eco e as 2 posições)

| fala | dono aceito, antes | dono aceito, depois | terceiro aceito, antes | terceiro aceito, depois |
|---|---|---|---|---|
| 0,4 s | 3/48 (6 %) | **29/48 (60 %)** | 0/306 | 2/306 |
| 0,6 s | 11/48 (23 %) | **34/48 (71 %)** | 0/306 | 5/306 |
| 0,8 s | 41/48 | 46/48 | 1/306 | 3/306 |
| 1,0 s | 42/48 | 47/48 | 1/306 | 11/306 |
| 1,4 s | 48/48 | 48/48 | **62/306 (20 %)** | **7/306** |
| **total** | 145/240 (60 %) | **204/240 (85 %)** | 64/1530 (4,2 %) | **28/1530 (1,8 %)** |

Latência do portão: p50 46 → 15 ms, p95 112 → 56 ms (menos janelas por chamada).

## Falso turno (protocolo do #533)

`MINUTOS=10 smoke_so_minha_voz.py` no worktree com a mudança dá **VEREDITO OK**:
- terceiro: 0 turno falso em 10 min;
- dono: cobertura de 90,0 %;
- p95 de 38–43 ms.
O caminho do turno não mudou (sem `fala_ms`), e o smoke confirma isso.

## O que fica de fora (e por quê)

- **O eco sozinho já passa como dono** em 6/40 trechos antes e 5/40 depois. O
  recorte não muda isso. Os falsos aceites que restam concentram-se em duas vozes
  (Andrea2, que é a mesma pessoa de uma das vozes do eco, e Miriam). É o problema de
  calibração do AS-norm 1,8 em fala PT (`evidence/979-limiar-ecapa-pt.md`: FAR de
  15 % em 3 s), que vai no task_d67b6cb8 (#982), não aqui.
- O recorte por energia supõe que o resíduo do eco (pós-AEC) fica abaixo da voz do
  dono. Com o eco a −12 dB isso se mantém. Eco mais alto que a voz não foi medido.

## Mordidas

Ver `tests/MORDIDAS.md`, seção #984.
