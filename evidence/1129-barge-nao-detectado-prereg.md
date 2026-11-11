# #1129 — interrupção do dono não detectada com AEC ligado: PRÉ-REGISTRO (2026-11-11, antes de medir)

**Base:** main `44bd859` · **Cards:** #1129 (`task_1e04a126`), aponta para o #870 (`task_b0c01c27`)
· **Bloqueado por:** log de barge persistente fora do `/tmp` (`task_9253e121`, infra)

Este arquivo existe para que o número da sessão anotada não seja lido com a hipótese
escolhida depois. O readout entra no fim, com data e SHA, no mesmo commit da medida.

## De onde vem a hipótese

No replay offline das 18 sessões reais do dono com AEC ligado (#870, 2026-11-11, só números,
dump apagado depois por decisão do dono):

- o motor abriu **9** eventos de barge em **13,85 min** de playback;
- o critério que eu tinha pré-registrado para "o dono falou" (Silero ≥ 0,5, ≥ −45 dBFS e NCC com a
  referência < 0,5) **não separa** o dono do resíduo do eco, porque com AEC o resíduo não
  correlaciona com a referência (NCC p50 0,17). Por isso os "374 perdidos / 0 eco falso" foram
  declarados **inválidos**;
- na checagem exploratória, declarada como tal, apareceram ~7,6 min de frames com voz durante o
  playback, com p50 de **−27,6 dBFS**, contra **−23,5 dBFS** da fala do dono no turno dele. Resíduo de
  eco depois do AEC costuma ficar 10–20 dB abaixo disso.

Leitura mais provável, **ainda não provada**: em boa parte desse tempo o dono fala por cima e o
motor não abre barge. É o pedido do dono: "não interrompe direito".

## Hipótese

| # | hipótese | MATA-SE se | Se sobreviver |
|---|---|---|---|
| H1 | com AEC ligado, o motor **não detecta** a maior parte das interrupções reais do dono | na sessão anotada, **≥ 4 de 5** interrupções abrem evento de barge (`barge_suspeito` sem detalhe, `barge_in` ou `speech_start{barge_in}`) dentro de [início anotado, início + 1,5 s] | medir o `eco_dbfs` calibrado e o envelope do mic nas perdidas, e só então propor o limiar (ver abaixo) |
| H2 (concorrente) | a "voz" no playback é resíduo de eco, e não o dono | nos trechos **sem** interrupção anotada, frames com Silero ≥ 0,5 e ≥ −45 dBFS somam **< 5 %** do playback | se sobreviver, a leitura exploratória do #870 estava errada: o problema volta a ser (a)/(b), e não (c) |

As duas podem morrer juntas. Nesse caso o readout diz isso e não força uma leitura.

## Desenho (fixado antes do número)

**Instrumento:** o log persistente do `task_9253e121`, fora do `/tmp` (o `tmp_cleaner` e o reboot
apagam o `/tmp/tts-studio.log`). Precisa ter, por sessão e com carimbo de tempo do servidor:
- `speech_start` / `speech_end` do motor (com `barge_in`);
- `barge_suspeito`, `barge_decisao`, `barge_pausa`, `barge_in` e `interrupted`;
- `eco_dbfs` calibrado e nível do mic (dBFS) a cada ~500 ms de playback;
- bordas do playback (`set_speaking` com duração), para reconstruir a janela.

**Nada de áudio, transcrição nem nome de voz no log.**

**Sessão do dono (~5 min, AEC LIGADO, a condição de produção):**
1. o dono faz uma pergunta que gere uma resposta longa (≥ 30 s);
2. durante a resposta, fala por cima **5 vezes**, com ~5 s entre elas, frases curtas e normais
   ("espera", "para aí", "não, outra coisa" — ele escolhe);
3. **anota a hora de cada interrupção.** Pode ser o botão "marcar" da UI, se o infra/frontend
   expuserem, ou a hora de relógio dita em voz alta antes ("um", "dois"…). A anotação é a
   verdade de chão. Sem ela, a medida não vale;
4. ao fim, fica ~20 s calado com a resposta tocando, que é o trecho de controle da H2.

**Métricas** (das 5 interrupções):
- detectadas / perdidas;
- tempo do início anotado até o 1º evento de barge;
- tempo do evento até o `interrupted` (o áudio parou).

Do trecho de controle, sai a fração de frames com "voz" no playback sem o dono falando.

**Alavancas, na ordem, só depois do número** (nada disso muda antes):
1. **limiar do barge com AEC ligado.** O limiar (envelope > eco + 4 dB, ou Silero ≥ 0,85 com
   degrau de 2 dB) foi desenhado para eco **sem** cancelamento. A pergunta é se o `eco_dbfs`
   calibrado nas sessões com AEC está alto (calibrado em cima da própria voz do dono, ou de um piso
   alto) e por isso empurra o limiar acima da voz dele;
2. `barge_in_ms` / `barge_decisao_ms` mais curtos;
3. a pausa reversível (`barge_pausa`, ~480 ms), que já existe atrás da flag. Ela só entra depois
   de (1), porque o duck rápido por NCC curta foi desligado de vez no #870.

**Fora de escopo até o número:** qualquer mudança no motor, nos filtros de produção ou nos
settings do dono. Mexer em `live_interrupcao` ou nos filtros de barge do dono é decisão dele.
