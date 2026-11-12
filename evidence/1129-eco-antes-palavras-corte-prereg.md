# #1129 / task_3ecbe8f5 — filtro `eco` antes de `palavras_corte`: PRÉ-REGISTRO (2026-11-11, antes do código)

## O defeito

No proxy S3 (readout `b461d59`, eco −6, `s3-eco-6-39834.log` linhas 754–767 e 1507–1519), o eco da
resposta do agente ("…os detalhes com calma…") sai do STT como `"Com calma."`. `_live_avalia_filtros`
checa `palavras_corte` antes do filtro `eco`, e `calma` está em `_COMANDO_PARAR`, que casa em qualquer
posição. Resultado: `tipo=interrupcao filtro=palavras_corte decisao_ms=0` e o agente cancela a própria
resposta. A lista também tem `para` e `nao`. Decisão do PM: é bug, não preferência.

## A mudança (decidida pelo PM)

1. `eco` roda **antes** de `palavras_corte` na cadeia.
2. **Match exato quando há palavra de corte:** se o transcript contém uma palavra de corte, só é eco
   se aparecer contíguo e **sem nenhuma troca** no texto em reprodução (`minimo=1.0`). Sem palavra de
   corte, a tolerância de sempre (40 %, teto 2). Motivo: com tolerância, "para tudo" sobre um trecho
   com "para X", ou o caso do #547 ("…e aí, para" com o eco na frente), seriam engolidos como eco.
   O `_ECO_MIN_PALAVRAS = 2` já garante que "para" sozinho nunca é eco.

Fora do escopo: o eco fechado com "?" (falsos cortes do −10, `filtro=pergunta`) é o card
`task_21d2c3b3`, depois deste trem.

## Células (motor real do Live, `_motor_inteligente` + `_live_decide_barge`)

- eco "Com calma." com o texto em reprodução contendo "com calma" → `eco`, sem cancelar;
- caso #547: "olá, meu nome é valentina e aí, para" sobre uma reprodução "olá, meu nome é valentina e
  aí tudo bem" → `interrupcao/palavras_corte` (cancela);
- "para tudo" sobre uma reprodução com "para isso" → corta; "para" sozinho → corta;
- passo intermediário (`so_comando`) com o eco de corte não decide nem cancela.

**Mordida:** com a ordem invertida de volta (`palavras_corte` antes de `eco`), a célula do eco cai.
Com tolerância no lugar do match exato, as células do #547 e de "para tudo" caem.

## Medida no mini (proxy S3, mesmo roteiro `mede_s3.sh`)

- Condições **eco −6** e **eco −10**; `--ciclos 10`, `--seed 875`; mesmo corpus FLEURS e mesma voz;
  LLM falso (`tools/llm_fake_sse.py`); estado sintético; nada no `/tmp`.
- **Antes:** clone do HEAD de `wt/speech-pipeline-cancel` sem a troca. **Depois:** o mesmo, com a troca.
  A comparação contra o `13716e3` do readout fica só como referência, porque o trem também traz
  `50e352f` e o cancel imediato.
- Validade: como na H6 e no S3 (`bancada.lock`, CI parado, 7860 sem `/api/tts` no bloco). Roda depois
  do glk (H6 genlock), em série.

**Critério, fixado antes:**

- **P-a1:** no depois, **0** decisões `barge_decisao tipo=interrupcao filtro=palavras_corte` sobre
  eco em −6 e em −10. "Sobre eco" = o texto normalizado é um trecho contíguo e exato da `RESPOSTA` do
  LLM falso. Palavra de corte que vem da fala B (FLEURS) não conta. Referência: no `13716e3`, 2 no
  −6 e 0 no −10.
- **P-a2, a detecção não piora:** em cada condição, entre as falas B **com playback**
  (`eco_tocado_s > 0`):
  - a fração com barge detectado (duck não nulo) no depois é ≥ a do antes;
  - as detectadas em até 1,5 s do `b_ini` no depois são ≥ as do antes − 1 (tolerância de 1, porque
    n = 4–7).
- Entram como dado, sem decidir: o número de falas B com playback no −6 (no `13716e3` era 0/4,
  porque o agente se cortava antes), falso corte e falso duck da bancada, duck e stop p50/máx, e a
  contagem de decisões por filtro.

Passa se P-a1 **e** P-a2 passarem. Se o P-a1 falhar, o texto que escapou mostra o caminho (match
exato rígido demais para o STT, ou a referência `texto_em_reproducao` errada). Se o P-a2 falhar,
volta para análise antes do deploy.

### Adendo (2026-11-12, ANTES de medir): os clones exatos

O trem cresceu antes da medida (regra do #446). O HEAD é o `5b12f9a` (`wt/speech-pipeline-cancel`):
`50e352f` → `9e2bab4` (cancel) → `c5966d7` (esta troca) → `9ea2faa` (gate do qa no cancel) → `c84a6cd`
(telemetria do árbitro) → `1dae648` (comentário) → merge do `f5a1333` (instrumento de ms).

- **Depois** = clone em `5b12f9a`.
- **Antes** = o MESMO clone com `git revert --no-commit c5966d7` (só a troca de ordem sai; cancel,
  telemetria e instrumento ficam). Assim a diferença entre os braços é só a troca.
- Ordem no mini, em série e depois do glk, sob o `bancada.lock`: primeiro a medida do cancel (C1–C4,
  `90948a1`, eco −15, ABAB), depois esta (eco −6 e −10, braços alternados por condição: antes, depois).
- Critérios P-a1/P-a2 sem mudança. O readout "depois" sai ANTES de qualquer deploy (P3 do qa).
- **Nome do campo no C1 do cancel:** o clone `5b12f9a` é anterior ao `d1ea6eb`, então a espera do `enviado` sai
  como `enviado.fila_ms` (o nome do `90948a1`). O nome `espera_ms` só vale do `d1ea6eb` em diante. O `fila_ms` do
  `niveis` é OUTRA coisa (áudio por tocar) e não entra no C1.
- **C2 do cancel, medido à parte (adendo de 2026-11-12, ANTES de largar):** o readout `7add436` deixou o C2 sem
  medir, porque o `live_bench` não exportava o áudio recebido depois do `interrupted`. O instrumento é o `f2c306b`
  (`S3.pos_corte_com_audio`: cortes com ≥ 1 chunk de áudio entre o `interrupted` e o `transcript_user` do turno
  seguinte). Rodada `s3c`, depois do glk2: 2 blocos **depois** (`5b12f9a`, eco −15, `--ciclos 10`, n ≈ 10 cortes),
  com o servidor do clone e o `live_bench` do `f2c306b`. Critério do `90948a1` sem mudança:
  **`pos_corte_com_audio` = 0** no braço depois. Mais 1 bloco **antes** (`50e352f`) como CONTROLE POSITIVO do
  instrumento. Ele não decide o C2, só mostra que o contador vê o vazamento quando existe. Se o controle der 0, o
  instrumento fica sob suspeita e o C2 não é dado como passado.
