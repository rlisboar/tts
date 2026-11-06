# #869 — Live descarta turno com fala real (`motivo=vad` / "sem fala")

Mini em `9b0230c`, log `/tmp/tts-studio.log` (sessões 00:34–01:05 de 2026-11-06),
STT local Whisper (remoto desligado), `TTS_LIVE_RNNOISE` no default (ligado).

## 1. Causa achada e reproduzida: a híbrida do #481 NUNCA entregou o cru ao STT

`_live_audio_cru` recortava o ring cru a partir de `ev.amostra`. No `speech_end`
o motor carimba o FIM do turno em `amostra` (`TurnEngine._fechar_turno`: `fim =
_turno_amostra + corte*FRAME`), não o início. O recorte começava DEPOIS da fala,
passava do fim do ring, o tamanho não batia e a função devolvia o áudio do motor
— o **filtrado pelo RNNoise**. Resultado: todo turno de produção foi ao Whisper
com o PCM filtrado, exatamente o caminho que o #442 mediu com ~50 % de WER.

Reproduzido com `evidence/869-repro-cru.py` (caminho real do WS: ring cru →
RNNoise real → `TurnEngine.feed` → `_live_audio_cru`), fala real e fixture:

| antes | depois |
| --- | --- |
| fonte do STT = `motor(filtrado)` em 100 % dos turnos | `ring@amostra` (cru) em 100 % |

Segundo defeito no mesmo recorte: o motor conta amostras do FILTRADO, que anda
**1776 amostras (111 ms)** atrás do cru (medido no ffmpeg real, envelope, frames
de 512 e de 1600 — mesmo valor). Sem descontar, o cru sairia 111 ms atrasado
(perde o ataque da 1ª palavra). Agora `RnnoiseAoVivo.atraso_amostras` =
`ATRASO_AMOSTRAS + faltas/2` e o recorte desconta.

## 2. O que NÃO reproduzi: o `motivo=vad` em ~20 ms

O `vad` vem do Silero do `_transcribe` (threshold 0,6, mínimo 0,3 s) recusando o
áudio do turno. Com fala real (voz do dono, `voices/9f5265a9f1.wav`, cópia em
/tmp) de 0,5/1,2/1,8 s, em −20/−35/−45 dBFS, piso −60/−45 dBFS, com e sem RNNoise,
TODOS os turnos que o motor fecha passam no Silero (tabela no script). Fixture
sintética idem. O que difere da bancada é a captura do navegador (EC + NS do
Chrome ligados, AGC desligado). Sem o áudio real não há como fechar o motivo
"por medição".

Instrumento novo, opt-in: `TTS_LIVE_DUMP_DIR=/caminho` grava, por turno, o PCM
que o STT recebeu (`.wav`) e o veredito (`.json`: ms, dBFS, motivo, detalhe,
texto). Desligado por padrão (o log continua só com metadados).

## 3. Medir antes/depois (10 falas reais)

Pendente de sessão do dono no Mini com o dump ligado — ver o card.
