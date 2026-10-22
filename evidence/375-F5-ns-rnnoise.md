# #375/F5 — NS do navegador + RNNoise: o filtro custa precisão do STT

O requisito 2 do #278 não estava provado: o teste do filtro mede o arnndn em PCM
sintético, e o index mantém `noiseSuppression: true` FIXO. A pergunta era se os
dois juntos comem consoante. Resposta medida: **o RNNoise, sozinho, já custa**, e
o efeito aparece tanto em áudio limpo quanto em áudio com ruído.

## Método

5 e 4 vozes de `voices/` (clones do dono), 6 s cada, 16 kHz — O MESMO formato que
o pipeline entrega ao filtro. O baseline é o próprio transcript do áudio limpo
(Whisper local do app, greedy, `_transcribe` direto), e a métrica é **erro de
palavra contra esse baseline** (distância de edição em palavras, normalizada).
Condições: limpo, limpo+filtro, ruído, ruído+filtro. Ruído em duas famílias:
branco e "ventoinha" (branco com passa-baixa de 900 Hz + tom de 118 Hz).

## Resultado

| condição | erro de palavra contra o limpo |
| --- | --- |
| limpo + RNNoise | **13,9 %** (11/79) |
| ruído branco ~−30 dBFS | 35,4 % (28/79) |
| ruído branco ~−30 dBFS + RNNoise | **57,0 %** (45/79) |
| ventoinha ~−30 dBFS | 45,0 % |
| ventoinha ~−30 dBFS + RNNoise | 58,3 % |
| ventoinha(low-pass) ~−30 dBFS | 15,0 % |
| ventoinha(low-pass) ~−30 dBFS + RNNoise | 20,0 % |
| ventoinha(low-pass) ~−24 dBFS | 26,7 % |
| ventoinha(low-pass) ~−24 dBFS + RNNoise | 41,7 % |
| ruído branco ~−20 dBFS e ~−10 dBFS | 90 % e 100 %, **iguais com e sem filtro** |

Leitura: o filtro é **quase transparente no limpo** (92 % de semelhança) mas
AINDA assim troca 1–2 palavras em 15. Com ruído, em nenhum nível medido ele
melhorou o ASR — piorou. Nos níveis altos (−20/−10 dBFS) os dois lados empacam.

## Ressalvas (o que este número NÃO diz)

- Amostra pequena: 6 s por voz, sem repetição, um modelo de ASR (o do app). As
  diferenças são de poucas palavras e o veredito "piora" é consistente no mesmo
  sentido em todas as células, mas não é um teste de inteligibilidade.
- O filtro é o `sh.rnnn` que veio no #278 — modelo "sh", sem garantia de ser o
  melhor para português. Trocar o modelo pode mudar esta tabela.
- **A metade do NAVEGADOR não foi medida aqui**: `noiseSuppression` do Chromium
  não roda offline. O que está provado é o estágio do servidor (o que o requisito
  do #278 pede para medir); afirmar "os dois juntos comem consoante" ainda exige
  a passagem manual com o NS fora, no navegador.
- Ruído sintético não é a ventoinha do dono: a família "ventoinha" é uma
  aproximação (baixa frequência + tom), não a captação real dele.

## Comando

Rodar de dentro de `ts-rod` (o script fica no histórico desta evidência):

```
./.venv-mlx/bin/python - <<'PY'
# ver corpo no comentário da task_5a34c69a — carrega vozes de `voices/`,
# aplica Ruído/RNNoise, transcreve com `app._transcribe` e conta erro de
# palavra contra o transcript limpo
PY
```

## Consequência prática (para o dono decidir, não aplicada)

Hoje o filtro é LIGADO por padrão no Live. O número sugere duas coisas: (a) um
knob visível para desligar (já existe: `TTS_LIVE_RNNOISE=0`), e (b) medir com um
modelo de ruído treinado para PT antes de manter o default. Não mudei default nem
o NS do navegador — os dois mexem no comportamento aprovado do #278.
