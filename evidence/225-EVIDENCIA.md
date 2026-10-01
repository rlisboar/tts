# #225 — o assistente tem de CALAR quando o usuário fala (corte no onset)

## O defeito (medido, não suposto)

O corte do playback existia SÓ no caminho do barge (`interrupted`). Quando o onset do
usuário cai FORA da janela de playback do servidor, o motor emite `speech_start` sem
`barge_in` e **nada manda o cliente parar**: o áudio que ele ainda tem na fila segue
tocando por cima da fala do usuário.

Assinatura já registrada na medição do #216 (célula A+B+C, 5 de 20 repetições):

```
✖ SEM barge · antes={'estado': 'falando', 'ativos': 1, ...} · eventos=['stats', 'speech_start', ...]
   motor: {'type': 'speech_end', 'barge_in': False, 'detalhe': 'silencio'}  · limiar=-53 (OCIOSO)
```

O cliente estava TOCANDO (`ativos: 1`) e o servidor já tinha fechado a janela (limiar de
ocioso) — o onset virou turno novo e o áudio antigo continuou saindo.

## Por que o corte mora no CLIENTE

A janela de playback do servidor é uma **estimativa** do que o cliente ainda vai tocar
(`_playback_restante` / `_playback_frames`), derivada de duração de chunk e de uma
cauda. Quem sabe o que está de fato agendado é o cliente (`LX.ativos` — fontes já
criadas no AudioContext, tocando ou na fila).

Decisão: no `speech_start`, se houver fila, o cliente chama `lxCancelaPlayback()` —
a MESMA política do barge, pela mesma porta. O `interrupted`, quando vem, é
idempotente. O SERVIDOR fica intocado: o motor segue decidindo janela/limiar e a
classificação do turno.

## Protocolo

`evidence/225-celulas.sh` — ANTES/DEPOIS na mesma máquina, célula a célula, trocando
SÓ o `static/index.html` (a linha do corte); o servidor é idêntico nas duas fases, o
que isola o efeito no cliente:

| célula | o que mede |
| --- | --- |
| `MODO=vao` (default) | controle: o barge do #167 (20/20) tem de continuar |
| `MODO=vao` + `TTS_LIVE_ECO_SO_TOCANDO=1` | regime em que a janela fecha antes (onset SEM barge com fila) |
| `MIC_FILE=1` (eco) | sentido do corte FALSO (não há humano; o wav toca em loop) |

Métrica (aditiva no `tests/live_barge_rep.sh`, resumo `=== CORTE (#225)`): por
iteração, a FILA (ms de áudio agendado no instante do onset) e o corte, com latência.
O alvo é `tocando por cima: 0`.

## Resultado (REP=20 por célula, `evidence/225-{antes,depois}-*.txt`)

| célula | ANTES (cliente sem o corte) | DEPOIS (cliente com o corte) |
| --- | --- | --- |
| `MODO=vao` default | com fila 19 · cortou 19 · **por cima 0** (mediana 148 ms) | com fila 19 · cortou 19 · **por cima 0** (142 ms) |
| `MODO=vao` + `eco_so_tocando=1` | com fila **18 · cortou 0 · por cima 18** | com fila **19 · cortou 19 · por cima 0** (112 ms) |
| `MIC_FILE=1` (eco) | com fila 17 · cortou 14 · por cima 3 | com fila 15 · cortou 14 · por cima 1 |

A célula que decide é a do meio: no regime em que a janela fecha antes, o defeito era
**18/18** (todo caso com fila seguiu tocando por cima, sem nenhum corte) e virou
**0/20** com o corte no `speech_start` — a latência mediana do corte cai de "nunca" para
112 ms.

Controles sem regressão: o vão no DEFAULT segue 20/20 barges e 20/20 cortes (o fix não
troca o caminho do barge, só acrescenta o do onset); a célula do eco muda de ritmo
(`audio` 65→47, `interrupted` 23→30) porque o assistente fala menos e mais turnos
cabem na rodada — a diferença está dentro do espalhamento da MESMA configuração
medido no #216 (34 e 20 em duas rodadas do baseline de eco).

## Limites

- A célula do eco é a mais ruidosa (o onset é contínuo, o wav toca em loop): o número
  que decide é o do vão.
- O guard do pytest é de TEXTO (`static/index.html` não roda em pytest): pina a linha,
  não o comportamento. Quem prova o comportamento é o harness.
- `com fila` conta casos com >= 400 ms de áudio agendado; abaixo disso o corte e o fim
  natural se confundem.
