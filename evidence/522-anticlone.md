# Anti-clone (#522) — marca d'água medida; AASIST reprovado como portão solo

O app CLONA voz, então o gate de identidade só vale se rejeitar o clone. Duas
camadas foram medidas: marca d'água (AudioSeal) e detector genérico de sintético
(AASIST). Corpus público, nada do dono.

**Resumo:** a marca é praticamente inaudível e sobrevive a codec, reamostragem e
ganho — pode ser ligada por padrão. O AASIST, neste corpus, **não sustenta um
portão sozinho**: 62 % de detecção com 29 % de falso positivo em fala humana.

## Marca d'água (AudioSeal, MIT, local, CPU)

Detecção ponta a ponta: **1,00** no marcado, **0,00** no cru do mesmo trecho.

### Robustez (probabilidade de detecção, 6 amostras, degradação uma de cada vez)

| degradação | detecção | prob. média |
|---|---|---|
| áudio marcado (controle) | **100 %** | 1,000 |
| cru, sem marca (falso positivo) | 0 % | 0,004 |
| mp3 32k | **100 %** | 0,999 |
| opus 32k | **100 %** | 1,000 |
| reamostragem 8 k → 16 k | **100 %** | 1,000 |
| reamostragem 44,1 k → 16 k | **100 %** | 1,000 |
| ganho −12 dB | **100 %** | 0,961 |
| ganho +6 dB | **100 %** | 1,000 |
| **corte de 60 % do trecho** | **83 %** | 0,694 |

Codec, reamostragem e ganho não derrubam a marca. **O corte derruba**: uma amostra
caiu a 0,26. A marca é espalhada, mas não infinitamente — trecho muito curto perde
detecção. É o limite honesto desta camada.

### Inaudibilidade

PESQ entre o original e o marcado, 6 amostras: **média 4,395** (mínimo 4,282).
PESQ sozinho não decide — ele foi feito para degradação de codec —, então os pares
foram para ABX com nomes neutros e gabarito fora de ordem
(`resultados/abx.json`). **O ABX ainda não foi ouvido por ninguém**: é o pendente
que fecha o critério de inaudibilidade, e depende de uma pessoa com fones.

## Regravação alto-falante→mic (o caso do eco do Live)

Autorizada pelo dono, volume moderado, 0,5 m e 2 m, 4 amostras. O piso da sala foi
medido antes (rms 0,0026) e o sinal ficou 8–20 dB acima dele — não é problema de
nível.

| distância | detectados | mediana da probabilidade |
|---|---|---|
| 0,5 m | **0 de 4** | 0,01 |
| 2 m | **0 de 4** | 0,00 |

Controlado: a fonte marcada pontua 1,00 na mesma sessão, e normalizar o gravado ao
nível da fonte sobe só para 0,08–0,12. Não é recorte, não é nível, não é o
detector: **a marca morre na transmissão acústica**.

Consequência dura para o uso (b) do card: o filtro de ECO do Live **não pode
contar com a marca**. O áudio do próprio TTS voltando pelo alto-falante e pelo mic
chega sem marca detectável — o eco tem de ser reconhecido por outro caminho
(correlação com o que foi tocado, ou o próprio controle de eco).

A gravação ficou em tmp e foi **APAGADA** ao fim, como combinado; sobrevive só o
`resultados/regrava.json` com os números.

## Detector genérico (AASIST, `clovaai/aasist`, MIT)

Licença revisada: código e pesos no mesmo repositório MIT (NAVER Corp.); os termos
do dataset ASVspoof2019 LA não são herdados para inferência. Registro no
`LICENCAS.md`. Descartados: `TTS4ALL/Eval_AASIST` (sem licença declarada) e
`lab260/AASIST3` (cc-by-nc-4.0, restringe uso comercial).

24 clones do próprio app SEM marca contra 24 falas humanas:

| corte | detecção nos clones | **falso positivo humano** |
|---|---|---|
| 0,10 | 83 % | 33 % |
| 0,30 | 71 % | 29 % |
| 0,50 | 62 % | 29 % |
| 0,90 | 50 % | 21 % |

Mediana dos clones 0,894 contra 0,038 dos humanos — a separação existe nos
extremos, mas a cauda dos humanos sobe e a curva é chata. **Não há corte que dê
detecção e falso positivo aceitáveis ao mesmo tempo.**

Leitura: o AASIST é de 2021, treinado na ASVspoof2019 LA (ataques de vocoder de
outra geração) e o corpus aqui é leitura de audiolivro — domínio e geração
diferentes. O recorte oficial (`pad`, 64.600 amostras) corta quando o áudio passa
de 4 s e repete quando é curto; os humanos que mais erraram tinham 2,9–7,5 s.
Parte do falso positivo pode ser o recorte, não o modelo — **não isolado**.

## Recomendação

1. **Marca d'água LIGADA por padrão** em toda saída (jobs, Live e
   `/v1/audio/speech`): custa 72–82 ms de embed, é inaudível no PESQ e sobrevive ao
   caminho por onde o áudio passa (arquivo: codec, reamostragem, ganho).
   **Correção depois da regravação:** ela NÃO atravessa o alto-falante e o
   microfone, então o (b) filtro de ECO do Live **não** pode contar com ela — o eco
   precisa de correlação com o que foi tocado ou de controle de eco próprio. O que
   a marca sustenta de verdade é (a) veto em áudio que chega como ARQUIVO e (c)
   proveniência.
2. **No gate de locutor, a marca é veto**: áudio com marca = é o próprio TTS
   voltando, não é o dono falando.
3. **AASIST entra como sinal secundário, não como portão solo.** Com 62/29 ele
   sozinho rejeitaria fala legítima demais. Usar só para o caso "clone de outro
   sistema" com confirmação (ex.: exigir dois trechos).
4. Truncar áudio antes de detectar a marca é o risco de operação: abaixo de ~40 %
   do trecho a detecção cai.

## O que falta

- **Regravação alto-falante→mic**: medida e REPROVADA (0 de 8). O eco do Live
  precisa de outra detecção que não a marca.
- **ABX ouvido por gente** (os pares estão gerados).
- AASIST em corpus mais próximo do produto (fala espontânea, microfone de Mac) e o
  recorte oficial testado separadamente do modelo.

## Reprodução

```bash
./venv312/bin/python tools/bakeoff/marca_robustez.py --limite 6 --sem-regravacao
./venv312/bin/python tools/bakeoff/marca_inaudibilidade.py --limite 6
./venv312/bin/python tools/bakeoff/clone_detect.py --limite 24
```
