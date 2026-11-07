#942 — bancada no mini sobre c3f4a78 (4º deploy), janela 16:16–16:50 BRT

Medição pedida pelo PM na janela 2 (porta 7890, instância sintética própria sobre a
árvore do mini `c3f4a78`, `TTS_STATE_DIR=/tmp/bench-mini/.state-local`, provider vivo
`api.ds.model.craton.app` + `deepseek-flash-41`, `omni_precision=bf16`, marca OFF
declarada, `TTS_LIVE_TTL_S=1800`, `TTS_MARCA_PREWARM_BOOT=0`, seed 875).

NÃO é a rodada de 20 ciclos: a janela fechou antes e duas instâncias de bancada
morreram no meio (16:35:33 e ~16:43, sem traço de exceção — a régua ficou em 3
ciclos, com o WS VIVO no fim e sem erro). Vale como amostra, não como série.

## Resultado (n pequeno; IC95 largos)

| métrica | valor |
|---|---|
| S1 aceitas | 4/6 (66,7 %, IC 30,0–90,3) |
| S1 WER do ciclo (mediana) | 0,467 |
| S1 perdidas ≥0,8 s | 2 |
| S2 p50 / p95 | 4 202 / 7 473 ms (n=5) |
| S2 `llm_ttft` p50 | 1 721 ms (n=3) |
| **S2 `pos_llm` p50 / p95** | **3 473 / 6 075 ms** |
| S3 duck p50 / stop p50 | 3 562 / 5 199 ms (1 amostra cada) |
| S3 eco falso corte / duck | 0/2 / 0/2 (IC 0–65,8) |
| S4 respostas com símbolo | 0/4 |
| S5 | 350 s, ws vivo, 0 erro |

Meta do #942 (`pos_llm` p50 ≤1,2 s): **não atingida** — 3 473 ms, ≈2,9× o alvo.
O `llm_ttft` (1 721 ms) sozinho já estoura o orçamento do elo; o `pos_llm` soma o
resto. Comparar com a bancada de 06/11 (`pos_llm` p50 2 931 ms, 18 amostras) é
tentador, mas muda precisão (fp32→bf16), cliente e instância — não é par A/B.

## Arquivos

- `942-bancada-mini-c3f4a78.json` — JSON cru do bench (resumo S1–S5 + turnos).
- Log da instância de bancada: `/tmp/bench-942-app2.log` (no mini, efêmero);
  log do bench: `/tmp/bench-942d.log`.

## Achados colaterais

1. Duas instâncias de bancada morreram sem `[shutdown]` nem traceback (16:35:33,
   ~16:43, esta com `send: no close frame received or sent`); a terceira durou os
   3 ciclos. Swap no pior momento 1,9/3,0 GB com compressor alto — suspeita de
   pressão de memória, não confirmada.
2. O ciclo do bench espera `turn_complete` (300 s) e depois `turn_complete` + fila
   vazia (900 s); quando o eco interrompe a resposta de B o servidor manda
   `interrupted` (terminal) e o cliente não avança — cada travamento custa o teto.
   Com 3 ciclos não deu para separar isso do defeito de produto.
