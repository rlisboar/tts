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

## #494 — rollback do `deploy.sh` voltava só o `server.py`

O deploy copia DOIS arquivos (`server.py` + `auth_policy.py`): o rollback tem de
cobrir o mesmo conjunto. Sabotagem de uma linha (voltar o `cp -a '$bak' server.py`
e largar o par) deixa as três células vermelhas — medido:

| Sabotagem | Resultado |
|---|---|
| `restaurar_par` copia só o `server.py` | `test_rollback_automatico_volta_o_auth_policy_junto` **FAILED**, `test_rollback_automatico_remove_o_auth_policy_que_nao_existia` **FAILED** e `test_rollback_manual_volta_o_par` **FAILED** (3/3) |

A célula do `auth_policy.py` AUSENTE antes do deploy é a que pega o caso sutil: a
volta é **remover** o arquivo (deixar o novo seria, de novo, um estado que nunca
existiu). Mordida medida com `git stash push -- remote/deploy.sh` e `git stash
pop` — o `.pyc` não entra na conta porque o script é shell.

## #497 — `test_7b_nao_espera_o_14b` não mordia (chamava o pool, não o handler)

A célula original exercitava `MT_FAST_POOL` direto: o pool estava certo e o
roteamento do handler podia quebrar em silêncio. Sabotagem de uma linha (passar
`MT_POOL` no `offload` do `/v1/chat/completions`, omni_server.py:361) e o teste
antigo continua verde; a célula nova **FAILED** com o 14B preso (medido: 3,06 s
de espera contra 0,05 s pelo pool certo).

| Sabotagem | Resultado |
|---|---|
| `offload(MT_FAST_POOL if fast else MT_POOL, …)` → `offload(MT_POOL, …)` | `test_7b_nao_espera_o_14b` **FAILED** (espera > 1 s e `x_gen_seconds` alto) |
| rotear TUDO para o `MT_FAST_POOL` | `test_14b_continua_indo_pelo_pool_lento` **FAILED** (o controle negativo) |

O `x_gen_seconds` é a medida do handler, e o nome do thread (`mt7b` × `mt`) diz
por qual pool a geração passou — as duas pontas ficam presas. O 14B fica preso
por um `Event` que só é solto no `finally` (e, no controle negativo, por um
`Timer` de 0,5 s).

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

`deploy_mini.sh` saía 0 com `[FALHA]` no log e o `deploy.sh` nem rodava smoke sem
diff: "nada a fazer" era lido como "produção boa". Sabotagem: voltar
`POS_DEPLOY=0 … smoke; return 0` (mini) e tirar o `smoke_do_servico` do ramo
`! tem_diff` (`deploy.sh`) —

| Sabotagem | Resultado |
|---|---|
| caminho "nada a fazer" volta a sair 0 | `test_nada_a_fazer_com_smoke_vermelho_nao_sai_verde` **FAILED** |
| `deploy.sh` sem diff deixa de rodar o smoke | `test_deploy_apply_sem_diff_ainda_faz_smoke_e_reprova` **FAILED** |

O controle `test_nada_a_fazer_com_smoke_verde_continua_zero` passa nos dois lados
de propósito. A célula do mini usa `T_CURL_HEALTH_CODE` (knob novo do shim) e não
o `/api/build`: o build vermelho tira a condição `-n "$(codigo_no_ar)"` e o
caminho deixa de ser "nada a fazer" — medido, era um falso alvo.

## #498 — gate de sintaxe do `auth_policy.py` e `.rev -dirty` (medidos, sem prova)

Duas sabotagens, um teste cada — ambas caem:

| Sabotagem | Resultado |
|---|---|
| `ast.parse` só no `server.py` (tirar o `$COMPARTILHADO` do comando) | `test_sintaxe_quebrada_no_auth_policy_para_antes_do_restart` **FAILED** |
| publicar `$local_rev` sem o sufixo `-dirty` | `test_rev_publicado_marca_dirty_quando_o_que_sobe_tem_wip` **FAILED** |

Os controles (`test_sintaxe_do_par_ok_quando_os_dois_parseiam`,
`test_rev_publicado_sem_dirty_na_arvore_limpa`) passam nos dois lados. A fixture
`script_proprio` copia `remote/` com um git PRÓPRIO: editar `auth_policy.py` na
árvore compartilhada por vários agentes mexeria no repo de todo mundo, e sem o
git próprio o `rev` do `.rev` nem existiria.

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

## #500 — recon mandava "compare à mão" com o `FragmentPath` do próprio repo

O `|| [ "$frag" = "$local_unit" ]` jogava no ramo de "não devolveu
FragmentPath" também quando o host DEVOLVIA o caminho do repo — e aí o diff (e o
`[IGUAL]`) se perdiam. Sabotagem: devolver a condição com o `||` e o texto
`[DIFERENTE]` —

| Sabotagem | Resultado |
|---|---|
| `elif [ -z "$frag" ] \|\| [ "$frag" = "$local_unit" ]` de volta | `test_recon_compara_quando_o_fragmentpath_e_o_do_repo` **FAILED** |
| dizer `[DIFERENTE]` quando o host não devolve `FragmentPath` | `test_recon_sem_fragmentpath_diz_indeterminado_e_nao_diferente` **FAILED** |

O shim passou a usar `${T_FRAGMENT-…}` (com `-`, sem `:`): com `:-`, `T_FRAGMENT=""`
caía no default e o caso "host sem FragmentPath" nem era exercitável.

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
| `demucs` fora do `requirements.txt` | `test_requirements_pinos` **FAILED** (dep sem pino e fora do lock) |
| peso ausente do disco | célula offline **skipa** (o skip nomeia o script) |

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
