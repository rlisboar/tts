# Runbook — janela C0 × C1 do #876 (motor do STT do Live)

Só rodar com a janela autorizada pelo PM. O objeto é decidir, com número, se o
Parakeet (`live_stt_engine=parakeet`, C1) entra como motor do STT do Live no
lugar do whisper (C0). **A janela é no mini de produção** — 16 GB, com o app do
dono no ar na 7860.

Quem faz o quê:

| passo | quem |
|---|---|
| deploy do C1 e do #1021 (integração + subida) | infra-remote / PM |
| janela, coleta e relatório | audio-ml |
| leitura do veredito | PM |

## Regras que valem em todos os passos

- **Produção intocada**: nada de restart da 7860, nada de `settings.json` do
  dono. A bancada usa `TTS_STATE_DIR` sintético em `/tmp`.
- **Porta 7890** para a instância de bancada; `pkill -f "port 7890"` no fim.
- Uma rodada por vez. Se outro agente estiver com a 7890, esperar.
- Se a máquina estiver com swap acima de ~1,5 GB ou com sessão Live do dono
  aberta, **não começar**.

## Antes: o que tem de estar verdadeiro

1. O SHA no ar é o que tem o C1. Conferir os dois lados:
   `git -C ~/Documents/tts-rod rev-parse --short HEAD` e
   `curl -s http://127.0.0.1:7860/api/build` (com a chave) — o `codigo` tem de
   bater com o rev.
2. `live_stt_engine` existe no `GET /api/settings`. Sem a chave, o C1 não é
   selecionável e a janela não tem objeto — parar e avisar o PM.
3. Corpus copiado (nunca mover o original de `voices/`):
   `mkdir -p /tmp/live-bench-audio && cp voices/*.wav voices/*.json /tmp/live-bench-audio/`
4. `sysctl vm.swapusage` anotado antes.

## Execução

```zsh
cd ~/Documents/tts-rod

# 1) estado sintético (settings mínimo; a bancada não lê o do dono)
mkdir -p /tmp/bench-mini/.state-local /tmp/bench-mini/outputs
printf '%s' '{"omni_num_steps":16,"live_num_steps":16,"tts_marca_dagua":false,"tts_marca_prewarm_boot":false}' \
  > /tmp/bench-mini/.state-local/settings.json

# 2) instância de bancada na 7890 (provedor do dono, sem chave impressa)
K=$(python3 -c 'import json;print(json.load(open("settings.json"))["chat_api_key"])')
B=$(python3 -c 'import json;print(json.load(open("settings.json"))["chat_base_url"])')
M=$(python3 -c 'import json;print(json.load(open("settings.json"))["chat_model"])')
TTS_STATE_DIR=/tmp/bench-mini/.state-local TTS_OUTPUTS_DIR=/tmp/bench-mini/outputs \
TTS_CHAT_BACKEND_LIVE=openai TTS_CHAT_BASE_URL="$B" TTS_CHAT_MODEL="$M" \
TTS_CHAT_API_KEY="$K" TTS_MARCA_PREWARM_BOOT=0 TTS_ALLOWED_HOSTS=127.0.0.1,localhost \
TTS_LIVE_TTL_S=1800 \
  nohup ./.venv-mlx/bin/uvicorn app:app --host 127.0.0.1 --port 7890 --log-level warning \
  > /tmp/bench-mini/app.log 2>&1 &
until curl -s -m 2 -o /dev/null http://127.0.0.1:7890/health; do sleep 2; done

# 3) a rodada inteira (C0 → C1 → eco → ventoinha → relatório)
tools/rodada_c0c1.sh --sha "$(git rev-parse --short HEAD)" \
  --corpus /tmp/live-bench-audio --ciclos 20 --min-eco 5 --min-ventoinha 5
```

`rodada_c0c1.sh` faz sozinho: recusa de pré-voo (7890/7860/SHA/corpus), amostra
de RSS e swap a cada 60 s, as seis fases, o relatório e a limpeza no `trap`.
Sai com `rc≠0` e sem tocar em nada se o pré-voo recusar.

## Rollback por passo

| onde parou | como voltar |
|---|---|
| pré-voo recusou | nada a desfazer; a mensagem diz o que falta |
| fase C0 | o `trap` derruba o bench e a 7890; a produção nunca foi tocada |
| entre C0 e C1 (POST de settings) | o `trap` devolve `live_stt_engine` ao valor lido no início |
| fase C1 | idem: `trap` restaura a chave e derruba a 7890 |
| bancada travada | `pkill -f live_bench; pkill -f fase_ruido.py; pkill -f "port 7890"` — e conferir `pgrep -f "port 7860"` = 1 |
| produção suspeita | parar tudo e avisar o PM; **não** reiniciar a 7860 sem combinar |

O `trap` roda em `EXIT INT TERM`, então `Ctrl-C` também limpa.

## Depois

1. Conferir no log da corrida: `7890 listeners: 0`, `uvicorn 7860: 1`, health 200.
2. Publicar o relatório (`<dir>/corrida.log`) no #876 e a curva de RSS/swap no
   #891.
3. Se algum SLO sair FALHA, o C1 **não** vira default: a chave fica, o veredito
   vai ao PM com o número.
4. Evidência: o diretório inteiro da rodada é a prova; copiar para fora do mini.

## O que o relatório mede

`stt p50` e WER das falas A saem do log do app (estágio `stt`); falso turno por
eco e por ventoinha saem das fases de ruído; S1–S4 saem do JSON da bancada; RSS
e swap saem do amostrador. Cada linha traz PASS/FAIL contra os aceites do card —
arquivo ausente vira INDETERMINADO, nunca PASSA por omissão.
