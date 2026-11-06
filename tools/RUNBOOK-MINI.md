# Runbook — medição no mini (janela do PM)

Só rodar quando o PM abrir a janela. Máquina de produção do dono: não mexer em
settings, não derrubar o serviço, não deixar o app de bancada vivo depois.

## Antes

1. Confirmar o SHA no ar: `git -C <árvore> log --oneline -1` tem de ser o
   `b2f6c33`. Se não for, parar e avisar.
2. Porta fora de 7861-7865 (a página sonda essa faixa): usar **7890**.
3. Estado sintético: `TTS_STATE_DIR=/tmp/bench-mini/.state-local` (nunca o
   Application Support).
4. Backend de produção: `TTS_CHAT_BACKEND_LIVE=openai`,
   `TTS_CHAT_BASE_URL=https://api.ds.model.craton.app/v1`,
   `TTS_CHAT_MODEL=deepseek-flash-41`, chave por env (não imprimir).
5. Marca d'água desligada nas duas pontas (`tts_marca_dagua=false` no
   `settings.json` do estado sintético) — declarar no relatório.
6. Copiar o áudio: `mkdir -p /tmp/live-bench-audio && cp voices/*.wav voices/*.json /tmp/live-bench-audio/`.
7. `sysctl vm.swapusage` antes, para registrar a folga.

## Rodada

    .venv-mlx/bin/python -I /tmp/live_bench.py --audio /tmp/live-bench-audio \
        --url ws://127.0.0.1:7890/api/live/ws --seed 875 \
        --ciclos 20 --teto-turno-s 300 \
        --out /tmp/live-bench-b2f6c33.json --rotulo b2f6c33-mini

- 20 ciclos em UMA sessão (o `TTS_LIVE_TTL_S` do app precisa aguentar; sem isso
  quebrar em blocos de 5 e juntar).
- Se travar: o bench tem teto de ciclo (300 s), de `send` (15 s), de `close`
  (5 s) e de `lsof` (15 s) — a rodada fecha mesmo com o app pendurado.
- Não rodar duas instâncias em paralelo no mini.

## Depois

1. Copiar o JSON para cá (`/tmp/bench-mini/`), nunca deixar só no mini.
2. Derrubar a instância de bancada (`pkill -f "port 7890"`).
3. Relatar S1–S5 com IC95 (o resumo já traz), o swap antes/depois e o SHA.
   O S2 vai separado em `llm_ttft` e `pos_llm` — não somar.
