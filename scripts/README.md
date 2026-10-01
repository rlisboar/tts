# `scripts/` — ferramentas de host fora do repo

## `dsh-acp-stream-patch.mjs` + `dsh-acp-stream.patch.js`

Patch LOCAL no bridge ACP do `dsh` (`@deepseek-ai/dsh-acp`) para ele projetar
`agent/assistant-stream` em `session/update` incremental. Sem o patch, o caminho dsh
entrega a resposta inteira num único chunk no fim (medido: 74 s numa resposta de
1,7 k; com o patch: 203 deltas, 1º em 382 ms). Detalhes e evidência em
`../evidence/dsh-acp-stream-EVIDENCIA.md`.

- Alvo real: `$(npm root -g)/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-acp/lib/index.js`
  (dependência ANINHADA do `dsh`; versão verificada `0.1.5-rc.3`).
- Backup ao lado do arquivo: `lib/index.js.orig-<versão>`; marcador `DSH4A_PATCH_V1`.
- `dsh-acp-stream.patch.js` é o texto do patch (fonte da verdade; o aplicador só
  insere o que está lá). Cada âncora tem de casar **exatamente uma vez**: se o pacote
  mudar de forma, o script sai com **rc=2 e não escreve** em vez de editar errado.

### Flags (o mesmo texto sai em `--help`)

| flag | efeito | escreve? |
|---|---|---|
| `--status` | relata: **rc=0 PATCHADO** · **rc=1 LIMPO** | não |
| `--dry-run` | mostra as âncoras e o diff de linhas | não |
| *(nenhuma)* | aplica; se já patchado, **no-op idempotente** | sim |
| `--revert` | **rollback**: restaura o backup ao lugar | sim |
| `--reapply` | **garante PATCHADO**: reverte (se houver backup) e aplica de novo | sim |
| `--dir <dir>` | força o diretório do pacote | — |
| `--force` | aceita versão fora da lista verificada | — |
| `--help` | o texto de uso (não exige o pacote instalado) | não |

`--reapply` **continua PATCHADO** quando o patch já está aplicado (não é "revert e
desiste" — era um bug: deixava o arquivo LIMPO dizendo "já patchado"). Depois de
`npm install -g @deepseek-ai/dsh`, que recria o node_modules e apaga patch **e**
backup, o caminho é: `--status` (avisa LIMPO) → `--reapply` (aplica direto e refaz o
backup do original).

`--revert` é a única flag que **exige** o backup (é o arquivo original). Se o backup
sumir com o arquivo já patchado, o `--reapply`/`--status` avisam e o `--revert` sai
com rc=2 em vez de fingir sucesso.

```sh
node scripts/dsh-acp-stream-patch.mjs --status     # PATCHADO | LIMPO (rc=1)
node scripts/dsh-acp-stream-patch.mjs --dry-run
node scripts/dsh-acp-stream-patch.mjs              # aplica
node scripts/dsh-acp-stream-patch.mjs --reapply    # depois de reinstalar o pacote
node scripts/dsh-acp-stream-patch.mjs --revert     # ROLLBACK num comando
node scripts/dsh-acp-stream-patch.mjs --help
```

### Trace do bridge (diagnóstico, ligado por arquivo)

Enquanto `/tmp/dsh4a-trace.on` existir, o bridge grava `/tmp/dsh4a-trace.jsonl` —
um JSONL por evento cru recebido de `agent/assistant-stream` e por `session/update`
emitido (`{t, pid, k: "frame"|"emit"|"commit", ...}`). **Desligado, o custo é um
`existsSync` na primeira chamada do processo** — nada de env, nada de overhead por
delta. É a ferramenta para correlacionar pontas:

```sh
touch /tmp/dsh4a-trace.on
python3 evidence/dsh-acp-tres-pontas.py 8 persona   # liga, mede e compara
rm /tmp/dsh4a-trace.on                              # volta ao default
```

### Ambiente

O alvo está fora do workspace: o patch (e o `dsh`, que reescreve
`~/.dsh/profiles/<perfil>/cordis.yml`) precisa de FS para `/opt/homebrew` e `~/.dsh`.
Em sandbox restrito o `dsh` morre com `EPERM` no boot.
## Convenção: temporário em `/tmp` é ÚNICO por execução

Classe de bug que já mordeu a equipe quatro vezes (#12, #83, #224, #240): caminho FIXO
em `/tmp` compartilhado entre processos — dois scripts ao mesmo tempo reescrevem/apagam
o mesmo arquivo e um lê o do outro. No #240 o efeito foi barrar o commit de um colega
com falso vermelho que MUDava de rodada em rodada (o hook roda a suíte inteira).

- script/teste da casa: `RUN="$$"` no topo e sufixo em todo caminho temporário
  (`/tmp/coisa-$RUN.log`), ou `mktemp -d /tmp/nome.XXXXXX` para diretório;
- em Python: `{os.getpid()}` no nome, ou `tempfile.mkdtemp()`;
- limpeza no fim (`trap ... EXIT` quando o script mexe em estado do dono);
- **exceção**: arquivo que dois processos DEVEM enxergar igual — mutex
  (`/tmp/tts-rod-modelo.lock`), log do app/túnel (um por máquina), handshake de probe.
  Esses ficam fixos e justificados.

A varredura é um teste: `tests/test_tmp_unico.py` (asserção ESTÁTICA — varre os scripts
e testes e falha em caminho fixo novo, com lista explícita das exceções).
