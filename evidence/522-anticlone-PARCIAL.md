# Anti-clone (#522) — primeiro corte: marca d'água AudioSeal

Estado: **começando**. O que já está provado e o que falta.

## Verificado (AudioSeal, MIT, local, CPU)

- Marca inserida e detectada ponta a ponta: **probabilidade 1,00** no áudio marcado e
  **0,00** no áudio cru do mesmo trecho. Sem falso positivo no limpo.
- **Latência com modelo quente** (trecho de 4 s): embed **72–82 ms**, detecção
  **31–33 ms**. O requisito do barge é ≤50 ms por segundo de áudio — a detecção
  cabe com folga (8 ms/s); o embed é offline por natureza (só a saída do TTS).
- Primeira chamada custa ~3 s (compilação): precisa de aquecimento, como o
  classificador de intenção.

## Armadilha de API registrada

O detector **não** tem `forward_detection` (a documentação sugere); o caminho é
`detector.detect_watermark(audio, sr)` dentro de `torch.no_grad()`, que devolve
`(probabilidade, mensagem)`.

## O que falta para fechar

- Inaudibilidade: PESQ/ViSQOL com e sem marca, e ABX curto.
- Robustez da detecção depois de mp3/opus 32k, resample, ganho, corte e
  **regravação alto-falante→mic** a 0,5–2 m (o caso do eco do Live).
- Falso positivo em fala humana gravada (o negativo do detector).
- AASIST: taxa de detecção dos clones do app SEM marca e falso positivo em humano.
- Custo de rodar a detecção no barge do Live (além da latência bruta).
- Recomendação de default (marca ligada por padrão?), que é do dono.

## Licenças

AudioSeal: MIT (código e pesos). AASIST: MIT. Nada sai da máquina: os dois rodam
local. **Nenhum pino novo** — bancada em `/tmp/tts-bakeoff/venv312`.

## Reprodução

```bash
/tmp/tts-bakeoff/venv312/bin/python - <<'PY'
import torch, soundfile as sf
from audioseal import AudioSeal
g = AudioSeal.load_generator("audioseal_wm_16bits")
d = AudioSeal.load_detector("audioseal_detector_16bits")
x, sr = sf.read("qualquer.wav", dtype="float32")
t = torch.from_numpy(x)[None, None]
marcado = t + g.get_watermark(t, sr)
with torch.no_grad(): print(d.detect_watermark(marcado, sr)[0])
PY
```
