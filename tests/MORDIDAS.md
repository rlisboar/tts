# Mordidas: a regra do bytecode (#238)

Uma **mordida** é a prova de que um teste cai quando o fix é revertido. Ela é o que
separa "o teste passa" de "o teste mede". E ela tem uma armadilha específica deste
repo (árvore compartilhada por vários agentes, arquivos grandes, rodadas curtas):

> `cp app.py /tmp/bak` → editar → medir → `cp /tmp/bak app.py` **pode não morder**.
> O `cp` preserva o TAMANHO e o mtime tem granularidade de 1 s; o CPython valida o
> `.pyc` por (mtime em segundos, tamanho) da fonte. Se a medição cai na MESMA
> segunda, o `.pyc` continua válido e o interpretador roda o **bytecode velho** —
> a mordida "passa" (falso PASSA) sem que o código revertido tenha sido executado.

Reprodução medida (task #238, célula do #230): revertendo o default do
`TTS_LIVE_BARGE_JANELA_TURNO` (mesmo tamanho) e devolvendo o mtime com `touch -r`,
o guard `test_engine_do_live_nasce_com_as_alavancas_do_167_ligadas` **passou**; com
o `.pyc` removido, o MESMO arquivo fez o teste cair. Duas saídas, um md5 só.

## Regra

**Toda mordida invalida o bytecode antes de medir.** Na prática:

- `tests/limpa_bytecode.sh [modulo.py ...]` — remove os `.pyc` dos módulos do build
  (ou dos que a célula toca). Chamar antes de rodar o teste que tem de cair.
- ou exportar `PYTHONDONTWRITEBYTECODE=1` no harness que invoca `python` — mata o
  problema na origem e é o preferido quando o harness já é Python.

Aplicado em: `evidence/232-mordidas.sh`, `evidence/223-revert-hand.py`,
`evidence/232-contraprova.sh` e no método dos gates que revertem default
(#230/#216). Os scripts de mordida do deploy (#233/#235/#240) seguem a mesma regra.

## Assinatura do falso PASSA

Se uma célula de mordida passar **e** o md5 do arquivo revertido for igual ao da
mordida anterior, desconfie do `.pyc` antes de concluir que o fix é inerte.

## Célula medida — #281 (RNNoise: `fechar()` correndo com `abrir()`)

Três mordidas, todas com o `.pyc` removido antes de medir. As três caem **só pelo
teste** — o `pyflakes` fica em `0` nas três, então nenhuma seria pega pelo estático.

| Sabotagem | Resultado |
|---|---|
| remover a instalação do filtro no handler (`sess["rnnoise"] = _live_rnnoise_novo()`) | `test_handler_abre_o_filtro_e_fecha_no_finally` **FAILED** |
| tirar o `fechar()` do `finally` da sessão | mesmo teste **FAILED** (a asserção `fechou == 1`) |
| arrancar as duas rechecagens de `_fechado` em `_abrir` | `test_fechar_durante_abrir_nao_vaza_ffmpeg` **FAILED** **e** o repro real volta a vazar **8/8** |

**Repro real do defeito** (antes do fix): `abrir()` em thread + `fechar()` na
sequência → ffmpeg vivo depois do `fechar`, `_ok=True` num objeto fechado, e os
processos acumulando (1→5 na repetição). Medido no processo vivo: 15,5 MB de RSS e
1,3 % de CPU por sessão vazada, até o app reiniciar. Com o fix: **8/8 limpo**
(5 com `fechar()` imediato e 3 a 15 ms), `ativo=False`, `_proc=None`, zero processo.

O teste do F1 usa um espião de `Popen` que **segura o `__init__`** para cair na
janela exata, cobra `_proc is None` antes de fechar e usa `returncode` (não
`poll`, que o espião mente de propósito). O do F2 exige o frame filtrado no `feed`
**e** `fechou == 1` no `finally`.

Commit: `ea29b2d`. Gate: `task_2c6d833c` (veredito APROVADO).

## #331 — OBS fora da viewport no mobile (`tests/mobile_layout.sh`)

A mordida aqui é **composta**, e uma sabotagem de uma linha só passa: tirar
`flex: 0 0 auto` (index.html:1965, mantendo o `nowrap`) ou voltar só o
`flex-wrap: wrap` (l.1963, mantendo o `flex`) deixa a suíte **verde** — cada
metade cobre a outra. A regressão só aparece na condição ORIGINAL inteira.

Sabotagem completa (medida, 2 edições):

- devolver `flex-wrap: wrap` no `.control-bar` (l.1963) **e** tirar o bloco
  `#obsTts/#obsVozes/#obsStt/#obsVozes { flex: 0 0 auto }` (l.1965) — o
  `flex-basis: 100%` global (l.2023) volta a valer como ALTURA em coluna;

Resultado: **✖ vermelho** com `390px · [gerar] obsTts sai 162px da viewport` e
`painel #obsTts (gerar) não está inteiro na viewport`.

Atenção a dois detalhes de medição:

- `#obsTts` vive em **`view-gerar`**, não na Configuração: medindo com
  `showView('config')` ele fica dentro de `section.view[display=none]` e o
  `getBoundingClientRect` dá 0×0 — um teste de "está dentro da viewport" passa
  por vacuidade se não exigir tamanho > 0;
- o alvo do teste é `#voiceList canvas.wave` (onda da lista) para o foco; a onda
  do **editor** (`#edWave`, l.3106) não é coberta — tirar o `tabindex` dela
  mantém a suíte verde.

Gate: `task_b91d2003` (veredito APROVADO, com a ressalva da mordida composta).

## #499 — probe não via o conector caído (queda parcial verde)

O `/health` público pode continuar 200 por um tempo com o conector do Cloudflare
caído (cache/DNS) e a LAN nunca enxerga o conector: a queda parcial passava verde.
A célula nova exige o `/ready` em `127.0.0.1:20242` (porta do `--metrics` que o
`cloudflare.sh` instala). Sabotagem: tirar o bloco do `curl` do `/ready` —
`test_conector_sem_ready_e_falha_mesmo_com_lan_e_publico_200`,
`test_conector_respondendo_nao_e_200_tambem_conta` e
`test_conector_cai_na_porta_local_20242` caem (3/3, medido com `git stash`).
O `test_knob_desliga_a_checagem_do_conector` passa nos dois lados de propósito: é
controle do `TTS_PROBE_CONECTOR=0`.

## #495 — caminho "nada a fazer" engolia o smoke

`deploy_mini.sh` saía 0 com `[FALHA]` no log: "nada a fazer" era lido como
"produção boa". Sabotagem: voltar `POS_DEPLOY=0 … smoke; return 0` —

| Sabotagem | Resultado |
|---|---|
| caminho "nada a fazer" volta a sair 0 | `test_nada_a_fazer_com_smoke_vermelho_nao_sai_verde` **FAILED** |

O controle `test_nada_a_fazer_com_smoke_verde_continua_zero` passa de propósito. A célula do mini usa `T_CURL_HEALTH_CODE` (knob novo do shim) e não
o `/api/build`: o build vermelho tira a condição `-n "$(codigo_no_ar)"` e o
caminho deixa de ser "nada a fazer" — medido, era um falso alvo.

(A metade do servidor RTX desta célula e as seções #494, #497, #498 e #500 saíram
no #1058, junto com o código e os testes que mediam; o histórico fica no git.)

## #496 — `_destino_do_tunel` casava qualquer `ssh -R` da máquina

Outro túnel (openwebui) antes na lista de processos fazia o status dizer "túnel
rodando" com destino/porta alheios — verde com o do TTS parado. A célula nova
individualiza pelo NOSSO plist (host + porta remota do ProgramArguments).

| Sabotagem | Resultado |
|---|---|
| largar o filtro de host/porta (`if False: continue`) | `test_status_nao_confunde_com_outro_tunel_antes_na_lista` **FAILED**, `test_status_nao_mente_verde_com_apenas_o_tunel_alheio` **FAILED** e `test_status_aceita_a_porta_do_plist_e_recusa_a_de_outro` **FAILED** (3/3) |

`test_status_sem_plist_legivel_prefere_a_chave_do_tts` é o caminho de degradação
(instalação antiga sem ProgramArguments): a chave `-i …/tts_tunnel` desempata e,
sem nada disso, aceitar é melhor que ficar cego. Cuidado medido aqui: a primeira
rodada de sabotagem passou por `.pyc` velho — a regra da #238 vale também para
este arquivo grande (`PYTHONDONTWRITEBYTECODE=1` na medição).

## #555 — mordida do `live_ws.sh` gravava no `.apikeys.json` do DONO

A prova do #463 ("tirar os caminhos de chave do ambiente faz a guarda reprovar")
só era verdadeira porque o app cai no arquivo real — e, ao medir, gravava
`probe-live-ws` no `.apikeys.json` do dono (aconteceu duas vezes, api-backend no
#463 e qa no #545; as duas limpas à mão). Mordida também tem de ser read-only no
dado real.

A saída é rodar a mordida contra um **dono de mentira**: um `git worktree` com
`.apikeys.json`/`.apikey` sintéticos fazendo o papel do arquivo do dono. Sem os
caminhos por ambiente, quem muda é o sintético; o sha256 do real é cobrado
idêntico na própria célula.

```bash
WT=$(mktemp -d)/mordida
git worktree add -q --detach "$WT" HEAD
ln -s "$PWD/.venv-mlx" "$WT/.venv-mlx"      # venv e voz vêm do repo
ln -s "$PWD/voices" "$WT/voices"
printf '{"enabled": true, "keys": [{"id": "fake1", "name": "dono-fake",
  "secret": "sk-fake-123456", "created_at": "2026-01-01 00:00:00", "role": null}]}' \
  > "$WT/.apikeys.json"
printf 'sk-fake-123456\n' > "$WT/.apikey"
REAL=$(shasum -a 256 .apikey .apikeys.json)
VOZES=$(ls -lT voices | shasum -a 256)     # `voices/` é LINK: escrita cairia no real
# SABOTAGEM: os caminhos de chave deixam de ir por ambiente (vazio = default)
sed -i '' 's/"TTS_APIKEYS_PATH": str(chaves)/"TTS_APIKEYS_PATH": ""/;
           s/"TTS_LEGACY_APIKEY_PATH": str(legado)/"TTS_LEGACY_APIKEY_PATH": ""/' \
  "$WT/tests/live_ws.sh"
(cd "$WT" && ./tests/live_ws.sh); echo "rc=$?"
[ "$REAL" = "$(shasum -a 256 .apikey .apikeys.json)" ] && echo "REAL INTOCADO"
[ "$VOZES" = "$(ls -lT voices | shasum -a 256)" ] && echo "VOICES INTOCADO"
git worktree remove --force "$WT"
```

| Sabotagem | Resultado |
|---|---|
| `"TTS_APIKEYS_PATH": ""` e `"TTS_LEGACY_APIKEY_PATH": ""` | `✘ a chave criada NÃO caiu no arquivo sintético: isolamento furado`, o sintético muda e recebe `probe-live-ws`, e os sha256/a listagem de `voices` do real ficam **idênticos** |

O `voices/` é link para o diretório do dono porque a fala de teste vem de lá: a
célula também cobra que a listagem não mude (o Live materializa preset em
`voices/` quando a voz não tem amostra — com link, escrita cairia no real).

Duas armadilhas medidas: (1) sabotar **apagando** as linhas com `sed` remove
junto o `env.update({` e o arquivo quebra com `IndentationError` — use a forma
"vazio = default"; (2) o worktree precisa dos links de `.venv-mlx` e `voices/`
(sem eles a suíte não sobe ou não acha fala de teste).

## #535 — runner: `navegador` no app do dono e teto comido pela fila

Duas sabotagens no runner, uma célula cada (as sondas vivem em
`tests/fixtures/run_sh/` e entram por `SUITES=`, sem tocar nas listas reais):

| Sabotagem | Resultado |
|---|---|
| `navegador` deixa de dar `unset BASE …` (volta a exportar `:7860` + escapes) | `test_navegador_nao_exporta_base_nem_os_escapes_do_dono` **FAILED** |
| o runner volta a armar o teto do WORK antes da trava (`if false`, sem `TTS_TETO_POS_TRAVA`) | `test_tempo_esperando_a_trava_nao_come_o_teto` **FAILED** |
| `serial_pega` deixa de armar o teto ao obter a trava | `test_suite_que_dorme_alem_do_teto_reprova_pelo_alarme` **FAILED** |

Controles: `test_instancia_exporta_base_e_liga_os_escapes_no_7860` (o outro lado do
BASE) passa nos dois sentidos.

Achado de medição que virou código: **o bash adia o trap de ALRM enquanto um filho
em foreground roda**. Suíte que trabalha dentro de UM `python` longo (as do Live)
só veria o alarme quando ele terminasse — a primeira versão do watchdog não matava
nada e o `live_barge_rep` passou de 9 min. O watchdog agora derruba os filhos
diretos e só então o shell: é o que faz o teto valer para essas suítes.

## #535 (achado do PM) — identificação de QUEM escreve no estado do dono

Duas guardas novas, uma por executor, e a sabotagem de cada uma:

| Sabotagem | Resultado |
|---|---|
| runner deixa de comparar a impressão digital (`_arquivos_tocados` sem uso) | `test_runner_nomeia_a_suite_que_escreveu_no_estado_do_dono` **FAILED** e `test_knob_exige_dono_intocado_vira_falha` **FAILED** |
| `_atribuir_escritas` sem efeito (não registra o teste) | `test_guarda_nomeia_quem_tocou_o_arquivo_do_dono` **FAILED** |

O controle `test_suite_que_nao_escreve_nao_aparece` passa de propósito. O hook do
`conftest` é pós-teste e nomeia pelo `item.nodeid`; a prova de que ele está ARMADO
é o próprio `[dono] … escreveu settings.json` que aparece no resumo do pytest
quando algum teste toca o arquivo (visível ao rodar esta suíte). Sem o knob
`TTS_EXIGE_DONO_INTOCADO=1` é AVISO, porque o app do dono pode gravar ao lado.

## #526 — `tools/baixa_modelos.sh`: divergência e idempotência

O script materializa pesos conferidos por sha256; baixar de novo à toa é caro
(85 MB) e sobrescrever divergência em silêncio troca o modelo sem ninguém ver.

| Sabotagem | Resultado |
|---|---|
| aceitar divergência sem `--forca` (sobrescreve) | `test_divergencia_e_erro_e_forca_rebaixa` **FAILED** |
| rebaixar mesmo com sha256 conferindo | `test_segunda_rodada_nao_rebaixa` **FAILED** |

O servidor do teste é um `http.server` local (`TTS_MODELOS_URL`): nada de rede no
CI. `test_app_nao_baixa_modelo_no_boot` e `test_manifesto_lista_todos_os_pesos`
cobrem o contrato do card por leitura (o boot não chama o downloader; peso sem
sha256 no manifesto reprova).

## #678 — `htdemucs` offline e pinado

| Sabotagem | Resultado |
|---|---|
| fonte do peso removida de `FONTES` (manifesto passa a ter entrada órfã) | `test_baixa_modelos` **FAILED** (6 células) — o `--check` acusa "está no manifesto e não tem fonte" |
| `demucs` sem `==` | `test_worker_nao_usa_pacote_fora_do_requirements` **FAILED** ("está sem `==` e fora da ALLOWLIST") |
| `demucs` fora do `requirements.txt` | idem **FAILED** ("importa 'demucs' e não há 'demucs' no requirements.txt") |
| `demucs` fora dos DOIS arquivos | idem **FAILED** (as células que comparam requirements↔lock sozinhas não pegavam — #684) |
| peso ausente do disco | célula offline **skipa** (o skip nomeia o script) |

## #727 — cópia do dado do dono no worktree

| Sabotagem | Resultado |
|---|---|
| checagem removida do `integra.sh` | `test_integra_recusa_worktree_com_copia_do_dado_do_dono` **FAILED** (integra com `voices`/`settings` copiados) |
| comparação por NOME em vez de sha | `test_verifica_por_conteudo_nao_por_nome` **FAILED** (voz copiada como `fixture.wav` passaria) |

A guarda só LÊ: compara sha256 dos arquivos de `voices/`, `outputs/` e dos quatro
de estado com os da raiz principal, nomeia cada cópia e nunca apaga nada.

## #690 — `integra.sh` de qualquer worktree e recusa de estado no commit

| Sabotagem | Resultado |
|---|---|
| raiz resolvida pelo diretório do script (`dirname/..`) | `test_integra_acha_a_raiz_chamado_de_dentro_de_outro_worktree` **FAILED**: "a raiz está em 'wt/agenteb' — a integração é sempre na main" |
| recusa de estado desligada | `test_integra_recusa_branch_que_leva_estado` **FAILED** (branch com `voices` symlink e `settings.json` passa) |
| `worktrees.sh` sem o `git check-ignore .venv-mlx` | `test_worktrees_falha_quando_venv_nao_esta_ignorado` **FAILED** |
| raiz com WIP não commitado | `test_integra_recusa_raiz_suja` **FAILED** ("trabalho não commitado") |
| atalho de hook aceito fora de tmp | `test_atalho_de_hook_so_vale_em_arvore_de_tmp` **FAILED** (hook não roda e nada avisa) |
| célula apontando o `integra` para o repo REAL | guarda de módulo **FAILED** (HEAD/branch/worktree do repo real mudou) |

## #688 — harness do Live não deixa servidor órfão

| Sabotagem | Resultado |
|---|---|
| `mata_servidor` vira no-op | `test_sigint_no_harness_nao_deixa_servidor` **FAILED** ("o servidor do harness sobreviveu ao SIGTERM") |
| `mata_servidores_antigos` não varre (early return) | `test_mata_orfaos_do_harness_sob_demanda` **FAILED** (o grupo órfão sobrevive) |
| varredura sem o filtro de dono vivo (mata todo pidfile de `/tmp`) | `test_varredura_de_orfao_nao_mata_rodada_viva` **FAILED**: "a varredura matou o servidor de uma rodada VIVA" |

O SIGTERM (`timeout`) é o caso que vazou: o SIGINT já chega como KeyboardInterrupt e
passa pela limpeza normal; o TERM não levanta nada. O trap cobre os dois e a rodada
seguinte varre pidfiles de grupos anteriores — varredura que NÃO pode derrubar
rodada concorrente: o dono do pidfile (PID no nome) vivo é vizinho, morto é órfão.

## #710 — sonda não pode escrever na raiz do repo (é o settings do DONO)

| Sabotagem | Resultado |
|---|---|
| volta o fallback `"${TTS_DONO_DIR:-$AQUI/../../..}"` e tira a guarda da sonda | `test_sonda_sem_dono_dir_nao_escreve_na_raiz` **FAILED**: a sonda devolve 0 em vez de 2 |
| tira o bloco do runner que dá a CÓPIA à sonda | mesma célula **FAILED**: `FALHA … suja_o_dono.sh … (rc=2)` |
| célula da guarda volta a tocar o arquivo REAL (mtime e flag `uchg`) | `test_guarda_nomeia_quem_tocou_o_arquivo_do_dono` deixa de provar o dono FALSO — no arquivo real o mtime muda a cada rodada |

A célula cobre as duas pontas: a sonda SOZINHA sem a env tem de sair 2, e a rodada
pelo runner sem a env tem de deixar o `settings.json` da raiz com sha **e** mtime
iguais (e ainda nomear a escrita, que acontece na cópia).

## #723 — segredo do dono não fica em /tmp

| Sabotagem | Resultado |
|---|---|
| desliga a guarda `conta_segredo_em_tmp` do runner | `test_segredo_do_dono_deixado_em_tmp_derruba_a_rodada` **FAILED** (rodada passa com o segredo em `/tmp`) |
| agulha com `%%\n*` (casa o `n` literal) | chave com `n` trunca a agulha e a rodada não acusa |
| `conta_segredo_em_tmp` volta sem imprimir nada | `guarda-muda`: a rodada reprova com "guarda de segredo não respondeu" |
| retrato volta a nome/mtime (sem hash) | `test_cp_p_por_cima_de_homonimo_e_acusado` **FAILED** (o `cp -p` sobre homônimo passa limpo) |
| volta a ler `.apikey` da raiz em vez do canário | `test_runner_nao_abre_arquivo_do_dono_para_a_guarda_de_segredo` **FAILED** |

O caminho: o app semeia a chave do AMBIENTE (`TTS_ROD_API_KEY`) e a persiste no
arquivo de chaves da rodada — o segredo real ia parar em `/tmp` (191 arquivos) e as
suítes não limpavam. A guarda varre `/tmp` pelos arquivos criados na rodada que
contêm o segredo e acusa; a suíte que limpa passa.

## #703 — suíte de medição não liga o `voices` do dono

| Sabotagem | Resultado |
|---|---|
| volta o `ln -s "$RAIZ/voices" "$ARVORE/voices"` | `test_freeze_nao_liga_o_voices_real_e_duas_rodadas_seguidas` **FAILED**: "voices é LINK" |

O `ln -s` sem `-n` SEGUE um alvo que já é link e cria `voices/voices` DENTRO do dado
do dono (aconteceu duas vezes, 03:20 e no `cf52376`). A cópia passa a ter pasta
própria com a fixture e exporta `TTS_VOICES_DIR`; a célula roda DUAS vezes seguidas
e cobra nenhum symlink na pasta da cópia e a listagem real igual.

## #659 — suíte em worktree SEM estado do dono

| Sabotagem | Resultado |
|---|---|
| célula volta a tocar `app.BASE/"settings.json"` (o arquivo do dono) | `test_worktree_sem_estado` **FAILED** |
| `worktrees.sh` volta a copiar `settings.json`/`.apikey*` para o worktree | a guarda não pega sozinha (o arquivo passa a existir); quem cobra é a revisão do script + a lista de "nunca compartilhados" no cabeçalho |

A guarda monta um worktree de verdade e roda a fatia que já caiu por isso: ela
reprova por ARQUIVO AUSENTE, que é o defeito (célula lendo o caminho real em vez
do caminho que o `conftest` aponta).

## #649 — DNSMOS offline em `models/dnsmos`

| Sabotagem | Resultado |
|---|---|
| `sig_bak_ovr.onnx` trocado por arquivo que não é ONNX | `test_ovrl_roda_offline_com_hfhome_vazio` **FAILED** (`INVALID_PROTOBUF`) |
| membro extraído do wheel entra no índice (`git add -f`) | `test_artefato_fica_fora_do_git` **FAILED** |
| `NOTICE` sem o texto MIT | `test_modelos_sao_regulares_e_a_licenca_esta_ao_lado` **FAILED** |

O caso do DNSMOS tem uma volta a mais que os outros: o artefato vem EMPACOTADO (o
wheel do `speechmos`), então o script tem dois modos novos — `url:` no `FONTES` e
`zip:` no `DERIVADOS` — e o teste do script monta um wheel-ZIP de mentira, com o
espelho local substituindo a URL do PyPI.

## #622 — grafo do DFN3 offline em `models/`

| Sabotagem | Resultado |
|---|---|
| `TTS_DFN3_DIR` aponta para um `deepfilter.onnx` que não é ONNX | `test_grafo_carrega_offline_com_hfhome_vazio` **FAILED** (`INVALID_PROTOBUF`) |
| peso do DFN3 entra no índice (`git add -f`) | `test_artefato_fica_fora_do_git` **FAILED** |

O skipif segue `TTS_DFN3_DIR`/`models/dfn3-onnx` (caminho de produção) e só cai na
bancada como queda: sem o grafo a célula de carga **skipa**, não reprova.

## #635 — `state/metrics` (e o mtime) é nota, não veredito

O knob `TTS_EXIGE_DONO_INTOCADO=1` reprovava por `state/metrics/` que o app do
PRÓPRIO dono anexa pelo polling da tela (+171 B em 40 s com o dono intocado).

| Sabotagem | Resultado |
|---|---|
| `state/metrics` volta para dentro de `_impressao_dono` (veredito) | `test_metrics_do_dono_vira_nota_e_nao_reprova_o_knob` **FAILED** |

O controle `test_settings_do_dono_continua_reprovando_o_knob` garante que a nota
não virou passe livre para os três arquivos de leitura-escrita.

O mesmo vale para o **mtime**: o app do dono, num rev antigo, regrava o
`settings.json` com o MESMO conteúdo (mtime muda, sha256 não). O veredito é por
sha256 e o mtime é nota (`_arquivos_mudados_conteudo`).

| Sabotagem | Resultado |
|---|---|
| veredito volta a olhar a impressão inteira (mtime junto) | `test_reescrita_do_mesmo_conteudo_nao_reprova_o_knob` **FAILED** |

A sonda `tests/fixtures/run_sh/reescreve_o_dono.sh` espera 1 s ANTES de escrever:
o `stat` só tem resolução de segundo e o retrato "antes" é tirado no instante em
que a suíte começa.

## #621 — medir mutando o `app.py` sem sujar a árvore compartilhada

| Sabotagem | Resultado |
|---|---|
| suíte volta a mutar `$RAIZ/app.py` (rodar com `FREEZE_ARVORE=$PWD`) | **FAILED**: "a suíte mutou a árvore principal em vez da cópia" |
| sentinela vazado no `app.py` da raiz (o defeito do #670) | **FAILED**: "o sentinela vazou para o `app.py` da árvore" |
| célula de marca d'água volta a pedir `voice: "voz-x"` (sem voz no worktree) | guarda #659 **FAILED**: `_ModeloFalso` sem `generate` (a resolução cai no preset) |
| sonda do import escreve no `settings.json` da raiz (mesmo restaurando) | `test_import_com_os_caminhos…` **FAILED**: "✖ TTS_EXIGE_DONO_INTOCADO=1" |
| célula do #621 com o `worktree remove` do `finally` desligado | **FAILED**: "a árvore descartável ficou em …" e o worktree segue no `git worktree list` |

A célula roda o cenário num worktree descartável desde o #670 — antes ela mesma
escrevia a sentinela no `app.py` da raiz (append + restore no `finally`, que morre
junto se a rodada for interrompida). Além do caminho da cópia e da forma do bloco,
ela cobra sha256 **e** mtime da raiz: a versão antiga restaurava o conteúdo, então
só o mtime denuncia — e é justamente ele que faz o `cp`-de-volta parecer intacto
para quem olha o `git status`.

Varredura da classe (#670): nenhuma outra célula do `test_run_sh` nem sonda de
`tests/fixtures/run_sh/` escreve arquivo versionado da raiz — o que elas tocam é
`settings.json`/`.apikey*` (não versionados, com backup e restore) e tmp. O risco
que sobra é o `finally` não rodar em rodada interrompida, e é o que o worktree
descartável resolve.

## #592 — teto pós-trava: rótulo, escada e rc do trap

Três defeitos, três mordidas. A célula do filho surdo cobra o PID MORTO: só a
mensagem de teto não prova nada, porque o teto externo do runner também reprova e
a suíte ficaria pendurada. `.pyc`-safe (é shell): nada de recompilação.

| Sabotagem | Resultado |
|---|---|
| rótulo volta a adivinhar por "sou eu o pid do lock?" (sonda troca o `pid`) | `test_rotulo_do_estouro_e_trabalho_e_nao_fila` **FAILED** |
| escada para no TERM (sem o KILL nos filhos) | `test_filho_surdo_a_term_ainda_morre_e_deixa_o_motivo` **FAILED** (medido: "o filho surdo … sobreviveu à suíte") |
| `return 0` sai do fim do `_teto_matar` | `test_suite_verde_sem_teto_sai_zero` **FAILED** |

O `return 0` só morde sob `set -e` (é ele que faz o bash ADOTAR o status do trap
de EXIT); a sonda usa `set -euo pipefail` de propósito, como as suítes.

## #526-bis — separador offline a partir de `models/`

O separador NÃO usa `from_hparams` (é `torch.load` do arquivo local), então o que
o prende ao disco são o caminho e o manifesto. As células fecham a rede de
propósito: `HF_HOME` vazio, `HF_HUB_OFFLINE=1` e `TTS_MODELOS_URL` apontando para
uma porta morta. `.pyc` removido com `tests/limpa_bytecode.sh tools/bakeoff/separa.py`
antes de cada linha.

| Sabotagem | Resultado |
|---|---|
| `MODELOS` volta ao `/tmp/tts-bakeoff/vendf/mf2_ss` (efêmero, sem os pesos) | `test_carrega_offline_com_hfhome_vazio` **FAILED** |
| `disponivel()` sem olhar o arquivo (sempre `True`) | `test_sem_os_pesos_a_disponibilidade_cai_e_a_mensagem_diz_o_script` **FAILED** |
| editar um byte de `separa_vend/mf2_ss/fsmn.py` | `test_codigo_vendorizado_e_o_da_revisao_fixada` **FAILED** |
| `import funasr` num arquivo vendorizado | `test_vendor_nao_puxa_pacote_alem_de_torch_einops_rotary` **FAILED** |

O pin tem prova própria fora destas células: `pip install --dry-run
--ignore-installed -c requirements.lock einops rotary_embedding_torch` resolve
`einops-0.8.2`/`rotary-embedding-torch-0.9.1` e mantém `torch-2.12.1` — nenhum
torch/transformers/mlx se move (medido em 2026-10-28).

## #547 — cadeia de interrupção: as DUAS direções

A cadeia tem extremos opostos e cada sabotagem só morde por um lado. A receita
abaixo prova que o `smoke_barge_intencao.py` morde nas duas (medido em
2026-10-28, `.pyc` removido com `tests/limpa_bytecode.sh live_turns.py` antes de
cada célula):

| Sabotagem em `live_turns.classifica_barge` | Resultado |
|---|---|
| `return "backchannel"` → `return "interrupcao"` ("sempre interrupção") | `backchannel` **0/25** → VEREDITO REPROVADO |
| `return "interrupcao"` final → `return "backchannel"` ("sempre backchannel") | `curto` **2/20** → VEREDITO REPROVADO |

Por que os corpora existem: `CONTEUDO` (3+ palavras de conteúdo próprio) e
`CURTO` (1–2 palavras fora da lista de backchannel) são os únicos grupos que
CHEGAM ao fim da função. `COMANDO`/`PERGUNTA` casam regras anteriores e seguem
verdes com a regra final sabotada — sem esses dois grupos a última linha da
função poderia estar morta sem ninguém notar.

Contraprova da cadeia (não da função): `test_cada_filtro_desligado_muda_o_desfecho`
e `test_paridade_entre_cadeia_e_classifica_barge` cobrem o `on` de cada filtro e a
paridade passo a passo — a cadeia não reimplementa a regra.

### O que a suíte de áudio (`live_barge_rep.sh`) cobra por bloco

| Bloco | Cenário | Piso |
|---|---|---|
| `comando` | "para, para, espera um pouco" no MEIO do playback → corta | 5/6 |
| `backchannel` | "uhum, sim" no meio → NÃO corta | 5/6 |
| `fim` | frase no ÚLTIMO segundo → não corta e é respondida depois | 5/6 |
| `misto` / `imediata` | diagnóstico / modo antigo | 4/6 |

Mordida do `fim`: voltar o default de `fim_de_resposta.restante_ms` para 1000
reprova — a regra passa a disparar depois do fim do áudio, quando a janela de
barge já fechou. O cenário tem de injetar na janela (turno `ocioso` + playback
ABERTO); sem essa preparação a fala entra no turno em voo e não vira barge.

**Condição da mordida (medida em 2026-11-07):** ela só morde quando a decisão
tem TEXTO. Com o STT estourando o prazo (fala de ~100 ms sob carga) o desfecho
cai em `ruido` antes da cadeia e a anotação não acontece nem com 2000 — o bloco
passaria nos dois sentidos. Por isso o resumo publica `decisões com texto` e a
cobra da anotação só vale quando há ao menos uma: rode a mordida em janela
folgada (o bloco também cai de 6/6 para 4–5/6 com load ≳ 30 sozinho).

## #533 — portão "só a minha voz" (smoke com o corpus do R4)

Duas mordidas no `smoke_so_minha_voz.py` (corpus `/tmp/tts-bakeoff/fala`, do
`tools/bakeoff/corpus.py publico`). Alvo do card: ≤1 turno falso em 10 min só com
a 2ª voz. Medido em 2026-10-28, carga ~5:

| Sabotagem | Resultado |
|---|---|
| `SEM_PORTAO=1` (portão aceita tudo) | terceiro **96 turnos falsos** em 2 min → REPROVADO |
| `CONFIRMACOES=1` (uma janela só) | terceiro **48 turnos falsos** em 10 min → REPROVADO |

Com o portão como está: terceiro **0** turnos falsos em 10 min, cobertura do dono
90,5 %, latência p50 36 ms (piso 50). O `misto` (dono + terceiro a 5 dB +
ventoinha, SEM a separação) fica em ~40 % de cobertura com piso próprio (35) — é o
pior caso do portão, e o motivo de a separação existir.

`LIMIAR=` troca o limiar em uso: no corpora do R4 o FAR 1 % (2,434) custa caro
(57 % de cobertura); 1,8 fica no joelho (90 % com 0 falso). O número fica impresso
em toda rodada para o PM decidir.

## #674 — pós-corte: fala depois do `interrupted` não pode sumir

Cadeia medida: a decisão de interrupção consumia o áudio do trecho → o turno
seguinte nascia com 224 ms → VAD recusava (`turno_descartado motivo=vad`) → sessão
`idle` para sempre. Três correções, cada uma com sua mordida:

| Sabotagem | Resultado |
|---|---|
| decisão síncrona no `_live_loop` (sem thread) | o loop trava e o turno abre 12–14 s depois |
| calibração do eco reiniciando a cada chunk | `_calibrando` nunca converge, `_avaliar_abertura` nunca roda |
| tirar o prepend do áudio do trecho em `_live_abre_turno` | o turno nasce com a cauda e morre em `vad` |
| tirar a janela de fusão (`barge_fusao_ms`) | `test_suspeita_nova_dentro_da_fusao_nao_abre_barge` **FAILED** |

Aceite medido com `MEDE_POS_CORTE`: **11 rodadas seguidas com áudio, 0 travadas**,
54–1269 ms (p95 ≈ 1,3 s). `N_CORTES=3` → **3/3 voltaram a falar**.

### Janela 2: o backlog do playback sobrevive ao corte

A `interrupted` manda o cliente descartar a fila, mas o motor seguia contando o
backlog inteiro como "vai tocar" (9,5–19,4 s medidos). Dentro dessa janela
fantasma a fala seguinte era lida como BARGE: suspeita → `interrupcao` → matava a
resposta que acabara de nascer (17 decisões e 24 `interrupted` em 6 rodadas), e
com o piso de ECO — alto, de um TTS que já não toca — a fala nem passava do
limiar: motor `ocioso` recebendo voz e nenhum turno abrindo (26 s sem áudio).

| Sabotagem | Resultado |
|---|---|
| `playback_cortado` virando no-op | `test_corte_fecha_a_janela_o_playback_descartado_nao_conta` e `test_fala_depois_do_corte_abre_turno_normal_e_nao_barge` **FAILED** |
| soltar o `_cortado` no `set_speaking(False)` (cauda de 900 ms volta) | `test_corte_fecha_a_janela_o_playback_descartado_nao_conta` **FAILED** |
| tirar `_funde_ate` do `_avaliar_abertura` | `test_continuacao_da_fala_depois_do_corte_nao_mata_a_resposta_nova` **FAILED** |

| tirar a CHAMADA do `playback_cortado` no `_live_cancela` (app.py) | `test_cancel_manda_o_motor_descartar_o_playback` e `test_cancel_com_pipeline_ocupado_tambem_descarta_o_playback` **FAILED** |

`decisao_ms` também voltou a ser real: a decisão pelo prazo chegava com
`fala_ms=0` (o evento `barge_suspeito` não carrega o campo) e o `fala_sustentada`
nunca disparava — decidia-se pelo texto de um fragmento de 200 ms.

## #707 — `comando`: o braço do barge zerava na pausa entre palavras

O contador do braço zerava no PRIMEIRO frame abaixo do limiar. "Para, para,
espera um pouco" tem pausas: o burst parava em 2–3 de 10, nenhuma suspeita saía
e a rodada caía num turno de 64 ms — que o VAD do STT recusa (`motivo=vad`, texto
vazio em 3 ms) — virando `fala_sustentada` ou nada. A folga (`barge_folga_ms`,
128 ms) só zera depois de silêncio SEGUIDO; eco é plano e não acumula.

| Sabotagem | Resultado |
|---|---|
| `_conta_barge` zerando no 1º frame sem voz (folga 0) | `test_braco_do_barge_aguenta_a_pausa_entre_palavras` e `test_pausa_entre_palavras_ainda_abre_a_suspeita` **FAILED** |

`barge_pico` no `stats.motor` mede o burst máximo do episódio: abaixo do alvo é
a fala que não sustentou; no alvo sem suspeita é outra coisa. Medido em
`BLOCO=comando`: 4 rodadas seguidas de 6/6 (antes, 3/6 e 4/6 com o pico travado).

### Perfil LEGADO com o ECAPA ligado (confirmação pedida pelo PM)

O portão pontua o perfil com o extrator QUE O GEROU; senão o ECAPA (192-d) era
comparado com o centróide do Resemblyzer (256-d) e o `_cos_sim` truncava calado.
AS-norm também só vale para perfil do extrator ATUAL: perfil legado vai para o
limiar CRU do Resemblyzer mesmo com coorte disponível.

| Sabotagem | Resultado |
|---|---|
| deixar o AS-norm valer para qualquer modelo | `test_portao_de_perfil_LEGADO_usa_o_extrator_do_perfil_e_limiar_cru` **FAILED** |
| pontuar com o extrator EFETIVO em vez do extrator do perfil | mesma célula **FAILED** (o dublê do ECAPA dispara) |

### Separação do turno no Live (item 2 do #533)

Opcional e desligada por default; roda atrás do turno já aberto, com o extrator
DO PERFIL e queda para o áudio cru.

| Sabotagem | Resultado |
|---|---|
| `_separacao_do_turno` ignorando o `separacao` do settings | `test_separacao_desligada_nao_custa_nada` **FAILED** |
| `_stt_app` transcrevendo o áudio CRU com a separação ligada | `test_stt_do_turno_transcreve_o_audio_separado_e_limpa` **FAILED** |

### Prewarm do separador

| Sabotagem | Resultado |
|---|---|
| `_separa_prewarm` aquecendo com a separação DESLIGADA | `test_prewarm_da_separacao_so_com_o_modo_ligado` **FAILED** |

### #569 — comando vence o `fim_de_resposta`

| Sabotagem | Resultado |
|---|---|
| subir o `fim_de_resposta` para antes do `palavras_corte` na cadeia | `test_comando_no_ultimo_segundo_CORTA_mesmo_com_fim_de_resposta` **FAILED** |
| idem, contraprova de ordem | `test_passo_de_comando_transcreve_a_janela_NOVA` **FAILED** |

### O joelho é SÓ do portão do Live

A faixa do joelho (`1.8` → `joelho`, `_VOZ_LIMIAR_JOELHO`) é constante própria do
portão de turno; a tabela `_SPEAKER_LIMIAR_FAR` fica com as bandas de FAR
CALIBRADO usadas pela identidade (`speaker_gate`, anti-clone, "só a voz de:").

| Sabotagem | Resultado |
|---|---|
| `_voz_limiar` devolvendo o FAR 1 % para a faixa "joelho" | `test_portao_usa_o_joelho_do_live_e_a_identidade_fica_no_far_1` **FAILED** |

### #527 — filtro `identidade` na interrupção

| Sabotagem | Resultado |
|---|---|
| tirar o ramo `identidade` da cadeia | `test_filtro_identidade_terceiro_continua_e_leva_o_score` **FAILED** |
| ligar o filtro por default | `test_defaults_tem_os_dez_filtros_com_modelo_e_identidade_desligados` **FAILED** |

### #559 — tolerância de `underrun` tem TETO

A tolerância por carga (1 vão a cada 5 chunks) é proporcional, mas sem teto solto
passaria a tolerar muitos vãos em turno longo (50 chunks = 10). O cap é 3.

| Sabotagem | Resultado |
|---|---|
| tirar `min(3, ...)` e deixar `max(1, n_chunks // 5)` | com 50 chunks e 4 vãos o turno passa (o teto reprova) |

### #596 — prazo e acerto do `provedor: dsh`

| Sabotagem | Resultado |
|---|---|
| tirar o piso de 900 ms do prazo no dsh | `test_modelo_dsh_tem_piso_de_prazo_de_900ms` **FAILED** |
| voltar o prompt antigo do filtro `modelo` | acerto no corpus cai de 91,4 % para 52,9 % (`mede_intencao.py --provedor dsh`) |

### #524 — o default do STT é CRU (não `filtrado`)

| Sabotagem | Resultado |
|---|---|
| trocar o default de `cru` para `filtrado` | `test_default_do_filtro_no_stt_e_CRU_travado` **FAILED** |
| remover o desvio `filtrado` do `_live_audio_cru` | mesma célula **FAILED** |

### #596 — `provedor: dsh` fora da oferta

| Sabotagem | Resultado |
|---|---|
| normalizador do filtro `modelo` aceitando `dsh` | `test_provedor_dsh_sai_da_oferta_e_cai_no_local` **FAILED** |

### #732 — todo `import app` em smoke/medição vem depois de `isola()`

A célula VARRE `smoke_*.py` e `tests/mede_*.py` e olha CADA `import app`: um
`isola()` no módulo não cobre um import que já aconteceu antes dele, e script com
dois caminhos precisa dos dois cobertos pela posição.

| Sabotagem | Resultado |
|---|---|
| tirar a chamada `isola()` de qualquer smoke (testado em `smoke_barge_intencao` e `smoke_worker_persist`) | `test_todo_script_que_importa_app_isola_o_estado_em_tmp` **FAILED** |
| importar o helper DEPOIS do import do app | mesma célula **FAILED** |
| deixar `isola()` só em COMENTÁRIO (`# isola()`) | mesma célula **FAILED** (nomeia as linhas do import) |
| pôr `from app import X` antes do `isola()` | mesma célula **FAILED** (`smoke_sintese.py:179`) |

### #501 — a matriz de intenção (três mordidas)

| Sabotagem | Resultado |
|---|---|
| `BARGE_MODO=imediata` (energia pura) | backchannel **0/25** → REPROVADO |
| `return "interrupcao"` final trocado por `backchannel` | grupo `curto` **2/20** → REPROVADO |
| `return "backchannel"` trocado por `interrupcao` | backchannel **0/25** → REPROVADO |

### #509 — o filtro `modelo` muda o desfecho dos ambíguos

O grupo `ambíguo` do `smoke_barge_intencao.py` só existe com `BARGE_MODELO=<p>` e a
própria rodada confere que SEM o filtro os casos não cortam (é o que os torna
ambíguos). `BARGE_MODELO=0.9` → 20/20; `BARGE_MODELO=0.1` → **0/20, REPROVADO**.

| Sabotagem | Resultado |
|---|---|
| `BARGE_MODELO=0.1` (o modelo manda continuar) | grupo `ambíguo` **0/20** → REPROVADO |
| corpus "ambíguo" com casos que as regras já cortam | a checagem "sem o filtro ≤2" reprova |

## #778 — prefixo duplo `/ttsproxy/ttsproxy/` (bypass de auth)

O Starlette tira o `root_path` de novo e a rota protegida casava sem passar pela
chave: qualquer aparelho da LAN (e a internet, pelo túnel) lia settings/vozes e
sintetizava com a voz do dono.

**Só 401 é cobertura**: é a rev corrigida (`_exige_chave` nega por padrão antes do
roteamento). **404 é a rev ANTERIOR** — passa no smoke (é o caminho de rollback)
mas com AVISO, e na sonda contínua é ALERTA, porque aceitar 404 como ok deixaria
a produção de hoje verde sem a correção. **2xx é o furo** e reprova.

| Sabotagem | Resultado |
|---|---|
| sonda removida do `deploy_mini.sh` | as TRÊS células do prefixo **FAILED** (2xx, 401 e 404) |
| sonda removida do `health_probe.sh` | `…_com_200_acusa_bypass` e `…_401_passa_na_rev_corrigida` **FAILED** |
| alvo responde 2xx no path duplicado | smoke/alerta reprovam e o deploy cai no rollback |
| sonda aceitando só 404 | `…_401_passa_na_rev_corrigida` **FAILED** (é o erro que passaria na suíte e quebraria no ar) |
| 404 tratado como `ok` na sonda contínua | `…_404_e_ALERTA_em_rev_antiga` **FAILED** |
| 404 sem a palavra de aviso no smoke | `…_avisa_mas_passa_no_404_da_rev_antiga` **FAILED** |

## #837 — `Host` público fora de `TTS_ALLOWED_HOSTS` (421)

O túnel entrega `Host: tts.the-dudes.com`; sem o nome na env o app recusa com
421 e a página sai do ar. A checagem é feita na LAN com o `Host` do público, para
valer mesmo com o túnel caído.

| Sabotagem | Resultado |
|---|---|
| sonda de Host removida do `deploy_mini.sh` | `test_smoke_reprova_quando_o_host_publico_da_421` **FAILED** |
| sonda de Host removida do `health_probe.sh` | `test_host_publico_fora_da_allowlist_vira_alerta` **FAILED** |
| `export TTS_ALLOWED_HOSTS` removido do `start-server.sh` | `test_start_server_exporta_o_host_publico` **FAILED** (o plist não chega ao app: sshd não herda) |

## #992 — limpeza da flag e do plist presas ao `rm` do PATH

O probe apagava a flag de alerta (na recuperação) e o plist (no `uninstall`) com o
`rm` resolvido por PATH. No harness de agentes o primeiro `rm` do PATH é um guard
que morre no parse de `-f` e sai **0 sem apagar** — as duas limpezas viravam no-op
silencioso: a flag ficava aberta mascarando alerta novo e o `uninstall` dizia
"removido" deixando o agente no lugar. Na máquina de quem não tem o guard no PATH
os mesmos testes passavam (suíte não hermética — visto primeiro no api-backend).

Fix: `/bin/rm` absoluto nas duas limpezas (o probe é de macOS) e o log da flag só
sai quando o arquivo sumiu. O shim de `rm` do teste reproduz o guard, e as células
conferem que ele **não** foi usado.

| Sabotagem | Resultado |
|---|---|
| `/bin/rm` de volta para `rm` (o shim vira o `rm` do PATH) | `test_recuperacao_zera_contador_e_limpa_a_flag` e `test_uninstall_tira_o_agente` **FAILED** |
| `rm` removido do caminho de recuperação | `…_zera_contador_e_limpa_a_flag` **FAILED** ("a flag ficou aberta") |
| `rm` removido do `uninstall` | `test_uninstall_tira_o_agente` **FAILED** ("o uninstall deixou o plist no lugar") |

## #1018 — a sonda copiava o estado REAL do dono para /tmp

O runner semeava o "dono" da sonda com `cp` do `settings.json`/`.apikey*` reais. Com
o `rm` do PATH no trap, a cópia nunca era apagada: 56 diretórios `dono-sonda.*`
acumulados em /tmp, 12 deles com o arquivo do dono dentro (viola a regra #728 — o
segredo real não deveria sair da máquina dele). Agora o estado é SINTÉTICO e o trap
usa `/bin/rm`.

| Sabotagem | Resultado |
|---|---|
| seeding volta para o `cp` do arquivo real | `test_sonda_semeia_estado_sintetico_e_nao_copia_o_dono` **FAILED** ("copiou o settings REAL do dono") |
| trap volta para o `rm` do PATH | a mesma célula **FAILED** na cobrança de o diretório sumir do /tmp |
| arquivo semeado fora de 0600 | a célula **FAILED** em `modo 600` |
| `TTS_*_PATH` de estado volta a ser herdado do shell (sai do `_ENV_ESTADO`) | `test_caminho_de_estado_do_dono_nao_atravessa` **FAILED**: "TTS_SETTINGS_PATH atravessou a neutralização (#1018)" |
| varredura do qa volta a `glob("*")` e a pular `conftest.py`/`_*.py` | a célula **FAILED** com `conftest.py` em "lendo o estado REAL do dono" |
| varredura volta a exigir `BASE /` (sem `REPO /`) — ponto cego 5 | arquivo de teste com `REPO / "settings.json"` passa verde e a célula **FAILED** o acusando |
| padrão do shell volta a só `$RAIZ/voices` (sem `${…}`, sem nome de estado) — ponto cego 4 | script com `${RAIZ}/settings.json` passa verde |

## #797 / #915 — marca fora do `_gen_lock`

O filho isolado grava o PCM cru (marcar no filho segurava o `_gen_lock` do pai
pelo tempo do IPC). O pai marca o trecho e o wav final depois que o filho sai.
No job local e no Live, `marca_wav` solta a trava e a repega antes de publicar.

| Sabotagem | Resultado |
|---|---|
| `_corre_sem_gen_lock` só chama `fn()` | `test_marca_nao_segura_o_gen_lock` **FAILED**: `Live marcou com o _gen_lock: [False, True]` |
| filho volta a chamar `marca_wav` | `test_filho_isolado_nao_marca` **FAILED** nas 11 famílias (a célula exige `chamadas == []`) |

O negativo (`test_trecho_cru_nao_acusa_marca`) segue: áudio cru não pode acusar marca.

## #915 — buraco do `_SpeechGate` (ticket sem dono)

`_pula_morto` devolvia True quando o ticket não tinha dono, e o `deliver`/`abort`
faziam `continue` sem `wait` e sem avançar. O Condition ficava preso num lookup
de dict; `/api/status` usa a mesma trava. O dono agora entra na mesma seção que
tira o ticket. Sem dono, espera curto e o prazo vale.

| Sabotagem | Resultado |
|---|---|
| `if job_id is None: return True` (continue seco) | `test_gate_buraco_sem_dono_espera_e_status_responde[deliver]` **FAILED**: `status não respondeu com o buraco aberto` |

`abort` é o mesmo ramo de `_pula_morto`. Com o conserto, deliver e abort voltam no prazo (CPU da thread abaixo de 0,25 s) e `status()` lê `speech_queue_depth == 2` com o buraco aberto.

## #915 → task_ee97f0f6 — watchdog só Python (o prazo C saiu)

Até o #1050 o watchdog também armava `dump_traceback_later(repeat=True)`, uma
thread C que despeja SEM a GIL. Com a GIL só disputada (o laço atrasou e outras
threads seguem em Python), ela lê frame liberado e falha em `dump_frame`: SIGSEGV,
ou a thread girando a 100 % de CPU enquanto o `cancel_dump_traceback_later` do
rearme a espera SEGURANDO a GIL, com o processo vivo e parado. Ficou só o
despejo Python (`_saude_despeja`, com a GIL). `<= 0` desliga; antes, 0 despejava a
cada 30 s sem nada em voo, porque `0.0 < 0` é falso.

| Sabotagem | Resultado |
|---|---|
| voltar o `app.py` com o prazo C (rearme no laço e no /health) | `test_nenhum_caminho_arma_o_prazo_c` **FAILED** (`dump_traceback_later` chamado) e `test_teto_zero_desliga_e_nao_despeja_sem_nada_em_voo` **FAILED** (log nasce com teto 0) |
| tirar o `if limite <= 0: return` do passo | `test_teto_zero_desliga_e_nao_despeja_sem_nada_em_voo` **FAILED** |
| `tests/conftest.py` sem `TTS_HEALTH_TRAVADO_S=0` | `test_sessao_nasce_sem_prazo_c_e_com_log_isolado` **FAILED** (`3.0 == 0`) |
| comentar `app.add_middleware(_SaudeWatchdog)` | `test_watchdog_esta_registrado_por_fora_do_teto` **FAILED**: `_SaudeWatchdog` ausente de `user_middleware` |

Ponto cego aceito: com a GIL presa numa chamada C o laço não roda e não há
despejo. Para esse caso, amostrador de fora (`sample <pid>`).

## #812 — retorno remoto: onde a marca é contada

O caminho remoto já marcava (compartilha o laço do job); a célula existe para a
regressão não passar despercebida.

| Sabotagem | Resultado |
|---|---|
| desligar o bloco de marca (`if False`) | `test_retorno_remoto_sai_marcado` e `test_fallback_para_local_marca_uma_vez_so` **FAILED** |

**A armadilha da célula do fallback**: ela conta CHAMADAS de `marca_wav`, não
probabilidade. Marca dupla continua dando detecção 1,0 — medir só a probabilidade
aprovava o defeito. Quem reescrever a célula, conte a chamada.

### #801 — o filtro `eco` lê a fala em reprodução do PIPELINE

`sess["ultimo_assistente"]` nunca era gravada: o filtro `eco` estava MORTO e o
classificador `modelo` decidia com referência vazia.

| Sabotagem | Resultado |
|---|---|
| voltar a cadeia para `sess.get("ultimo_assistente")` | `test_filtro_eco_usa_a_fala_em_reproducao_do_pipeline` **FAILED** (`frase_longa`) e `test_filtro_modelo_recebe_a_referencia_de_eco_do_pipeline` **FAILED** |

### #802 — motor concorrente: RLock e época por suspeita

| Sabotagem | Resultado |
|---|---|
| tirar o `with self._lock` de um método público | `test_metodos_publicos_do_motor_tomam_o_lock` **FAILED** (nomeia o método) |
| tirar a guarda de época do `decidir_barge` | `test_decisao_de_epoca_velha_nao_derruba_a_suspeita_nova` **FAILED** |
| voltar a zerar `_suspeito_audio` na suspeita nova | `test_segunda_suspeita_antes_da_decisao_preserva_o_audio_das_duas` **FAILED** |
| tirar o `abrir_turno` atômico | `test_abertura_concorrente_do_pipeline_nao_duplica_nem_perde_trecho` **FAILED** |

### #803 — passo de comando: uma passada por vez no whisper

| Sabotagem | Resultado |
|---|---|
| tirar o `with _stt_lock` da passada de comando | `test_passadas_de_comando_nao_rodam_o_whisper_em_paralelo` **FAILED** ("whisper chamado 2x em paralelo") |
| tirar a checagem `desistiu` entre janelas | `test_janelas_param_no_ponto_seguro` **FAILED** ("rodou 3 janelas depois de desistir") |
| usar uma trava NOVA de módulo no passo (não a `_stt_lock`) | `test_comando_e_turno_disputam_a_MESMA_trava_do_whisper` **FAILED** ("whisper chamado 2x") |
| parar de repassar `desistiu=` na rota | `test_rota_do_comando_adianta_o_desistiu_ate_as_janelas` **FAILED** |

### #804 — o Live não pode pular o `_gen_lock`

A mordida original (o modo de TTS remoto pulando a trava) saiu junto com o TTS
remoto (#1059). A trava do Live segue coberta por `test_live_metal_lock.py` e
`test_tts_do_turno_segura_o_gen_lock_como_o_job_normal` (contraprova com
`contextlib.nullcontext()` no lugar do `_gen_lock`).

### #805 — taxa do modelo × taxa do fio

O filho dita a taxa (Fish 44,1k, VoxCPM2/MOSS 48k); o fio, a janela de barge e o pós
são de 24 kHz. Sem reamostrar, 1 s vira 2 s no fio (2× lento) e uma oitava abaixo.

| Sabotagem | Resultado |
|---|---|
| devolver o áudio do filho sem `_reamostra` | `test_audio_do_filho_em_48k_chega_em_24k_sem_mudar_o_tom` **FAILED** ("48000 amostras") |
| tirar a reamostragem do caminho IN-PROCESS (`return r`) | `test_caminho_in_process_reamostra_para_a_taxa_do_fio` **FAILED** ("48000 amostras") |

### #870 — decisão do barge em 13 s, "Tchau." sumindo, `barge_suspeito` em rajada

Mini `9b0230c`, sessão `cea89ecf4c`: 22 `barge_suspeito` em 1 s (fusão a cada frame),
duas decisões de 13 s com o whisper em laço ("Tch-tch-tch…") e "Tchau." decidido com
`restante_ms=0` saindo `ruido`. Cada linha abaixo foi rodada com o conserto removido.

| Sabotagem | Resultado |
|---|---|
| tirar o `_barge_frames = 0` da fusão (`live_turns`) | `test_fusao_nao_emite_suspeita_a_cada_frame` **FAILED** ("51 suspeitas em 60 frames") |
| não desarmar timer/passadas da suspeita anterior | `test_suspeita_fundida_desarma_timer_e_passadas_da_anterior` **FAILED** |
| timer da suspeita sem `epoca=` | `test_timer_da_suspeita_leva_a_epoca` **FAILED** (`[None] != [7]`) |
| armar passadas com `palavras_corte` desligado | `test_sem_filtro_de_comando_nao_ha_passadas_de_stt` **FAILED** |
| transcrever com a cadeia sem filtro de texto | `test_cadeia_sem_filtro_de_texto_decide_sem_stt` **FAILED** |
| tirar o `sample_len` do `_transcreve_em_janelas` | `test_whisper_tem_teto_de_tokens_pela_duracao` **FAILED** ("sem teto") |
| tirar o resgate `playback_acabou` | `test_fala_decidida_depois_do_fim_da_resposta_vira_pendente` **FAILED** |

### #870 item 2 — duck rápido (S3), flag `TTS_LIVE_BARGE_DUCK_RAPIDO`

O cliente já abaixa em `barge_suspeito` (rampa 50 ms) e devolve em `barge_retoma`.
O motor, com a flag, emite esse tipo aos 3 frames (~96 ms) com `detalhe="duck rapido"`
e, aos ~480 ms, `barge_pausa` (`filtro="duck_rapido"`, com `fala_ms` e `epoca`).
A pausa não cancela e não fecha a suspeita. O `barge_in` / `interrupted` só sai
de `decidir_barge("interrupcao")`; eco, ruído e backchannel saem `barge_retoma`.
Default OFF. Não mexe na folga (#707) nem na janela do STT (#547).
A NCC do eco de sala mede-se offline, com `tools/ncc_eco.py` sobre o dump
`eco-*` (o mesmo `TTS_LIVE_DUMP_DIR` do #869). O wav do turno não traz a
referência do playback.

| Sabotagem | Resultado |
|---|---|
| nascer com `duck_rapido=True` no `Config` / no app sem env | `test_duck_rapido_desligado_nao_abaixa_no_burst_curto` e `test_engine_do_live_nasce_com_as_alavancas_do_167_ligadas` **FAILED** |
| não emitir o duck aos 3 frames | `test_duck_rapido_no_terceiro_frame_sem_abrir_suspeita` **FAILED** |
| tratar o duck como suspeita (`_live_suspeita_barge`) | `test_duck_rapido_nao_arma_stt_e_a_pausa_nao_cancela` **FAILED** (detalhe `duck rapido` na lista) |
| pausa virar corte (`barge_in` / `interrupted`) | `test_duck_rapido_pausa_dentro_do_slo` **FAILED** |
| não pausar dentro de 800 ms | `test_duck_rapido_pausa_dentro_do_slo` **FAILED** (`pausa_no is None`) |
| cortar `so_comando` por energia | `test_so_comando_nao_corta_por_energia` **FAILED** |
| duck no eco calibrado | `test_eco_puro_nao_abaixa_com_duck_rapido` **FAILED** |
| não soltar o volume quando o burst morre na folga | `test_duck_rapido_sem_confirmacao_retoma_uma_vez` **FAILED** |
| pausa desarmar o timer ou cancelar | `test_duck_rapido_nao_arma_stt_e_a_pausa_nao_cancela` **FAILED** |
| conteúdo não cancelar / não retomar depois da pausa | `test_depois_da_pausa_o_cancel_sai_do_conteudo` **FAILED** |
| achar que o tom plano cobre o eco da bancada (degrau do TTS, sem referência) | `test_eco_de_fala_sem_referencia_ainda_abaixa` **FAILED** |
| contar o eco que correlaciona com a referência | `test_eco_correlacionado_nao_abaixa` **FAILED** |
| voz fora da referência não abaixar dentro de 192 ms (chunk 2) | `test_voz_nao_correlacionada_abaixa_no_chunk_2` **FAILED** |
| anotar a referência em 24 kHz | `test_referencia_24k_chega_em_16k` **FAILED** (len 24000) |
| correlacionar o pós-RNNoise (eco destruído) em vez do cru | `test_eco_no_cru_nao_abaixa_com_filtrado_destruido` **FAILED** |
| voz no cru não abaixar quando o filtrado é o eco | `test_voz_no_cru_abaixa_mesmo_se_o_filtrado_e_eco` **FAILED** |
| a ferramenta offline divergir do correlacionador do motor | `test_janela_casa_com_o_correlacionador` **FAILED** |
| o dump de eco nascer 0755/0644 (umask) | `test_dump_de_eco_nasce_0700_0600` **FAILED** |
| chmod num pai que já existia (dump em /tmp deixa /tmp 0700) | `test_dump_nao_chmod_pai_que_ja_existia` **FAILED** |
| o dump do turno dar chmod no pai que já existia (`/tmp`) | `test_dump_do_turno_nao_chmod_pai_que_ja_existia` **FAILED** |
| flag off anotar a referência ou pôr `fala_ms` no `interrupted` | `test_flag_off_sem_dump_nao_anota_nem_poe_fala_ms` **FAILED** |

### #871 — markdown fora do TTS (Live em streaming e Conversa)

O `LimpadorFala` só solta o texto até antes do último espaço (o espaço fica
pendente, para o ponto da lista não sair afastado): a última palavra (que pode
ser URL, `_ênfase_` ou `**` ainda aberto) espera o próximo delta. O `flush()` fecha
o resto no fim da resposta; na Conversa o campo `_fala` é o limpo e o cru fica na UI.

| Sabotagem | Resultado |
|---|---|
| cortar em `len(p)-1` em vez do último espaço (sem buffer de fronteira) | `test_link_partido_entre_chunks_nao_vaza_url` e `test_streaming_qualquer_corte_da_o_mesmo_texto` (link e URL nua) **FAILED** |
| não chamar o `flush()` no fim do turno do Live | `test_live_tts_recebe_limpo_e_ui_recebe_markdown` **FAILED** (a última palavra some) |
| mandar o `assistant_text` cru ao chunker | `test_live_tts_recebe_limpo_e_ui_recebe_markdown` **FAILED** (`*`, `http`, emoji) |
| limpar o `reply`/`text` e devolver o limpo também na UI | `test_chat_reply_fala_limpo_e_instrucao_do_dono_intacta` **FAILED** (`reply != resposta`) |
| destino do link sem parênteses (`[^()]`) | `test_link_com_parenteses_nao_vaza` **FAILED** (`(city)` falado) |
| segurar todo `[` e falar o colchete no `flush` | `test_colchete_sem_url_nao_e_falado` **FAILED** (`[` na fala) |
| emitir o espaço da lista e pendurar o ponto depois | `test_lista_longa_nao_deixa_espaco_antes_do_ponto` **FAILED** (`fim .`) |

### #877 — continuação: pausa de respiração não fecha a frase longa

Fala que recomeça com o turno anterior ainda SEM áudio de resposta cancela esse turno
sem gravar history e o áudio dele vai na frente do turno seguinte.

| Sabotagem | Resultado |
|---|---|
| sem o conserto (app + pipeline do `99a2e76`) | `test_fala_que_recomeca_antes_da_resposta_funde_no_mesmo_turno`, `..._vai_inteira_ao_pendente`, `test_fora_da_janela_e_fala_nova`, `test_sem_historico_nao_gruda_no_turno_seguinte` **FAILED** |
| gravar history mesmo com `sem_historico` | `test_cancel_de_continuacao_nao_grava_a_metade_no_history` **FAILED** |
| `interrupted` de continuação sem `motivo` | `test_cancel_de_continuacao_nao_grava_a_metade_no_history` **FAILED** (o campo some e a tela mostra "interrompido") |
| tirar o guarda "nenhum áudio de resposta saiu" | `test_depois_do_primeiro_audio_da_resposta_nao_ha_continuacao` **FAILED** |
| tratar `speech_start` de barge como continuação | `test_speech_start_de_barge_nao_e_continuacao` **FAILED** |

### #869 — PCM cru no STT do turno: ring do tamanho do turno

O recorte de `_live_audio_cru` pede o trecho do tamanho do áudio do turno. Quando o
ring guardava só 10 s, todo turno de fala contínua mais longo caía calado no
filtrado: o tamanho não batia e o filtrado servia. O motor fecha turnos de até
`turno_max_ms` (30 s, com o pré-roll), o prefix do cliente vai até 2 s e o atraso
do filtro soma ~0,11 s; por isso o ring tem 34 s.

| Sabotagem | Resultado |
|---|---|
| `_CRU_CAUDA_S = 10.0` (o ring antigo) | `test_turno_longo_tambem_vai_cru_ao_stt` **FAILED** (turno de 15,4 s FILTRADO ao STT) |

Guarda sem mordida (registro do gate #878): `test_turno_de_1500ms_de_fala_real_passa_no_vad_do_stt`
**não morde** o `motivo=vad` do relato. O turno de 1,5 s já passava no `_vad_tem_fala`
antes do conserto. A célula prende o aceite (fala de 1,5 s fechada pelo motor não sai
`motivo=vad`), não a causa: a causa do `motivo=vad` em produção não foi reproduzida
offline (`evidence/869-live-vad-cru.md`).

### #984 — "só a minha voz": barge curto pontua a fala nova, não o eco

O trecho do barge leva o eco da resposta do começo ao fim. Com 0,4–0,6 s de fala do
dono, toda janela de 1 s era quase toda eco e o portão recusava o próprio dono. Com
`fala_ms`, `_voz_do_dono` pontua só o pedaço de maior energia desse tamanho
(`_voz_recorta_fala`).

| Sabotagem | Resultado |
|---|---|
| tirar `a = _voz_recorta_fala(...)` de `_voz_do_dono` | `test_barge_curto_do_dono_sobre_o_eco_passa_com_a_fala_recortada` **FAILED** |
| `_live_decide_barge` chamar `_voz_do_dono(audio)` sem `fala_ms` | `test_decide_barge_passa_a_fala_ao_portao` **FAILED** |
| filtro `identidade` da cadeia sem `fala_ms` | `test_filtro_identidade_passa_a_fala_ao_portao` **FAILED** |

### #982 — ECAPA decide no cosseno cru, por duração

No MLS Portuguese, o AS-norm do ECAPA (coorte EN, top-10) pedia 10,4 em 3 s e 7,0 em
1 s, contra os 2,434/1,8 do código, e mesmo assim perdia para o cru. O ECAPA passa a
decidir no cru: 0,44 na identidade (0,39/0,48 com `speaker_far_alvo`) e 0,35 por
janela de 1 s no Live. O Resemblyzer (perfil legado, 0,6) não muda.

| Sabotagem | Resultado |
|---|---|
| janela do Live em 0,31 (o FAR 1 % do MLS; 48 turnos falsos no smoke) | `test_portao_ecapa_decide_no_cru_por_janela`, `test_escala_crua_do_ecapa_por_duracao_e_legado_intocado`, `test_speaker_check_pontua_como_o_gate_e_diz_o_veredito` **FAILED** |
| identidade do ECAPA em 0,6 (a escala do Resemblyzer) | `test_escala_crua_...`, `test_speaker_check_...` **FAILED** |
| tabela de FAR do ECAPA de volta a 2,434/3,044 | `test_escala_crua_...`, `test_speaker_far_alvo_normaliza_...`, `test_speaker_far_alvo_sem_coorte_...` **FAILED** |
| `_SPEAKER_DECIDE_CRU` vazio (volta o AS-norm) | 6 células **FAILED** (as três acima + `test_escore_do_ecapa_e_cru_mesmo_com_coorte_e_alvo` e as duas de `far_alvo`) |
| a queda 0,44 de `_voz_do_dono` sobrescrever o limiar do ECAPA | `test_portao_ecapa_decide_no_cru_por_janela` **FAILED** |
| `_speaker_asnorm` normalizar o ECAPA de novo | `test_escore_do_ecapa_e_cru_...`, `test_speaker_far_alvo_normaliza_...` **FAILED** |

O piso do `misto` no smoke caiu de 35 % para 10 %. Os 35 % vieram do AS-norm com
uma coorte que CONTÉM os locutores 1673 e 2277 do corpus (vazamento). Dono e
terceiro ao mesmo tempo, sem separação, é limitação conhecida até a separação do
turno (#533 item 2). O piso do terceiro (≤1 turno falso em 10 min) segue duro.
## #1021 — turno que FECHOU não pode esperar o playback esvaziar

Uma vez em 20 ciclos da rodada final do #875 (fala 35): o `turn_complete` chegou, o
`play` do cliente tinha 12 s de eco na fila e não esvaziou — a espera só saiu nos
900 s do teto triplo, sem `llm_corte` (o app estava ocioso). O `play` vazio deixou de
ser condição: turno fechado por qualquer via terminal solta, e a cauda ganha uma
carência de 2 s.

| Sabotagem | Resultado |
|---|---|
| condição volta a exigir `len(c.play) == 0` | `test_turno_que_fechou_com_playback_pendente_nao_esmaga_o_teto` **FAILED** |
| contar qualquer `interrupted` (sem olhar o motivo) | `test_interrupted_de_continuacao_nao_fecha_e_o_terminal_fecha` **FAILED** |

### #876 C1 — motor do STT do Live (`live_stt_engine`) e release do MLX fora do caminho

Duas chaves que valem na hora, ambas com default que não muda nada:
- `live_stt_engine` (vazio = segue o `stt_local_engine`) escolhe o motor do turno,
  da passada de comando do barge e do prewarm do Live;
- `live_stt_release_adiado` tira o `_release_mlx_memory` de dentro do estágio `stt`
  e o roda em segundo plano depois do texto, sob o `_stt_lock` e só com o
  `_gen_lock` livre.

| Sabotagem | Resultado |
|---|---|
| `_stt_motor` ignorar a chave do Live | `test_turno_do_live_com_parakeet_...`, `test_passada_de_comando_usa_o_motor_do_live[0.0/5.0]`, `test_prewarm_aquece_o_parakeet_...` **FAILED** |
| `_transcribe` voltar a ler só o `stt_local_engine` | `test_turno_do_live_com_parakeet_e_o_arquivo_segue_no_whisper` **FAILED** |
| passada de comando sempre no Whisper | `test_passada_de_comando_usa_o_motor_do_live[0.0/5.0]`, `test_prewarm_...` **FAILED** |
| release sempre dentro do STT | `test_release_adiado_sai_do_stt_e_volta_depois` **FAILED** |
| release adiado ESPERAR o `_gen_lock` (prende o `_stt_lock` enquanto o TTS gera) | `test_release_adiado_nao_cruza_com_a_geracao_do_tts` **FAILED** |
| o turno não chamar o `_stt_solta_adiado` | `test_turno_do_pipeline_solta_o_release_depois_do_stt` **FAILED** |
| prewarm só aquecer o Whisper | `test_prewarm_aquece_o_parakeet_quando_e_o_motor_do_live` **FAILED** |
| default `live_stt_engine = "whisper"` (mudaria quem já usa `stt_local_engine=parakeet`) | `test_motor_do_live_vazio_segue_o_stt_local_engine[whisper/parakeet]` **FAILED** |

### #1111 — suíte em `screen` + `timeout` morria em silêncio no `test_deploy_mini_sh`

Causa (provada no 2×2, 2026-11-10): o `_run` disparava o `deploy_mini.sh` herdando o
tty de quem roda a suíte. O shim de `ssh` faz `exec zsh -c` LOCAL, então o `read -r
-d $'\0'` do `pesos_faltando` (zsh mexe no modo do terminal) rodava num grupo de
FUNDO do pty quando o pytest estava sob `timeout` sem `--foreground` → SIGTTOU →
grupo parado → `timeout` sai 1 → suíte morre sem resumo em `test_peso_ausente_*`.
Mínimo: `printf 'a\0' | zsh -c 'read -r -d "" p'` sob `script` + `timeout` → rc 1.
`stdin=/dev/null` NÃO resolve (não é leitura de stdin); `start_new_session` (sem
terminal de controle, como o lado remoto de um `ssh` sem `-t`) resolve.

| Sabotagem | Resultado |
|---|---|
| `_SEM_TTY` sem `start_new_session` | `test_suite_sob_tty_de_fundo_nao_morre_no_peso_ausente` **FAILED** |
| `_run` sem `**_SEM_TTY` | a célula do pty + `test_todo_disparo_do_script_vai_sem_tty` **FAILED** |
| `_SEM_TTY` sem `stdin=DEVNULL` | passa — esperado: o canal é o tty de controle, não o stdin (mantido como defesa) |
### #1108 — sonda do WS do Live com ticket, não `?key=`

A sonda do `deploy_mini.sh` (e o `live_ws.sh` contra servidor externo) abria
`/api/live/ws?key=<chave>`: cada deploy gravava a chave do dono no access log do mini
e a deixava no argv do `curl`. Agora `POST /api/live/ticket` com a chave em arquivo
0600 (`-H @arquivo`, apagado no `always`) e WS com `?ticket=`. Rev sem a rota do
ticket: sonda sem credencial (o 101 é do upgrade). Prova por shim, sem tocar o mini.

| Sabotagem | Resultado |
|---|---|
| WS volta a `"$base/api/live/ws?key=$chave"` | as 3 células `sonda_*`/`rev_sem_rota_*` **FAILED** + `test_deploy_mini_nao_monta_key_na_url` |
| ticket com `-H "X-API-Key: $chave"` (chave no argv) | `test_sonda_do_ws_usa_ticket_e_a_chave_nao_vai_a_url_nem_ao_argv` **FAILED** |
| arquivo do header não é apagado (`rm` → `:`) | `test_sonda_do_ws_usa_ticket_*`, `test_sonda_reprova_sem_ticket_*` **FAILED** |
| emissão falha → cai para `?key=$chave` | `test_sonda_reprova_sem_ticket_e_nunca_cai_para_a_chave` **FAILED** |
| sem a guarda `tem_rota` (sempre pede ticket) | `test_rev_sem_rota_do_ticket_sonda_o_ws_sem_credencial` **FAILED** |
| `live_ws.sh` volta a `?key={CHAVE}` | `test_live_ws_nao_monta_key_na_url`, `test_live_ws_conecta_com_ticket_pedido_por_post` **FAILED** |

`chmod 600` não tem mordida própria: o `mktemp` já cria 0600; a célula confere o
modo do arquivo que o `curl` leu.

### #1109 — `deploy_mini.sh`: nenhum `curl` com a chave no argv

Além da sonda do #1108, 7 `curl` do smoke (`/api/voices`, `/api/build`,
`POST /api/tts`, job, peça ×2) e o `codigo_no_ar` passavam `-H "X-API-Key: $chave"`:
a chave do dono no argv, visível em `ps` durante o smoke (classe do #1075). Agora
um header por RODADA (`$HDR_DIR/x-api-key`, dir 0700 por `mktemp -d`, arquivo 0600
por `umask 077`, escrito por `print` builtin), `-H @arquivo` em todo `curl`
autenticado, e o diretório sai no trap (EXIT/HUP/INT/TERM). O `ticket_live` do
#1108 passou a usar o mesmo arquivo (o `always` próprio saiu).

| Sabotagem | Resultado |
|---|---|
| cada um dos 8 pontos (ticket, `codigo_no_ar`, voices, build, POST tts, job, peça ×2) volta a `-H "X-API-Key: $chave"`, um por vez | `test_nenhum_argv_da_rodada_leva_a_chave_e_o_header_some` **FAILED** (8/8; o do ticket derruba também a célula da sonda) |
| `escreve_hdr` sem `umask 077` (header 0644) | as 3 células **FAILED** |
| sem `trap _limpa_hdr EXIT` | `test_nenhum_argv_*`, `test_sonda_do_ws_*` **FAILED** |
| sem o trap de TERM | `test_header_da_rodada_some_quando_o_smoke_leva_sinal` **FAILED** |

### #1109 — `admin_ui_flow.sh`: a suíte não herda a credencial do operador

O `CHAVE_TESTE` caía para `TTS_ROD_ADMIN_KEY` (a credencial de OPERADOR do dono)
e ia no argv de 5 `curl` — até no modo instância própria, onde o loopback
dispensa chave. Contrato novo: instância própria e BASE em loopback sem header;
BASE fora do loopback só com `TESTE_API_KEY` (sem ela, erro); header por arquivo
0600 apagado no trap; `unset TTS_ROD_ADMIN_KEY` no topo. Prova numa cópia do
script com shims de `curl`/`python3`/`ipconfig` (`test_admin_ui_flow_chave_1109.py`).

| Sabotagem | Resultado |
|---|---|
| fallback `${TESTE_API_KEY:-${TTS_ROD_ADMIN_KEY:-}}` de volta (com o `unset` no lugar) | `test_script_nao_le_a_credencial_do_operador` **FAILED** (o `unset` neutraliza o efeito; a guarda estática pega o código) |
| fallback de volta **e** sem o `unset` (o código antigo) | `test_instancia_propria_*`, `test_base_externo_sem_chave_*` **FAILED** |
| sem o `unset TTS_ROD_ADMIN_KEY` | `test_instancia_propria_*` **FAILED** (a instância/navegador herdavam a env) |
| header volta a `-H "X-API-Key: $CHAVE_TESTE"` | `test_base_externo_com_chave_*` **FAILED** |
| instância própria monta header com `TESTE_API_KEY` | `test_instancia_propria_*` **FAILED** |
| `encerra` sem `_limpa_hdr_chave` | `test_base_externo_com_chave_*` **FAILED** |
| sem o trap antecipado (falha antes do `trap encerra`) | `test_header_sai_tambem_quando_a_suite_falha_antes_do_trap_principal` **FAILED** |

`umask 077` sem mordida própria: o `mktemp` já cria 0600; a célula confere o modo.

### #1109 (resíduo) — chaves da rodada fora do argv do `python3`

As etapas de contrato e de UI recebiam `USE_KEY`/`ADM_KEY` (chaves criadas pela
rodada — vivas na instância alvo com BASE externo) no argv do `python3`. Agora vão
por env só daquele filho (`ADMINUI_USE_KEY`/`ADMINUI_ADM_KEY`) e o python as tira
do ambiente ao ler (`os.environ.pop`), então o navegador não herda.

| Sabotagem | Resultado |
|---|---|
| etapa de contrato volta a `"$PY" - "$LANURL" "$USE_KEY"` | `test_chaves_da_rodada_vao_ao_python_por_env_e_nao_pelo_argv` **FAILED** |
| etapa de UI volta a passar `"$USE_KEY" "$ADM_KEY"` no argv | idem **FAILED** |
### #1110 — `cloudflare.sh`: token da API da Cloudflare fora do argv do `curl`

O `_api` passava `-H "Authorization: Bearer $CF_TOKEN"`: o token que controla
tunnel e DNS ficava no argv do `curl`, legível em `ps`. Agora o header vem de um
arquivo 0600 (`umask 077` + `print` builtin) num diretório 0700 da rodada
(`mktemp -d`), `-H @arquivo`, apagado no trap (EXIT/HUP/INT/TERM). `CF_TOKEN` segue
variável de shell (não exportada). Prova por shim com token sintético hostil.

| Sabotagem | Resultado |
|---|---|
| header volta a `-H "Authorization: Bearer $CF_TOKEN"` | `test_provision_sem_eval_*`, `test_token_da_api_nao_vai_ao_argv_nem_ao_env_do_curl` **FAILED** |
| sem `trap _limpa_hdr EXIT` | `test_token_da_api_*` **FAILED** |
| sem o trap de TERM | `test_header_do_token_some_quando_o_script_leva_sinal` **FAILED** |
| `export CF_TOKEN` | `test_token_da_api_*` **FAILED** (o token aparece no env do filho) |
| sem `umask 077` | 4 **FAILED** (header e arquivos do token deixam de nascer 0600) |
### #1107 — a UI não põe credencial em URL; o WS do Live entra só por ticket

Guarda estática em `tests/test_ui_chave_fora_da_url_1107.py`, com 5 células. O
index é lido sem comentários, porque são eles que explicam o `?key=` proibido.
Nomes cobertos em query: `key`, `api_key`/`apikey`/`api-key`, `x-api-key`, `token`,
`access_token`, `secret`, e `ticket` fora do fluxo do ticket. Fontes da chave:
`KEY`, `keyRead()` e o storage (`ttsStudioKey`/`ttsRodKey`).

Na lista abaixo, as sabotagens injetadas dentro de `lxWsUrl` também derrubam
`..._abre_por_ticket_e_unico` por tabela, porque o corpo passa a conter "key".

| Sabotagem | Resultado |
|---|---|
| S1 `q = "?key=" + encodeURIComponent(KEY)` no lugar do ticket | 3 de 5 **FAILED** (`..._credencial_em_query`, `..._so_sai_..._no_header`, `..._abre_por_ticket_e_unico`) |
| S2 o catch do ticket degradar para `q = "?key=" + KEY` | 4 de 5 **FAILED** (todas, menos `..._lida_por_um_caminho`) |
| S3 o catch do ticket perder só o `return` (abre o WS sem credencial) | 1 **FAILED**: `test_falha_do_ticket_vira_erro_sem_degradar_para_a_chave` |
| S3b o `return` do catch trocado por `q = ""` | 2 **FAILED**: `..._abre_por_ticket_e_unico` (q ganha 3ª atribuição) e `..._sem_degradar_para_a_chave` |
| S4 `new WebSocket(lxWsUrl() + q + "&key=" + KEY, …)` | 3 de 5 **FAILED** |
| S5 `URLSearchParams().set("key", KEY)` em `lxWsUrl` | 3 de 5 **FAILED** (`..._em_query`, `..._no_header`, `..._abre_por_ticket` por tabela) |
| S6 template `` `${location.host}?k=${KEY}` `` | `test_a_chave_so_sai_do_navegador_no_header` **FAILED** |
| S7 `searchParams.set("api_key", keyRead())` (P4 do veredito) | 4 de 5 **FAILED** (`..._em_query`, `..._no_header`, `..._lida_por_um_caminho`, `..._abre_por_ticket` por tabela) |
| S8 `searchParams.set("x", keyRead())` (nome inocente) | `..._no_header`, `..._lida_por_um_caminho` **FAILED** (+ `..._abre_por_ticket` por tabela) |
| S9 `searchParams.set("x", localStorage.getItem("ttsStudioKey"))` | `test_a_chave_so_e_lida_por_um_caminho` **FAILED** (+ `..._abre_por_ticket` por tabela) |
| S10 `"/x?a=1&token=" + …` | `test_nenhuma_string_da_ui_monta_credencial_em_query` **FAILED** |
| S11 `"/x?ticket=" + t` fora do fluxo do ticket | `test_nenhuma_string_da_ui_monta_credencial_em_query` **FAILED** |
| S12 `new URLSearchParams({ api_key: k })` | `..._credencial_em_query` **FAILED** (+ `..._abre_por_ticket` por tabela) |

### #1113 — `_run` derruba o grupo do script; worktree sem `.venv-mlx` para com conserto

(1) Com `start_new_session` (#1111) a árvore do `deploy_mini.sh` fica num grupo
próprio; o `subprocess.run` só matava o filho DIRETO quando a espera era
interrompida (pytest-timeout, `TimeoutExpired`, Ctrl-C) — shim de curl e `sleep`
ficavam órfãos. O `_run` agora é `Popen` + `finally` com `killpg` (TERM → KILL).
(2) Hook e `integra.sh` param com a causa e o conserto quando falta o venv (a
queda para o `python3` do sistema saiu); `tools/worktrees.sh liga [--aqui]` adota
o worktree feito com `git worktree add` cru.

| Sabotagem | Resultado |
|---|---|
| `_run` volta a ser `subprocess.run` | `test_run_derruba_o_grupo_do_script_quando_a_espera_e_interrompida` **FAILED** (órfão vivo) |
| hook volta a `[ -x "$PY" ] \|\| PY=python3` | `test_hook_em_worktree_sem_venv_*`, `test_hook_na_raiz_sem_venv_*` **FAILED** |
| `integra.sh` sem a checagem do venv | `test_integra_recusa_worktree_sem_venv_antes_do_hook` **FAILED** |
| `worktrees.sh` sem o comando `liga` | `test_liga_aqui_*`, `test_liga_recusa_*` **FAILED** |
| `liga` aceita a árvore principal | `test_liga_recusa_a_arvore_principal` **FAILED** |

### #728 (#1126) — script não toca o `voices/` da árvore; instância de teste isola voz e saídas

Guarda estática em `tests/test_suites_sem_voices_728.py` (6 células). A fala do mic
falso das 5 suítes já vinha só da fixture; o vazamento era a INSTÂNCIA que elas sobem
(`app.py` resolve `BASE / "voices"`), agora com `TTS_VOICES_DIR`/`TTS_OUTPUTS_DIR`/
`TTS_SPEAKER_PATH`/`TTS_SPEAKER_AUDIO_DIR` em tmp.

| Sabotagem | Resultado |
|---|---|
| `cp voices/*.wav` no `obs_ui.sh` | `test_nenhum_script_toca_o_voices_da_arvore` **FAILED** |
| `sorted((RAIZ / "voices").glob(...))` no `live_ws.sh` | idem **FAILED** |
| `pathlib.Path("voices").glob(...)` no `live_barge_rep.sh` | idem **FAILED** |
| `live_ui.sh` sem `TTS_VOICES_DIR` | `test_toda_suite_que_sobe_o_app_isola_voz_e_saidas` e `test_as_cinco_suites_usam_so_a_fixture_como_fala` **FAILED** |
| `live_dsh_fallback.sh` sem `TTS_OUTPUTS_DIR` | `test_toda_suite_que_sobe_o_app_isola_voz_e_saidas` **FAILED** |
| `admin_ui_flow.sh` sem `TTS_OUTPUTS_DIR` | idem **FAILED** |
| `live_dsh_fallback.sh` com a fala em `glob("tests/fixtures/*.wav")` | `test_as_cinco_suites_usam_so_a_fixture_como_fala` **FAILED** (antes de apertar a célula passava: a fixture ainda aparecia na mensagem de erro) |
| (delta pós-#1125) `X = pathlib.Path("voices")` acrescentado ao `tools/live_bench.py`, isento por só CITAR `voices/` | `test_excecoes_de_bancada_so_citam_nao_montam_caminho` **FAILED** |

### #1127 — bake-offs de `tools/bakeoff/` isolam o estado do app antes do `import app`

Guarda estática + célula de comportamento em `tests/test_bakeoff_estado_isolado_1127.py`
(3 células). `comum.isola_estado()` aponta `TTS_STATE_DIR` e os nove
`TTS_*_PATH/_DIR` de estado para um `mkdtemp` (apagado no `atexit`) e levanta se o `app`
já foi importado.

| Sabotagem | Resultado |
|---|---|
| `tools/bakeoff/clone.py` sem a chamada `comum.isola_estado()` antes do `import app` | `test_todo_import_app_em_tools_vem_depois_de_isolar_o_estado` **FAILED** |
| `isola_estado()` fixa só o `TTS_STATE_DIR` (sem os nove caminhos) | `test_app_importado_depois_de_isolar_nao_ve_nenhum_caminho_do_dono` **FAILED** |
| `isola_estado()` sem a checagem de `"app" in sys.modules` | `test_isolar_depois_do_import_app_levanta` **FAILED** |
| `isola_estado()` sem o `atexit.register(shutil.rmtree, …)` | `test_app_importado_depois_de_isolar_nao_ve_nenhum_caminho_do_dono` **FAILED** (o tmp fica para trás) |

### #1129 — o log persistente carrega os 3 campos do pré-registro `22d2298`

O qa reprovou o `e225b9c` (#1128): caminho e rotação certos, mas o log não tinha 3 dos 4
campos que a medida do #1129 exige (`grep -c` dava 0 para `eco_dbfs`, `dbfs=` e
`set_speaking` no log de produção). Agora, em `app.py`:
(1) `speech_start` e `speech_end` logam `barge_in=`; (2) `niveis` (`dbfs=` do mic e
`eco_dbfs=` calibrado) a cada ~500 ms, SÓ com a resposta tocando e só como número;
(3) `playback_inicio` / `playback_fim` com `duracao_ms` (e `chunks`). Nada de áudio,
transcrição nem nome de voz. Células em `tests/test_log_campos_1129.py`.

| Sabotagem | Resultado |
|---|---|
| `speech_start` sem `barge_in=` (campo 1) | `test_speech_start_e_speech_end_levam_barge_in` **FAILED** |
| frame sem a chamada de `_live_niveis` (campo 2) | `test_o_envio_do_audio_marca_as_bordas_e_o_frame_chama_o_niveis` **FAILED** |
| `_live_niveis` loga mesmo fora do playback (campo 2) | `test_niveis_so_durante_o_playback_e_com_os_dois_campos` **FAILED** |
| sem a borda `playback_fim` (campo 3) | `test_o_envio_do_audio_marca_as_bordas_e_o_frame_chama_o_niveis` **FAILED** |

**Acréscimos pedidos pelo speech-pipeline (card do #1129)**

Uma amostra instantânea perde a fala curta (o barge decide em ~224 ms), então o `niveis`
passa a acumular CADA frame e emitir, a cada ~500 ms de playback: `dbfs_pico` e `dbfs_media`
do mic (pós-RNNoise), `prob_pico` (o MÁXIMO da prob dos quadros de 32 ms da janela, de `eng._historico`, e não o último nem o `st_prob`), `noise_dbfs` do `estatisticas()` do
motor e `ts` (relógio de parede do servidor, para casar com a anotação do dono). O
`playback_fim` ganha `audio_ms` (soma das durações dos chunks; o cliente bufferiza, então
diverge do `duracao_ms`) e `ts`.

| Sabotagem | Resultado |
|---|---|
| `niveis` volta à amostra instantânea (sem acumular pico/média) | `test_niveis_so_durante_*`, `test_a_janela_de_500ms_*`, `test_cada_janela_*` **FAILED** |
| sem `noise_dbfs` | `test_niveis_so_durante_*`, `test_niveis_so_leva_numero_*` **FAILED** |
| sem acumular `audio_ms` no envio dos chunks | `test_o_envio_do_audio_marca_as_bordas_*` **FAILED** |
| `niveis` sem `ts` | `test_niveis_so_durante_*`, `test_niveis_so_leva_numero_*` **FAILED** |
| a janela não zera depois de emitir | `test_a_janela_de_500ms_*`, `test_cada_janela_*` **FAILED** |
| `prob_pico` volta a ser só o último quadro da janela | `test_prob_pico_e_o_maximo_dos_quadros_da_janela_nao_o_ultimo` **FAILED** (quadro de 0,9 no meio de 0,1 → dá 0,1) |
| `prob_pico` lê o `st_prob` (velho entre eventos) | idem **FAILED** |
| a janela não ancora o histórico no início do playback | `test_quadros_de_antes_do_playback_nao_entram_na_janela` **FAILED** (voz de antes da resposta vaza) |
| `dbfs_pico` perdido (só amostra instantânea) | `test_dbfs_pico_maior_que_a_media_com_um_pico_no_meio` **FAILED** |
| o gate/bordas voltam a ler `sess["falando"]` (liga e desliga no mesmo instante por chunk) | `test_regime_real_um_chunk_por_vez_o_falando_volta_a_false` **FAILED** (motor REAL, `set_speaking(True)`/`(False)` por chunk, `falando` sem forçar): sem `niveis` e bordas de ~0 ms |
| `_live_niveis` volta a rodar ANTES do `feed` | `test_o_envio_acumula_o_episodio_e_o_niveis_roda_depois_do_feed` **FAILED** (`prob_pico` um frame atrás) |
| o episódio zera a cada chunk (`audio_ms`/`t0` reiniciados no envio) | `test_bordas_derivam_da_janela_*` e `test_regime_real_*` **FAILED** |

O gate agora é `eng.estatisticas()["playback_ativo"]` (a JANELA do motor, que cobre o áudio que
o cliente toca do buffer), e as bordas `playback_inicio`/`playback_fim` derivam dela, com
`chunks`/`audio_ms`/`duracao_ms` acumulados por EPISÓDIO (no envio de cada chunk).

**Mais 3 campos do speech (card do #1129)**

(1) `tocando=<bool>` em cada `niveis` (de `estatisticas()["tocando"]`): o `playback_ativo` inclui os
vãos de LLM/TTS (até 20 s) com o assistente calado, e sem esse campo o H2 dilui. (2) `n=`, os frames
na janela (a 1ª do episódio tem 1). (3) `playback_fim` também quando a SESSÃO fecha com a janela
aberta (`motivo=fechou`; o normal leva `motivo=janela`), pelo `_live_libera_sessao`.

| Sabotagem | Resultado |
|---|---|
| `niveis` sem `tocando=` | `test_niveis_diz_se_esta_tocando_*`, `test_niveis_so_leva_numero_*` **FAILED** |
| `niveis` sem `n=` | `test_niveis_traz_n_os_frames_da_janela`, `test_niveis_so_leva_numero_*` **FAILED** |
| a sessão fecha sem emitir o `playback_fim` do episódio aberto | `test_sessao_fechando_com_a_janela_aberta_emite_playback_fim_fechou` **FAILED** |

### #1129c — `fila_ms`: o áudio audível, no lugar do `tocando` do motor

O `tocando` do `estatisticas()` NÃO separa o vão mudo: com o turno aberto o backlog não escoa
(#167) e o motor real segue em `tocando=True` com o assistente calado. A célula antiga do infra
usava um motor FALSO com `tocando=False` no vão e passava enganando; foi trocada por uma com o
`TurnEngine` REAL (`test_motor_real_vao_de_geracao_*`): turno aberto + vão de 2 s dá
`tocando=True` e `fila_ms=0`; turno fechado deixa a janela e o `tocando` caírem sozinhos.
`fila_ms` é a fila estimada do cliente (`pb_fim_est = max(agora, pb_fim_est) + dur_do_chunk`),
zerada em `_live_cancela` (cancel do cliente e barge do motor); áudio audível = `fila_ms > 0`.

| Sabotagem | Resultado |
|---|---|
| a fila não parte de `agora` (sem o `max(agora, …)`) | `test_motor_real_vao_*`, `test_fila_ms_estima_o_audio_*` **FAILED** |
| o cancel/barge não zera a fila | `test_cancel_zera_*`, `test_barge_in_zera_*` **FAILED** |
| `fila_ms` derivado do `tocando` do motor | `test_motor_real_vao_*`, `test_fila_ms_estima_o_audio_*` **FAILED** |
| o gate do `niveis` de volta ao `tocando` | 4 células de janela/bordas **FAILED** |
| `niveis` sem `fila_ms=` | `test_niveis_so_leva_numero_*`, `test_fila_ms_estima_o_audio_*` **FAILED** |
### #1130 — trava atômica de máquina para a suíte cheia (`tools/fila_suite.sh`)

As esperas por `pgrep` checavam "máquina vazia" e largavam depois: checar e largar não
são atômicos, e duas suítes cheias largaram com 35 s de diferença. Agora quem larga é
quem tem a trava (`mkdir`, atômico), em `~/.tts-rod-locks/suite` (fora do /tmp que o
macOS varre às 00:00). Trava de dono morto sai por `mv` (só um recuperador vence); sinal
derruba o filho e solta; reentrante via `TTS_FILA_SEGURA`; o hook roda a suíte pela trava.

| Sabotagem | Resultado |
|---|---|
| `mkdir` vira "checa e depois cria" (não atômico) | `test_duas_largadas_*`, `test_recuperadores_simultaneos_*` **FAILED** |
| sinal não mata o filho | `test_sinal_derruba_o_filho_*` (SIGTERM e SIGINT) **FAILED** |
| sem reentrância (`TTS_FILA_SEGURA`) | `test_reentrante_*` **FAILED** (o aninhado espera a própria trava) |
| hook volta a rodar o pytest direto | `test_hook_roda_a_suite_cheia_pela_trava` **FAILED** |
| `TTS_FILA=0` (escape) | `test_sem_a_trava_as_duas_rodam_juntas` documenta a sobreposição |
| recuperação por `rm -rf` em vez de `mv` | **NÃO morde**: é uma corrida de milissegundos (o recuperador lento apaga a trava NOVA do mais rápido); 4 recuperadores não a provocam. O `mv` fica por desenho, não por célula. |
