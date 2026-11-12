# #1129 → #301 — régua do eco com STT ruidoso (task_21d2c3b3): PRÉ-REGISTRO (2026-11-12, antes do código)

## (a) o "?" do Whisper anula o eco

**Achado** (proxy S3, eco −10, `s3-eco-10-44011.log` linhas 107, 193 e 1485): o eco de "Claro, posso te ajudar com isso
agora mesmo." sai do STT como "Posso te ajudar com isso?". O `_marcador_de_humano` vê o "?" sem "?" na referência e
classifica como humano. O eco cai, e o `pergunta` corta o próprio agente: 3/8 falsos cortes [IC95 13,7; 69,4].

**Régua:** se o transcript tem ≥ 3 palavras e casa EXATO (0 troca, após `_normaliza`) com uma janela contígua do texto
em reprodução, o "?" sozinho não o faz humano. As palavras-marcador (`_MARCADORES_HUMANO` fora da referência)
continuam valendo.

**Custo (reabre o #301), declarado:** o dono que repete LITERALMENTE ≥ 3 palavras do agente em tom de pergunta ("o
prazo de entrega é sexta-feira?" sobre o agente dizendo isso) passa a ser descartado como eco. Pelo texto, isso não se
separa do eco que o Whisper fecha com "?". A matriz do `smoke_eco_regra.py` já trata esse caso como ECO (é o único eco
que passa hoje).

**Critério (a):**
- **A1:** matriz `smoke_eco_regra.py` antes × depois: falso-descarte humano continua **0/29** e o eco descartado não
  cai (antes **30/31**). Entram na matriz 2 casos novos: o eco "Posso te ajudar com isso?" (ref = a resposta do
  proxy), que tem de ser descartado, e o humano "posso te ajudar com isso, né?", que tem de seguir humano;
- **A2:** proxy S3 eco −10 (`--ciclos 10`, `--seed 875`, mesmo corpus): falso corte ≤ **1/8** no depois. O antes é o
  `3/8` do readout `b461d59`, e o −10 do readout `3c89f31` (1/8) entra só como dado;
- células com o motor real para o eco com "?" e para a pergunta do dono com marcador, mais mordida.

## (b) eco com 1 palavra trocada + palavra de corte

PENDENTE da decisão do PM sobre a régua (b) × (b2) (análise offline enviada em 2026-11-12). Este arquivo ganha um
adendo com a régua escolhida ANTES do código da (b).
