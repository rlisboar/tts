# #1159 — PRÉ-REGISTRO: verificação de locutor em fala curta (0,3–2 s)

Data: 2026-11-13. Escrito e commitado ANTES de qualquer medida. Autor: audio-ml-engineer.
Gate: qa-review (não-autor). Sem ligar enforce, sem tocar em `voices/`, perfis, `settings.json`
ou `models/`. Corpus 100 % público (decisão do PM): **MLS PT dev+test** (CC BY 4.0) e
**FLEURS pt_br test** (CC BY 4.0). Nada do dono entra na medida.

## Problema

`_speaker_embed` e `_speaker_embed_ecapa_x` devolvem `None` abaixo de 0,6 s; o `enforce`
recusa. Alvo: a menor duração em que a verificação ainda é útil, com FAR ≤ alvo configurado
(`speaker_far_alvo` "1" = 1 %, "0.1" = 0,1 %), sem passar fala sem conferir.

## Extrator e régua

ECAPA (`ecapa:v1`), cosseno CRU (régua do #982). Piso de 0,6 s é baixado SÓ no script de
medida (chama `_speaker_encoder_ecapa()` direto); o app não muda antes do veredito.
Referência de produção: θ(3 s) = 0,44 (alvo "1" 0,39; "0.1" 0,48).

## Dados e protocolo

1. MLS PT dev+test, todos os locutores baixados (n medido e declarado no readout). Cada
   enunciado passa por trim de silêncio por energia (limiar −40 dBFS relativo ao pico,
   janela 20 ms), para o corte ser FALA, como a frase curta real.
2. Cadastro: centróide L2 de 3 recortes de 3 s de 3 enunciados distintos por locutor
   (como o app). Esses enunciados NÃO entram como teste.
3. Teste: por locutor, por duração d ∈ {0,3; 0,5; 0,75; 1,0; 1,5; 2,0; 3,0} s, até 40
   recortes de início aleatório (seed fixa 1159) em enunciados não usados no cadastro.
   Genuíno = recorte × cadastro do próprio. Impostor = recorte × cadastro de TODOS os
   outros locutores.
4. Validação cruzada por LOCUTOR (k = 5 dobras): θ(d) tirado só nos locutores da
   calibração, aplicado nos de teste. Nunca limiar escolhido no conjunto avaliado.
5. Confirmação independente de FAR: cadastros MLS × recortes do FLEURS pt_br test (corpora
   disjuntos, logo impostor garantido; FLEURS não tem rótulo de locutor, então NÃO serve
   de genuíno). Mesmas durações, mesmo θ(d).

## Alavanca (a) — limiar por duração

θ_alvo(d) = max(θ_alvo(3 s), quantil_{1−FAR_alvo}(escore impostor de duração d na calibração)).
Para cada d e alvo reporta-se: θ(d), FAR (IC Wilson 95 %), **FRR (IC Wilson 95 %)**, EER.
- **Critério de aprovação por duração:** o limite superior do IC de FAR, no teste da CV
  E no FLEURS, ≤ alvo (1 % e 0,1 % reportados separadamente). Reprovou → essa duração e as
  menores ficam `indeterminado`.
- **Utilidade:** a duração é "verificável" se o FRR pontual ≤ 25 %. A menor duração
  aprovada E útil é o resultado principal. FRR é a métrica que o dono sente; reportado em
  toda linha, sem resumir só em EER.
- Mínimo de 20 000 pares impostores por duração (0,1 % resolvível); senão o alvo 0,1 %
  é marcado "não resolvível" e não aprovado.

## Alavanca (b) — continuidade de sessão

Regra candidata: fala curta (≤ 2 s) em até T ∈ {3; 8} s depois de fala longa (≥ 3 s)
VERIFICADA, na mesma sessão, usa limiar reduzido θ_s(d) = θ_alvo(d) − δ, δ ∈ {0,03; 0,06; 0,10},
e escore contra centróide(cadastro + embedding da fala longa verificada).
- Risco pré-declarado: impostor que fala logo depois do dono herda o canal/ambiente, e o
  corpus de locutores em canais distintos SUBESTIMA o FAR. Por isso a medida é feita com
  canal pareado: genuíno e impostor passam pela MESMA resposta ao impulso (3 IRs de sala
  sintéticas + 1 filtro de microfone, seed 1159) dentro de cada sessão simulada.
- **Aprovação:** FAR de sessão (IC Wilson superior) ≤ alvo com canal pareado, em
  ≥ 20 000 sessões impostoras simuladas. Senão a alavanca (b) NÃO vai para o código e o
  readout diz "não provada".
- Sessão simulada: longa do locutor A (verificada pelo θ(3 s)) + silêncio de 0,5–T s +
  curta de A (genuíno) ou de B ≠ A (impostor).

## Comportamento em `enforce` (já acordado com o PM)

`indeterminado` continua REJEITADO, com a mensagem "fala curta demais para conferir a
voz" (não "áudio curto p/ identificar a voz"). Nunca passa sem conferir. Alinhar o texto
com o #1158 (transcrição) sem mudar o que o #1158 decidir sobre o filtro.

## Entrega

`evidence/1159-biometria-curta.md` + script + JSON bruto, com tabela por duração:
θ, FAR (CV e FLEURS), FRR, n. Veredito APROVADO/REPROVADO por critério, contra este
texto. Só então muda `app.py` (piso por duração, θ(d), mensagem), com testes; qa-review
gateia. Qualquer desvio do protocolo é declarado no readout, não corrigido aqui.

---

## ADENDO #1169 (2026-11-13) — mudança de ESCOPO, declarada antes da recontagem

O readout do #1159 (commit `fcdfc19`) mostrou que o alvo de 0,1 % não é resolvível
com 18 360 pares (o mínimo aqui é 20 000) e que o θ de 3 s aceita 2,14 % de impostor
no FLEURS contra alvo de 1 %. O #1169 recalibra o θ; para o número valer, o corpus de
impostor cresce. **O que muda, e só isto:**

- **Locutores de cadastro (MLS PT): 18 → 48.** O split `dev`+`test` do MLS PT tem
  só 20 locutores e estava todo baixado; os novos vêm do split **`train`** (133
  disponíveis), mesmo corpus e mesma licença (CC BY 4.0). Parou em 48 porque o
  rendimento cai (vários `.tar.gz` do `train` são do MESMO locutor) e 48 já passa
  do alvo com folga.
- **Arquivos FLEURS por duração: 400 → 919** (todos os do split `test`). Com 48
  cadastros isso dá **44 112 pares impostores por duração**, 2,2× o mínimo — o alvo
  de 0,1 % deixa de ser "não resolvível" por tamanho.

**O que NÃO muda:** protocolo (trim por energia, cadastro 3×3 s de 3 enunciados,
recortes de início aleatório, seed 1159, k=5 dobras por locutor, θ só na calibração),
critério de aprovação (IC95 superior de FAR ≤ alvo na CV **e** no FLEURS; útil =
FRR ≤ 25 %) e o θ de produção como ponto de partida.

**Custódia do corpus** (exigência do PM): tudo fora do repo, em
`~/.cache/tts-bancada/{mls-pt,fleurs-pt}`; procedência = `facebook/multilingual_librispeech`
`data/mls_portuguese/{dev,test,train}` e `google/fleurs` `data/pt_br/test`, ambos
CC BY 4.0. O readout publica split, contagem por split e sha256 do manifesto de
arquivos usados — FAR sem corpus identificado não vale.
