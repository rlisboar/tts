"""#1018: trechos de teste que ainda LEEM o estado REAL do dono (regra #728).

O alvo é esta lista ir a zero: cada entrada é um trecho que monta caminho para
`settings.json`, `.apikey*`, `.speaker-profiles.json` ou `voices/` da raiz. A
célula que a cobra é `test_tmpdir_sem_credencial.py::
test_nenhum_teste_nao_listado_le_o_estado_real_do_dono` — leitor novo falha, e
entrada que não casa mais também (bidirecional).

Fica fora dos `test_*` de propósito: os próprios trechos listados casariam com a
varredura se morassem num arquivo varrido.
"""

LEEM_O_DONO = {
    "test_ambiente_neutro.py": {
        'origem = BASE / "settings.json"',
        'assert r.stdout.split() == [_esperado("APIKEYS_PATH"), str(BASE / "voices")]',
    },
    "test_tmpdir_sem_credencial.py": {
        'origem = BASE / "settings.json"',
        'original = app.BASE / ".apikeys.json"',
        'dono = app.BASE / ".apikeys.json"',
    },
    "xss_frontend_repro.sh": "usa `voices/` como áudio de fixture (hoje via `app.VOICES_DIR`)",
}
