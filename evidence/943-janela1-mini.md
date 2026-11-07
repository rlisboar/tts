#943 / #901 — Janela 1 na porta 7890 (2026-11-07): estágio stt e matriz de passos

Instância sintética em `ws://127.0.0.1:7890/api/live/ws`, seed 875. Os números
abaixo saem de `/tmp/bench-mini-evidencia.tgz` (JSON da bancada e `app8.log`),
relidos nesta sessão. O instrumento que fecha o estágio é `tools/stage_stt.py`.

Não é a rodada do #942 (`evidence/942-bancada-mini-c3f4a78.md`, 16:16–16:50,
`pos_llm` p50 3473 ms). Os relógios se encostam; as fontes são outras.

## #943 — estágio `stt` em `app8.log` (16:35:44–16:41:42)

`tools/stage_stt.py app8.log --desde 16:00:00 --ate 16:50:00 --minimo-ms 0`:

- 16 intervalos, todos sem `turno_pendente`
- p50 984 ms, p95 1612 ms, máximo 1733 ms
- fechamento: `tts` 8, `stt` 5, `interrupted` 3
- os três `interrupted`: 111 ms (16:37:07), 120 ms (16:36:08) e 240 ms (16:37:38)

O laço para no próximo `stage_inicio` ou `stage_fim` com `stage=stt` da sessão
e não herda o `tts` desse stt. Os cinco `stt` (115 ms às 16:36:11, 239 ms às
16:36:22, 120 ms às 16:37:09, 209 ms às 16:37:40 e 1252 ms às 16:37:44)
voltaram a idle; o `tts` que o instrumento antigo colava neles é o do
intervalo seguinte. O `tts` que permanece é o `stage_inicio stage=tts`
deste intervalo, depois do `stage_inicio stage=llm` — não é o primeiro evento
depois do `stage_fim`, nem um `stt` deixado aberto por cima de áudio antigo.
O log de antes do conserto não está neste arquivo, então a comparação com a
cauda antiga não entra aqui.

## #901 — passos no 1º chunk (bancada, um ciclo cada, n pequeno)

| rótulo | quando | S2 p50 | llm_ttft p50 | pos_llm p50 |
|---|---|---|---|---|
| s12 | 16:02:59 | 4156 ms (n=1) | 1763 ms | 2393 ms |
| s8 | 16:03:45 | 3634 ms (n=1) | — | — |
| s4 | 16:07:17 | 3177 ms (n=1) | 1712 ms | 1465 ms |
| s12 sem cláusula | 16:10:52 | 4288 ms (n=1) | 1876 ms | 2412 ms |
| s12 quente (2 ciclos) | 16:12:42 | 3006 ms (n=4) | 1893 ms | 1035 ms |

O `llm_ttft` fica em 1,7–1,9 s quando a bancada o registra. O `pos_llm` do s12
cai de 2393 ms no primeiro ciclo para 1035 ms já quente. O s12 sem cláusula
rodou depois e ficou 132 ms acima do s12 no S2 total (4288 contra 4156); não é
par A/B, é um ciclo de cada em momentos diferentes.

A guarda de timbre ECAPA não está neste arquivo. O tar guarda quatro WAV de
2,04 s a 24 kHz (`b16`, `b16s8`, `b16s4`, `f32`) e nenhum cosseno. A decisão
de manter bf16 e 12 passos no primeiro chunk fica a que o PM já registrou; este
resumo não a repete como número medido.
