# #876 — STT do Live: no mini já é o turbo greedy; o próximo ganho é o motor (Parakeet −30 %) e tirar o release do caminho crítico

Data: 2026-11-08. A bancada local roda no MacBook; o mini só foi lido (GET
`/api/settings` e trecho do `/tmp/tts-studio.log` desde o boot do e2bb4f7, copiado
para /tmp, 0600). Scripts: `evidence/876-stt-motor-parakeet.py`,
`evidence/876-stt-decompoe.py`, `evidence/876-refs-mls.py`.

## 1. O que o mini roda hoje

`GET /api/settings`: `stt_whisper_repo = whisper-large-v3-mlx`, `stt_beam = 8`,
`live_stt_whisper_repo = ""`, `stt_local_engine = whisper`, `remote_stt = false`.

- O Live usa o `LIVE_WHISPER_PADRAO` (turbo) quando `live_stt_whisper_repo` está
  vazio (#876 anterior, 978b50a).
- O `stt_beam = 8` só vale no STT remoto; o `mlx_whisper` local é sempre greedy
  (`evidence/876-stt-live.md`).
- Logo, a premissa "beam 8 + large-v3" do card **já não vale para o Live**: o large-v3
  é só o do STT de arquivo.

Estágio `stt` no build e2bb4f7 (`stage_fim … stage=stt ms=`, n=64):
**p50 1038 ms · p90 1724 · p95 1787 · máx 3002**. O piso fica em ~850 ms mesmo nos
turnos curtos, e o p95 de 6,3 s da rodada anterior não se repetiu.

O WER de 0,53 do resumo S1 só é medido nos ciclos **B** (interrupção por cima do eco,
cobertura mediana 0,61). Mede a CAPTURA do barge, não a precisão do modelo. Nas falas
A, a cobertura mediana é 0,85 e 17/20 são aceitas.

## 2. Onde vai o tempo (turbo, trecho de 3 s, local)

| parte | p50 |
|---|---|
| Silero (`_vad_tem_fala`) | 7,9 ms |
| leitura do wav | 0,2 ms |
| **decode** (`_transcreve_em_janelas`) | **190,6 ms** |
| `_release_mlx_memory()` dentro do `_transcribe` | **38,2 ms** |
| total | 237,2 ms |

O turbo preenche tudo até 30 s antes do encoder, então o custo quase não muda com a
duração (1 s: 219 ms, 6 s: 269 ms). O release é ~16 % do estágio e está no caminho
crítico do turno. No mini, onde tudo custa ~4×, isso dá **~150 ms**.

## 3. Turbo × Parakeet TDT 0.6B v3 (os dois já instalados no app)

Pelo caminho do app (`app._transcribe`, Silero incluso), modelos quentes. WER na
normalização do `live_bench`. Corpus: MLS Portuguese dev+test (20 locutores, 120
falas) + FLEURS pt_br local (40 frases de 3 vozes).

| motor | WER MLS (média / mediana) | WER FLEURS (média / mediana) | lat. 1 s | 3 s | 6 s | fala inteira (p50 / p95) |
|---|---|---|---|---|---|---|
| Whisper large-v3-turbo (hoje) | 6,45 % / 6,0 % | 4,4 % / 0,0 % | 219 ms | 238 | 269 | 372 / 479 |
| **Parakeet TDT 0.6B v3** | 7,52 % / 6,38 % | 5,27 % / 3,08 % | **145 ms** | **162** | **193** | **279 / 355** |

O Parakeet fica **~75 ms (−30 %) mais rápido** por turno curto, com **~1 pp de WER a
mais**. No mini, a proporção dá algo como 1,04 s → ~0,7 s, o que precisa ser medido lá.

## 4. Proposta (configuração candidata para a janela do mini)

| | C0 (hoje) | **C1 (candidata)** |
|---|---|---|
| motor do turno do Live | turbo greedy | **Parakeet v3** (chave nova `live_stt_engine`, default `whisper` = nada muda até ligar) |
| passada de comando do barge (`_live_transcreve_trecho(cru=True)`) | turbo | o MESMO motor do turno (senão ficam os dois modelos na RAM de 16 GB) |
| `_release_mlx_memory` do STT do Live | dentro do estágio | **depois** do `transcript_user` (fora do caminho crítico) |
| STT de arquivo | large-v3 (`stt_whisper_repo`) | inalterado |

Ganho esperado no estágio STT do mini: −250 a −400 ms no p50 (motor + release).

**Riscos a medir na janela:**
- O Parakeet não traz `no_speech_prob`/`avg_logprob`, então o juiz anti-alucinação
  (`_stt_ok`) fica só com VAD, tamanho e blacklist. Medir transcrição falsa com eco
  puro e com ruído puro.
- O Parakeet não aceita idioma fixo: frase curta pode sair em outra língua.
- RAM: o turbo deixa de ficar residente; conferir o RSS do processo no fim.

## 5. Medição pedida no mini (bancada do #875)

20 ciclos em C0 e 20 em C1, mesmo build, mesma semente. Para cada uma:
- S1 (aceitas, cobertura A/B), WER **das falas A**;
- estágio `stt` p50/p95 do log e S2 decomposto (`llm_ttft` × `pos_llm`);
- RSS no fim;
- 5 min de eco puro + 5 min de ventoinha sem fala: transcrições falsas.

Aceite de C1: p50 do estágio STT ≤ 0,75 s, WER das falas A sem piorar mais que 2 pp,
nenhuma transcrição falsa a mais que C0, RSS ≤ C0.

## Limites

- A latência local não é a do mini; o que se transfere é a proporção.
- Sem TTS em paralelo nesta bancada (a disputa de GPU está no `876-stt-live.md`).
- MLS é audiolivro; a fala do Live é conversa.

## 6. C1 implementada (atrás das chaves) — modelo REAL pelo caminho do Live

`evidence/876-c1-modelo-real.py`: `live_pipeline._stt_app` (o turno de produção,
com o juiz) e `_live_transcreve_trecho(cru=True, prazo_s=5)` (a passada de comando),
40 falas do MLS PT, modelos quentes, local.

| | turno p50 | turno p95 | WER médio | passada de comando p50 |
|---|---|---|---|---|
| C0 (`live_stt_engine=""`, release dentro) | 380 ms | 457 ms | 5,77 % | 182 ms |
| C1 (`parakeet` + `live_stt_release_adiado`) | **232 ms** | **294 ms** | 8,09 % | **103 ms** |

O turno cai **−39 %** e a passada de comando **−43 %**. O WER subiu **+2,3 pp** nestas
40 falas: fica na beira do aceite (≤ C0 + 2 pp) e quem decide é a medição das falas
A no mini, com as mesmas falas nas duas configurações.
