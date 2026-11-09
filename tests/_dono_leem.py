"""#1018: trechos de teste que ainda LEEM o estado REAL do dono (regra #728).

Alvo: os `test_*` do repo NÃO leem mais o estado do dono — todos foram trocados por
estado sintético (`tmp_path`, `TTS_*_PATH` para tmp, ou, no shell, o caminho
perguntado ao app). Sobra só a GUARDA, abaixo. A célula que cobra é
`test_tmpdir_sem_credencial.py::test_nenhum_teste_nao_listado_le_o_estado_real_do_dono`
— leitor NOVO falha aqui, e entrada que não casa mais também (bidirecional).

A varredura cobre `tests/` INTEIRO desde o #1018: `conftest.py`, helpers `_*.py` e
subdiretórios entram; só este arquivo fica de fora (senão ele casaria consigo).

Padrões reconhecidos: `BASE / "<estado>"` (ou `REPO /`, qualquer prefixo) em Python
e `$RAIZ/voices` / `app.VOICES_DIR` no shell — quem escrever assim entra na lista.
"""

LEEM_O_DONO: dict[str, set[str]] = {
    "conftest.py": {
        # A GUARDA: é ela que redireciona o estado para o tmp. Hoje o valor já é
        # sintético (`TTS_*_PATH`/`TTS_*_DIR` do dono são neutralizados no import),
        # então a varredura só acha o exemplo do docstring — mas é o arquivo onde
        # uma leitura do arquivo REAL passaria mais despercebida.
        'Quem quiser o arquivo do repo usa `app.BASE / "settings.json"`',
    },
}
