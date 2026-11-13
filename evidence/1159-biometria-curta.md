# #1159 — READOUT: verificação de locutor em fala curta (0,3–3 s)

Contra o pré-registro `evidence/1159-prereg-biometria-curta.md` (commitado antes da
medida). Execução: `evidence/1159-biometria-curta.py`, JSON bruto
`evidence/1159-biometria-curta.json`. Corpus 100 % público: MLS PT dev+test (impostor
e genuíno da CV) e FLEURS pt_br test (impostor garantido, corpora disjuntos). **Nada
de `voices/`, perfis, `settings.json` ou `models/` foi tocado.**

18 locutores, 60 recortes por locutor/duração, seed 1159, k=5 dobras, θ derivado só
na calibração. n = 18 360 pares impostores e 1 080 genuínos por duração na CV; 7 200
impostores por duração no FLEURS (400 arquivos).

## Desvio de protocolo, declarado antes dos números

**A alavanca (a) é INERTE como pré-registrada.** A fórmula é
`θ(d) = max(θ(3 s), quantil_{1−FAR}(impostor_d))`, e o quantil fica **abaixo** de
θ(3 s) em todas as durações (p99 do impostor vai de 0,233 a 0,355; θ de produção é
0,39/0,48). O `max` então devolve θ(3 s) para todo d — a coluna `theta_por_dobra` é
constante nas cinco dobras de todas as durações. Ou seja: o pré-registro mediu a
curva, mas a fórmula não deixa o limiar descer, e é descendo que o FRR cairia.

Isso é achado, não conserto: mantive a fórmula do pré-registro e reporto o que ela
dá. Um θ(d) = quantil puro é alavanca **diferente** e precisa de decisão própria.

## Tabela — alvo FAR 1 % (θ = 0,39, o de produção)

| d (s) | FAR CV | IC95 sup CV | FRR CV | FAR FLEURS | IC95 sup FLEURS | veredito |
|---|---|---|---|---|---|---|
| 0,3 | 0,005 % | 0,031 % | 92,2 % | 0,014 % | 0,079 % | FAR ok · **inútil** |
| 0,5 | 0,038 % | 0,079 % | 65,0 % | 0,056 % | 0,143 % | FAR ok · **inútil** |
| 0,75 | 0,022 % | 0,056 % | 34,4 % | 0,333 % | 0,496 % | FAR ok · **inútil** |
| **1,0** | 0,076 % | 0,128 % | **15,9 %** | 0,389 % | 0,561 % | **APROVADO e útil** |
| 1,5 | 0,174 % | 0,246 % | 4,2 % | 0,889 % | 1,133 % | **reprovado** (FLEURS > 1 %) |
| 2,0 | 0,185 % | 0,259 % | 0,7 % | 1,181 % | 1,457 % | **reprovado** |
| 3,0 | 0,441 % | 0,548 % | 0,2 % | 2,141 % | 2,503 % | **reprovado** |

Critério do pré-registro: IC95 **superior** de FAR ≤ alvo na CV **e** no FLEURS;
útil = FRR pontual ≤ 25 %.

## Tabela — alvo FAR 0,1 % (θ = 0,48) — **não resolvível**

O pré-registro exige ≥ 20 000 pares impostores por duração para resolver 0,1 %;
medi 18 360 (CV) e 7 200 (FLEURS). **Abaixo do mínimo, então o alvo 0,1 % não é
aprovado** — os números ficam como registro.

| d (s) | FAR CV | IC95 sup CV | FRR CV | FAR FLEURS | IC95 sup FLEURS |
|---|---|---|---|---|---|
| 0,3 | 0 % | 0,021 % | 98,5 % | 0 % | 0,053 % |
| 0,5 | 0 % | 0,021 % | 88,1 % | 0 % | 0,053 % |
| 0,75 | 0 % | 0,021 % | 65,9 % | 0,014 % | 0,079 % |
| 1,0 | 0 % | 0,021 % | 38,9 % | 0,028 % | 0,101 % |
| 1,5 | 0 % | 0,021 % | 14,0 % | 0,069 % | 0,162 % |
| 2,0 | 0,005 % | 0,031 % | 4,4 % | 0,111 % | 0,219 % |
| 3,0 | 0,011 % | 0,040 % | 1,1 % | 0,140 % | 0,257 % |

Nenhuma duração é útil no alvo 0,1 % (FRR ≤ 25 % só a partir de 1,5 s, e lá o FAR
no FLEURS já passou de 0,1 %). Confirmado o "não resolvível".

## Resultado principal

**A menor duração aprovada E útil é 1,0 s** (alvo 1 %): FAR 0,076 % na CV e 0,389 %
no FLEURS, ambos com IC95 superior abaixo de 1 %; FRR 15,9 % — abaixo do teto de
25 % e **9× menor** que o do piso atual de 0,6 s na régua de 0,75 s (34,4 %). O piso
de 0,6 s hoje rejeita 1 em 3 falas legítimas dessa faixa.

## Achado de produto que o #1159 não previa

**θ(3 s) = 0,39 NÃO segura 1 % fora do corpus de calibração.** No FLEURS o FAR aos
3 s é **2,14 % (IC 1,4–2,5 %)** — mais que o dobro do alvo que o limiar promete, e
cresce com a duração em vez de cair. O padrão é o mesmo do #986 (limiar não
atravessa de corpus): a curva do MLS é mais frouxa que a do FLEURS nas durações
longas. Consequência prática: o `speaker_far_alvo="1"` entrega ~0,08 % no curto e
~2 % no longo, o inverso do que a fórmula `max` supõe.

## Ressalvas

- **18 locutores** é pouco para FAR de 0,1 % mesmo com 18 360 pares (os pares não
  são independentes: vêm de 18 cadastros).
- O FLEURS só valida o lado **impostor** (não tem rótulo de locutor); o genuíno
  curto vem só do MLS.
- **A duração é a do áudio DECODIFICADO** — o "1,0 s" da tela é tempo de gravação,
  que inclui silêncio e é maior. Cortar por energia antes de medir é o que torna a
  régua conservadora.
- O piso de 0,6 s está em três constantes do `app.py`; unificar em `_SPEAKER_PISO_S`
  é o `task_88254a38`, que anda junto deste.

## Veredito

Alvo 1 %: **APROVADO em 1,0 s** (e só nele). Alvo 0,1 %: **não resolvível** com o
tamanho desta amostra.

Próximo passo de código, se o PM aprovar: piso por duração em 1,0 s, abaixo disso
`indeterminado` com a mensagem "fala curta demais para conferir a voz" (nunca
passa sem conferir), e o θ de 1,0 s como ponto de partida — **com o achado do
FLEURS aberto em card próprio**, porque mexer no θ de 3 s é outro trabalho.
