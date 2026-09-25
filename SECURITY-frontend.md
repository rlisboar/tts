# Auditoria de frontend — sinks de HTML e chave no navegador (task #28)

Escopo: `static/index.html` (HTML+CSS+JS vanilla, arquivo único, sem build).
Data: ver o commit. Autor: frontend-engineer.

## Por que isto é P2 e não cosmético

A chave da API vive no `localStorage` deste navegador e, no modo admin, cria e
apaga chaves. A UI monta tela com `innerHTML` em ~40 pontos. Um `<img onerror>`
que entre no DOM vira **execução de script na origem do app** — e script na
origem lê a chave, chama a API e revela os outros segredos. Ou seja: XSS aqui
é escalada de privilégio, não só "layout quebrado".

A CSP do `index.html` tem `script-src 'unsafe-inline'` justamente porque o app
é um `<script>` inline único de 3.4k linhas (sem build, requisito do projeto).
`unsafe-inline` faz o `onerror=` do payload acima executar: **a barreira real é
escapar o sink, não a CSP**. É o que esta auditoria ataca.

## F1 — XSS confirmado, reproduzido e corrigido

**Sink:** `refreshVoices()` montava a ficha da voz com o `id` e o `created_at`
vindos de `/api/voices` crus dentro do template de `innerHTML`
(`<code class="vid">${v.id}</code>`).

**Entrada controlável:** `POST /api/voices/import` (restaurar backup) valida só
o *nome do arquivo* (`[A-Za-z0-9_-]+\.(json|wav)`), não o campo `id` do JSON
copiado para `voices/`. O `id` do backup é dado do atacante e o `/api/voices`
o devolve sem sanitizar.

**Payload** (voz com id `<img src=x onerror="window.__xss=(window.__xss||0)+1">`):

| | antes | depois |
|---|---|---|
| `window.__xss` | `1` (executou) | `0` |
| `<code class="vid">` | virou `<img>` | texto |

**Correção:** `esc()` no `id`, no `created_at` e no `duration` dos dois ramos da
lista de vozes. Nada de `textContent`/`createElement` aqui: o item usa
`innerHTML` porque tem `<canvas>`/`<audio>` e botões — trocar o mecanismo
mexeria em `drawPeaks`/`protectedAudio` sem ganho.

**Teste de regressão:** `tests/xss_frontend_repro.sh` — monta o payload, importa
pelo endpoint do produto e cobra o DOM num Chromium headless. Verificado nos dois
sentidos: passa no código novo, **falha** no código antigo (`__xss=1`, revertendo
o ramo real da lista; `2` se os dois ramos forem revertidos de uma vez, porque aí
o payload renderiza duas vezes). Também reproduz o sink antigo inline, para
provar que o payload é XSS de verdade e não uma string sortuda.

O gate (`task_e7f2abde`) achou duas lacunas de cobertura neste script e as
fechou: reverter só o `esc()` do ramo preset/virtual, ou só o `esc(created_at)`,
deixava a suíte verde. Agora ele importa um segundo payload que cai no ramo
virtual e cobra o `created_at` como texto — as três contraprovas por ramo
falham como devem.

## F2 — mesma classe, corrigidos na mesma passada

Todos com dado de API dentro de `innerHTML`:

- `refreshOutputs()` — `o.created_at` / `o.elapsed` / `o.duration` (:4830).
- `streamJob()` progresso — `j.progress.stage` cru num dos ramos (o outro já
  escapava; o par divergente é que era o bug) (:3891).
- status da geração — `blabel` (label do backend) (:3935).
- `loadApiKeys()` — `e.message`, que é o `detail`/`hint` devolvido pelo servidor
  (:5009). É por onde erro de API entra como HTML.
- `loadApiKeys()` — `lan_urls` dentro de `<code>` (:4889).
- `updateModelHint()` — `b.label/notes/repo/size/license` do `/api/backends`
  (:5299).

Resultado: 18 interpolações escapadas nos templates de `innerHTML`; as cruas que
sobram são literais do próprio código (`tag`, `editBtn`, classes) ou ramos já
cobertos por `esc()`.

## F3 — os dois bundles de CDN rodavam FORA da CSP e sem SRI

O `<meta http-equiv="Content-Security-Policy">` vinha **depois** dos dois
`<script src>` de `cdn.jsdelivr.net`. Uma CSP entregue por `<meta>` só passa a
valer quando o parser lê o elemento: como script clássico sem `defer` bloqueia o
parser, os dois bundles eram buscados e executados antes — fora da política.

Isso importa porque os dois rodam **na origem da página**, com acesso ao DOM e ao
`localStorage`, ou seja, à chave.

- CSP movida para antes dos scripts (comportamento verificado: `window.ort` e
  `window.vad` continuam carregando, zero violação de CSP no console).
- **Depois**, quando a #36 pôs a policy no header de resposta, o `<meta>` saiu de
  vez: enquanto os dois existiam a policy efetiva era a interseção, e o header
  vale desde a resposta (inclusive sob `/ttsproxy/`), então o `<meta>` só somava
  a limitação de valer só depois do parser.
- `integrity="sha384-…"` + `crossorigin="anonymous"` nos dois: uma troca do
  arquivo no CDN (ou do alias) deixa de executar em silêncio. Hashes conferidos
  contra o conteúdo servido hoje.

## F4 — `frame-ancestors` em `<meta>` é ignorado — RESOLVIDO pela #36

O Chromium logava em toda carga: *"The Content Security Policy directive
'frame-ancestors' is ignored when delivered via a meta element"* — a página podia
ser emoldurada (clickjacking em cima do *Revelar/Copiar* da chave).

Resolvido em duas etapas: o backend passou a emitir a CSP **e** `X-Frame-Options:
DENY` + `Referrer-Policy: no-referrer` no header (mesmo texto do `<meta>`, byte a
byte, `/docs` e `/redoc` excluídos por montarem a própria página com CSS de CDN),
e o `<meta>` saiu do HTML quando o header entrou.

Verificação na remoção (não só "o header existe"): `window.ort`/`window.vad`
seguem carregando, o erro de `frame-ancestors` **sumiu** do console, e a policy
continua sendo *aplicada* — provei forçando uma violação (imagem de origem fora do
`img-src` gera o aviso de CSP), o que só acontece se o header estiver valendo.

## Chave no navegador — o que mudou

O pedido era "armazenamento que preserve UX". Preservado: o padrão continua
`localStorage` (a UI já entra autenticada, sobrevive a fechar o navegador) e o
envio continua só no header `X-API-Key`, nunca na URL.

Adicionado:

- **Guardar só nesta sessão** (Configurações → Acesso): muda o cofre para
  `sessionStorage` e o segredo some ao fechar o navegador — útil em máquina
  emprestada. Desligado por padrão, então nenhuma UX existente muda sozinha.
- **Um cofre só.** As escritas estavam espalhadas em 5 pontos
  (`localStorage.setItem("ttsStudioKey", …)`); viraram `keySave()`/`keyDrop()`,
  que sempre limpa o *outro* storage. Sem isso, alternar para "sessão" deixaria
  uma cópia esquecida no `localStorage` (e o `ttsRodKey` legado ressuscitaria a
  chave no próximo boot) — o "modo sessão" seria mentira.
- `keyRead()`/`keySave()`/`keyDrop()` toleram storage bloqueado (modo privado
  antigo), onde o acesso ao `localStorage` joga exceção.

Invariante testada no navegador real, marcando/desmarcando o controle: o segredo
existe em exatamente um dos dois storages em cada estado, e o rótulo de status
acompanha (`Salva neste navegador: chav…3456` ↔ `Nesta sessão: chav…3456`).

Riscos que **permanecem** (aceitos, com motivo):

- Um XSS na página continua lendo o cofre — nenhum armazenamento de navegador
  resolve isso. O que fecha a porta é não ter sink aberto (F1/F2) e o bundle de
  CDN não poder mudar sozinho (F3).
- A chave é a mesma para uso e para administração (criar/apagar chaves, trocar
  config), então quem a tem é admin. Separar os dois papéis é a task #21.

## Falsos positivos verificados (não eram bugs)

Checados um a um; ficam registrados para a próxima auditoria não gastar tempo:

- `toast()` — `textContent`. Nome de voz/chave/erro com `<b>` nunca virou HTML.
- Lista de chaves (`loadApiKeys`) — nome, máscara e data por `textContent`;
  só o texto de erro do `catch` escapava (F2).
- Conversa (`cvAdd`/`cvAddIA`) — `createElement` + `textContent`.
- `esc()` cobre `& < > " '` — logo é seguro também em **contexto de atributo**
  (`title=`, `data-*`), não só em texto. As 18 chamadas em atributo estão ok.
- `CSS.escape` no seletor `audio[data-voice-id=…]` — id hostil não quebra a
  busca.
- `renderOneControl()` (controles dinâmicos por backend) — já escapava tudo,
  inclusive `placeholder` e `value`.
- Texto de saída, tradução e perfis de biometria — já escapados.

## Achado que NÃO é do frontend (task para api-backend)

Um `id` de voz fora de `[A-Za-z0-9_-]` (o do payload!) faz `/api/voices/{id}/audio`
e `…/peaks` caírem em `_safe_id` → 404. No navegador aparece como dois 404 no
console e a voz sem waveform (o `esc()` resolve o HTML, não a URL). O conserto
certo é validar o campo `id` no `/api/voices/import`, não afrouxar o `_safe_id`.

## Como verificar

```bash
./run.sh                                   # app de pé em http://127.0.0.1:7860
./tests/xss_frontend_repro.sh              # espera-se "✔ OK"; falha no código antigo
./tests/xss_frontend_repro.sh --cleanup    # remove a voz de teste depois
```

**Checagem de integridade do arquivo** (o working tree tem vários agentes
escrevendo; `static/index.html` já voltou para o HEAD uma vez durante esta task,
por fora). A correção está de pé quando estes três marcadores existem:

```bash
grep -c 'integrity="sha384'                 static/index.html  # 2 — SRI nos bundles
grep -c 'clientApiKeySession'               static/index.html  # 5 — controle da chave
grep -c 'const keyDrop'                     static/index.html  # 1 — cofre único
grep -c 'copiar o id">${esc(v.id)}</code>'  static/index.html  # 2 — o sink do F1
grep -c 'let KEY = localStorage'            static/index.html  # 0 — keyRead() entrou no lugar
grep -c 'let KEY = localStorage'            static/index.html  # 0 — keyRead() entrou no lugar
# a CSP saiu do <meta> (#36): sem o header, a policy sumiria — confira os DOIS lados
grep -c 'http-equiv="Content-Security-Policy"'   static/index.html  # 0
curl -sD- -o /dev/null http://127.0.0.1:7860/ | grep -ci content-security-policy  # 1
```

**Números de suíte: cite com data.** O `pytest` cresce a cada frente que entra
(aqui rodam 5 agentes): ~150 no começo do dia, 166 no fechamento do gate desta
task (#38), 221 no fim da #42. Dizer "a suíte tem N passed" sem data envelhece
em minutos — o que vale como evidência é "verde no estado X, md5 Y".

Evidência coletada no app real (Chromium headless via Playwright):
`evidence/antes-xss-vozes.png` (payload executando, `<code>` virou `<img>`),
`evidence/depois-xss-vozes.png` (mesmo registro já como texto) e
`evidence/depois-chave-sessao.png` (controle novo em Configurações → Acesso).
Console final sem `pageerror`; os únicos erros são 404 de recurso e `ERR_CONNECTION_REFUSED`
nas portas 7861-7865 (roteador de microfone não está de pé) — nenhum é violação
de CSP nem falha de SRI.

## Fora do escopo (propostas)

- `require-trusted-types-for 'script'` + `trustedTypes.createPolicy('default', …)`:
  sink de `innerHTML` passa a exigir política, o que dá onde plugar sanitizador
  depois. Não entrou para não misturar "prova de XSS" com mudança que depende de
  suporte de navegador (Safari 16.4+, Firefox recente).
- Tirar `'unsafe-inline'` do `script-src`: precisa de hash/nonce do bloco inline
  — exige mexer em quem serve o arquivo (o hash muda a cada edição). Só vale com
  um passo de serve calculando o hash.
- `style-src` sem `'unsafe-inline'`: o `<style>` é único e grande, mesmo caso.

## Gate qa-review (task #38) — o que foi verificado de forma independente

Aprovado. Rodei os dois sentidos (código novo verde, sink antigo vermelho) e
reexecutei os contraprovas por ramo, sem confiar no relato:

- `tests/xss_frontend_repro.sh` ✔ OK (`__xss=0`, `__xssNaive=1`) e **falha** ao
  reverter `esc()` — confirmei um ramo por vez, não só o par.
- `tests/key_storage_repro.sh` (novo, do gate) ✔ OK: a chave existe em exatamente
  um storage em cada modo; falha se `keySave` parar de limpar o outro storage,
  de apagar o `ttsRodKey` legado, ou se o rótulo parar de acompanhar o modo.
- SRI conferido no conteúdo real do CDN: os dois `sha384` do `index.html` batem
  byte a byte com `ort.min.js@1.14.0` e `bundle.min.js@0.0.19`; a `<meta>` de CSP
  está antes dos dois `<script src>` no HTML **servido**, e `window.ort`/`window.vad`
  carregam sob a política (se o hash estivesse errado o browser bloquearia).
- Sem escrita de `ttsStudioKey`/`ttsRodKey` fora do `keySave()` (grep) e nenhuma
  requisição observada com a chave na URL — ela vai só no header `X-API-Key`.

Cobertura que o gate acrescentou ao teste (o relato avisava que os ramos eram
diferentes e um deles já escapava antes): o script agora cobre **os dois ramos**
da lista (voz real e preset/virtual — o virtual entra por um `.json` órfão com
`preset: true` no mesmo import) e o `created_at` como texto. Antes, reverter só o
ramo virtual ou só o `esc(created_at)` deixava o teste verde.
