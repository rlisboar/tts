# Bake-off de identidade de voz (#521) — veredito

Corpus público em `/tmp/tts-bakeoff` (LibriSpeech dev-clean, CC BY 4.0; manifesto
sha256 em `resultados/corpus.json`), 40 locutores, recortes de 1–8 s, CPU do M3.
Nada de `voices/`, `outputs/` ou `.speaker-profiles.json`.

## Placar (40 locutores, cadastro de 5, centróide, AS-norm, IC 95 % por bootstrap)

| modelo | EER 1 s (IC 95 %) | EER 3 s (IC 95 %) | clone aceito @FAR1% |
|---|---|---|---|
| SpeechBrain ECAPA | **2,55 %** (1,6–5,2) | **1,86 %** (0,13–4,3) | 0 de 24 |
| 3D-Speaker ERes2NetV2 | a medir | a medir | a medir |
| Resemblyzer (hoje no app) | 4,39 % | a medir | a medir |

**O número de 20 locutores enganava:** com 20 o Resemblyzer marcava 1,9 % e o
ECAPA 5,3 %; com 40 o ECAPA vai a 2,55 % e o Resemblyzer a 4,39 %. Os intervalos
são largos dos dois lados — diferença de 1–2 pontos com este corpus não decide
nada sozinha, que é exatamente o que o card alertou.

Carga da máquina na rodada: loadavg **6,2** (outros agentes na mesma máquina). A
latência do R2 foi medida nessa condição; a latência de produção precisa de
máquina folgada (≤0,5 por núcleo), que ainda não foi registrada.

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

## Tentativa do pipeline oficial do WeSpeaker — não confirmada

Uma rodada, como o PM pediu. O pacote não está no PyPI (só no git) e falha no
import nas duas versões do ambiente: `torchaudio.set_audio_backend` e
`torchaudio.sox_effects` sumiram na 2.9+, e são usados pelo `wespeaker` e pelo
`s3prl` que ele arrasta. Instalar uma 2.4 exigiria um quarto interpretador.

Consequência para a leitura: o número do WeSpeaker é **"não confirmado por falta
de ambiente"**, não "modelo ruim". As duas causas candidatas continuam abertas
(exportação ONNX ou normalização de escore), e o número dele não é usado para
rejeitar o modelo no veredito.

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


---

# Fechamento do R2 — as quatro condições do PM

## (a) Sanidade de integração: ECAPA reproduz o cartão oficial

`tools/bakeoff/sanidade.py`. Pares do cartão oficial do SpeechBrain, com a
expectativa publicada junto:

| par | esperado | medido |
|---|---|---|
| `spk1_snt1` × `spk1_snt2` (mesmo locutor) | predição 1 | **0,7295** |
| `spk1_snt1` × `spk2_snt1` (diferentes) | predição 0 | **0,0762** |
| controle no corpus (1272 × 8842) | baixo | 0,0053 |
| o MESMO par positivo pelo `verify_files` oficial | — | **0,7295** |

O nosso caminho de codificação dá o número idêntico ao oficial, então a
integração é equivalente — é essa igualdade que pega regressão, não a coincidência
de limiar. **Armadilha registrada:** `example1.wav`/`example2.flac` do repositório
NÃO são o par do cartão, são falantes diferentes (0,18, predição 0); quem usa esse
par como positivo conclui que a integração está quebrada.

## (b) Cenário em pt-BR, com e sem ruído do R1

`tools/bakeoff/pt_ruido.py`, 8 vozes pt-BR do macOS, cadastro com 2 frases e teste
com a 3ª, ventoinha do R1 em SNR 10 e 5 dB aplicada em cadastro e teste:

| condição | EER (IC 95 %) | FAR @0,44 | FRR @0,44 |
|---|---|---|---|
| limpo | 0,9 % (0,0–2,7) | 23 % | 0 % |
| SNR 10 dB | 0,0 % | 16 % | 0 % |
| SNR 5 dB | 0,0 % | 20 % | 0 % |

**O ruído quase não dói** (0 % de EER em SNR 5 dB) e o EER em pt é tão bom quanto
em inglês — o extrator atravessa idioma.

**Mas o limiar cru NÃO atravessa corpus.** O mesmo 0,44 que zera a FAR em inglês
deixa passar 23 % em pt; em pt a FAR só cai a 5–7 % em 0,68–0,74, com FRR ainda em
0 %. Duas leituras: (1) é preciso recalibrar por corpus; (2) as 8 vozes sintéticas
compartilham o mesmo sintetizador, então a FAR em pt é pessimista e não é
estimativa de população. O caminho que dispensa recalibrar por corpus é o AS-norm —
mais um argumento para ele, e para a coorte maior.

## (c) Tentativas publicadas

Cada linha da tabela do placar tem **40 positivos e 780 negativos** (40 locutores,
todos os pares cruzados), com IC 95 % por bootstrap no EER. A rodada de clones tem
24 tentativas por condição. O IC do ECAPA em 1 s (1,6–5,2) continua largo: é o que
40 locutores permitem, e é por isso que a decisão se apoia na comparação com o
Resemblyzer no mesmo ensaio, não no valor absoluto.

## (d) Coorte do AS-norm é pública

`cohorte_resemblyzer.json`: `fonte = "LibriSpeech dev-clean (CC BY 4.0), 40 falas"`,
37 vetores, nenhum dado do dono. No ensaio do ECAPA a coorte é montada em tempo de
execução com locutores FORA do conjunto de teste, do mesmo corpus público.

## Vozes parecidas — o caso que o R4 deixou aberto

`tools/bakeoff/parecidos.py`: centróide de 5 amostras por locutor, cosseno entre
todos os 780 pares de locutores distintos, e os 10 pares **mais parecidos** usados
como impostores. Teste sempre com amostra que não entrou no centróide.

| métrica nos 10 pares mais parecidos | valor |
|---|---|
| FAR no limiar 0,35 (o que o R4 recomendou) | **60 %** |
| FAR no limiar 0,40 | 10 % |
| FAR no limiar **0,44** | **0 %** |
| FRR do dono no 0,44 | 12 % (3 s de teste) |
| FAR nos pares distintos (controle) | 0 % |
| **erro do seletor do R4** (preferir o parecido ao alvo) | **0 %** |

O 0,35 do R4 estava calibrado só em vozes distintas — com parecidos ele deixa
passar 60 %. **O ponto honesto é 0,44**, ao custo de 12 % de FRR (fala legítima
barrada, sempre recuperável repetindo). A curva completa está em
`resultados/parecidos.json`.

**O seletor de fluxo é bem mais fácil que o portão:** nos 10 pares mais parecidos
ele errou **0 %** — o escore do próprio locutor (0,47–0,78) sempre bateu o do
parecido (0,24–0,43). Escolher entre dois candidatos é mais simples que decidir
"é ou não é ele", e isso vale para o R4.

## Correção que sai daqui

O R4 (#532) recomendou 0,35 para o portão do Live. **Passa a 0,44**, e o teto de
FAR de 1 % do card fica cumprido mesmo no pior caso medido. O limiar continua
tendo de ser recalibrado por corpus até o AS-norm entrar em produção.
