# DSH-4b — o BLOCO da resposta: onde a resposta é retida

Veredito: **upstream**. A retenção está entre o provedor e o harness (a rota LLM
`dsflash`, SGLang atrás do Cloudflare, em `api.ds.model.craton.app`), NÃO no nosso
patch, não no `notify`, não no stdout, não na leitura do cliente.

Assinatura: em ~metade dos turnos o provedor entrega os primeiros ~1,2 KB (cabeçalho
+ 1–2 deltas, em 0,3–2 s) e **para**; 17–88 s depois despeja o resto de uma vez, e a
geração termina em < 1 s. O 1º delta sai cedo (por isso o patch DSH-4a parece bom) e
a fala fica presa na primeira sentença.

Métodos (scripts versionados; `-x` = needs FS fora do workspace: `~/.dsh` e o pacote):

| # | evidência | artefato |
|---|---|---|
| 1 | TCP CRU no provedor, com carimbo em cada `recv()` | `dsh-acp-wire-probe.py` |
| 2 | controle sem stream (mesmo prompt) | `dsh-acp-rota-alternativa.py`… ver §2 |
| 3 | bridge × cliente no mesmo turno e relógio | `dsh-acp-tres-pontas.py` |
| 4 | taxa de stall por tamanho de prompt | `dsh-acp-stall-por-tamanho.txt` |
| 5 | concorrência nossa (3 e 6 paralelos) | `dsh-acp-concorrencia-3x.txt` |
| 6 | rota alternativa (OpenRouter) | `dsh-acp-rota-alternativa.py` |

## 1. O stall existe no fio, sem passar pelo dsh (prova central)

`python3 evidence/dsh-acp-wire-probe.py curta 6 none`

```
rodada 1: total=29.17s · maior gap=26.65s · gap entre os recvs #1 e #2 — em t=2.07s já tinham chegado 1194 bytes
rodada 5: total=57.41s · maior gap=55.48s · ... em t=0.85s já tinham chegado 1200 bytes
rodada 6: total=38.03s · maior gap=35.95s · ... em t=1.57s já tinham chegado 1198 bytes
rodada 2/3/4: total 1.4–1.9s, gaps < 0.7s
```
Em `enorme` (~5000 chars) aparecem **4–5 gaps** de 26–55 s no mesmo turno — e
`tempo em gaps>1s: 44.0s de 175.5s`. O primeiro flush é sempre ~1,2 KB.

## 2. Controle: sem stream o tempo é o MESMO (não é buffering de flush)

Mesma pergunta, `"stream": false`, 4 rodadas: **36,05 s · 29,95 s · 1,53 s · 1,71 s**.
Se fosse proxy guardando bytes para "flushar no fim", o não-stream seria rápido
sempre. Os 30–36 s são do servidor. Resposta traz `cf-cache-status: DYNAMIC` e
`CF-RAY` → a rota está atrás do Cloudflare.

## 3. Três pontas no mesmo turno (nosso lado não segura nada)

`python3 evidence/dsh-acp-tres-pontas.py 8 persona` (trace do bridge × chegada no
`dsh_client._rota_update`, mesmo relógio de parede) — arquivo cru:
`dsh-acp-3pontas-persona.txt`:

```
rodada 2: total=43.02s · frames-texto=10 emits=10 commit=1 chegadas=11
  bridge:  1º frame t+1127 ms · maior gap interno=41.77s
  cliente: 1ª chegada t+1128 ms · maior gap=41.77s · atraso bridge→cliente=0 ms
rodada 6: total=22.68s · frames-texto=28 emits=28 commit=1 chegadas=29
  bridge:  1º frame t+715 ms · maior gap interno=21.60s
  cliente: 1ª chegada t+716 ms · maior gap=21.60s · atraso bridge→cliente=1 ms
```
Nas outras 6 rodadas (turnos bons): total 0,7–1,2 s e atraso bridge→cliente 0–1 ms.
O gap do bridge e o do cliente são o MESMO número (59.55 = 59.55) e o atraso
bridge→cliente é de 1 ms ⇒ o bridge **recebe em bloco** e repassa na hora. Nos turnos
bons: atraso 0–10 ms.

## 4. Descarte das suspeitas

- **flush só no fim do `notify`**: não — o `emit` acontece 1 ms após o frame (§3).
- **coalescência no harness**: não — o stall já aparece no fio, sem o dsh (§1).
- **buffering de stdout / leitura**: não — atraso bridge→cliente 0–10 ms (§3).
- **concatenação no patch**: não — 1 emit por `text-delta`, sem acumular.
- **MLX/carga local**: agrava, não causa — com 6 requisições paralelas nossas todas
  ficaram em 1,6–2,0 s, sem stall (`.txt` de concorrência).
- **tamanho do prompt**: correlaciona — prompt curto 2/6 turnos com stall, `media`
  1/6, `persona` (~1,3 k) 3/4 com stalls de 48–88 s (`dsh-acp-stall-por-tamanho.txt`).

## 5. Contornar possível (é decisão de produto, não do patch)

A rota alternativa não gira no meio, mas paga caro no 1º token
(`z-ai/glm-5.3-flash`: 1º em 5,6–12,6 s, gaps < 0,4 s; `meta/muse-spark`: 1º em
8,6–43,2 s). Ou seja: `dsflash` continua certo para o Live (1º em ~0,4 s); o que
falta é o provedor parar de preemptar no meio.

## 5b. Achado lateral (não é a causa): prewarm x slot de prompt

Com o provedor girando, o `prewarm` do `dsh_client` chega a **63,7 s** (boot + prompt
de prewarm). Quando ele passa do prazo, o prewarm é engolido e o 1º turno bate em
`-32602 a prompt is already in flight for this session` (o `_stream_com_lock` tem 1
retry de 0,25 s só). Arquivo do api-backend; o probe agora re-tenta.

## 6. Estado do patch

O patch ganhou um trace opcional, ligado por arquivo (`/tmp/dsh4a-trace.on` →
`/tmp/dsh4a-trace.jsonl`), sem mudar comportamento (sem o arquivo, custo zero).
sha256 do arquivo patchado: `dcfa3790c65b` (original) → **`64c0d1ccb615`** (patchado).
Marcador `DSH4A_PATCH_V1`, `partial: true|false` e `--revert` preservados. Achado e
corrigido no caminho: `--reapply` com o patch já aplicado deixava o arquivo LIMPO
(cache do conteúdo lido antes do revert) — regressão coberta por teste.