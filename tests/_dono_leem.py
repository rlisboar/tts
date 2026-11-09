"""#1018: trechos de teste que ainda LEEM o estado REAL do dono (regra #728).

**Alvo atingido: a lista está vazia.** Cada leitor foi trocado por estado
sintético (`tmp_path`, `TTS_*_PATH` para tmp ou, no shell, o caminho perguntado ao
app). Uma entrada aqui é um trecho que monta caminho para
`settings.json`, `.apikey*`, `.speaker-profiles.json` ou `voices/` da raiz. A
célula que a cobra é `test_tmpdir_sem_credencial.py::
test_nenhum_teste_nao_listado_le_o_estado_real_do_dono` — leitor novo falha, e
entrada que não casa mais também (bidirecional).

Fica fora dos `test_*` de propósito: os próprios trechos listados casariam com a
varredura se morassem num arquivo varrido.
"""

#: arquivo → {trecho}. Vazio = nenhum leitor do estado real (alvo atingido).
LEEM_O_DONO: dict[str, set[str]] = {}
