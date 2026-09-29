#!/usr/bin/env python3
"""GATE #194 — bateria de MUTAÇÃO: cada fix é revertido ao código velho e o teste
tem de MORDER. Teste que passa dos dois jeitos não prova nada.

Roda um alvo por mutação (processo pytest novo, app importado do zero).
"""
import pathlib
import subprocess
import sys

RAIZ = pathlib.Path("/Users/lisboa/Documents/tts-rod")
APP = RAIZ / "app.py"
ORIG = APP.read_text()

MUTS = [
    ("M1 #185 devolve usa a chave GLOBAL de agora (carimbo na devolução)",
     '    chave = getattr(cli, "_pool_chave", None)',
     '    chave = _chat_dsh_chave_do(_chat_dsh_cfg())',
     ["tests/test_api.py::test_pool_nao_devolve_cliente_da_config_antiga_como_nova",
      "tests/test_qa_gate194.py::test_cliente_do_pool_volta_pelo_modelo_DELE_e_o_antigo_e_fechado"]),

    ("M2 #186 pop INCONDICIONAL no finally",
     '            if _live_sessions.get(sid) is sess:\n                _live_sessions.pop(sid, None)',
     '            if True:\n                _live_sessions.pop(sid, None)',
     ["tests/test_api.py::test_retomada_com_sessao_antiga_viva_nao_derruba_a_nova",
      "tests/test_qa_gate194.py::test_socket_ANTIGO_morrendo_nao_apaga_a_sessao_retomada"]),

    ("M3 #187 HTTPException volta a escapar do handshake",
     '    except HTTPException as exc:\n        # `_resolve_voice`',
     '    except ZeroDivisionError as exc:\n        # `_resolve_voice`',
     ["tests/test_api.py::test_setup_sem_voz_e_sem_vozes_grava_devolve_erro"]),

    ("M4a #188a pipeline que estoura volta a PROPAGAR (sem degradar)",
     '        except Exception as exc:         # noqa: BLE001 — sessão já está no ar',
     '        except ZeroDivisionError as exc:  # noqa: BLE001 — sessão já está no ar',
     ["tests/test_api.py::test_pipeline_que_nao_nasce_nao_prende_a_sessao",
      "tests/test_qa_gate194.py::test_pipeline_que_nao_nasce_fecha_o_dsh_orfao"]),

    ("M4b #188a pipe criado FORA do try (finally não roda)",
     '    sender = None\n    try:\n        try:',
     '    sender = None\n    sess["pipe"] = _live_pipe_novo(sess)\n    try:\n        try:',
     ["tests/test_api.py::test_pipeline_que_nao_nasce_nao_prende_a_sessao",
      "tests/test_qa_gate194.py::test_pipeline_que_nao_nasce_fecha_o_dsh_orfao"]),

    ("M5 #188b teto checado FORA do lock e longe da inserção",
     '    with _live_lock:\n        # teto atômico com a inserção (ver comentário no topo do handler)\n'
     '        ocupado = (sid not in _live_sessions\n'
     '                   and len(_live_sessions) >= _LIVE_MAX_SESSIONS)\n'
     '        if not ocupado:\n            _live_sessions[sid] = sess',
     '    ocupado = (sid not in _live_sessions\n'
     '               and len(_live_sessions) >= _LIVE_MAX_SESSIONS)\n'
     '    time.sleep(0.2)\n'
     '    if not ocupado:\n        _live_sessions[sid] = sess',
     ["tests/test_qa_gate194.py::test_teto_de_sessoes_e_atomico_sob_concorrencia"]),

    ("M6 #189 resumidor volta a ser o da CONVERSA (_chat_llm)",
     '    return _chat_llm_dsh if _live_stats_ia(sess)["backend"] == "dsh" else _chat_llm',
     '    return _chat_llm',
     ["tests/test_api.py::test_compressao_do_live_segue_o_backend_efetivo",
      "tests/test_qa_gate194.py::test_resumo_do_live_usa_o_POOL_e_nao_o_cliente_da_sessao"]),

    ("M7 #201 guarda do pop FORA do lock (retomada no meio do fechamento)",
     '        with _live_lock:\n            if _live_sessions.get(sid) is sess:\n'
     '                _live_sessions.pop(sid, None)',
     '        if _live_sessions.get(sid) is sess:\n            with _live_lock:\n'
     '                _live_sessions.pop(sid, None)',
     ["tests/test_qa_gate194.py::test_retomada_que_nasce_DURANTE_o_fechamento_sobrevive"]),
]

falhas = []
try:
    for nome, antigo, novo, alvos in MUTS:
        n = ORIG.count(antigo)
        if n != 1:
            print(f"!! {nome}: âncora aparece {n}x (esperado 1)")
            falhas.append(nome)
            continue
        APP.write_text(ORIG.replace(antigo, novo, 1))
        r = subprocess.run([".venv-mlx/bin/python", "-m", "pytest", "-q", "-p", "no:randomly",
                            "--no-header", "-x", *alvos],
                           cwd=RAIZ, capture_output=True, text=True)
        morreu = [l for l in r.stdout.splitlines() if " FAILED" in l or l.startswith("FAILED")]
        veredito = "MORDE" if r.returncode != 0 else "PASSOU (teste permissivo!)"
        print(f"{veredito:26} {nome}")
        if r.returncode == 0:
            falhas.append(nome)
        else:
            print(f"    alvo que mordeu: {(morreu or ['(ver acima)'])[0][:120]}")
        APP.write_text(ORIG)
finally:
    APP.write_text(ORIG)

print("\ntudo mordeu" if not falhas else f"\n!! {len(falhas)} mutação(ões) não morderam")
sys.exit(1 if falhas else 0)