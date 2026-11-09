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
`eco_falso_duck` ≤ 5 % (IC95 superior ≤ 5 %) **E** `eco_falso_corte` ≤ C1/F0
**E** S1 e S2 sem piora contra C1/F0.

Qualquer um falhando, ou amostra faltando → **não liga** (ou INDETERMINADO).
Os limites estão no cabeçalho do `rodada_c0c1.sh` e nas constantes do
`relatorio_rodada.py`.

## Desenho

4 células, 10 ciclos cada (40 ciclos = 80 falas, o mesmo volume de um 20+20),
cada célula seguida de 3 min de eco puro e 3 min de ventoinha:

| célula | motor | flag |
|---|---|---|
| `f0-c0` | whisper | off |
| `f0-c1` | parakeet | off |
| `f1-c0` | whisper | on |
| `f1-c1` | parakeet | on |

A troca de motor é por `POST /api/settings` (sem restart). A troca de flag é por
`TTS_LIVE_BARGE_DUCK_RAPIDO` no ambiente, então a instância da 7890 **reinicia**
entre os blocos — a produção (7860) nunca.

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

1. O SHA no ar tem o C1 e o #870 (patch-id conferido pelo PM). Conferir
   `git -C ~/Documents/tts-rod rev-parse --short HEAD` e o `codigo` do
   `/api/build` com a chave.
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

O script faz pré-voo que recusa, sobe/derruba a 7890 por bloco, roda as 4
células + 4 fases de eco + 4 de ventoinha, amostra RSS/swap a cada 60 s e
imprime o relatório. O `trap` (EXIT/INT/TERM) devolve `live_stt_engine` e derruba
tudo que subiu.

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
