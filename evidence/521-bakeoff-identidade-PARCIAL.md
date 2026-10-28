# Bake-off de identidade de voz — PARCIAL (#521)

Estado: **em andamento**. Registro do que já está medido e do que ainda falta.

## Bug de corpus encontrado e corrigido (muda os números)

As falas do corpus têm ~3 s, e o protocolo pedia recortes de 1/2/3/**8** s: o
recorte menor que o pedido era completado com ZEROS, e o "8 s" virava 3 s de fala
com 5 s de silêncio. Efeito medido: o WeSpeaker caía de 14 % de EER em 1 s para
**47 % em 8 s**. Corrigido repetindo a fala (`_recorta`), e a partir daí os dois
modelos voltam a melhorar com a duração — que é o esperado.

Antes de achar isso testei e descartei como causa: CMVN por utterance, `dither`
0/1, `snip_edges` true/false, janela deslizante de 100–400 frames, poda de
silêncio e normalização de volume. Também comparei o frontend de fbank com o do
`torchaudio.compliance.kaldi` (14,0 % x 14,6 %): o frontend não era o problema.

## Números correntes (LibriSpeech dev-clean, 20 locutores, CPU do M3)

Cadastro de 5 amostras, centróide, escore cosseno, recortes de 1–8 s:

| modelo | EER 1 s | EER 2 s | EER 3 s | EER 8 s | FAR de clone @FAR1% |
|---|---|---|---|---|---|
| Resemblyzer (hoje no app) | 3,5 % | 1,8 % | **1,6 %** | 1,8 % | **0 %** |
| WeSpeaker ResNet34 | 17,5 % | 10,9 % | 10,7 % | 12,3 % | **0 %** |

Com cadastro de 1 amostra (o caso realista de quem acabou de gravar): Resemblyzer
8,0 % e WeSpeaker 24,1 % de EER em 1 s, FRR@1% de 32,1 % e 87,6 %.

## O número que mais importa: clone do próprio app

24 clones de 8 locutores, gerados pelo app com a voz de referência do próprio
locutor, comparados contra o cadastro dele no limiar calibrado para FAR 1 %:

- **Resemblyzer: 0 % de aceitação** (escore médio do clone 0,578 contra 0,542 de
  impostor — o clone fica acima da média, mas abaixo do limiar 0,664)
- **WeSpeaker: 0 %** (clone 0,346 contra impostor 0,383 — o clone fica ABAIXO da
  média dos impostores)

Leitura: hoje um clone do próprio app **não passa** como o dono no limiar de FAR
1 %. É bom para anti-fraude e ruim para quem espera que a voz clonada sirva de
credencial — a decisão é de produto e depende do R3 (#76d13f93).

## Ressalvas

- Ruído é sintético (ventoinha gerada em `tools/bakeoff/ruido.py`), não a do dono.
- 20 locutores: EER ruidoso; a versão final usa o conjunto maior.
- LibriSpeech é leitura de audiolivro; fala espontânea é mais difícil.
- Faltam CAM++ e ERes2NetV2 (só existe checkpoint torch no HF, exige `funasr`) e
  ECAPA (SpeechBrain); sem eles o veredito de "melhor modelo" ainda não fecha.
- AS-norm e cadastro por máximo ainda não entraram na tabela.

## Reprodução

```bash
./.venv-mlx/bin/python tools/bakeoff/corpus.py publico && ./.venv-mlx/bin/python tools/bakeoff/corpus.py monta
./.venv-mlx/bin/python tools/bakeoff/clone.py 8
/tmp/tts-bakeoff/venv/bin/python tools/bakeoff/identidade.py --modelos resemblyzer,wespeaker34 --k 1,5 --locutores 20 --clones
```
