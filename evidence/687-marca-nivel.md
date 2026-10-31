# Marca d'água audível em fala baixa (#687) — diagnóstico e correção

O dono ouviu a marca no único par do ABX v1 que era de nível baixo. Este arquivo
guarda o que foi medido e o que mudou; a decisão de manter a marca é do dono e
depende do ABX v2 (`/tmp/abx-marca-v2/`).

## O sintoma não é a força relativa

Medido nos 8 pares do ABX v1 (marca medida como a diferença entre o arquivo
marcado e o original):

| par | pico | marca (dBFS) | SNR da marca | ouvido? |
|---|---|---|---|---|
| 7 | **−21,9** | **−63,6** | 24,2 | **sim** |
| 4 | −8,4 | −50,7 | 19,9 | não |
| 1 | −8,2 | −49,5 | 26,1 | não |
| 6 | −7,8 | −49,7 | 25,8 | não |
| 5 | −7,5 | −54,1 | 30,3 | não |
| 2 | −4,1 | −53,6 | 32,7 | não |
| 8 | −3,1 | −50,8 | 27,5 | não |
| 3 | −1,6 | −49,0 | 30,7 | não |

O par ouvido tem a marca **13 dB mais baixa** que o par que passou despercebido. O
que manda é o nível ABSOLUTO do material, não a razão marca/sinal. A pausa também
foi checada e descartada: no par 7 a marca sobe a pausa só ~1,3 dB.

## Correção: a força acompanha o nível

Fator aplicado sobre a saída do gerador: `0,6` no material baixo subindo até `1,0`
no alto (`0,6 + (nível_dBFS + 32)·0,02`, preso em 0,6..1,0).

O piso 0,6 saiu de varredura de detecção, não de escolha:

| fator | detecção no par 7 |
|---|---|
| 1,00 | 1,000 |
| 0,60 | 1,000 |
| 0,50 | 0,875 |
| 0,25 | 0,003 |

## Antes e depois nas 8 amostras

| par | nível (rms) | marca antes | marca depois | detecção antes | detecção depois |
|---|---|---|---|---|---|
| 7 | −39,4 dBFS | −63,6 | **−68,3** | 1,000 | **0,998** |
| 4 | −30,8 dBFS | −50,7 | −54,7 | 1,000 | **0,983** |
| 1 | −23,4 dBFS | −49,5 | −51,4 | 1,000 | 1,000 |
| 6 | −23,9 dBFS | −49,7 | −52,2 | 1,000 | 1,000 |
| 5 | −23,8 dBFS | −54,1 | −56,0 | 1,000 | 1,000 |
| 8 | −23,3 dBFS | −50,8 | −52,8 | 1,000 | 1,000 |
| 2 | −20,9 dBFS | −53,6 | −55,7 | 1,000 | 1,000 |
| 3 | −18,3 dBFS | −49,0 | −50,3 | 1,000 | 1,000 |

PESQ (inaudibilidade objetiva), par ouvido × par alto: **3,781 → 4,196** e
4,435 → 4,515.

## A detecção variava por PROCESSO — e isso explicava o 0,684 do gate

O gate viu 0,684 numa execução da célula. Reproduzido: o MESMO trecho, pelo worker,
dava **0,868 / 1,000 / 0,916** em processos diferentes e ficava **idêntico dentro do
mesmo processo**. A causa é a mensagem: o gerador do AudioSeal **sorteia** os 16
bits a cada embed e algumas sortes grudam mal em áudio baixo — a mesma sorte se
repete enquanto o worker vive.

Consequência real de produção: sem conferência, uma saída podia sair com marca
franca (0,68) e ninguém sabia. O worker agora **confere a própria saída** e, se a
detecção não passar de `TTS_MARCA_ALVO` (padrão 0,97), sorteia de novo — até 4
vezes, guardando a melhor. Custo medido: 0,1 ms/s no piso e 0,8 ms/s no alto; no
piso o alvo passa a ser atingido em **5 de 5** processos (era 0,868 a 1,000).

## Falha do detector não pode custar a marca

A conferência é otimização em cima da marca, não pré-requisito. Sem `try` em volta
do `detect_watermark`, um detector que levanta matava o laço, o `except` do `main`
engolia e o arquivo saía **sem marca** (o gate mediu: `ESTOUROU detector morreu`).

Agora: exceção no detector → fica a embedding já feita (com marca), o erro é
devolvido no protocolo e logado. Célula própria com detector que levanta cobra que
a saída **difere do original** e que a probabilidade vem NaN.

## Como as células mordem

- `test_re_sorteio_insiste_ate_passar_do_alvo`: detector falso fraco e depois bom —
  com re-sorteio passa do alvo na 3ª; com `tentativas=1` fica abaixo. Determinístico.
- `test_esgotar_as_tentativas_ainda_deixa_marca_detectavel`: **5 subprocessos**, cada
  um sorteando a própria mensagem, com o laço forçado a esgotar as 4 tentativas
  (alvo inalcançável) — a marca final tem de seguir ≥0,96. Ambas caem se o
  re-sorteio for removido (conferido).

## Sob carga: a marca podia sair FRACA mesmo esgotando (#706)

Relato do infra na suíte cheia: a célula do piso viu **0,613** contra 0,96. O
suspeito era o produto, e era: `embute_conferido` fazia `melhor = y` em TODA volta
e só atualizava a probabilidade — então o que saía era a **última** tentativa, não a
melhor. Enquanto o laço para no alvo isso não aparece (a 1ª já é a boa); quando ele
**esgota** — exatamente o que a célula do piso força — uma sorte ruim no fim
derrubava a marca para ~0,6.

Dois defeitos, os dois corrigidos:

1. só trocar a guardada quando a tentativa é melhor;
2. `melhor_prob` começa `NaN` e `prob >= NaN` é **sempre falso** — sem teste de NaN
   a primeira tentativa nunca seria guardada e o retorno sairia `NaN`.

Célula determinística nova (`test_esgotar_guarda_a_MELHOR_tentativa_nao_a_ultima`):
probabilidades 0,99 / 0,99 / 0,30 / 0,30 com alvo inalcançável → tem de guardar a de
0,99 e o áudio dela. Reverter para o comportamento antigo derruba a célula.

Reprodução sob carga aqui: **8 e 16 processos simultâneos** a −38 dBFS, marca
1,000 em todos nas três rodadas — a sorte fraca é rara, por isso a célula precisa do
forçamento para morder, e o defeito só se via no caminho de esgotamento.

Comportamento certo, agora: depois das 4 tentativas sai a **melhor** delas. Se todas
forem fracas, ainda sai uma marca (com marca > sem marca) e o número vai para o log
pelo `prob` do protocolo — quem chama pode alertar.

## O que continua valendo

- O ABX v2 para o dono está em `/tmp/abx-marca-v2/` (4 pares, detecção 1,0 nos
  quatro, 2 com A=marcado).
- Limite honesto: a redução é de ~4,7 dB no pior caso — real, mas modesta. Se o
  dono ainda ouvir, o próximo passo é mascaramento no nível LOCAL, que custa mais
  e disputa espaço com o piso de detecção.
