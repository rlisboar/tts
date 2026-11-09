# Runbook — janela 2×2 #876 (motor do STT) × #870 (duck rápido)

Só rodar com a janela autorizada pelo PM. Duas perguntas, uma rodada:

- **#876**: o Parakeet (`live_stt_engine=parakeet`, C1) substitui o whisper (C0)
  sem estragar S1/S2 e com o `stt` mais rápido?
- **#870**: a flag `TTS_LIVE_BARGE_DUCK_RAPIDO` (F1) pode LIGAR? Ela abaixa em
  ~96 ms e pausa em ~480 ms sem esperar o whisper — se o número couber no S3 e
  não inventar falso duck, liga.

**A janela é no mini de produção** — 16 GB, app do dono no ar na 7860.

## Limites declarados ANTES da rodada (não se movem depois)

Liga a flag ⇔ **no C1**: pausa p95 ≤ 800 ms **E** duck p50 ≤ 300 ms **E**
`eco_falso_duck` ≤ 5 % (PONTO falsos/respostas) **E** `eco_falso_corte` ≤
C1/F0 **E** S1, WER pareado, S2, S3, S4 e RSS sem piora contra C1/F0.

O critério do eco é o **ponto**, não o IC95: com n=10 e zero falso o Wilson dá
`[0; 27,8]` e o ramo LIGA nunca passaria. O IC sai impresso junto, só como
resolução. Faixas: ≤5 % PASSA · **5–10 % INDETERMINADO** · >10 % NÃO LIGA.

Qualquer outro critério falhando, ou amostra faltando → **não liga** (ou
INDETERMINADO). Os limites estão no cabeçalho do `rodada_c0c1.sh` e nas
constantes do `relatorio_rodada.py`.

## Provedor de LLM (a régua do S2)

O S2 é `STT + 1º token da LLM + TTS + fila`. Se o provedor oscila, a comparação
C0×C1 e F0×F1 fica refém da rede. Com `--llm-fake` o script sobe o
`tools/llm_fake_sse.py` na 7898 (TTFT fixo de 300 ms, sem sair da máquina) e
aponta a instância de bancada para ele; o `trap` derruba o fake junto.

**O que isso obriga a declarar no relatório:** o S2 absoluto vira PISO — em
produção o TTFT é 0,8–1,3 s, então o número sai otimista e NÃO serve para o
aceite `p50 ≤ 1,5 s` sem essa etiqueta. O que continua comparável é o
`s2_pos_llm_ms` (STT + TTS + fila) e o delta entre células; o `llm_ttft` fica
constante e auditável. O relatório imprime a ressalva sozinho quando a base é
local.

Sem `--llm-fake` o script usa o `chat_base_url` do `settings.json` da árvore e
recusa a rodada se o `/models` responder 5xx/000 (foi o que pegou o provedor
morto em 530).

## Desenho

4 células, **10 ciclos contados + 1 de aquecimento descartado** cada, seguidas de
3 min de eco puro e 3 min de ventoinha. O aquecimento existe porque cada bloco
reinicia o app e o `_cond_for` em cache miss custa ~1500 ms — sem ele a 1ª
amostra envenena 10 % da célula. Ele é um turno com marca própria (`aquec`), e a
janela do `stt` começa no FIM dele: o `live_bench` escreve `<célula> ciclos`
quando o aquecimento acaba, e é dali que a janela conta.

**Ordem contrabalançada, só o motor inverte** (o motor novo cai no fim de um
bloco de flag e no começo do outro; a deriva térmica e de RSS não se cola num
motor só):

| # | célula | motor | flag |
|---|---|---|---|
| 1 | `f0-c0` | whisper | off |
| 2 | `f0-c1` | parakeet | off |
| 3 | `f1-c1` | parakeet | on |
| 4 | `f1-c0` | whisper | on |

A ordem executada é lida do próprio `marcas.txt` (ordem de 1ª aparição) e sai no
topo do relatório — não há arquivo de ordem à parte para divergir. A troca de
motor é por `POST /api/settings` (sem restart). A troca de flag é por
`TTS_LIVE_BARGE_DUCK_RAPIDO` no ambiente, então a instância da 7890 reinicia
quando a flag muda — a produção (7860) nunca.

n=10 não sustenta p95: o relatório traz p50 e máximo ao lado e o veredito diz
que a decisão não sai só da p95. A base do `eco_falso_corte` é **C1/F0** (não o
C1 da própria célula), senão o próprio duck vira confundidor.

## Regras

- Produção intocada: nada de restart da 7860, nada de `settings.json` do dono.
- Porta 7890; o script sobe e derruba a instância sozinho, **pelo PID que ele
  mesmo registra** (casar linha de comando não alcança o app do pos_llm).
- **Em SÉRIE com o `mede_pos_llm.sh` (#1022), nunca em paralelo.** Ele sobe app
  e LLM falso; as duas cópias do OmniVoice na GPU não cabem no swap do mini. As
  portas não colidem (7890 × 7895/7899) — o recurso escasso é a GPU. O pré-voo
  recusa se 7895/7899 estiverem no ar ou se já houver bancada rodando.
- Uma rodada por vez. Swap acima de ~1,5 GB ou sessão Live do dono aberta: não
  começar.
- **A primeira execução é teste do instrumento.** Se o pré-voo ou o `trap`
  falharem, PARAR e consertar antes de gerar número — não adaptar no meio da
  coleta.

## Antes

1. O SHA no ar tem o C1 e o #870 (patch-id conferido pelo PM). O `--sha` é
   **obrigatório** e o pré-voo compara com o `codigo` do `/api/build`: não bateu,
   `exit 1` antes de subir a 7890 — célula medida em build errado é número
   perdido.
2. `live_stt_engine` existe no `GET /api/settings`; sem a chave, parar.
3. Corpus copiado (nunca mover o original de `voices/`):
   `mkdir -p /tmp/live-bench-audio && cp voices/*.wav voices/*.json /tmp/live-bench-audio/`
4. Estado sintético:
   `mkdir -p /tmp/bench-mini/.state-local && printf '%s' '{"omni_num_steps":16,"live_num_steps":16,"tts_marca_dagua":false,"tts_marca_prewarm_boot":false}' > /tmp/bench-mini/.state-local/settings.json`
5. `sysctl vm.swapusage` anotado.

## Execução

```zsh
cd ~/Documents/tts-rod
tools/rodada_c0c1.sh --app-dir "$PWD" --sha "$(git rev-parse --short HEAD)" \
  --corpus /tmp/live-bench-audio --estado /tmp/bench-mini/.state-local
```

O script faz pré-voo que recusa (inclusive por SHA divergente), sobe/derruba a
7890 por bloco — esperando o listener soltar antes de cada subida —, roda as 4
células + 4 fases de eco + 4 de ventoinha, amostra RSS/swap a cada 60 s e
imprime o relatório. Cada fase tem a própria marca de tempo: a janela do `stt`
usa só a fase `ciclos` (a de eco injeta gatilhos de 0,7 s e puxaria o p50 para
baixo). Cada estado de flag tem o seu log (`app-0.log`/`app-1.log`) — misturar
os dois é misturar dois processos. O `trap` (EXIT/INT/TERM) devolve
`live_stt_engine` (lido do arquivo de estado) e mata tudo pelo PID registrado.

## Rollback por passo

| onde parou | como voltar |
|---|---|
| pré-voo recusou | nada a desfazer; a mensagem diz o que falta |
| em qualquer célula | `trap` mata bench e fases, restaura a chave e mata o app pelo PID registrado |
| bancada travada | o `trap` já matou o app; se sobrar algo: `pgrep -f 'live_bench|fase_ruido|port 7890'` e `kill` no PID; conferir `pgrep -f "port 7860"` = 1 |
| produção suspeita | parar tudo e avisar o PM; **não** reiniciar a 7860 sozinho |

## Depois

1. Conferir no log da corrida: `7890 listeners: 0`, `uvicorn 7860: 1`, health 200.
2. Publicar o relatório no #876 (motor) e no #870 (flag); a curva de RSS/swap
   vai no #891.
3. Veredito NÃO LIGA → a flag fica como está e o número vai ao PM. LIGA → o PM
   decide a virada do default (é env, não settings).
4. Evidência: o diretório inteiro da rodada; copiar para fora do mini.
