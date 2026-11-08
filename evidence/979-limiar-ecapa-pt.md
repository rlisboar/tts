# #979 — limiar do ECAPA em fala PT-BR: o AS-norm 2,434 deixa passar 12,7 % dos impostores; o cosseno cru separa

Data: 2026-11-08. Medição offline no MacBook (dev). Sem gravar nada em
`.speaker-profiles.json`, `settings.json` ou `models/`, e sem ligar enforce. O mini
só foi lido (um GET).

## (1) Extrator efetivo

- Mini (`GET /api/settings` em 127.0.0.1:7860, só leitura): `extrator_efetivo = ecapa:v1`.
- Local (`app._speaker_modelo_efetivo()`): `ecapa:v1`; coorte `models/asnorm-coorte-ecapa.npy` 80×192.
- **Estado do mini diferente do card:** `speaker_gate = enforce` (o card diz `off`),
  `speaker_threshold = 0.6`, `speaker_far_alvo = ""`, `live_so_minha_voz.on = false`.
  Como o perfil "Rodrigo" é legado, o gate pontua no extrator DO PERFIL
  (Resemblyzer, presente no venv do mini) com o cru 0,6. É a régua antiga, e é
  coerente com ela. As linhas "ECAPA indisponível" de `/tmp/tts-studio.log` são de
  boots anteriores à chegada dos pesos. Nada foi alterado.

## (2) Medição

Corpus que a coorte não viu: as vozes reais PT-BR de `voices/` (16 nomes; Andrea e
Andrea2 contam como uma pessoa; as `vd-*`, sintéticas, ficam fora). Trechos de 3 s.
Cadastro = centróide dos 3 primeiros trechos, para os 7 locutores com ≥4 trechos.
Genuínos: os trechos seguintes (n = 14). Impostores: todos os trechos dos outros
locutores (n = 371). Régua: a do app (`_speaker_asnorm`, top-10, coorte do app).
Scripts: `979-limiar-ecapa-pt.py`, `979-asnorm-topk.py`, `979-coorte-pt-medicao.py`.

### AS-norm (`speaker_far_alvo = "1"`)

| coorte · K | FAR @2,434 | FAR @1,8 (joelho do Live) | FRR @2,434 | impostor p99 / máx |
|---|---|---|---|---|
| **app (LibriSpeech EN) · K=10** (o de produção) | **12,67 %** | **15,09 %** | 7,14 % | 9,32 / 14,66 |
| app (EN) · K=50 (o K do R2) | 17,52 % | 26,15 % | 7,14 % | 6,53 / 7,07 |
| FLEURS pt_br (40 loc., só medição, em /tmp) · K=10 | 7,82 % | 8,09 % | 7,14 % | 6,80 / 9,59 |
| FLEURS pt_br · K=50 | 8,63 % | 11,59 % | 7,14 % | 4,88 / 6,81 |

O alvo era FAR de 1 %. O pior par é Camilla ← Sandra Annenberg: duas pessoas, com
cosseno cru de **0,318** e nota AS-norm de **14,66**. Para uma fala PT, o top-10 da
coorte EN fica longe e apertado (mu 0,12; sd 0,04). Dividir por esse sd infla qualquer
cosseno modesto. A normalização é só do lado da fala, então a nota não é comparável
entre falas de domínios diferentes. Trocar a coorte por PT ou mudar o K reduz o
estrago, mas não chega perto de 1 %.

### Cosseno cru do ECAPA (`speaker_far_alvo = ""`)

| limiar cru | FAR (n = 371) | FRR (n = 14) |
|---|---|---|
| 0,44 | 0,27 % | 7,14 % |
| **0,50** | **0 %** | 7,14 % |
| **0,55** | **0 %** | 7,14 % |
| 0,60 | 0 % | 7,14 % |

Impostor cru: p50 0,086, p99 0,379, máx 0,441. Genuíno cru: p50 0,723, p5 0,435. O
único genuíno recusado é um trecho com nota 0,057, que não parece fala do locutor
(trecho sem voz). Voz "Rodrigo" de `voices/`, um trecho contra o outro: 0,748.

## (3) Recomendação

1. **Não ligar `speaker_far_alvo = "1"` com a coorte de hoje.** O 2,434 foi calibrado no
   LibriSpeech (R2, `evidence/521`) e não se transfere para fala PT: aqui o FAR medido é
   12,7 %.
2. **Enforce no ECAPA em modo CRU, limiar 0,44** (CORRIGIDO em 2026-11-08 pelo #982;
   esta nota dizia 0,55). Com 20 locutores do MLS Portuguese e o limiar tirado por
   validação cruzada (`evidence/982-calibra-mls.md`), o 0,44 em 3 s dá FAR 0,34 % e
   FRR 0,83 %; o 0,55 recusava o dono em 7,2 % (FRR) sem ganho real de FAR. Os 371
   impostores daqui seguem a 0 % em 0,50–0,60 e a 0,27 % em 0,44. O ECAPA passa a ter
   régua própria, que não lê o `speaker_threshold` (#982). Vale **depois** que o dono
   recadastrar no ECAPA: ≥3 amostras de ≥3 s, em ambiente quieto (regra do #521).
   Antes disso, o enforce de hoje segue no Resemblyzer com 0,6.
3. **Conferir no microfone do dono antes de travar o número:** `/api/speaker/check`
   com ≥10 falas dele (todas `passaria`) e ≥10 de TV ou de outra pessoa (nenhuma).
4. **Risco fora deste card (para o PM):** o portão "só a minha voz" do Live usa o
   AS-norm com o joelho 1,8 por padrão. Na régua de 3 s, isso deu 15 % de FAR aqui.
   Hoje ele está desligado no mini. Ligá-lo pede antes uma nova calibração (S-norm
   simétrico + coorte PT, ou o cru), medida no protocolo de janela de 1 s do #533. O
   ramo AS-norm do `separa_voz` (#976) só vale com `speaker_far_alvo = "1"`, que
   segue vazio.

## Limites

- `voices/` são gravações de mídia, várias `denoised`, não o microfone do dono. São 7
  cadastros, 14 genuínos e 371 impostores; o FRR de 7 % é um caso só.
- Trechos de 3 s, sem ruído somado. O Live usa janela de 1 s; isso não está medido aqui.
- A coorte PT foi montada só para medir (em /tmp) e não é artefato do app.
