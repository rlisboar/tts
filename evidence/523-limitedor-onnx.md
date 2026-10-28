# Limpar amostra pelo DFN3 em ONNX (#523) — medição refeita

O R1 elegeu o DeepFilterNet3 para limpar a amostra de referência, mas mediu-o pelo
pacote `df` (torch, binário/vendorizado). Aqui a mesma limpeza roda pelo **grafo
ONNX** e a medição é refeita da mesma forma.

## Por que ONNX

Dispensa o torch no processo que atende o pedido e não depende do binário
externo. O grafo vem de `soniqo/DeepFilterNet3-ONNX` (MIT, export verificado do
DFN3 **v0.5.6** — a mesma versão do pacote instalado aqui, então não há deriva de
arquitetura). O repositório traz só a REDE; o consumidor faz STFT, normalização,
aplicação de máscara/filtro e síntese. É isso que `tools/bakeoff/dfn_onnx.py` faz,
reusando o DSP do pacote `df` e trocando apenas a chamada da rede.

## A integração foi provada equivalente, não presumida

Mesma entrada nos dois caminhos:

| comparação | valor |
|---|---|
| SI-SDR ONNX × torch | **136,9 dB** |
| correlação | **1,0000** |

Dois defeitos apareceram no caminho e ficam registrados porque voltariam:

1. **Atraso do STFT.** A saída do grafo vinha certa em nível e DESALINHADA
   (correlação 0,04): falta a compensação que o `enhance` de referência faz —
   preencher `n_fft` no fim e cortar `n_fft − hop`. Sem isso a correlação não sobe.
2. **`synthesis` devolve `[canais, amostras]`.** Cortar a saída sem `squeeze` fatia
   o eixo do canal e o resultado sai vazio.

## Números (ventoinha, DNSMOS 1–5; timbre = similaridade do locutor; WER do Whisper)

| cenário | candidato | OVRL | timbre | WER | RTF |
|---|---|---|---|---|---|
| ventoinha 10 dB | gating atual | 2,63 | 0,797 | 0,24 | 0,002 |
| ventoinha 10 dB | RNNoise | 2,73 | 0,947 | 0,18 | 0,015 |
| ventoinha 10 dB | DFN3 torch | 2,95 | 0,986 | 0,24 | 0,255 |
| ventoinha 10 dB | **DFN3 ONNX** | **2,96** | **0,987** | 0,24 | 0,298 |
| ventoinha 10 dB (2ª fala) | DFN3 torch | 3,21 | 0,962 | 0,29 | 0,608 |
| ventoinha 10 dB (2ª fala) | **DFN3 ONNX** | **3,22** | **0,972** | 0,29 | 1,123 |
| ventoinha 5 dB | RNNoise | 2,93 | 0,912 | 0,80 | 0,035 |
| ventoinha 5 dB | DFN3 torch | 3,11 | 0,986 | 0,80 | 0,637 |
| ventoinha 5 dB | **DFN3 ONNX** | **3,12** | **0,987** | 0,80 | 1,080 |

**A conversão não custa qualidade — é o mesmo sinal, célula a célula.** O RTF sobe
(0,64 → 1,08) porque o caminho de bancada passa por subprocesso e reamostra duas
vezes; dentro do processo, a diferença é de overhead, não de conta. Para uso
offline em amostra de 10–30 s isso é irrelevante.

## Fio no app (feito na janela quieta nº 2, sem medir)

`_denoise_para_referencia` deixa de ser um denoise solto e vira a cadeia do card:

- ordem **música → dereverb → denoise**, cada etapa opcional por configuração
  (`voice_limpar_musica`, `voice_limpar_dereverb`) e pulada, com registro no
  relatório, quando o pacote não existe;
- motor por `voice_limpar_motor`: `dfn3` (ONNX) por padrão, caindo no gating de
  sempre quando o modelo falta ou o import quebra — a queda é registrada, não
  silenciosa;
- **guarda de timbre**: similaridade de locutor antes/depois contra
  `voice_timbre_min` (0,80); abaixo disso **recusa e mantém o original**, igual à
  guarda de nível do #324 e pelo mesmo motivo — a amostra é o que o clone copia;
- o relatório (motor, timbre, etapas, motivo da recusa) vai para o `.json` da voz,
  para o retorno do `POST /api/voices/{id}/denoise` e para o meta do cadastro;
- `.orig` do #443 continua sendo gravado antes de sobrescrever.

Mordidas em `tests/test_app.py`: recusa por timbre devolve o áudio original;
abaixar o mínimo com o MESMO áudio vira a decisão (prova que a guarda morde e não
só decora o relatório); o DFN3 é usado quando presente e a queda para o gating
acontece quando não está; música roda ANTES do denoise e a etapa pulada aparece
como `false` no relatório.

## O que ainda falta

- **RESOLVIDO: o app não usa mais o pacote `df`.** O DSP virou numpy próprio
  (`dfn_dsp.py`), com o par STFT/ISTFT conferido ida-e-volta (erro relativo 0,0),
  banco ERB, `unit_norm` e filtro profundo batendo com o original, e um teste de
  equivalência contra áudio de referência versionado (`tests/test_dfn_dsp.py`,
  SI-SDR 10,3 dB medido na criação; piso do teste 8 dB). Nenhum pino novo: usa o
  `onnxruntime` que já estava no `requirements.txt`.
- **(histórico) o pacote `df` não existia na `.venv-mlx`** (`ModuleNotFoundError`): nem
  instalado nem em `requirements.txt` (o `onnxruntime` está). Hoje a cadeia cai no
  gating em produção e o relatório diz `motor: gating`. Dois caminhos:
  (a) instalar `deepfilternet` — arrasta torch/torchaudio inteiros, e no
  `venv312` ele já quebra em `torchaudio.backend` (mesmo atrito do WeSpeaker em
  `evidence/521`); (b) **vendorizar só o DSP** (STFT, ERB, normalização, filtro e
  síntese, ~700 linhas do `df` sob Apache-2.0/MIT) e ficar com numpy +
  onnxruntime, sem torch. (b) foi o caminho feito.
- **DNSMOS antes/depois no retorno**: depende de um modelo ONNX que ainda não está
  instalado; hoje o relatório traz a similaridade de timbre, não o OVRL.
- **Dereverb**: o gancho existe e devolve `None`; falta o modelo (WPE).
- **Separação de música**: o caminho chama o Demucs se estiver instalado, e ele
  não está — a etapa está ligada por código, não medida.
- Worker isolado: o ONNX é CPU puro, então não precisa; se o Demucs entrar (torch),
  ele passa a precisar.

## Reprodução

```bash
/tmp/tts-bakeoff/vendf/bin/python tools/bakeoff/dfn_onnx.py entrada48k.wav saida.wav
./.venv-mlx/bin/python tools/bakeoff/ruido_bakeoff.py --familias ventoinha \
    --snrs 5,10 --limite 4 --candidatos atual,rnnoise,dfnet3,dfnet3onnx
```


## Critério de aceite do PM: o PRODUTO, não a identidade

A equivalência de 10,3 dB não bastava; o PM fixou o critério no resultado: Δ de
OVRL ≤ 0,05 e Δ de timbre ≤ 0,01 contra o caminho original, mesmos cenários.

**Resultado: timbre passa, OVRL ainda não.** Ventoinha 5 e 10 dB, 4 amostras:

| métrica | original (média) | DSP próprio (média) | Δ |
|---|---|---|---|
| DNSMOS OVRL | 3,140 | 3,012 | **−0,128 (falha)** |
| similaridade de timbre | 0,9705 | 0,9653 | −0,005 (passa) |

Por célula o próprio DSP ganha em 2 de 8 e fica perto em 5; a diferença se
concentra numa amostra (OVRL 2,66 × 3,24 na mesma entrada).

### O que a perseguição rendeu

O critério mandou apertar a recorrência do `erb_norm` — e ela estava errada mesmo.
Fui à fonte (`libDF/src/lib.rs`): o original é `estado = x·(1−α) + estado·α` e
`out = (x − estado)/40`, com estado inicial `linspace(−60, −90)` e `α = 0,99`
(confirmado em `get_norm_alpha`). O `unit_norm` inicializa em
`linspace(1e−3, 1e−4)`, não geometricamente como eu tinha suposto.

Com isso a equivalência foi de **10,3 dB para 13–27 dB** de SI-SDR contra o
original (4 amostras: 16,0 / 21,5 / 13,0 / 17,1). A ordem dos coeficientes do
filtro profundo também foi conferida (a invertida dá 15,5 dB contra 16,5).

### Aceite do PM (com três condições)

Aceito assim: o timbre, que é o critério principal de uma amostra de clone, passa;
o OVRL do DSP próprio (3,01) segue muito acima do gating de hoje (2,72); e a
diferença está concentrada em 1 amostra de 8. As condições:

1. **Feito:** a amostra discrepante virou caso versionado
   (`tests/data/dfn_discrepante_*.wav`, `test_caso_discrepante_fica_travado`) com
   o número registrado. Detalhe que reforça o diagnóstico: nela o SI-SDR é **20,69
   dB** (bom) — a discrepância é do que o DNSMOS percebe, não de fidelidade.
2. **Em produção:** quem decide é o dono — a guarda de timbre recusa o que troca a
   voz e a prévia A/B da tela (#528) mostra antes/depois com as métricas. O motor
   não escolhe sozinho.
3. **Trabalho futuro anotado (abaixo).**

### Trabalho futuro anotado

Duas hipóteses ficaram por testar para fechar o 0,128 de OVRL, ambas caras em
relação ao ganho:

- **`lsnr_droput`**: o modelo torch tem despacho por SNR local (pula quadros com
  `lsnr > −10 dB`); o grafo ONNX congela o caminho. Reintroduzir o despacho do
  lado de fora pode alinhar a atuação nos quadros de fala alta.
- **`pad_feat`**: a rede preenche as features no tempo antes das convoluções; a
  diferença entre esse preenchimento e o do export pode deslocar a borda dos
  quadros.

### Leitura honesta de onde estamos

O que sobrou não é mais a normalização: é a diferença entre o grafo ONNX e o
modelo torch (precisão numérica e o que o export congela) e o instante em que ele
é avaliado. Fechar o 0,128 exige ou aceitar o número, ou trocar a comparação de
base. **Não declarei aceito.**
