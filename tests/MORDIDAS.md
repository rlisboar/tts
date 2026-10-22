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
