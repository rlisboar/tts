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

## #915 — watchdog C sem a GIL

`dump_traceback_later(repeat=True)` corre numa thread C. O laço e cada
`/health` cancelam e armam de novo; com a GIL presa numa chamada C ninguém
rearma e o log nasce em 0600 com `Timeout (`. A célula segura a GIL com
`ctypes.PyDLL.nanosleep` (um `time.sleep` soltaria a GIL e o laço rearmaria).

| Sabotagem | Resultado |
|---|---|
| `_saude_rearma` volta na hora, sem armar | `test_watchdog_c_despeja_com_a_gil_presa` **FAILED**: `o rearme não criou o log` |

Com o rearme, o mesmo `nanosleep` de 1,2 s (prazo 0,4 s) faz o arquivo crescer com `Timeout (` e o nome da célula, modo 0600, sem o cabeçalho `[watchdog]` da trilha Python.

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

### #804 — `remote_tts` ligado não pode pular o `_gen_lock`

`_tts_app`, o prewarm e o worker geram LOCAL (`_generate_chunk`/filho MLX) mesmo com
o modo remoto: quem paraleliza no servidor de lá é o job, não estes caminhos.

| Sabotagem | Resultado |
|---|---|
| voltar `trava = _NO_LOCK if _use_remote_tts() else _gen_lock` | `test_remoto_tambem_serializa_a_geracao_local` **FAILED** ("gerou local concorrente com o modo remoto") |

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
