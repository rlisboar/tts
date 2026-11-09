# Produção no Mac mini — deploy, CI e monitoração

> O app roda em produção no **Mac mini** (`tts.the-dudes.com`); este arquivo cobre o
> deploy (`remote/deploy_mini.sh`, §6), o runner de CI (§7) e o probe de saúde (§8).
>
> As §1–§4 eram do stack RTX (servidores OmniVoice/Voxtral na máquina CUDA, deploy
> por systemd). A máquina não volta (decisão do dono, 2026-11-09) e o stack saiu do
> repo no #1058; o histórico fica no git. A numeração das seções que ficaram foi
> mantida porque o código e o README citam §6–§9.

## 5. Segurança (leia antes de rodar ssh no escuro)

- **A chave `~/.ssh/id_ed25519` do Mac abre dezenas de máquinas públicas da frota.** Um
  `ssh` no host errado funciona e parece certo — confirme com `hostname` antes de
  concluir qualquer coisa. Recon é read-only; `pip install` e restart de serviço já
  são intervenção (pedir OK).
- Nunca imprimir token/chave: o token do Cloudflare Tunnel mora no plist (600) e a
  chave do app no `.apikey` do mini (Application Support); o `deploy_mini.sh` lê por ssh e a
  usa só no `curl`.
- Túnel do app: o destino é **sempre o IP de LAN da máquina do TTS**,
  nunca `127.0.0.1` (loopback na VPS dispensa chave). O `tunnel.sh` **valida** isso
  (só 10/8, 172.16/12, 192.168/16) e recusa o resto com o motivo; para provar o
  lado público sem apply, `curl` sem chave em `/api/voices` tem de dar **401**
  (200 = o destino é loopback e a auth virou no-op).

## 6. Produção no Mac mini (tts.the-dudes.com)

A produção do app é um **checkout git no Mac mini**
(`192.168.15.34`, usuário `lisboa`, `~/Documents/tts-rod`), servido pelo agente launchd
`studio.tts.server` (que é um `ssh lisboa@127.0.0.1 start-server.sh` com `KeepAlive`; o
script faz `pkill` do `run.sh`/`uvicorn` e re-executa) e exposto ao público pelo
Cloudflare Tunnel do próprio mini. **Não há auto-pull**: quem sobe código é o
`remote/deploy_mini.sh` — sem ele a produção congela no commit do dia em que foi
instalada (foi o achado #231: 66 commits / 8 dias, com o app em uso).

```
./remote/deploy_mini.sh recon                 # read-only: host, git, serviço, deps, uso
./remote/deploy_mini.sh compare               # rev alvo × o que está no ar
./remote/deploy_mini.sh smoke                 # no ar: /health, auth, /api/build, WS, index, TTS real
TTS_MINI_REV=<corte> ./remote/deploy_mini.sh deploy [--apply]   # push → backup → switch → deps → restart → smoke
./remote/deploy_mini.sh rollback [--apply]    # volta o SHA salvo pelo último deploy
```

Sem `--apply` nada muda (nem no `origin`). `--sem-push` pula a publicação, `--sem-deps`
pula o `pip install` e `--sem-tts` pula a síntese real. Config por env: `TTS_MINI_HOST`,
`TTS_MINI_USER`, `TTS_MINI_DIR`, `TTS_MINI_LABEL`, `TTS_MINI_PORT`, `TTS_PUBLIC_HOST`,
`TTS_MINI_REV` (rev alvo; padrão `HEAD`).

O alvo é um **rev**, não necessariamente o HEAD: `TTS_MINI_REV=3e663a6` sobe um corte
específico. O mini fica numa branch `prod-<sha>` (não detached, para o próximo deploy não
se perder) e o `rollback` repete o mesmo passo com o sha que o deploy salvou.

O que ele garante:

- o que sobe é o **commit** (HEAD ou o corte pedido), não a árvore viva — WIP não
  commitado fica de fora (o script avisa o que ficou);
- o mini tem que estar **limpo**, e o alvo tem que existir no mini depois do fetch —
  senão para **antes** de reiniciar (restart derruba a produção por alguns segundos);
- deps são conferidas contra o `requirements.txt` **do rev alvo** (o do mini, antes do
  fetch, ainda é o antigo — foi assim que um preview mentiu "nada a instalar" enquanto
  faltava o `websockets`, sem o qual o `/api/live/ws` responde 500 no navegador). O
  preview vai para o **/tmp do mini**, não para a árvore: dry-run não suja produção;
- a instalação leva **`-c requirements.lock`**, e o lock também vem **do rev alvo**
  (#343): as entradas sem pino da lista curada (`torch`, `fastapi`, `uvicorn`,
  `websockets`, `onnxruntime`) não podem mover-se num deploy sem diff de código. Se o
  pip estourar o teto, o deploy **para antes do restart** (`[PARA] pip install
  falhou`) — a produção continua no processo antigo. Rev sem lock (corte antigo)
  avisa e segue sem teto. O venv do ANTES e o do DEPOIS viram `.deploy-mini-freeze-*`
  e o delta é gravado em `.deploy-mini-deps-delta-*` (`venv IDÊNTICO` é dito quando
  nada mudou). O caminho de rollback **não** reresolve: deps não voltam sozinhas;
- `smoke` confere `GET /api/voices` **com a chave do mini** (LAN e público), o `codigo`
  do `/api/build` contra o hash dos módulos do rev alvo — pega "código novo no disco,
  processo velho" (a feature inerte do #190) —, `boot_ms` do restart e um
  **`POST /api/tts` real** até a peça 0 sair com bytes. Duas checagens são conscientes
  do ALVO: `/api/build` e `/api/live/ws` só são exigidas se o rev alvo tiver a rota
  (num corte anterior ao #190/épico Live o smoke diz "ausente NESTE rev", não "falha"),
  e a paridade do `index.html` ignora o `nonce="…"` que o app injeta por request (CSP).
  O WS é sondado com **`--http1.1`**: o upgrade é mecanismo h1 e contra o Cloudflare
  (h2 entre cliente e edge) o pedido chega no origin SEM upgrade — o middleware
  devolve 401 e a sonda false-vermelhava o caminho público embora o navegador
  (h1) conecte (#247).
- `deploy` grava `.deploy-mini-estado` (sha + data) e um `.deploy-mini-freeze-*` no mini
  antes de mexer — é o que o `rollback` usa. Re-deploy do MESMO rev **preserva** o
  estado (senão o rollback viraria no-op); deps **não** voltam sozinhas no rollback
  (o freeze fica lá para um `pip install -r` manual, se precisar).
- **caminho "nada a fazer" também faz smoke**: rev alvo no ar, sem deps novas e
  código já carregado → não reinicia, mas roda o smoke e **sai ≠0** se ele ficar
  vermelho (`[PARA] smoke vermelho SEM diff`), sem rollback — nada foi reiniciado,
  a produção já estava assim (#495). Antes o caminho saía 0 com `[FALHA]` no log;
- **smoke vermelho depois do restart reverte sozinho** (#345) para o sha do
  `.deploy-mini-estado`, reinicia e repete o smoke; sai ≠0 dizendo que reverteu. Sem
  estado (ou com o estado apontando para o próprio rev que falhou) ele não inventa
  alvo: diz `[PARA]` e manda reverter à mão. Se o smoke do rollback também falhar,
  grita `✖ REVERTIDO PARA …, MAS O SMOKE DO ROLLBACK TAMBÉM FALHOU` — o operador
  precisa saber que a produção está de pé num rev e vermelha, não que "deu erro".

### `Host` público: `TTS_ALLOWED_HOSTS` (#837)

O `#779` passou a validar o cabeçalho `Host`. O túnel entrega
`Host: tts.the-dudes.com`, e esse nome **não** está na allowlist implícita
(loopback, IPs da máquina, `*.local`) — sem a variável, a página pública responde
**421** e sai do ar.

Antes de subir um rev com o `#779`, o serviço precisa de:

```
export TTS_ALLOWED_HOSTS="${TTS_ALLOWED_HOSTS:-tts.the-dudes.com}"
```

em **`~/.tts-studio/start-server.sh`**, seguido de
`launchctl kickstart -k gui/$(id -u)/studio.tts.server` e conferência:

```
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: tts.the-dudes.com' http://127.0.0.1:7860/health
```

Tem de dar **200** (421 = nome fora da lista). Nome público novo entra na mesma
env, separado por vírgula.

**Não ponha isso no `EnvironmentVariables` do plist** — ele não chega ao app: o
agente sobe `ssh ... start-server.sh` e o sshd não herda o ambiente do launchd
sem `SendEnv`/`AcceptEnv`. O plist traz o bloco só como registro; quem vale é o
script. (Custou uma hora de diagnóstico; o comentário no plist avisa.)

O smoke do `deploy_mini.sh` e o `health_probe.sh` acusam 421 — a checagem é
feita na LAN com o `Host` do público, então vale mesmo
com o túnel caído.

### Atenção em produção (chave legada)

A produção do mini roda hoje com uma chave **sem papel** em `.apikeys.json`. Pela regra
de compatibilidade do `_admin_is_allowed`, chave sem papel **continua admin enquanto
`TTS_ROD_ADMIN_KEY` não estiver setada**. Se alguém setar essa variável no mini (plist do
`studio.tts.server` / `run.sh`), a chave atual **perde o admin** e a UI quebra nos campos
admin (`/api/settings`, seletor de modelo). Antes de setar: dar `role: "admin"` à chave
na UI (Acesso) ou criar a chave admin e configurá-la.

### Plano em dois passos (recomendação do PM, 2026-09-30)

Produção é usada e não assistida: não subir a árvore em movimento de uma vez.

1. **Corte `3e663a6`** (24/09 23:45, último commit antes do épico Live) — só o
   endurecimento, já gateado. **Código só, `--sem-deps`**: o delta de requirements no
   intervalo é `httpx2`/`httpcore2`/`truststore` (teste), `importlib_resources` e o
   `silero-vad==6.2.1`, que é **downgrade** do 6.2.2 que já roda em produção.
   `TTS_MINI_REV=3e663a6 ./remote/deploy_mini.sh deploy --apply --sem-deps`
   — **EXECUTADO em 2026-10-01 01:42** (`prod-3e663a60ff3a`, smoke verde;
   `evidence/231-passo1-*.txt`).
2. **HEAD + `websockets` no venv** (obrigatório para o `/api/live/ws`; sem ele o
   navegador toma 500), quando a fila do Live fechar. Aí sim `pip install` e smoke de
   VAD (por causa do downgrade do silero).
   — **EXECUTADO em 2026-10-01 03:29** no rev `d252693` (= HEAD da hora do dry-run;
   `prod-d25269369284`, smoke verde nos dois caminhos, WS público provado com cliente
   real; `evidence/231-passo2-*.txt`). Rollback armado em `3e663a6`
   (`.deploy-mini-estado`, com o `pip uninstall -y websockets` anotado).

Medido em 2026-09-30 (dry-run, nada aplicado): produção em `cb36edb`; o corte exige
publicar 18 commits, o HEAD exige 64; delta de deps do HEAD = `httpcore2 httpx2
importlib_resources silero-vad-6.2.1 truststore websockets`.

## 7. CI e runner self-hosted (#293)

O gate do repo roda no GitHub Actions (`.github/workflows/ci.yml`) em push/PR para
`main`: `pyflakes` na lista do hook, suíte hermética com `--timeout`, e as duas
suítes `.sh` que não pedem navegador nem modelo. Regra e limites no
`README.md` §Desenvolvimento.

**A medição decidiu, e ela está no lugar de opinião.** O `pyflakes` e as suítes
`.sh` ficam no runner HOSPEDADO (`ubuntu-latest`): não importam o app, rodam em
segundos e são gratuitos (repo público). A SUÍTE não passa lá — em linux/amd64 o
venv instala (com `mlx[cpu]`) e o `app` importa, mas o resultado é **36 falhas em
642 passes**: MLX/onnx sem Metal, `ps`/pid dos testes de `dsh_client`/`live_worker`,
ffmpeg do webm e convenções de `/tmp` do macOS. Vermelho de ambiente em todo PR não
é gate, é ruído — então `testes` vai para o runner do mini, por label:

```yaml
runs-on: [self-hosted, tts-mini]   # .github/workflows/ci.yml, job `testes`
```

O label `tts-mini` está declarado em `.github/actionlint.yaml` (o `actionlint` do
job `estatica` acusaria label desconhecida sem ele) e é o que impede um runner
self-hosted futuro de roubar o job.

Instalação, uma vez, **dentro do mini** (não por ssh):

```
./remote/ci_runner.sh install      # baixa, registra e instala o agente launchd
./remote/ci_runner.sh status
./remote/ci_runner.sh uninstall
```

Enquanto o runner não estiver instalado, os jobs `testes` e `testes-lentos` ficam
enfileirados — não vermelhos. Instale-o antes de tornar o check obrigatório no
branch protection.

**Dois jobs, dois tetos (#783):** `testes` roda a suíte menos os `slow` (teto 15
min) e `testes-lentos` roda `-m slow` (teto 45 min). A separação existe porque o
job único encostava no teto de 30 e era cancelado — e cancelamento não diz se o
código quebrou. Se um teste novo passar de ~1 min, ele vai para `slow` com
`@pytest.mark.slow`; o artefato `duracoes-*` de cada job mostra a cauda.

O que o script garante, e por quê:

- **não disputa o `:7860`**: o agente é `ProcessType=Background` com `Nice 10` e
  `LowPriorityIO` (produção mantém prioridade). Aceite da mudança: com a suíte
  rodando, `curl -m 5 http://192.168.15.34:7860/health` continua 200;
- **não toca o checkout de produção**: o `actions/checkout` escreve em
  `~/actions-runner/_work`, nunca em `~/Documents/tts-rod` (um CI verde não pode
  deixar a produção no meio de um `git switch`);
- **token de registro nunca versionado**: é gerado por `gh api` na hora (vale
  ~1 h) e não vai para arquivo do repo;
- **labels fixas** (`self-hosted,macOS,ARM64,tts-mini`): o workflow só pega este
  runner pelo label `tts-mini`, então um runner futuro não rouba o job.

Pré-requisitos no mini: `gh` autenticado como dono do repo. Ao remover,
`./remote/ci_runner.sh uninstall` desregistra e tira o agente; `~/actions-runner`
fica para apagar à mão.

## 8. Monitoração da produção (#344)

Produção caída era descoberta **pelo uso**: nada vigiava, `/health` era um `{"ok":true}`
e o log em `/tmp` crescia sem rotação. O que existe agora:

`/health` publica `boot_ts` e `rev` (commit curto) além do `ok` — sem detalhe interno,
o fingerprint `codigo` continua só no `/api/build`. Com isso o histórico distingue
**reinício** (boot_ts novo) de **instância travada** (valor repetido) e diz em que
commit, sem chave.

`remote/health_probe.sh` roda **no mini** (LaunchAgent `StartInterval 120`, ver
`install`) e, a cada rodada: `/health` na LAN e no público (200 + `"ok"`),
`/api/voices` **sem chave** no público, que tem de dar **401** — 200 ali é o
invariante do túnel furado (destino loopback ⇒ auth no-op) — e o `/ready` do
conector na porta local de métricas (`127.0.0.1:20242`), que tem de dar 200: com
o conector caído o público ainda responde 200 por um tempo (cache/DNS) e a LAN
não enxerga o conector, então sem essa linha a queda **parcial** passava verde
(`TTS_PROBE_CONECTOR=0` desliga). Depois de
`TTS_PROBE_FALHAS` falhas consecutivas (padrão 3, ~6 min) ele grava
`~/Library/Logs/tts-health-ALERTA.txt` (0600) e tenta uma notificação do macOS; a
primeira rodada boa zera o contador e apaga a flag.

```
./remote/health_probe.sh install      # instala o agente e faz uma rodada
./remote/health_probe.sh status       # alerta aberto + últimas linhas + contador
./remote/health_probe.sh uninstall
```

**Rotação** (precisa de root, não é feita pelo `install`):
`sudo cp remote/newsyslog.d/tts-studio.conf /etc/newsyslog.d/` e
`sudo newsyslog -v -f /etc/newsyslog.d/tts-studio.conf` para conferir (roda uma
rotação forçada). 1 MB × 3 gerações, modo 0600 já na rotação.

**Canal de alerta — decisão do dono, em aberto.** O arquivo-flag é o mínimo que
existe hoje (visível, sem terceiro, sem segredo). Quando o dono escolher o canal
(Slack/WhatsApp/…), o ponto de chamada é `_dispara_alerta` e **nada mais**;
até lá, quem tem acesso ao mini descobre por `status`/flag.

## 9. Arquivos relacionados

`remote/deploy_mini.sh` · `remote/ci_runner.sh` · `remote/health_probe.sh` · `.github/workflows/ci.yml` · `README.md` §"servidores `remote/`"
· `tunnel.sh` (Mac↔VPS) · `cloudflare.sh` (conector no Mac mini).

