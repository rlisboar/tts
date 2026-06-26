# TTS-Rod

Clonagem de voz 100% local para Mac (Apple Silicon). Grava sua voz pelo navegador,
gerencia perfis de voz e gera fala natural com o **OmniVoice (Xiaomi/k2-fsa)
quantizado e rodando via MLX** — zero-shot, 646 idiomas, mais rápido que tempo real no M3.

Nenhum áudio ou texto sai da máquina.

## Requisitos

- macOS com Apple Silicon (testado em M3, 16 GB)
- Python 3.12 (`brew install python@3.12` se não tiver)
- ~3 GB livres em disco (modelo + dependências)

## Uso

```bash
./run.sh
```

Abra <http://127.0.0.1:7860> no navegador. O servidor escuta em `0.0.0.0`:
outros dispositivos da rede acessam por `http://Mac-mini.local:7860` (Apple)
ou pelo IP do Mac (ex.: `http://192.168.15.177:7860`).

Toda a API (`/api/*` e `/v1/*`) exige chave. O `run.sh` gera uma na primeira
execução, salva em `.apikey` e imprime no terminal. A UI pede a chave uma vez
e guarda no navegador. Aceita `Authorization: Bearer`, `X-API-Key` ou `?api_key=`.

1. **Gravar voz** — 10–30 s de fala limpa. Opcional: informe a transcrição da
   amostra (`ref_text`) para clonagem mais estável.
2. **Gerar fala** — digite o texto, escolha a voz e o idioma (ou deixe em Auto).
   A primeira geração baixa e monta o modelo (~2 GB); depois fica em cache.
3. **Histórico** — ouça, baixe (WAV) ou apague os áudios gerados.

## Estrutura

| Caminho             | Conteúdo                                    |
|---------------------|---------------------------------------------|
| `app.py`            | Servidor FastAPI (API + síntese MLX)        |
| `static/index.html` | Interface web (gravação e gerenciamento)    |
| `voices/`           | Amostras de voz gravadas (`.wav` + `.json`) |
| `outputs/`          | Áudios gerados                              |
| `.venv-mlx/`        | Ambiente Python (MLX)                       |
| `.omnivoice-bf16/`  | Modelo montado (symlinks p/ o cache do HF)  |

## Configurações (dashboard ⚙️)

Card "Configurações padrão" na UI: modelo, idioma (Auto = detecta do texto),
voz padrão da API, pré-prompt, tamanho de trecho, velocidade e os **controles do
OmniVoice** (passos, aderência, variações, voice design, duração). Persiste em
`settings.json` e **vale para UI e API** — parâmetro explícito na requisição
sempre sobrepõe. Programaticamente: `GET/POST /api/settings`.

## Modelo

OmniVoice (masked-diffusion não-autoregressivo, ~0,6 B, Apache-2.0, sem
watermark). No M3 16 GB: RTF ~0,8 (bf16, ref cacheada), ~3 GB de RAM.

As conversões MLX publicadas vêm quebradas (o repo `-bf16` perde o encoder
semântico do tokenizer; o `-4bit` não quantiza no `load_model`). O app conserta
sozinho na primeira carga: baixa o backbone bf16 e junta o `audio_tokenizer`
completo do repo sem sufixo num dir `.omnivoice-bf16/` (symlinks para o cache do
Hugging Face; ~2 GB no total). Sobrepor os repositórios:

```bash
TTS_ROD_OMNI_BACKBONE=mlx-community/OmniVoice-bf16 \
TTS_ROD_OMNI_TOKENIZER=mlx-community/OmniVoice ./run.sh
```

Para usar um id/dir MLX de OmniVoice já pronto, defina `TTS_ROD_MODEL` (ou o
campo "modelo" no dashboard).

### Controles de geração

| Controle | Faixa | Default | Efeito |
|---|---|---|---|
| `num_steps` | 4–64 | 16 | passos de unmasking; ↑ qualidade, ↓ velocidade |
| `guidance_scale` | 0–10 | 2.0 | aderência ao texto/voz de referência |
| `class_temperature` | 0–2 | 0.0 | variação de token (0 = estável) |
| `position_temperature` | 0–20 | 5.0 | variação da posição revelada |
| `layer_penalty_factor` | 0–20 | 5.0 | penalidade por camada de codebook |
| `t_shift` | 0–1 | 0.1 | deslocamento do cronograma de difusão |
| `instruct` | texto | "" | voice design (ex.: "female, low pitch") |
| `duration_s` | 0.5–60 / auto | auto | força duração fixa |
| `omni_ref_max_s` | 3–30 | 10 | quanto da amostra de referência usar |

Todos os parâmetros de geração do OmniVoice são expostos. `lang_code` é coberto por
`language`; `ref_audio` é substituído por `ref_tokens` cacheados (clonagem mais rápida).

### Vozes padrão (voice design)

O OmniVoice cria vozes a partir de uma descrição (`instruct`), sem gravação. O app
traz seis vozes padrão prontas (Narrador, Locutora, Jovem masc./fem., Formal,
Podcast). Escolha uma na lista de vozes e gere — na primeira vez o app cria uma
amostra-semente e a salva como voz normal (ancorando o timbre para ficar consistente
entre os trechos); "resetar" recria essa amostra. Para uma voz sob medida, use o campo
**voice design** (`instruct`) com a sua própria descrição.

### Idioma

Aceita o **OmniVoice ID** (código, ex.: `pt`, `en`, `es`, `fr`, `de`, `it`) ou
`auto` (detecta do texto — recomendado). Nomes em pt/inglês também são aceitos e
mapeados para o código (`português`/`portuguese` → `pt`). Lista completa de 646
idiomas no repositório do OmniVoice.

## API compatível com OpenAI

`POST /v1/audio/speech` — mesmo contrato da OpenAI; funciona com o SDK oficial
e com clientes xAI/Grok apontando o `base_url` para o servidor local.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:7860/v1", api_key="<chave do .apikey>")
resp = client.audio.speech.create(
    model="tts-1",            # tts-1-hd = mais passos de difusão (qualidade)
    voice="Rodrigo",          # nome ou id de uma voz gravada na UI
    input="Olá, mundo!",
    response_format="mp3",    # mp3 | wav | flac | aac | opus | pcm
    speed=1.0,                # 0.25–4.0
)
resp.write_to_file("fala.mp3")
```

Campos extras fora do padrão OpenAI (opcionais): `language` (idioma) e os
controles do OmniVoice (`num_steps`, `guidance_scale`, `instruct`, etc.) — cada
um sobrepõe o default do dashboard só naquela requisição.

```bash
curl -s http://127.0.0.1:7860/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","voice":"Rodrigo","input":"Olá!","response_format":"mp3"}' \
  -o fala.mp3
```

- `voice` desconhecida (ex.: `alloy`) cai na voz gravada mais recente.
- Autenticação: use a chave do `.apikey` como `api_key` do SDK.
- Conversão de formato/velocidade usa `ffmpeg` (`brew install ffmpeg`).
- `GET /v1/models` lista `tts-1` e `tts-1-hd`.

## Dicas de qualidade

- Quanto mais limpa a gravação (sem eco, sem ruído), mais parecida a voz clonada.
- Frases curtas (1–3 sentenças) por geração soam mais naturais; textos longos são
  divididos automaticamente em trechos.
- Informar a transcrição da amostra (`ref_text`) estabiliza a clonagem e evita a
  auto-transcrição por Whisper na primeira geração.

## Privacidade e uso responsável

O pipeline MLX **não embute marca-d'água** nos áudios gerados. Use apenas com a
sua própria voz ou com consentimento explícito da pessoa clonada.
