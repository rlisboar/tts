# DSH-4a — evidência do patch do bridge ACP (streaming incremental)

Pacote: `@deepseek-ai/dsh-acp` **0.1.5-rc.3** (dependência aninhada do `dsh` global;
`$(npm root -g)/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-acp/lib/index.js`).
Patch: `scripts/dsh-acp-stream.patch.js` · aplicador: `scripts/dsh-acp-stream-patch.mjs`.
sha256 do arquivo: original `dcfa3790c65b…` → patchado `465c1b7f70e5…`.

Antes: o bridge assinava só `session/event` → `assistant/message`, então o cliente ACP
recebia **um** `agent_message_chunk` no FIM da geração. Depois: assina
`agent/assistant-stream` e emite um chunk por delta.

## 1. `evidence/dsh-chunks-spike.py` (bruto, por chunks)

| caso | antes | depois |
|---|---|---|
| `tts-studio`, resposta curta | 1 chunk @ 410 ms | 4 chunks, 1º @ 874 ms |
| `tts-studio`, resposta ~1,5–2,8 k | **1 chunk @ 74 168 ms** | **203 chunks, 1º @ 382 ms**, fim @ 4 043 ms |
| `acp` puro (com tools), resposta longa | 3 chunks, 1º @ 74 117 ms | 259 chunks, 1º @ 1 671 ms |

Saídas cruas: `dsh-acp-chunks-before.txt` / `dsh-acp-chunks-after.txt`.
(nos dois arquivos o total de chars conta deltas **e** a mensagem comitada — o
cliente deduplica; a contagem pós-dedup está na seção 3.)

## 2. `evidence/dsh-provider-sse-spike.py` (teto do provedor, sem ACP)

1º delta 372–376 ms e 230 deltas numa resposta de 1,5 k. É o teto: o ACP agora
chega perto dele, o que prova que o gargalo era a projeção e não a rota.

## 3. `evidence/dsh-acp-partial-spike.py` (a marca `partial` + dedup real)

Usa a função de dedup **importada** do `dsh_client` (não uma cópia).

| estado | chunks | deltas `partial=True` | comitado `partial=False` | 1º delta | chars pós-dedup |
|---|---|---|---|---|---|
| sem patch (`--revert`, curta) | 1 | 0 | 0 (sem marca) | — | 10 (= bruto) |
| patchado (curta) | 4 | 3 | 1 | 1028 ms | **16** (bruto 32) |
| patchado (longa ~1,4 k) | 240 | 239 | 1 | **402 ms** | **1757** (bruto 3514) |

`messageIds distintos: 1` → deltas e comitado caem no MESMO id (uma bolha, não duas).
O dedup do cliente entrega exatamente o texto: nada duplica, nada some. Confirmado
também após `--revert` + `--reapply` (mesmo sha256, mesmo comportamento).

## 4. Ponta do cliente (`dsh_client`, o caminho que o Live usa)

`boot+prewarm 5986 ms` · **1º delta 377 ms** · `deltas=251 chars=1916
chunks_emitidos=251 chunks_duplicados=1` (o único descartado é a mensagem comitada).

## 5. Vizinho: daemon do claudinhos (`ops/scripts/dsh-smoke-acp.mjs`)

Antes/depois em `dsh-acp-daemon-smoke-{before,after}.jsonl` (frames crus; o
`prompt.stopReason` ficou `end_turn` nos dois, `dsh_exit=0`, sem erro).

| | antes | depois |
|---|---|---|
| `agent_message_chunk` no turno | 1 | 2 (1 delta + 1 comitado) |
| tempo até o 1º chunk de texto | +49 908 ms | **+2 378 ms** |
| `partial` no 1º / no último | ausente | `True` / `False` |

O daemon não conhece a marca: ele ganha granularidade (recebe cedo) e, se renderizar
os dois chunks, mostra o texto 2× — por isso o patch MARCA em vez de suprimir.