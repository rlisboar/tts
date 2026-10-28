# Bake-off de redução de ruído (#520) — PARCIAL

Estado: **em andamento**. Corpus já montado (o mesmo do #521: LibriSpeech +
sintético, 9 famílias de ruído, SNR 0/5/10/20, manifesto sha256 em tmp).
Rodada curta de validação do instrumento: 4 arquivos × 2 cenários.

## Números (DNSMOS OVRL em escala 1–5, timbre = similaridade do locutor antes/depois)

| cenário | candidato | OVRL | BAK | timbre | RTF |
|---|---|---|---|---|---|
| ventoinha 5 dB | atual (gating espectral) | 3,18 | 4,00 | 0,818 | **0,002** |
| ventoinha 5 dB | RNNoise (arnndn) | 3,26 | 4,00 | **0,937** | 0,016 |
| ventoinha 5 dB | DeepFilterNet3 | **3,33** | **4,13** | 0,907 | 0,279 |
| babble 5 dB | atual | 3,23 | 4,03 | 0,829 | **0,002** |
| babble 5 dB | RNNoise | 3,23 | 3,95 | **0,946** | 0,017 |
| babble 5 dB | DeepFilterNet3 | **3,39** | **4,14** | 0,913 | 0,299 |

Leitura provisória: o DFN3 limpa mais (OVRL e BAK maiores nos dois cenários) e o
RNNoise **preserva mais o timbre** — que é o que a amostra de referência precisa,
porque o clone copia dela. O gating atual perde nos dois eixos e só ganha em custo.

RTF do DFN3 (0,28) ainda cabe no Live — uma janela de 10 ms custaria ~2,8 ms — mas
é 17× o RNNoise; em trecho longo e máquina carregada isso pesa.

## O que falta para fechar

- PESQ e STOI (precisam de referência alinhada) e **WER do Whisper** (o runner já
  tem o gancho, não rodou ainda).
- SNR 0/10/20 e as famílias que faltam (teclado, música, ar-condicionado, rua).
- Demucs no caso de import com música (cadeia separação → melhor denoiser).
- Custo no VAD/barge com `smoke_live_turns.py` sob ventoinha.
- Os quatro casos de recomendação (amostra / Live VAD / Live STT / import).

## Ressalvas

- Ruído sintético; a ventoinha do dono não foi gravada.
- DNSMOS em fala sintética pode ser generoso: o corpus mistura LibriSpeech e TTS.
- Amostra pequena (4 arquivos) — a rodada final usa o conjunto inteiro.

## Reprodução

```bash
./.venv-mlx/bin/python tools/bakeoff/corpus.py publico && ./.venv-mlx/bin/python tools/bakeoff/corpus.py monta
./.venv-mlx/bin/python tools/bakeoff/ruido_bakeoff.py --familias ventoinha,babble --snrs 5 --limite 4
```
