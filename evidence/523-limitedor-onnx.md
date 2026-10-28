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

## O que fica para a próxima etapa do card

A medição está feita; falta o **fio no app**, que é a outra metade do #523:

- cadeia `separa música → denoise` com etapas opcionais por configuração;
- guarda de timbre antes/depois (mesmo limiar do R1) recusando e mantendo o
  original;
- DNSMOS antes/depois e similaridade no retorno;
- worker isolado (o modelo é pesado) e o `.orig` do #443 preservado.

O `dfn_onnx.py` roda hoje na venv de bancada (`vendf`). Para o app falta conferir
o import do pacote `df` na `.venv-mlx` — no `venv312` ele quebra em
`torchaudio.backend`, que sumiu nas versões novas; é o mesmo atrito já registrado
no WeSpeaker em `evidence/521`.

## Reprodução

```bash
/tmp/tts-bakeoff/vendf/bin/python tools/bakeoff/dfn_onnx.py entrada48k.wav saida.wav
./.venv-mlx/bin/python tools/bakeoff/ruido_bakeoff.py --familias ventoinha \
    --snrs 5,10 --limite 4 --candidatos atual,rnnoise,dfnet3,dfnet3onnx
```
