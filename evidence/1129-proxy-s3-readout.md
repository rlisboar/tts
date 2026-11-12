# #1129 — proxy sintético S3 (H1, curva do limiar por margem): READOUT

Pré-registro: `b945837` + leitura por faixa `9cc6bdb` + adendo `c26aae4` (n = 5 por condição,
P1 como proporção com IC95 de Wilson). Este readout não muda nenhum critério.

## Execução

- Mini, clone `13716e3` (o que está no ar), estado sintético, LLM falso (`tools/llm_fake_sse.py`),
  corpus e voz FLEURS em `~/tts-bench`, nada no `/tmp` de produção.
- `~/tts-bench/s3-res/s3.log`: largada 21:13:08, fim 22:52:46. As 4 condições com `rc=0`,
  `prod_tts_no_bloco=0` e `ci_amostras=0` (bloco limpo). Swap 2,3–3,5 GB.
- Brutos: `s3-eco-{30,15,10,6}-*.json` e `.log` no mesmo diretório.
- 1ª largada (20:34) inválida, sem `ffmpeg` no PATH; arquivada em `s3-res-invalida-2034`.

## P1 — detectadas em até 1,5 s do `b_ini`

"Detectada" = 1º evento de barge (`barge_suspeito`/duck no cliente) até 1,5 s do início da fala B.

| condição (margem) | detectadas ≤ 1,5 s | IC95 Wilson | duck por interrupção (ms) | duck p50 | stop p50 / máx | falso duck (fase A) | falso corte |
|---|---|---|---|---|---|---|---|
| eco −30 (~28 dB) | **3/6** | 18,8–81,2 % | 1054, 1154, 1354, 1854, 1954, 2655 | 1604 | 4910 / 6594 | 0/10 | 0/10 |
| eco −15 (~13 dB) | **2/5** | 11,8–76,9 % | 1154, 1254, 1854, 1954, 2354 | 1854 | 3374 / 6640 | 0/10 | 0/10 |
| eco −10 (~8 dB)  | **2/7** | 8,2–64,1 % | 1154, 1254, 1852, 2062, 2154, —, — | 1852 | 3493 / 6395 | 3/8 | 3/8 |
| eco −6 (~4 dB)   | **0/4** (não mensurável) | 0–49 % | —, —, —, 10762 | — | — | 4/6 | 0/6 |

- Nenhuma pausa (`barge_pausa`) em nenhuma condição: a pausa rápida segue desligada, como previsto.
- eco −10: 2 das 7 interrupções caíram sem playback tocando (`eco_tocado_s = 0`); nas 5 com
  playback, 2/5.
- eco −6: nas 4 interrupções, `eco_tocado_s = 0`. O agente já tinha cortado a própria resposta
  antes da fala B (ver achado abaixo), então não havia o que interromper. O bloco levou 38 min,
  contra ~15 min nos outros.

## Leitura pela tabela pré-registrada

A linha que vale é **"< 9/10 até em ~28 dB"**: **não é o limiar**. Com playback tocando, todas as
interrupções acabaram detectadas, mas **tarde**: duck p50 1,6–1,9 s e stop p50 3,4–4,9 s em todas
as margens, **sem gradiente com a margem**. A alavanca é a latência da cadeia de decisão
(`barge_decisao`, filtro `frase_longa`), coerente com a tabela de latência do card (decisão por
conteúdo ~1,4–2,5 s, mais ~1–1,5 s servidor→cliente ainda sem atribuição; o instrumento de ms
`f5a1333` existe para atribuí-la).

Ressalva do adendo: com n = 5–7 o teste tem pouco poder. 2–3/n em todas as faixas é um sinal
consistente, não uma prova isolada. O H2 (resíduo de eco × voz do dono) não sai deste proxy:
só a sessão anotada do dono decide.

## Achado fora do critério (registrado como dado): auto-corte por `palavras_corte`

`s3-eco-6-39834.log`, linhas 754–767 (21:43:00–04) e 1507–1519 (21:53:49–53):

1. o eco do TTS (`"…os detalhes com calma, sem pressa nenhuma."`, `tools/llm_fake_sse.py:24`)
   chega ao STT;
2. o 1º trecho sai `tipo=eco filtro=eco` (certo);
3. 4 s depois, o trecho seguinte é transcrito como `"Com calma."` e sai
   `tipo=interrupcao filtro=palavras_corte decisao_ms=0` → `cancel` + `interrupted` do turno do
   próprio agente.

Causa no código: `_live_avalia_filtros` checa `palavras_corte` (app.py ~13333) **antes** do filtro
`eco` (~13342), e `_COMANDO_PARAR` (`live_turns.py:104`) casa em qualquer posição. A lista tem
`calma`, mas também `para` e `nao`, comuns na fala do agente em pt. Qualquer resíduo de eco que
contenha essas palavras corta a resposta. No proxy o eco −6 é sem AEC; em produção depende do
resíduo do AEC do Chrome.

Não mexi no código. A ordem `palavras_corte` × `eco` é uma escolha de produto (o "para" do dono
dito sobre um trecho que também contém "para" seria lido como eco). Card próprio
(`task_3ecbe8f5`), a pré-registrar antes de qualquer mudança.

No mesmo trecho aparecem `cancel`/`interrupted` em dobro no mesmo turno: é a anomalia do #1136,
consertada em `wt/speech-pipeline-cancel` (gate pendente).
