# #876 — STT do Live: modelo e beam (bancada local, antes do Mini)

Corpus: FLEURS pt_br test, os 20 primeiros dos 40 locutores (fala real, CC BY 4.0). Caminho do app
(`_transcreve_em_janelas`, greedy). Máquina: Mac17,8 48 GB (NÃO é o Mini M3 16 GB).
Carga = `tts_worker.py` OmniVoice real gerando em laço (24 gerações durante a rodada).
Script: `evidence/876-stt-bench.py`; números crus em `876-stt-bench-{livre,carga}.json`.

## `stt_beam` não age no STT local

`stt_beam` só é enviado ao STT REMOTO. O `mlx_whisper` local levanta
`NotImplementedError("Beam search decoder is not yet implemented")` para qualquer
`beam_size`; o app nem o passa. No Mini (remoto desligado) o `stt_beam=8` não tem
efeito: o custo é do MODELO (large-v3), não do beam.

## Números

| modelo | WER médio | WER mediano | 1 s p50/p90 | 3 s p50/p90 | 6 s p50/p90 |
| --- | --- | --- | --- | --- | --- |
| large-v3, livre | 4,4 % | 0,0 % | 258/322 ms | 358/428 | 482/545 |
| turbo, livre | 5,1 % | 2,2 % | 178/195 | 197/203 | 228/247 |
| large-v3, com TTS | 4,4 % | 0,0 % | 284/459 | 395/520 | 539/756 |
| turbo, com TTS | 5,1 % | 2,2 % | 193/252 | 214/279 | 254/313 |

Turbo: 1,5–2,4× mais rápido (p90 sob carga: 2,4× em 6 s), +0,7 pp de WER médio
(n=20, dentro do ruído de uma amostra desse tamanho). No Mini o log mostra o
estágio `stt` de ~1,1–1,2 s em turnos de 1–2 s com large-v3 — ~4× esta máquina;
a proporção entre modelos precisa ser confirmada lá.
