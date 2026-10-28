# Bake-off de identidade de voz (#521) — veredito

Corpus público em `/tmp/tts-bakeoff` (LibriSpeech dev-clean, CC BY 4.0; manifesto
sha256 em `resultados/corpus.json`), 40 locutores, recortes de 1–8 s, CPU do M3.
Nada de `voices/`, `outputs/` ou `.speaker-profiles.json`.

## Placar (20 locutores, cadastro de 5, centróide, escore cosseno)

| modelo | EER 1 s | EER 3 s | EER 3 s com AS-norm | FRR@FAR1% (3 s, AS-norm) | clone aceito @FAR1% |
|---|---|---|---|---|---|
| Resemblyzer (hoje no app) | 1,9 % | 1,7 % | 1,4 % | 14,0 % | 0 de 24 |
| SpeechBrain ECAPA | 5,3 % | 1,6 % | **0,19 %** | **2,5 %** | 0 de 24 |
| 3D-Speaker ERes2NetV2 | 3,1 % | 1,8 % | 3,5 % | 7,0 % | 0 de 24 |
| 3D-Speaker CAM++ | 8,8 % | 5,3 % | n/m | n/m | 0 de 24 |
| WeSpeaker ResNet34 | 17,5 % | 8,8 % | n/m | n/m | 0 de 24 |

Cadastro de 1 amostra (quem acabou de gravar): 9,5 % a 22,1 % de EER em 1 s nos
modelos com AS-norm medido — 5× pior que com 5 amostras.

## Veredito

**ECAPA-TDNN com AS-norm é o vencedor** — e não por pouco: 0,19 % de EER em 3 s
contra 1,4 % do Resemblyzer. Ele é 3× pior no trecho de 1 s sem normalização, e é
exatamente o AS-norm que o conserta (5,3 % → 3,5 % em 1 s; 1,6 % → 0,19 % em 3 s).

**O AS-norm é específico do modelo, não um enfeite:** aplicado ao Resemblyzer ele
PIORA o trecho de 1 s (1,9 % → 5,0 %). Quem trocar de modelo tem de refazer a
coorte; não dá para manter a normalização "porque é boa prática".

**Nenhum modelo deixa passar o clone do próprio app** no limiar de FAR 1 % (0 de
24 em todos). Para credencial biométrica isso é bom; para "minha voz me representa"
é uma decisão de produto e o R3 (#522) é quem a informa.

## Limiar calibrado (não número cru)

Os limiares saem do protocolo, por FAR alvo, e estão nos JSON
(`resultados/identidade_*_asnorm.json`, campo `clone.cadastro_3s.limiar_*`):

| modelo (3 s, AS-norm) | limiar p/ FAR 1 % | limiar p/ FAR 0,1 % |
|---|---|---|
| ECAPA | 2,434 | 3,044 |
| ERes2NetV2 | 3,469 | 3,915 |
| Resemblyzer | 2,906 | 3,173 |

São escores normalizados: **não reaproveitar** os 0,6/0,75 de hoje (`app` e UI
divergem nisso — ver bug citado no card).

## Regra de qualidade do cadastro (derivada dos números)

1. **Mínimo de 3 amostras, alvo 5.** Com 1 amostra o EER multiplica por ~5 em
   todos os modelos; centróide só faz sentido com 3+.
2. **Centróide vence medoide** em todos (ECAPA 3 s: 0,19 % x 1,34 %).
3. **Duração mínima de 3 s por amostra** — 1 s é o caso do barge, não do cadastro.
4. **Consistência entre amostras**: descartar a amostra cujo escore médio contra
   as outras fica abaixo do limiar de FAR 1 % (é voz trocada/noisy).
5. **Ruído**: ventoinha a 5 dB degrada todos; o cadastro deve ser feito em
   ambiente quieto e o gate de qualidade roda antes de gravar.

## Política de recadastro

Embeddings **não são compatíveis entre modelos** — trocar o modelo invalida todos
os perfis salvos (`.speaker-profiles.json`). A troca exige: reextrair de todas as
amostras guardadas (as vozes continuam válidas, os vetores não), recalibrar o
limiar com a coorte nova e só então apagar os vetores antigos. Como o app já
guarda os `.wav` de referência, o recadastro é offline e não pede nova gravação —
mas é irreversível e tem de ser anunciado.

## Licenças (conferir antes de embarcar)

ECAPA: Apache-2.0 (código SpeechBrain; pesos CC BY 4.0 do VoxCeleb) ·
ERes2NetV2/CAM++: Apache-2.0 · Resemblyzer: MIT · WeSpeaker: Apache-2.0 (pesos
CC BY 4.0). Corpus LibriSpeech: CC BY 4.0. **Nenhum pino novo** — a bancada usa
venv próprio (`/tmp/tts-bakeoff/venv312`).

## Ressalvas

- Ruído sintético; LibriSpeech é leitura de audiolivro, não fala espontânea.
- 20 locutores: diferenças abaixo de ~1 ponto percentual precisam de corpus maior.
- CAM++ e WeSpeaker ficaram sem AS-norm medido (o teste de 1 s já os descartava).
- Coorte de 40 falas é pequena para AS-norm em produção; o alvo é ~200.

## Reprodução

```bash
./.venv-mlx/bin/python tools/bakeoff/corpus.py publico && ./.venv-mlx/bin/python tools/bakeoff/corpus.py monta
./.venv-mlx/bin/python tools/bakeoff/clone.py 8
/tmp/tts-bakeoff/venv312/bin/python tools/bakeoff/identidade.py --modelos ecapa,eres2netv2,resemblyzer --k 5 --asnorm --clones
```
