# TTS-STUDIO

Clonagem de voz 100% local para Mac (Apple Silicon). Grava sua voz pelo navegador,
gerencia perfis de voz e gera fala natural com o **OmniVoice (Xiaomi/k2-fsa)
quantizado e rodando via MLX** — zero-shot, 646 idiomas, mais rápido que tempo real no M3.

Roda inteiro no Mac. **Nada do seu áudio sai da máquina** e o texto só sai pelo
que você ligar em Configurações → Rede — ou, na Conversa e no Live, se você
escolher a IA pelo harness `dsh`, cuja rota é a que estiver configurada nele
(ver *O que pode sair da máquina*).

## Requisitos

- macOS com Apple Silicon (testado em M3, 16 GB)
- Python 3.12 (`brew install python@3.12` se não tiver)
- ~3 GB livres em disco (modelo + dependências)

## Uso

```bash
./run.sh
```

Abra <http://127.0.0.1:7860> no navegador. O servidor escuta em `0.0.0.0`:
outros dispositivos da rede acessam por `http://NomeDoMac.local:7860` (Apple)
ou pelo IP do Mac (ex.: `http://<ip-do-mac>:7860`).

Toda a API (`/api/*` e `/v1/*`) exige chave. O `run.sh` gera uma na primeira
execução, salva em `.apikey` e imprime no terminal. A UI pede a chave uma vez
e guarda no navegador. Aceita `Authorization: Bearer` ou `X-API-Key`.

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
(`POST /api/voices/import` — sobrescreve vozes com o mesmo id). O import **alinha o
`id` do meta ao nome do arquivo** e devolve em `renomeados` o que mudou (um id com
`<img …>` ou espaço dava 404 em áudio/peaks, porque o `_safe_id` das rotas é
estrito). Voz editada à MÃO em `voices/*.json` não passa por esse alinhamento:
com id fora de `[A-Za-z0-9_-]` ela lista e gera normalmente, mas áudio/peaks
respondem 404 — a UI mostra a voz sem waveform. Vale para o mesmo caso: voz cujo
arquivo é SYMLINK apontando para fora de `voices/` também é tratada como
desconhecida (a verificação segue o arquivo resolvido; o id acaba caindo no
padrão).

## Administração x uso (chave de uso)

Administrar = gerenciar chaves (`/api/apikeys*`), mexer nas **conexões externas**
do `/api/settings` (`remote_*`, `chat_*`, `remote_api_key`, `chat_api_key`) e
trocar de **modelo** (`model`, `translate_model`, `stt_whisper_repo`). O resto —
gerar fala, voz, transcrição, ajustes de qualidade — é uso e qualquer chave faz.

Quem é admin: **loopback** (o Mac), a chave de `TTS_ROD_ADMIN_KEY` e chaves
marcadas com `role: "admin"`. Duas formas de separar:

```sh
# 1) credencial do operador na env (autentica E administra; não vai para o git)
TTS_ROD_ADMIN_KEY=$(openssl rand -hex 24) ./run.sh
```

```sh
# 2) por chave, na UI (Acesso) ou na API — não precisa de env nenhuma
curl -H "X-API-Key: $ADMIN" -H 'Content-Type: application/json' \
     -d '{"name":"tablet","role":"use"}' http://127.0.0.1:7860/api/apikeys
```

Uma chave `role: "use"` gera fala normalmente, mas: não cria/apaga chaves
(`403`), não lê os segredos em `GET /api/settings` (vêm mascarados como
`••••abcd`) e tem os campos administrativos ignorados no `POST /api/settings`
(a resposta lista o que foi ignorado em `admin_ignored`). `PATCH
/api/apikeys/{id}` troca o `role` depois.

**Migração/compatibilidade**: chave em `role: null` (criada antes deste campo) e
instalação **sem** `TTS_ROD_ADMIN_KEY` continuam exatamente como antes — toda
chave válida administra, e `GET /api/settings` continua devolvendo os segredos
em claro para ela. Nada muda até você usar uma das duas formas acima.

Única exceção, e é conserto: se você **já** subia o app com `TTS_ROD_ADMIN_KEY`,
essa chave antes não autenticava (401: ela não era chave de API) e a chave
comum não era admin (403) — administrar pela rede só funcionava com as duas na
mão. Agora a env admin autentica e administra, e uma instalação que tenha só
ela passa a exigir chave na rede (antes ficava aberta).

Recomendações: `TTS_ROD_ADMIN_KEY` é a credencial do operador (guarde fora do
repo); para os outros dispositivos, crie chaves `role: "use"`. Se as chaves
forem trocadas, rotacione também a env (o app lê no boot).

Pela UI (Configurações → Acesso → **Nova chave**) você escolhe o papel na
criação — o default é `uso`, que é a chave que se compartilha. A lista mostra o
papel de cada uma (`🔒 uso` / `🔑 admin`; chave antiga sem papel aparece como
*legado (administra)*), o botão `virar admin`/`virar uso` troca depois
(`PATCH /api/apikeys/{id}`), e adotar a chave nova pelo botão *"usar esta chave
neste navegador"* já rebaixa/sobe o privilégio da sessão na hora — o selo do
topo acompanha.

## Configurações (dashboard ⚙️)

Card "Configurações padrão" na UI: modelo, idioma (Auto = detecta do texto),
voz padrão da API, pré-prompt, tamanho de trecho, velocidade e os **controles do
OmniVoice** (passos, aderência, variações, voice design, duração). Persiste em
`settings.json` e **vale para UI e API** — parâmetro explícito na requisição
sempre sobrepõe. Programaticamente: `GET/POST /api/settings`.

Contrato do endpoint para quem integra: `GET` devolve as settings mais
`is_admin` (bool) e `admin_fields` (lista dos campos que só admin altera); sem
admin, os segredos vêm mascarados. `POST` aceita payload parcial, devolve
`is_admin` e `admin_ignored` (lista, vazia quando tudo foi aplicado) — não é
`403`: uma chave de uso que mandar o blob inteiro continua salvando o que é de
uso. Reenviar uma máscara (`••••abcd`) mantém o segredo guardado; `""` limpa.

**Na UI** isso aparece: um selo no topo das Configurações (`🔑 chave admin` x
`🔒 chave de uso`), os campos de conexão externa aparecem esmaecidos e travados
com o motivo no `title`, os segredos de terceiros chegam mascarados (o
`placeholder` avisa que aquele `••••1234` não é o valor) e a aba Acesso
desabilita criar/rotacionar/apagar chave. Salvar com chave de uso não mente: o
`POST` manda o blob inteiro, o servidor descarta o que não é daquela chave e
devolve a lista em `admin_ignored`, que a tela exibe com nome humano. Ação que
exige admin e leva `403` abre a faixa de chave com o passo a passo
(`Configurações → Acesso → Nova chave → papel admin`).

Para quem mexe no HTML: um campo administrativo precisa do marcador
`data-admin-setting="<nome da setting>"` — é ele que faz o travamento valer (o
servidor decide a lista, a UI cruza). Se o servidor promover um nome a admin sem
marcador, a UI avisa no console no load; os que ainda não têm campo na tela
(`translate_model`, `remote_tts_model`) ficam numa lista explícita para não
virar aviso eterno.

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

O `/api/youtube-audio` só aceita link cujo host seja `youtube.com`,
`youtube-nocookie.com` ou `youtu.be` — exato ou subdomínio de verdade (o ponto
conta: `evil-youtube.com` e `youtube.com.evil.com` não passam). Link curto
`youtu.be/...` é resolvido normalmente; se o yt-dlp for redirecionado para fora
dessa lista (ex.: `youtube.com/redirect?q=…`), o pedido volta 400.

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

- `voice` desconhecida (ex.: `alloy`) cai na voz gravada mais recente — vale
  também para id que escaparia de `voices/` (ex.: `"../fora"`, que antes
  apontava para um WAV de fora do diretório de vozes).
- Autenticação: use a chave do `.apikey` como `api_key` do SDK.
- Conversão de formato/velocidade usa `ffmpeg` (`brew install ffmpeg`).
- `GET /v1/models` lista `tts-1` e `tts-1-hd`.

### Limites de geração (429)

As rotas que geram (`/api/tts`, `/api/translate-speech`, `/api/modify-speech`,
`/v1/audio/speech`) aceitam até `TTS_JOBS_ACTIVE_MAX` jobs **em andamento**
(padrão 20). Acima disso a resposta é `429` com `Retry-After` e nada é criado: o
pedido recusado não sobe thread e nenhum job ativo é descartado. O botão
*Gerar* mostra o `detail` e é o usuário quem repete; a **Conversa** repete sozinha
uma vez (o `Retry-After` diz o tempo) porque lá uma frase recusada sai muda do
turno — ver a seção Conversa.

`POST /api/tts` aceita `model` no body e a resposta traz `model_aplicado_global`.
Com admin/loopback a escolha vale para as próximas gerações (é assim que o
seletor da UI aplica o modelo ao gerar); com **chave de uso** vale só naquele
pedido — a chave gera com o modelo pedido sem sequestrar a config da instalação
(`model` é campo admin). Nos dois casos, **só depois de o pedido passar** na
validação: `400` (texto vazio/longo), `404` (voz) ou `429` (fila) devolvem o erro
sem tocar em `settings.json` nem na memória.

O histórico guarda os 20 últimos jobs (`_JOBS_MAX`) e só descarta job já
**terminado** — descartar um job em execução apagaria os trechos `.job-*` e o
cliente perderia status e áudio no meio da fala. `GET /api/status` publica
`jobs_active`, `jobs_active_max` e `jobs_history_max` para acompanhar a ocupação.

Pela internet o `POST /api/tts` também passa pelo limitador de taxa
(`TTS_RATE_LIMIT` / `TTS_HEAVY_RATE_LIMIT`, `TTS_POLL_RATE_LIMIT`); loopback é
isento dele.

O limitador guarda uma janela de 60 s por (chave/IP × rota). Como a rota entra
**normalizada** (`/api/tts/jobs/<id>/pieces/0` → `/api/tts/jobs/*/pieces/*`),
pollar 500 jobs não cria 500 baldes; e o dicionário tem teto
(`TTS_RATE_MAX_BUCKETS`, padrão 10000): acima dele sai primeiro o que expirou e,
se ainda estiver cheio (rajada de chaves descartáveis na mesma janela), o balde
tocado há mais tempo. `GET /api/status` publica `rate_limit_buckets` e
`rate_limit_buckets_max`. O teto de cada rota continua saindo do path cru, então
`/api/voices/import` mantém o limite pesado.

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

Os servidores opcionais em `remote/` exigem chave: `OMNI_API_KEY` no servidor
OmniVoice e `VOXTRAL_API_KEY` no servidor Voxtral (o app envia por
`remote_api_key` / `remote_stt_key`). Sem a variável o processo **não sobe** —
antes ele subia em `0.0.0.0` sem avisar que estava aberto; para assumir o modo
legado (só atrás de firewall/VPN) exporte `OMNI_ALLOW_NO_AUTH=1` ou
`VOXTRAL_ALLOW_NO_AUTH=1`. `/health` não exige chave e publica o modo em
`auth`; a política mora em `remote/auth_policy.py`.

Inventário do deploy (host, serviço, portas, recon e comandos de subida):
[`remote/DEPLOY.md`](remote/DEPLOY.md).

## Acesso pela internet (Cloudflare Tunnel)

Alternativa à VPS: o conector `cloudflared` roda **na própria máquina do TTS**
e o Cloudflare publica o hostname com TLS. O ingress mora no dashboard
(Zero Trust → Networks → Tunnels) — modo **gerenciado remotamente**: o host de
produção guarda apenas o token, sem `config.yml` nem credenciais locais.

```
internet ─▶ https://tts.seu-dominio (Cloudflare, TLS) ─▶ tunnel
         ─▶ conector na máquina do TTS ─▶ IP-LAN-da-máquina:7860
```

> **Neste setup**: hostname `tts.the-dudes.com`, conector no Mac mini
> (`192.168.15.34`), cliente de navegador `claudinhos` (provedor "TTS-Rod").
> A CSP desse cliente precisa listar o hostname no `connect-src` — passo a
> passo no `docs/TTS-ROD.md` do repo dele.

> Mesma armadilha do túnel SSH: o destino é o **IP de LAN**, nunca `127.0.0.1`
> — pelo loopback a API dispensaria chave e a internet entraria sem autenticação.

Provisionar (numa máquina com `cloudflared tunnel login` já feito):

```sh
./cloudflare.sh provision tts-mac-mini tts.seu-dominio 192.168.15.34
```

Na máquina que roda o TTS (o comando `install` sai impresso pelo `provision`):

```sh
./cloudflare.sh install --token-file ~/.cloudflared/tts-mac-mini.token
./cloudflare.sh status | uninstall
```

O `provision` **não despeja o token no terminal**: ele grava o token em
`~/.cloudflared/<nome>.token` (0600) e imprime o `install --token-file` para
colar — fora do scrollback e do history. Se for outra máquina, copie o arquivo
antes (ex.: `scp`); o token cru (`./cloudflare.sh install <token>`) e o stdin
(`... | ./cloudflare.sh install -`) continuam aceitos — nesses dois o script grava
o token em `~/.cloudflared/<label>.token` (0600) e o plist aponta para o arquivo,
então o segredo não aparece no `ps` nem dentro do plist. `./cloudflare.sh token`
mostra o token quando você precisar copiá-lo na mão.

Teste: `https://tts.seu-dominio/health` → `{"ok":true}`; o mesmo host sem chave
em `/api/status` → `401` (chave exigida fora do loopback). Textos longos não
batem no limite de 100 s de primeiro byte do Cloudflare porque a UI usa
`/api/tts/jobs` (polling de trechos).

### Cliente de navegador (CORS e CSP)

A API responde CORS liberado para qualquer origem, **inclusive nas respostas de
erro** (401/429 saem com `access-control-allow-origin`, senão o navegador
mostra `TypeError: Failed to fetch` em vez do motivo). Quem chama de outra
origem precisa de:

- **base URL com esquema** (`https://tts.seu-dominio` — sem ele o cliente monta
  URL relativa);
- chave da API no header (`Authorization: Bearer` ou `X-API-Key`);
- **a origem liberada na CSP do cliente**: se a página que chama tiver
  `Content-Security-Policy` com `connect-src` em allowlist, a requisição é
  bloqueada *antes de sair* — nada aparece no log do servidor e o console
  mostra violação de CSP. A CSP viaja com o documento: depois de mudar a
  política, recarregue a aba.

Diagnóstico rápido: se o cliente falha com "Failed to fetch" e **nada** chega em
`/tmp/tts-studio.log`, o problema é antes da rede (CSP ou base errada); se chega
`401`, é chave.

## Conversa (decidir o texto com IA)

Sessões de conversa para decidir, com IA, o texto que um agente vai falar.
Provedor OpenAI-compat configurável nas settings (`chat_base_url`,
`chat_model`, `chat_api_key` — vazio herda `remote_base_url`).

### Backend alternativo: `dsh` (harness local via ACP)

Em vez do endpoint+chave, a IA da Conversa (e, com o DSH-2, a do Live) pode rodar
no harness **dsh** já instalado na máquina, por um perfil próprio sem tools
(`~/.dsh/profiles/tts-studio`). `chat_backend: "dsh"` liga o caminho; `"openai"`
(default) mantém o comportamento acima **intacto**. O processo é persistente e
recebe pre-warm, porque o boot custa 2–4 s e o 1º prompt de uma sessão paga
1,3–7 s sozinho (medido; ver task_129d2183).

Campos (todos admin): `chat_backend_live` (vazio = herda `chat_backend`; ver
abaixo), `chat_dsh_bin` (default `dsh`, resolvido no PATH),
`chat_dsh_profile` (default `tts-studio`), `chat_dsh_model` (par opaco JSON
`["rota","modelo"]`; default seguro `["dsflash","deepseek-flash-41"]`, porque a
rota default do catálogo fica sem chave e falha `-32603`) e `chat_dsh_effort`
(`off|low|high|max`, default `off`). Env equivalente, por campo:
`TTS_CHAT_BACKEND` / `TTS_CHAT_DSH_{BIN,PROFILE,MODEL,EFFORT}`.

**Conversa e Live podem usar backends DIFERENTES** (#176): `chat_backend` vale para
a Conversa e é a herança; `chat_backend_live` (env `TTS_CHAT_BACKEND_LIVE`) vale só
para o Live e **vazio herda** o global. Motivo: as recomendações de rota divergem —
no Live o dsh entrega o 1º token em ~0,4 s contra 4–11 s do provedor remoto, e na
Conversa o provedor do dono serve bem. Ex.: `chat_backend=openai` +
`chat_backend_live=dsh` mantém a Conversa no endpoint e põe o Live no harness.

Invariantes do caminho dsh: `session/request_permission` é **sempre negada**
(nada de tool executando); `mcpServers: []`; cwd = diretório vazio do app; env do
filho é mínimo (as nossas chaves não vão — o dsh lê `~/.dsh/.credentials.yaml`).
O texto vai para a rota do dsh (configuração explícita do dono), como já acontece
com o provedor.

#### Patch local do bridge ACP (streaming de verdade — task DSH-4a)

O `@deepseek-ai/dsh-acp` assina só `session/event` → `assistant/message` (comitado),
então o ACP entregava **um** `agent_message_chunk` no FIM da geração (medido: 74 s
numa resposta de 1,7 k chars) — os deltas do provedor existem em
`agent/assistant-stream` e só o app web os consumia. `scripts/dsh-acp-stream.patch.js`
é o patch textual (versionado) e `scripts/dsh-acp-stream-patch.mjs` é o aplicador
idempotente: ele exige que cada âncora do arquivo case **exatamente uma vez** e
falha alto (rc=2) se o pacote mudar de forma.

- Alvo real: `$(npm root -g)/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-acp/lib/index.js`
  (a dependência é aninhada do `dsh`; versão verificada: `0.1.5-rc.3`).
- Backup ao lado (`index.js.orig-<versão>`) + marcador `DSH4A_PATCH_V1` no arquivo.
- Marca os deltas com `partial: true` e a mensagem comitada com `partial: false`
  (mesmo `messageId`) — quem deduplica é `dsh_client._nao_duplicar`; sem a marca,
  o cliente continua correto, só mais lerdo.

```sh
node scripts/dsh-acp-stream-patch.mjs --status    # PATCHADO (rc=0) | LIMPO (rc=1) — não escreve
node scripts/dsh-acp-stream-patch.mjs --dry-run   # mostra as âncoras e o diff de linhas — não escreve
node scripts/dsh-acp-stream-patch.mjs             # aplica; se já patchado, no-op idempotente
node scripts/dsh-acp-stream-patch.mjs --reapply   # GARANTE patchado (reverte se houver backup, aplica de novo) — é o remédio depois de `npm install -g @deepseek-ai/dsh`
node scripts/dsh-acp-stream-patch.mjs --revert     # ROLLBACK num comando (exige o backup)
node scripts/dsh-acp-stream-patch.mjs --help      # o mesmo texto, sem exigir o pacote
```

Semântica completa das flags (e o que cada uma faz quando o backup não existe) em
`scripts/README.md`; o `--help` repete a tabela.

##### Trace do bridge (diagnóstico, ligado por arquivo)

Para correlacionar as pontas sem mexer em env: enquanto `/tmp/dsh4a-trace.on`
existir, o bridge grava um JSONL por evento cru recebido e por `session/update`
emitido em `/tmp/dsh4a-trace.jsonl`; sem o arquivo, o custo é um `existsSync` na
primeira chamada. `python3 evidence/dsh-acp-tres-pontas.py 3 persona` liga, mede e
compara bridge × cliente no mesmo turno.

Foi assim que o DSH-4b localizou o "resto da resposta em bloco": é **upstream** — a
rota LLM (`dsflash`) para 17–88 s no meio da geração e despeja a cauda de uma vez; o
bridge recebe em bloco e repassa em 1 ms (prova em
`evidence/dsh-acp-DSH-4b-EVIDENCIA.md`). O patch não é a causa e não há contorno
nosso.

**Provedor com preempção estraga a cauda.** O `dsflash` emite o 1º token em ~0,4 s e
às vezes trava 17–88 s no meio (provedor, não o nosso lado; medido no fio). A rota
alternativa do `~/.dsh/settings.yaml` (OpenRouter) não trava no meio, mas paga 5,6–43 s
no **1º token** — inaceitável para o Live, que vive do 1º áudio. Por isso o default
segue `dsflash`; **trocar de rota é decisão do dono** (`chat_dsh_model` em
Configurações → IA), e o trade-off é: cauda com rajada x cabeça lenta.

Descobrir os modelos/efforts disponíveis (com cache; a rota 400/502 traz o motivo):

```sh
curl -s $BASE/api/chat/dsh/models -H "X-API-Key: $KEY" | jq '.models[].id'
```

Na UI (Configurações → Rede & memória → *Backend da IA da Conversa*) a escolha é
um seletor: `Endpoint + chave` (campos de Base URL/modelo/chave, comportamento de
sempre) ou `dsh`, que troca o bloco pelos campos do harness — binário, perfil,
modelo (lista preenchida pelo próprio dsh via `/api/chat/dsh/models`, com estado
de carregando porque a descoberta leva 1–4 s) e effort. Os 5 campos são
administrativos. Dois detalhes que a tela deixa explícito e valem para quem
mexer no código:

- a descoberta usa o binário/perfil **salvos** (o endpoint lê settings, não o
  formulário): mudou o campo → Salvar → Recarregar; e se a descoberta falha o
  erro aparece com o motivo, sem esvaziar o seletor de modelo (senão o save
  seguinte apagaria o valor guardado);
- `effort ≠ off` liga o raciocínio e pode **estourar o orçamento de latência do
  Live** (1,5 s no 1º áudio) — no Live `off` é o default por isso.

O env `TTS_CHAT_DSH_*` tem precedência sobre o settings (padrão `TTS_CHAT_*`):
com ele setado, o campo correspondente na tela fica decorativo.

Contexto longo: numa sessão ACP o contexto vive no harness, então a compressão do
Live não se aplica a ela. Ao bater o teto (`TTS_DSH_CTX_MAX_MSGS`/
`TTS_DSH_CTX_MAX_CHARS`, iguais aos do Live), a sessão é fechada e uma sessão NOVA
recebe o resumo + os últimos turnos. A persona vai no 1º prompt de cada sessão.

Se a admissão do TTS recusar a síntese de um bloco com **429** (`TTS_JOBS_ACTIVE_MAX`),
a Conversa repete aquele bloco **uma vez** depois do `Retry-After` do servidor, em
vez de engolir a frase: pico de jobs concorrentes (ex.: API externa martelando)
deixa de custar uma frase do turno. A espera é cancelável — barge-in no meio dela
mata a repetição, não o turno.

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

### O que pode sair da máquina

Tudo é processado localmente até você apontar para um serviço externo. Estes são
os únicos caminhos de saída, **todos desligados por padrão** (Configurações →
Rede):

| Controle | O que sai | Para onde |
|---|---|---|
| TTS remoto (`remote_tts`) | o texto da fala e, em voz clonada, a amostra dessa voz + o texto de referência (sobe uma vez por mudança; some com `remote_tts_voice` preenchido) | `remote_tts_url` (ou `remote_base_url`) |
| STT remoto (`remote_stt`) | o áudio da fala a transcrever | `remote_stt_base_url` (ou `remote_base_url`) |
| Tradução remota (`remote_translate`) | o texto a traduzir | `remote_base_url` |
| Base URL da Conversa (`chat_base_url`) | histórico da conversa + preprompt | `chat_base_url` (ou `remote_base_url`) |
| Backend de IA pelo harness `dsh` (`chat_backend: "dsh"`, na Conversa e no Live) | o texto do turno; no modo histórico, o histórico renderizado. **Nada de áudio.** | a rota do provedor escolhida no `chat_dsh_model` — que, por default, é um provedor remoto; trocar o modelo pode trocar a rota |

A chave do provedor (`remote_api_key`, `remote_stt_key`, `chat_api_key`) vai no
header da requisição, só para a URL configurada — e com uma chave de uso ela nem
é lida em claro (ver *Administração x uso*). No caminho `dsh` a credencial fica
com o próprio harness (`~/.dsh/.credentials.yaml`): o app não lê nem copia o
arquivo. Fora do fluxo de fala, o único tráfego é o que você pede: download do
Hugging Face na primeira carga do modelo e o vídeo no `/api/youtube-audio`.

O caminho `dsh` é o mesmo material que já sairia pelo `chat_base_url` quando a
Conversa usa um endpoint remoto — a diferença é QUEM fala com o provedor (o
harness, na sua máquina, com a rota dele) e não o conteúdo. Para ficar 100% local
nele, escolha um modelo cuja rota aponte para um provedor na sua rede.

**Mesmo com o modelo já baixado**, a primeira carga do Whisper em cada processo
confere os metadados do repo no Hugging Face (`snapshot_download` resolve a
revisão e olha os arquivos do modelo — com o cache quente nenhum payload é
transferido). Medido com guarda de `socket.connect`/`getaddrinfo`: 2 tentativas
(DNS `huggingface.co` + um peer CloudFront:443), **zero áudio e zero texto**.
Para zerar de vez:

```bash
export HF_HUB_OFFLINE=1      # medido: 0 tentativas, STT igual (modelo em cache)
```

Se o modelo **não** estiver em cache, a primeira carga falha com
`LocalEntryNotFoundError` ("outgoing traffic has been disabled") — rode uma vez
sem a variável para baixá-lo.

O pipeline MLX **não embute marca-d'água** nos áudios gerados. Use apenas com a
sua própria voz ou com consentimento explícito da pessoa clonada.

A chave da API fica no navegador (localStorage; veja `SECURITY-frontend.md`) e é a
credencial que a UI usa para falar com a API. Se ela for `role:admin` (ou legada,
num servidor sem `TTS_ROD_ADMIN_KEY`), também administra; uma chave `role:use` gera
fala e transcreve, mas não mexe em conexão externa nem em modelo — ver
*Administração x uso*.

O que a protege na prática: a chave vai só no header `X-API-Key` (nunca na URL, nunca em log de proxy), os dois bundles de
CDN têm SRI, e os sinks de `innerHTML` são auditados. Em máquina compartilhada,
Configurações → Acesso → *Guardar só nesta sessão* tira o segredo do disco do
navegador.

## Desenvolvimento

```bash
# testes (funções puras + API via TestClient, sem carregar MLX)
./.venv-mlx/bin/python -m pytest tests/ -q

# venv completo: sem imageio-ffmpeg o time-stretch cai no phase vocoder e a
# fala sai ~15x mais baixa (o OmniVoice não tem speed nativa e passa por ele)
./.venv-mlx/bin/python -c "import imageio_ffmpeg, resemblyzer; print('deps ok')"

# análise estática — nomes indefinidos em funções só explodem em runtime
./.venv-mlx/bin/python -m pyflakes app.py common.py tts_worker.py backends.py \
    smoke_sintese.py live_turns.py smoke_live_turns.py tests/*.py client/mic_router.py
# esse pyflakes NÃO lê `# noqa` (noqa é do flake8): import que só existe para virar
# fixture, ou nome reusado como parâmetro de teste, se declara em `__all__ = [...]`
# no módulo — `# noqa` na linha não silencia nada aqui (ex.: tests/test_qa_gate176.py).

# ambiente reproduzível: `requirements.txt` é a lista CURADA (com o porquê de
# cada pin); `requirements.lock` é o retrato do venv verificado — com os
# transitivos, para um venv novo não resolver outra combinação sozinho
python3.12 -m venv .venv-locktest
./.venv-locktest/bin/python -m pip install -r requirements.lock      # idêntico
# ...ou resolvendo a lista curada com o lock como teto:
./.venv-locktest/bin/python -m pip install -r requirements.txt -c requirements.lock

# gate de bump de mlx-*/transformers: SÍNTESE REAL (falha se o áudio sair mudo).
# Com --stt transcreve e exige texto de volta. Instalar sem erro não basta.
./.venv-locktest/bin/python smoke_sintese.py --stt
# verde? aí sim: pip freeze > requirements.lock e atualize o pin no requirements.txt

# smoke test do worker isolado (lento ~1 min, carrega Kokoro real)
TTS_TEST_WORKER=1 ./.venv-mlx/bin/python -m pytest tests/test_worker.py -q

# regressão de XSS da UI contra o app de pé (./run.sh antes) — Chromium headless
./tests/xss_frontend_repro.sh            # --cleanup remove a voz de teste

# política de chave: uso x admin, ponta a ponta (cria e remove as chaves de teste)
./tests/admin_ui_flow.sh                 # precisa de IP de LAN (en0/en1)

# Conversa: retry do 429 de admissão do TTS (determinístico, sem carregar modelo)
./tests/conversa_429.sh

# deep-link por hash (#live): sobe servidor próprio em porta livre, sem modelo
./tests/hash_deep_link.sh

# Live: motor de turnos (falso barge-in, latência). Ver LIVE.md.
./.venv-mlx/bin/python -m pytest tests/test_live_turns.py -q   # sem MLX/Metal
./.venv-mlx/bin/python smoke_live_turns.py                     # precisa de voices/
```

Pre-commit opcional (pyflakes + pytest antes de cada commit):

```bash
git config core.hooksPath .githooks
```

O hook roda `pyflakes` **puro**: os `# noqa` espalhados pelo repo são códigos do
flake8 e não contam para ele — os de `tests/conftest.py` e `tests/test_app.py`
inclusive (ver a nota na lista de análise estática, acima).

### Instância viva x código do commit (regra de restart)

O `run.sh` sobe o `uvicorn` **sem `--reload`** (de propósito: com vários agentes
editando, reload contínuo mata job e refaz o boot do modelo). Consequência: o app de
pé pode estar rodando código de dias atrás — e o pin de um ticket não dá para ser
conferido contra ele. Já aconteceu: a instância em 7860 estava sem o backend de IA
por caminho e `POST /api/settings` ignorava o campo novo em silêncio.

Para conferir, sem subir modelo nem processo:

```bash
curl -s localhost:7860/api/build -H "X-API-Key: $CHAVE"
# {ok, version, boot_ts, boot_ms, codigo, modulos, admin_fields}
```

`codigo` é o sha256-8 do **conteúdo** dos módulos do servidor (`version` é o commit):
se ele não casa com o sha do ticket, a instância é velha. `admin_fields` denuncia o
caso clássico — instância antiga com menos campos administrativos que o código.

**REGRA:** quem fecha mudança de SERVIDOR (`app.py`, `backends.py`, `tts_worker.py`,
`live_*.py`, `dsh_client.py`, `common.py`) reinicia o app local **como último passo
do próprio ticket** — antes, confira que não há job nem sessão Live ativos — e
registra o restart no comentário do ticket (`codigo` antes/depois).

### Pin de arquivo em veredito (sha256-8)

Veredito e gate dizem *qual* versão mediram citando o arquivo com um hash de 8
dígitos — sem isso, "a tela mudou" vira discussão e alguém re-mede outra coisa.

```bash
shasum -a 256 static/index.html | cut -c1-8     # ex.: 0198ae84
```

É **sha256** e truncado em 8 (não md5, não o blob do git): `git hash-object`
devolve o hash do *objeto* do git e não bate com o do arquivo, o que já fez
gente achar que a tela tinha mudado sem ter. `mtime` também não serve de pin —
restauro com `cp -p`/`rsync -a` preserva data e troca conteúdo.

Quem altera um arquivo pinado re-pina no mesmo lugar (e, se houver gate aberto,
avisa: a versão dele acabou de deixar de ser a medida).
