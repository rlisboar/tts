# #875: linha de base da bancada E2E do Live no `9b0230c` (mini, 2026-11-06)

**Instrumento**: `tools/live_bench.py`. Roda **no mini** contra `ws://127.0.0.1:7860/api/live/ws`, que é a instância de produção. Antes da rodada, `deploy_mini.sh compare` deu `mini HEAD = 9b0230c` e árvore limpa.

**Áudio**: 14 gravações reais de `voices/`, todas com `ref_text`, **copiadas** para `/tmp/live-bench-audio` no próprio mini. O original não foi movido e nenhum áudio saiu da máquina.

**Métricas brutas**: `live-bench-9b0230c.json`. O arquivo tem só métricas por fala e por resposta, sem texto e sem áudio.

## Comando

```
.venv-mlx/bin/python -I /tmp/live_bench.py --audio /tmp/live-bench-audio \
    --ciclos 10 --rotulo 9b0230c --out /tmp/lb-9b0230c.json --textos /tmp/lb-9b0230c-textos.json
```

Parâmetros padrão: eco reinjetado a −15 dB com 100 ms de atraso, piso de ruído −60 dBFS, interrupção após 1,5 s de playback, pausas de 1 a 4 s, `seed` 875. A sessão durou 871 s, com 10 ciclos (20 falas, 24 respostas).

## Resultado (linha de base)

| SLO | meta | `9b0230c` | |
|---|---|---|---|
| S1 falas aceitas | ≥95 % | **65 %** (13/20) | ❌ |
| S1 WER (ciclo, A longa + B) | ≤10 % | **mediana 0,98**; 0 de 5 ciclos ≤10 % | ❌ |
| S1 falas ≥0,8 s perdidas | 0 | **7** (o servidor marcou 4 turnos `descartado`) | ❌ |
| S2 fim da fala → 1º áudio | p50 ≤1,5 s / p95 ≤2,5 s | **p50 5,15 s / p95 17,0 s** (n=18) | ❌ |
| S3 duck (evento + rampa de 50 ms) | ≤300 ms | **p50 806 ms**, máx 4,45 s; 1/9 ok; 1 sem duck | ❌ |
| S3 stop (`interrupted`) | ≤800 ms | **p50 5,19 s**, máx 10,5 s; 0/9 ok; **4/9 sem corte** | ❌ |
| S3 falso corte por eco | ≤5 % | **4/9 (44 %)**; o falso duck veio em 9/9 | ❌ |
| S4 respostas com markdown/emoji | 0 em 50 | **22/24**: 204 marcas de negrito/itálico, 90 emojis, 10 listas | ❌ |
| S5 30 min | sem travar nem vazar | não rodado nesta passada (`--sessao-min 30`). Nos 14,5 min: o WS ficou vivo, 0 `error`, RSS de 388→391 MB, threads 64→67, 2 filhos | — |

## Leitura

1. **Endpointing.** Fala longa com pausa natural é cortada no meio. A resposta começa por cima do resto, e o resto vira `turno_pendente`, que funde com a fala seguinte ou é descartado. É daí que vêm a cobertura baixa e o WER ≈1 do ciclo, mesmo nas falas que o servidor "aceitou". No smoke, uma fala de 6,8 s virou um turno com metade do texto e a outra metade foi parar no turno de B.
2. **Interrupção.** O duck chega em ~0,8 s, e não nos ≤300 ms da meta. O `interrupted` chega em ~5 s, e quase metade das falas por cima nem cortou. A fala por cima frequentemente é tratada como pendente ou descartada.
3. **Eco.** A resposta tocando sozinha, com eco a −15 dB, gerou duck em todas as respostas e corte em 44 % delas.
4. **Texto para o TTS.** No `9b0230c`, os deltas de `assistant_text` vão crus ao chunker do TTS (`live_pipeline.py`, laço do `_deltas`). O `chat_system` custom do dono substitui o default "sem markdown".
5. **Latência.** O S2 sobe porque a fila do pipeline nunca esvazia: há pendentes acumulados e respostas longas, de ~500 a 900 caracteres.

## Limites do instrumento (ler antes de comparar)

- **Duck.** Conta `speech_start` ou `barge_suspeito`, que são os dois eventos em que a UI no modo `inteligente` abaixa o volume, mais os 50 ms da rampa. Evento anterior a `início + 100 ms` não conta, porque o primeiro frame com voz ainda nem chegou ao servidor. Esses casos ficam em `duck_antes_da_fala`.
- **Eco.** É um modelo: o playback simulado em tempo real volta ao mic a −15 dB. O valor real depende do AEC do navegador e da sala. A comparação antes/depois vale com o **mesmo** `--eco-db`.
- **S1.** É medido por ciclo, como a cobertura (LCS) das palavras de referência na transcrição do ciclo. A atribuição por janela de tempo erra quando o servidor corta e funde trechos. A fala curta (0,9 s) não tem referência palavra a palavra e é aceita quando sobra transcrição além da fala B. A referência é o `ref_text` do cadastro, ele próprio uma transcrição: pode ter erro pequeno.
- **S4.** É medido no `assistant_text`. Se a correção do #871 sanear só o caminho do TTS e mantiver o `assistant_text` cru, a bancada precisa passar a ler o texto saneado. **Revisitar ao integrar o #871.**
- **Variância.** O LLM é de produção, então as respostas variam entre rodadas, e com elas o S2 e o S4. Para comparar, use o mesmo `--seed` e de preferência `--ciclos ≥10`.
