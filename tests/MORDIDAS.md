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
