# Locutor-alvo (#532) — bake-off: seleção por janela × separação

Pergunta do dono: "me entender corretamente mesmo que outras pessoas falando e
outros barulhos acontecendo". Denoise (R1) não resolve — fala de terceiro não é
ruído para um denoiser — e identidade (R2) só diz "é ou não é ele". Aqui se mede a
camada que falta.

**Resumo:** o problema tem dois regimes e eles pedem camadas opostas. Com
sobreposição **total**, a separação (MossFormer2_SS_16K + escolha do fluxo pelo
ECAPA) devolve o WER ao nível limpo em todos os SIR testados. Com sobreposição
**parcial**, a seleção por janela faz o mesmo e a separação não ajuda. O portão de
turno do Live (VAD de energia + ECAPA) não abriu **nenhum** turno falso em 10 min
só com terceiros.

## Corpus, cadastro e âncoras

LibriSpeech dev-clean (fala pública), falantes distintos, 12 s de cada lado,
alvo e concorrente de falantes diferentes; SIR 0/5/10 dB. Cadastro do dono = 20 s.
Os números que importam são relativos à **linha de base limpa** (o alvo sozinho,
mesmo encadeamento):

| falante | WER limpo | vazamento limpo |
|---|---|---|
| 1272 | 0,49 | 2,1 % |
| 1462 | 0,41 | 8,2 % |

O WER limpo de ~0,4–0,5 é alto para LibriSpeech: a emenda de clipes em `junta()`
corta palavra na borda e o texto de referência é a concatenação crua. Como a
mesma emenda vale para todas as condições, a comparação se sustenta — mas a meta
"≤ limpo + 5 p.p." é um portão grosso, não uma promessa de qualidade absoluta.

## Resultado — sobreposição TOTAL

| alvo | SIR | mistura | seleção | **separação** | sep+veto | SI-SDRi (sep) |
|---|---|---|---|---|---|---|
| 1272 | 0 | 0,93 | 0,91 | **0,51** | 0,51 | +7,8 dB |
| 1272 | 5 | 0,57 | 0,86 | **0,49** | 0,49 | +14,7 dB |
| 1272 | 10 | 0,57 | 0,60 | **0,49** | 0,49 | +12,5 dB |
| 1462 | 0 | 0,97 | 0,87 | **0,43** | 0,46 | +21,0 dB |
| 1462 | 5 | 0,48 | 0,59 | **0,43** | 0,46 | +17,5 dB |
| 1462 | 10 | 0,43 | 0,57 | **0,43** | 0,43 | +14,2 dB |

Contra a linha de base limpa (0,49 e 0,41): a separação fica **dentro da meta de
+5 p.p. em todas as células** (pior caso 1462 a SIR 0: 0,43 contra 0,41).

Seleção por janela sozinha **piora** a sobreposição total em 4 de 6 células: a
janela tem as duas vozes, então apagar a janela apaga fala do alvo junto. No
melhor caso (SIR 0) ela só empata com a mistura.

Vazamento de terceiros (fração de palavra alheia na transcrição), contra a meta
de **≤ 2 %** do card:

| camada | faixa medida | piso limpo do falante | meta ≤ 2 % |
|---|---|---|---|
| mistura | 6,4 %–32 % | 2,1 % e 8,2 % | **não cumpre** |
| seleção | 0 %–6,4 % | idem | **não cumpre em parte das células** |
| separação | 2,1 %–9,0 % | idem | **NÃO cumpre em nenhuma célula** |

**A meta de ≤ 2 % não é atingida pela separação em célula alguma** — o melhor caso
(2,1 %) empata com o piso do falante e fica acima da meta; no 1462 ela nunca desce
ao piso (8,2 %), que ali já é 4× a meta. A seleção desce a 0 % em várias células,
mas passa de 2 % em parte delas. O piso limpo alto do corpus (2,1 % e 8,2 %) é
parte da explicação: com ele, "≤ 2 %" é uma meta que o corpus quase não permite
medir — não é só o método que falha.

## Resultado — sobreposição PARCIAL (blocos alternando)

| alvo | SIR | mistura | **seleção** | separação | sep+veto | SI-SDRi (sep) |
|---|---|---|---|---|---|---|
| 1272 | 0 | 0,87 | **0,49** | 0,87 | 0,50 | +1,0 dB |
| 1272 | 5 | 0,84 | **0,49** | 0,87 | 0,50 | +0,6 dB |
| 1272 | 10 | 0,87 | **0,49** | 0,84 | 0,50 | +0,5 dB |
| 1462 | 0 | 0,82 | **0,41** | 0,70 | 0,69 | −11,9 dB |
| 1462 | 5 | 0,82 | **0,41** | 0,62 | 0,67 | −13,2 dB |
| 1462 | 10 | 0,72 | **0,41** | 0,66 | 0,69 | −13,7 dB |

A seleção devolve o WER **exatamente ao valor limpo** (0,49 e 0,41) e derruba o
vazamento ao piso (2,1 % e 8,2 %). A separação fica **pior que a mistura** em
1462: separar um bloco que já é de um falante só atrapalha, e o SI-SDRi negativo
mostra isso. O SI-SDR também não é a métrica certa aqui — metade do áudio não tem
alvo dentro da mistura, então "errar" nos blocos do concorrente não aparece.

`sep+veto` empata com a seleção em 1272 e perde em 1462 (0,67–0,69).

## Portão de turno do Live (VAD pessoal) — 10 min

VAD de energia (piso 0,15 do percentil 90) + ECAPA em janela deslizante de 1 s a
cada 0,25 s, exigindo 2 janelas seguidas. Escore mediano: dono **0,613**, terceiro
**0,037**, terceiro com ruído **0,026**.

| limiar | turnos falsos/10 min | com ruído | cobertura do dono |
|---|---|---|---|
| 0,20 | 0,0 | 1,0 | 100 % |
| 0,25–0,45 | 0,0 | 0,0 | 100 % → 93 % |
| 0,50 | 0,0 | 0,0 | 85 % |
| 0,55 | 0,0 | 0,0 | 74 % |
| 0,60 | 0,0 | 0,0 | 56 % |

Meta do card (≤1 falso/10 min): **cumprida em toda a faixa**. O joelho fica em
~0,35–0,40 (99 % → 96 % de cobertura), com 0 falso. **O limiar adotado é 0,44**,
mais alto que o joelho, porque esta curva é de vozes distintas — com parecidos o
0,35 passa terceiro (ver `evidence/521`, seção de vozes parecidas).

Curva por duração de cadastro, limiar 0,35: 10 s → 97 % de cobertura e 0 falso;
30 s e 60 s → 99 % e 0 falso. Ou seja, 10 s de cadastro já bastam **neste
corpus**; o cadastro não é o gargalo.

## Latência e memória

| item | CPU | MPS (Metal) |
|---|---|---|
| RTF da separação | 6,09 | **0,31** (0,25 na `.venv-mlx`) |
| bloco de 1 s | 8.807 ms | **470 ms** |
| bloco de 0,6 s | — | 221 ms |
| carga do modelo | 1,8 s | 1,8 s |
| RSS pico | 1.160 MB | 1.160 MB |
| portão ECAPA, 1 janela (modelo já carregado) | 17 ms | — |

Em CPU o MossFormer2 é **inutilizável no Live** (RTF 6–9: produz 6–9 s de áudio por
segundo). Em MPS cai para RTF 0,31 — cabe em fluxo contínuo — e a qualidade é a
mesma (mesmos WER). Ainda assim a latência **adicionada** é de ~0,8–1,5 s (janela
+ computação), muito acima dos ≤150 ms do card: a separação não pode ficar na
borda do microfone. O desenho que os números sustentam é o portão decidir com o
ECAPA (17 ms) e a separação correr **atrás** do turno já aberto, alimentando o STT
com ~1 s de atraso.

Memória: 1,16 GB só do separador, somados ao resto do app.

## Recomendação

1. **Live (turno):** portão ECAPA-only, limiar **0,44** — era 0,35, corrigido no
   fechamento do R2 (`evidence/521`): com vozes parecidas o 0,35 deixava passar
   60 % e o 0,44 zera isso, ao custo de ~6 % de cobertura (94 % na curva acima).
   2 confirmações e piso de energia. Custo 17 ms, 0 falso em 10 min com vozes
   distintas. Não colocar separação na decisão do turno.
2. **Live (áudio que segue para o STT):** separação em MPS, escolha do fluxo pelo
   ECAPA, correndo atrás do turno com ~1 s de atraso, **com veto por `corte-abs`**.
3. **Transcrição offline (STT/Tradutor/Conversa):** separação em MPS com escolha
   pelo ECAPA, **também com veto por `corte-abs`**.
   **Correção do que a primeira versão dizia:** "separar sempre" está errado. Na
   sobreposição PARCIAL a separação é PIOR que a mistura (SI-SDRi −11,9 a −13,7 dB
   no 1462), então separar sem veto atrapalha. O desenho que os números sustentam é
   separar **só onde há sobreposição**, com o veto por janela decidindo o resto — é
   o que a coluna `sep+veto` já mostrava e a recomendação não aproveitava.
4. **Seleção por janela:** só quando houver motivo para acreditar em
   sobreposição parcial; nunca sobre o fluxo já separado.

## Limite honesto

- **Vozes muito parecidas não foram testadas.** O corpus tem falantes de timbres
  bem distintos (escore mediano do terceiro 0,037 contra 0,613 do dono). Com dois
  homens de timbre próximo, o limiar baixo deixa passar terceiro. Medido depois
  em `evidence/521` (seção de vozes parecidas): 0,35 passa 60 % dos pares
  parecidos, 0,44 passa 0 %. A escolha do fluxo na separação, que era o outro
  risco, errou 0 % nos mesmos pares.
- **Sobreposição total só é resolvida até 2 falantes.** O modelo é `num_spks=2`;
  com três pessoas falando ao mesmo tempo, o terceiro entra como resíduo em um dos
  fluxos.
- **O alternado do corpus alterna blocos inteiros de 1,5 s**, sem sobreposição de
  borda. O caso real de conversa cruzada (as bordas se sobrepondo) não foi medido;
  é justamente onde a seleção pura deve começar a falhar.
- **A referência contém os clipes avaliados** (sem holdout): os escores de
  identidade estão otimistas.
- **O modelo é torch, não MLX.** A `.venv-mlx` **tem** torch 2.12.1 com MPS e ele
  funciona — a primeira versão desta evidência dizia o contrário e estava errada.
  Medido de novo **na própria `.venv-mlx`**: RTF 0,25 e SI-SDR 12,5 dB no fluxo do
  alvo, iguais aos da venv de bancada. A única dependência que faltava era
  `torchinfo` (puro Python, importado sem uso pelo código vendorizado), instalada
  e sem pino. Nenhum pino foi mexido nesta fase.
- O WER limpo alto (0,41–0,49) limita a resolução da meta de +5 p.p.

## Instrumento (o que foi corrigido para estes números existirem)

O runner registrava **0 % de corte no alternado** contra 62 % na chamada isolada.
Não era o método: `alternado()` fatiava alvo e concorrente com o **mesmo índice
`i`** e avançava `i` pela linha do tempo inteira, então o concorrente curto morria
no 2º bloco, o `break` disparava e a "mistura" ficava **um bloco só com o alvo** —
sem terceiro não há o que cortar.

```
alvo 63916 amostras, concorrente 19452
i=0     base=alvo pedaco=24000
i=24000 base=conc pedaco=0      <- morre aqui
i=48000 base=alvo pedaco=15916  <- nunca chega
```

Correções: ponteiro próprio por fonte; temporários com `tag` por chamada; clipe
mais curto que a janela vira uma janela (antes gerava zero); `junta()` emenda
clipes do mesmo falante para a janela de 1,5 s significar algo; e `suspeita`
marcada quando a abertura dos escores mostra que as duas vozes não estão na
mistura — 0 % deixa de passar silencioso.

O limiar também mudou: era `max(escores) − 0,12`, que no fluxo separado comia
bloco de alvo (o alvo pontua 0,55–0,78 e o terceiro 0,00–0,13, mas o máximo em
0,78 puxava o corte por cima dos blocos de 0,55). Passou a ser absoluto e explícito
(`--corte-abs`), 0,35.

Outra armadilha: a primeira integração da separação usou blocos de 6 s com janela
de Hann e deu **SI-SDRi negativo** (−4,8 dB). O modelo é treinado no enquadramento
do decodificador de referência do ClearerVoice — janela de 1 s, passo de 0,75 s,
descarte de 0,125 s nas bordas e reescala da saída para o RMS da entrada. Seguindo
isso: +10,8 dB no mesmo par.

## Reprodução

```bash
# piso: só seleção
./.venv-mlx/bin/python tools/bakeoff/alvo.py --sirs 0,5,10 --n 2 --seg 12 --corte-abs 0.35
# com separação
./.venv-mlx/bin/python tools/bakeoff/alvo.py --sirs 0,5,10 --n 2 --seg 12 \
    --separa --corte-abs 0.35
# linha de base limpa
./.venv-mlx/bin/python tools/bakeoff/alvo.py --n 2 --seg 12 --somente-limpo
# portão de turno
./.venv-mlx/bin/python tools/bakeoff/vad_pessoal.py --minutos 10
```

O runner roda na `.venv-mlx` (Whisper do app) e chama a venv de bancada
(`/tmp/tts-bakeoff/venv312`) por subprocesso para o ECAPA e a separação. O código
do separador foi vendorizado em `tools/separa_vend/` (revisão fixada) e os pesos
foram para `models/mossformer2-ss-16k` com sha256 no manifesto (#526-bis) — os
caminhos de `/tmp` desta evidência são os da rodada, não os do repo. Licença
Apache-2.0 (código e pesos) registrada no `LICENCAS.md`.

## Ainda em aberto

- Vozes parecidas (mesmo sexo/timbre) e o SIR 0 com as duas muito próximas.
- Sobreposição de borda no alternado e 3+ falantes.
- Ruídos do R1 somados a todos os casos (só o portão foi testado com ruído).
- Perda de turno do alvo medida ponta a ponta no Live (aqui só a cobertura do
  portão).
