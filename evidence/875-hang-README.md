# #875 — hang do turno no Live (rodada final INCOMPLETA, build `afc3f42`)

Data: 2026-11-08, janela liberada pelo dono. Build `afc3f42` no mini (`codigo=fb93039b` conferido contra os módulos do rev), sem mic físico (cliente WS com eco reinjetado).

## O que aconteceu

A rodada de 20 ciclos **não fechou em tempo útil porque o turno trava**: 6 falas ficaram com `s2_ms: null` e duas sessões ficaram paradas 16+ min cada, com `estado=ocioso` e os frames seguindo. O sintoma é sempre o mesmo:

1. o turno abre, STT responde, LLM responde, o TTS sintetiza o primeiro chunk e o estágio volta a `idle`;
2. **nenhum `turn_complete` e nenhum `interrupted`** — a sessão fica ociosa segurando o turno;
3. a bancada paga `--teto-turno-s` (usei 90 s) e mais `teto * 3` na segunda espera.

Sessões afetadas: **`b50ec5a60b`** (17:33, primeira tentativa, 16+ min parada) e **`bffb1c9d09`** (17:50, segunda, idem).

## Suspeita (para quem for consertar)

O backend do dono é `openai` (sem processo dsh vivo). `_marcar_render` e `_dsh_pos_turno` são não-bloqueantes, então o ponto provável é o **stream do LLM não encerrar** — o `for delta in self._llm(msgs)` fica pendurado, o chunker nunca faz `flush()` e o `tts_thread.join(180)` só devolve no teto. O `interrupted` do #942 não cobre porque não há cancelamento: ninguém emite `turn_complete` nem `interrupted`.

## Reprodução

No mini, com o app de produção (`afc3f42`) e o corpus em `/tmp/live-bench-audio`:

```
./.venv-mlx/bin/python -u tools/live_bench.py \
  --url ws://127.0.0.1:7860/api/live/ws --audio /tmp/live-bench-audio \
  --ciclos 20 --seed 875 --teto-turno-s 90 --rotulo afc3f42 -v
```

O travamento aparece em 1–3 ciclos (nas duas tentativas foi no 1º e no 3º). Sem `--teto-turno-s` curto cada ocorrência custa 300 s + 900 s.

## Artefatos

- `875-bancada-afc3f42-PARCIAL.json` / `.log` — a rodada parcial (20 ciclos, 26/40 falas aceitas; 6 sem áudio).
- `875-hang-<sessão>-relogio.jsonl` — a linha do tempo do dump da sessão (`~/tts-live-dump/eco-*` no mini; `mic.wav`/`ref.f32` ficaram lá, ~30 MB cada).
- `875-hang-log.txt` — o log de produção filtrado das duas sessões (a sequência `stage_inicio stt → llm → tts → stage_fim tts` e o silêncio depois).

## Números parciais (não substituem a rodada final)

S4 e S5 passam; S1 65 %, S2 p50 4 334 / p95 6 295, S3 duck 755 / stop 4 558, eco falso corte 0 %, eco falso duck 41 %. **A rodada de 20 ciclos refaz depois do conserto.**
