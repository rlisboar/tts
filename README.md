# TTS-STUDIO

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
ou pelo IP do Mac (ex.: `http://192.168.1.20:7860`).

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
| `backends.py`       | Catálogo de backends TTS + adapter unificado|
| `tts_worker.py`     | Worker isolado (crash do Metal não derruba) |
| `common.py`         | Texto/DSP/modelo compartilhados app↔worker  |
| `static/index.html` | Interface web (gravação e gerenciamento)    |
| `remote/`           | Servidores RTX opcionais (OmniVoice, Voxtral)|
| `client/`           | Roteador de microfone (BlackHole)           |
| `voices/`           | Amostras de voz gravadas (`.wav` + `.json`) |
| `outputs/`          | Áudios gerados                              |
| `.venv-mlx/`        | Ambiente Python (MLX)                       |
| `.omnivoice-bf16/`  | Modelo montado (symlinks p/ o cache do HF)  |

**Backup das vozes**: `voices/` é gitignored e contém as gravações (o dado mais
valioso do app). Na UI: **Vozes → Vozes salvas → ⬇ Backup** (zip com `.wav` +
`.json`), ou programaticamente `GET /api/voices/export`. Para restaurar em
outra máquina (ou depois de um apagão): **⬆ Importar** selecionando o zip
(`POST /api/voices/import` — sobrescreve vozes com o mesmo id).

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
campo "modelo" no dashboard). Se um vídeo do YouTube exigir login
("sign in to confirm"), exporte cookies do navegador (formato Netscape) e
aponte `TTS_ROD_YT_COOKIES=/caminho/cookies.txt` antes do `./run.sh`.

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
    voice="Minha voz",        # nome ou id de uma voz gravada na UI
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
  -d '{"model":"tts-1","voice":"Minha voz","input":"Olá!","response_format":"mp3"}' \
  -o fala.mp3
```

- `voice` desconhecida (ex.: `alloy`) cai na voz gravada mais recente.
- Autenticação: use a chave do `.apikey` como `api_key` do SDK.
- Conversão de formato/velocidade usa `ffmpeg` (`brew install ffmpeg`).
- `GET /v1/models` lista `tts-1` e `tts-1-hd`.

## Acesso pela internet (VPS como proxy)

Dá para consumir a API de fora de casa mantendo o processamento no Mac: um
**túnel reverso SSH** faz o Mac se conectar PRA FORA à VPS (nada de abrir
portas no roteador) e a VPS publica o serviço com TLS via nginx. O modelo
recomendado é **proxy por path** num domínio que a VPS já atende (sem DNS
novo, sem abrir porta no cloud):

```
internet ─▶ https://seu-dominio/ttsproxy/... (nginx na VPS)
         ─▶ túnel SSH (Mac conecta pra fora) ─▶ IP-LAN-do-Mac:7860
```

> **Atenção à autenticação:** a API dispensa chave no loopback (`127.0.0.1`).
> Por isso o túnel aponta para o **IP de LAN do Mac** e não para `127.0.0.1`
> — via loopback, a internet entraria **sem chave**. Aponte o túnel pelo
> `tunnel.sh` (ele resolve o IP da LAN sozinho) e mantenha as chaves ativas.

### No app (já pronto)

O servidor remove o prefixo `/ttsproxy` (configurável via
`TTS_ROD_BASE_PATH`) antes do roteamento, e a UI detecta o prefixo pela URL
e prefixa as próprias chamadas — o mesmo binário atende LAN e internet.

### Na VPS (uma vez)

1. Chave do túnel (a linha exata é impressa por `./tunnel.sh install`):

```sh
echo 'command="",no-pty,no-agent-forwarding,no-X11-forwarding,permitlisten="127.0.0.1:7860",permitopen="127.0.0.1:7860" ssh-ed25519 AAAA... tts-tunnel' \
  >> ~/.ssh/authorized_keys
```

> Não use `restrict` nessa linha: em algumas versões do OpenSSH ele desliga
> o forward por completo ("Server has disabled port forwarding"), mesmo com
> `permitlisten`. O conjunto `no-*` acima endurece o mesmo ponto.

2. Dentro do bloco `server` 443 do domínio, um `location` (use `^~` para o
   prefixo vencer as regex de assets):

```nginx
location = /ttsproxy { return 301 /ttsproxy/; }
location ^~ /ttsproxy/ {
    proxy_pass http://127.0.0.1:7860;   # sem barra no fim: o app tira o prefixo
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 600s;            # /v1/audio/speech pode levar minutos
    proxy_send_timeout 600s;
    proxy_buffering off;                # streaming dos trechos de áudio
    client_max_body_size 600m;          # import de vozes (até 512 MB)
}
```

3. **Se o nginx usar `sites-enabled` com arquivos físicos** (não symlinks),
   edite o arquivo de lá — e não deixe `.bak` dentro de `sites-enabled`
   (o include `*` carrega duplicatas e o `nginx -t` falha).

### No Mac

```sh
./tunnel.sh install ubuntu@ip-da-vps   # gera chave + LaunchAgent (sobe no login)
# ou apenas em foreground:  ./tunnel.sh ubuntu@ip-da-vps
```

Teste: `https://seu-dominio/ttsproxy/health` → `{"ok":true}`. A UI abre em
`/ttsproxy/` (a chave da API é pedida uma vez) e o zip do mic-router baixado
pelo proxy já sai com `server_url` incluindo o prefixo. Clientes OpenAI:
`base_url = https://seu-dominio/ttsproxy/v1`. Variante com subdomínio
próprio (server block dedicado + `location /`) também funciona, sem o
prefixo. `TTS_TUNNEL_IF=enX` sobrescreve a interface de rede detectada;
`./tunnel.sh uninstall` remove o agente.

## Conversa (decidir o texto com IA)

Sessões de conversa para decidir, com IA, o texto que um agente vai falar.
Provedor OpenAI-compat configurável nas settings (`chat_base_url`,
`chat_model`, `chat_api_key` — vazio herda `remote_base_url`).

```sh
# 1) abre a sessão com o objetivo
SID=$(curl -s -X POST $BASE/api/chat/start -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"objective":"fala de abertura do episódio 5","context":"tom informal"}' | jq -r .session_id)

# 2) cada fala do humano (transcrição do STT) vira um POST
curl -s -X POST $BASE/api/chat/$SID -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{"message":"pode mandar!"}'
# → {"reply":"...","status":"chatting"|"confirmed","text":"..."}

# 3) status "confirmed" traz o texto final aprovado em "text"
# GET /api/chat/$SID (estado/histórico) · DELETE encerra a sessão
```

Sessões expiram em 1h sem uso; o LLM só conversa (não executa nada) — a
confirmação explícita do humano é o gatilho do `text` final.

## Dicas de qualidade

- Quanto mais limpa a gravação (sem eco, sem ruído), mais parecida a voz clonada.
- Frases curtas (1–3 sentenças) por geração soam mais naturais; textos longos são
  divididos automaticamente em trechos.
- Informar a transcrição da amostra (`ref_text`) estabiliza a clonagem e evita a
  auto-transcrição por Whisper na primeira geração.

## Privacidade e uso responsável

O pipeline MLX **não embute marca-d'água** nos áudios gerados. Use apenas com a
sua própria voz ou com consentimento explícito da pessoa clonada.

## Desenvolvimento

```bash
# testes (funções puras + API via TestClient, sem carregar MLX)
./.venv-mlx/bin/python -m pytest tests/ -q

# análise estática — nomes indefinidos em funções só explodem em runtime
./.venv-mlx/bin/python -m pyflakes app.py common.py tts_worker.py backends.py \
    tests/*.py client/mic_router.py

# smoke test do worker isolado (lento ~1 min, carrega Kokoro real)
TTS_TEST_WORKER=1 ./.venv-mlx/bin/python -m pytest tests/test_worker.py -q
```

Pre-commit opcional (pyflakes + pytest antes de cada commit):

```bash
git config core.hooksPath .githooks
```
