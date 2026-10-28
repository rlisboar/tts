# Bake-off de identidade de voz — PARCIAL (#521)

Estado: **em andamento**. Este arquivo existe para o número já medido não ficar
só no terminal; a versão final sai com todos os candidatos, AS-norm, cadastro
1×3×5, cruzamento de canal e o limiar calibrado.

## O que já está medido (LibriSpeech dev-clean, 20 locutores, CPU do M3)

Protocolo: cadastro k ∈ {1,5} amostras (centróide), teste no restante, recortes de
1/2/3/8 s, escore cosseno. Corpus em `/tmp/tts-bakeoff` com manifesto sha256.

| modelo | EER 1 s (k=1) | EER 1 s (k=5) | EER 8 s (k=5) | FAR de clone @FAR1% (cadastro 3 s) |
|---|---|---|---|---|
| Resemblyzer (hoje no app) | 10,2 % | **3,5 %** | 2,1 % | 4,2 % |
| WeSpeaker ResNet34 | 24,1 % | 14,4 % | 47,4 % | 8,3 % |
| WeSpeaker ResNet221 | 31,4 % | a medir | a medir | a medir |

## Achado que precisa de investigação antes de concluir

**O WeSpeaker piora quando o trecho é MAIOR** (14 % em 1 s → 47 % em 8 s), enquanto
o Resemblyzer melhora (10 % → 2 %). Piora com duração e escore médio de clone
(0,394) abaixo do impostor (0,425) são sintoma de pré-processamento errado, não de
modelo ruim.

Testado sem efeito: CMVN por utterance (idêntico), `dither` 0/1, `snip_edges`
true/false, janela deslizante de 100–400 frames (melhor 300, ainda 16 %).
Testado com efeito: escala de entrada — ×32768 (14,6 %) contra ×1 (49 %), então a
escala está certa.

Suspeita principal: a exportação ONNX (input `feats` (B,T,80)) espera estatística
de normalização do treino, não por-utterance. Próximo passo é rodar o **mesmo
corpus** pelo pipeline oficial do WeSpeaker (speakerlab) e comparar; se o número
oficial for bom com o mesmo ONNX, o defeito é meu, não do modelo.

## Ressalvas

- Ruído é sintético (ventoinha/babble/música gerados em `tools/bakeoff/ruido.py`),
  não a ventoinha do dono.
- EER com 20 locutores é ruidoso; a versão final usa o conjunto maior.
- Corpus é leitura (LibriSpeech); fala espontânea tende a ser mais difícil.

## Reprodução

```bash
./.venv-mlx/bin/python tools/bakeoff/corpus.py publico && ./.venv-mlx/bin/python tools/bakeoff/corpus.py monta
./.venv-mlx/bin/python tools/bakeoff/clone.py 8
/tmp/tts-bakeoff/venv/bin/python tools/bakeoff/identidade.py --modelos resemblyzer,wespeaker34 --k 1,5 --locutores 20 --clones
```
