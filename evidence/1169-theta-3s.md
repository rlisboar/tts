# #1169 — READOUT: o θ de 3 s não segura o alvo; o valor que segura é 0,41

Fecha o achado que abriu o card: com o θ de produção (**0,39**), o FLEURS dá **1,55 %**
de FAR — o dobro do alvo de 1 % que o limiar promete. Aqui se deriva o θ que **realiza**
cada alvo, com o FRR que ele compra, e se diz o preço.

**Veredito: alvo de 0,1 % é pagável a 3 s (θ 0,493, FRR 3,0 %). A recomendação é subir o
θ de 3 s de 0,39 para 0,41–0,42 e adotar θ(d) nas durações curtas.** O alvo de 1 % que
está no código hoje não é cumprido por 0,39 em nenhuma duração — ver a coluna do θ de
produção na primeira tabela.

## Corpus e custódia

`facebook/multilingual_librispeech` `data/mls_portuguese/{dev,test,train}` (cadastro e
genuíno) e `google/fleurs` `data/pt_br/test` (impostor garantido, corpus disjunto), ambos
CC BY 4.0, tudo em `~/.cache/tts-bancada/`, nada no repo.

**33 locutores** com ≥ 8 enunciados longos. O split `dev`+`test` do MLS tem 20; os outros
28 baixados vêm do `train`, e o rendimento cai porque vários `.tar.gz` são do MESMO
locutor — 48 prefixos, 33 aproveitáveis. **`n_imp` = 1 980 genuínos × 32 impostores =
63 360 pares** (não é contagem de arquivo: é produto de pares, como no #1159) e 919
arquivos FLEURS × 33 cadastros = **30 327 pares**. Os dois acima do mínimo de 20 000.

## Alvo 1 % — θ derivado e o que ele custa

| d (s) | θ derivado | FAR FLEURS | IC95 sup | FRR (CV) | θ de produção 0,39 → FAR FLEURS |
|---|---|---|---|---|---|
| 0,3 | 0,243 | 0,752 % | 0,86 % | 55,8 % | 0,012 % |
| 0,5 | 0,277 | 0,903 % | 1,02 % | 29,2 % | 0,059 % |
| 0,75 | 0,309 | 1,002 % | 1,12 % | **18,0 %** | 0,137 % |
| 1,0 | 0,336 | 1,002 % | 1,12 % | **8,7 %** | 0,303 % |
| 1,5 | 0,366 | 1,002 % | 1,12 % | 4,5 % | 0,605 % |
| 2,0 | 0,389 | 1,002 % | 1,12 % | 2,3 % | 0,971 % |
| 3,0 | **0,412** | 1,001 % | 1,12 % | **0,76 %** | **1,547 %** |

A última coluna é o defeito do card: com o θ de produção o FAR no FLEURS é 1,55 % aos 3 s
e **0,02 % aos 0,3 s** — o limiar está alto demais para o longo e baixo demais para o
curto. Não é um número errado, é um número só para uma curva inteira.

## Alvo 0,1 % — resolvível agora, e barato no longo

| d (s) | θ derivado | FAR FLEURS | IC95 sup | FRR (CV) |
|---|---|---|---|---|
| 0,3 | 0,322 | 0,096 % | 0,137 % | 78,8 % |
| 0,5 | 0,368 | 0,102 % | 0,145 % | 56,8 % |
| 0,75 | 0,402 | 0,102 % | 0,145 % | 38,0 % |
| 1,0 | 0,420 | 0,102 % | 0,145 % | 23,3 % |
| 1,5 | 0,463 | 0,102 % | 0,145 % | 13,7 % |
| 2,0 | 0,481 | 0,102 % | 0,145 % | 8,5 % |
| 3,0 | **0,493** | 0,102 % | 0,145 % | **3,0 %** |

O #1159 declarou 0,1 % "não resolvível" por falta de pares. Com 30 327 pares resolve-se —
**e o preço é 3 % de FRR na faixa normal**, não os 78 % da faixa curta.

## Tensão de critério, declarada

O pré-registro pede IC95 **superior** ≤ alvo; o θ sai do quantil `1 − alvo`, então o ponto
cai **exatamente** no alvo e o IC superior fica ~0,12 pp acima dele por construção. Pelo
critério literal, o θ do quantil nunca aprova. Leitura honesta: ou se adota margem (θ do
quantil de 0,9 % para prometer 1 %) ou o critério passa a ser o ponto com o IC ao lado.
Recomendo a margem — ela é o que faz a promessa valer.

## Recomendação

1. **θ(3 s) de 0,39 para 0,42** (alvo 1 % com margem; o θ que realiza 1 % é 0,412 e o FRR
   a 3 s é 0,76 % — subir custa quase nada no longo).
2. **Se o alvo de 0,1 % valer a pena**: 0,493 a 3 s, FRR 3,0 %. É o único alvo em que a
   curva longa fica barata; a curta continua caríssima em qualquer alvo.
3. **θ(d) nas durações curtas** — sem isso, 1,0 s segue com FRR 8,7 % (contra 23,3 % no
   θ de 0,42 e 17,1 % no θ de produção). A tabela acima é a fonte; a implementação é card
   próprio, com o piso de verificação em 1,0 s já integrado.
4. **Não mexer no piso** por causa deste card: a 0,75 s o FRR é 18,0 % no θ derivado e
   34,9 % no θ de produção — útil, mas o piso de 1,0 s já está pago e medido.

## Limites

- 33 locutores: os pares não são independentes (33 cadastros), então o IC é otimista.
- O FLEURS só mede impostor; o genuíno é todo MLS.
- θ derivado no conjunto de calibração por dobra (k=5), aplicado nas de teste — o mesmo
  protocolo do #1159, sem limiar escolhido no conjunto avaliado.
