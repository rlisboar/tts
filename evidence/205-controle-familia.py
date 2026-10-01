#!/usr/bin/env python3
"""REVISÃO do gate #194 sobre a task #205 (task_62ef4e5f) — o fix de `dsh_limpo`
nos 2 testes reféns do backend do Live.

Prova três coisas, e nenhuma delas é "o teste passa":

  1. REPRO do achado nos DOIS vetores de vazamento (env do dono e CAMPO
     `chat_backend_live` da tela), nos dois lados do fix: antes = 2 failed,
     depois = 2 passed. O vetor do ENV é emulado com um plugin que exporta a var
     DEPOIS do conftest (o conftest do HEAD já a popa desde o #206 — sem isso o
     repro do autor não reproduz mais, ver a nota no fim).
  2. MORDIDA: quebra o comportamento que os 2 testes medem e cobra o vermelho nos
     TRÊS ambientes (default, env do dono, campo do dono) — teste que passa nos
     dois jeitos não prova nada.
  3. CONTROLE DA FAMÍLIA (#206): suíte inteira verde com o campo em `openai` e em
     `dsh`, e com o ambiente duplo `TTS_CHAT_BACKEND(_LIVE)=openai`.

Uso: `./.venv-mlx/bin/python evidence/205-controle-familia.py [--suite]`
"""
import os
import pathlib
import subprocess
import sys

RAIZ = pathlib.Path("/Users/lisboa/Documents/tts-rod")
PY = str(RAIZ / ".venv-mlx/bin/python")
TESTE = RAIZ / "tests/test_live_dsh.py"
ALVOS = ["tests/test_live_dsh.py::test_stats_ia_reflete_o_fallback",
         "tests/test_live_dsh.py::test_app_cria_cliente_por_sessao_e_fecha_com_ela"]
PLUGS = pathlib.Path(f"/tmp/gate205-plugs-{os.getpid()}")   # sufixo: paralelo não troca os plugs
falhas = []


def cobrar(cond, msg):
    print(("  ok   " if cond else "  FALHA ") + msg)
    if not cond:
        falhas.append(msg)


def prepara_plugins():
    PLUGS.mkdir(parents=True, exist_ok=True)
    # o conftest do HEAD popa TTS_CHAT_* no import: setar aqui reproduz o cenário
    # original do achado (o env do dono exportado no shell).
    (PLUGS / "plug_env.py").write_text(
        'def pytest_configure(config):\n'
        '    import os\n'
        '    os.environ["TTS_CHAT_BACKEND_LIVE"] = "openai"\n')
    # o dono com o Live em openai na TELA: o campo vaza pelo settings.json copiado.
    (PLUGS / "plug_campo.py").write_text(
        'def pytest_configure(config):\n'
        '    import app\n'
        '    app._settings["chat_backend_live"] = "openai"\n')


def pytest(extra, alvos=ALVOS):
    env = {**os.environ, "PYTHONPATH": str(PLUGS)}
    r = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:randomly", "--no-header",
                        *extra, *alvos], cwd=str(RAIZ), env=env,
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def sem_fix(texto: str) -> str:
    """Reverte os dois hunks do #205 (a versão de antes do commit 9a68088)."""
    t = texto.replace("def test_stats_ia_reflete_o_fallback(monkeypatch, dsh_limpo):",
                      "def test_stats_ia_reflete_o_fallback(monkeypatch):", 1)
    return t.replace("def sessao_min(monkeypatch, dsh_limpo):",
                     "def sessao_min(monkeypatch):", 1)


def repro():
    print("\n[1] repro do achado — antes x depois, nos dois vetores")
    atual = TESTE.read_text()
    try:
        for nome, extra in (("env do dono (pós-conftest)", ["-p", "plug_env"]),
                            ("campo do dono (tela)", ["-p", "plug_campo"])):
            TESTE.write_text(sem_fix(atual))
            rc_antes, saida = pytest(extra)
            cobrar(rc_antes != 0, f"{nome}: SEM o fix os 2 testes falham "
                                  f"({[l for l in saida.splitlines() if 'passed' in l or 'failed' in l][-1:]})")
            TESTE.write_text(atual)
            rc_depois, _ = pytest(extra)
            cobrar(rc_depois == 0, f"{nome}: COM o fix os 2 testes passam")
    finally:
        TESTE.write_text(atual)


def mordida():
    print("\n[2] mordida — comportamento quebrado tem de derrubar os 2 testes")
    app = RAIZ / "app.py"
    orig = app.read_text()
    muts = [
        ("MA stats.ia ignora o fallback (diz dsh sempre)",
         'return {"pedido": pedido, "backend": "openai" if (caiu or not tem_dsh) else "dsh",',
         'return {"pedido": pedido, "backend": "openai" if not tem_dsh else "dsh",'),
        ("MB pipe do Live não cria cliente dsh",
         '    if _chat_backend_live() == "dsh":\n        try:\n            cfg = _chat_dsh_cfg()',
         '    if False:\n        try:\n            cfg = _chat_dsh_cfg()'),
        ("MC fallback nunca marcado", '"fallback": caiu,', '"fallback": False,'),
    ]
    ambientes = [("default", []), ("env do dono", ["-p", "plug_env"]),
                 ("campo do dono", ["-p", "plug_campo"])]
    try:
        for nome, velho, novo in muts:
            if orig.count(velho) != 1:
                cobrar(False, f"{nome}: âncora {orig.count(velho)}x (esperado 1)")
                continue
            app.write_text(orig.replace(velho, novo, 1))
            for amb, extra in ambientes:
                rc, _ = pytest(extra)
                cobrar(rc != 0, f"{nome} [{amb}]: morde")
    finally:
        app.write_text(orig)


def controle_familia():
    print("\n[3] controle da família (#206) — suíte inteira em 3 ambientes hostis")
    (PLUGS / "plug_campo_dsh.py").write_text(
        'def pytest_configure(config):\n'
        '    import app\n'
        '    app._settings["chat_backend_live"] = "dsh"\n')
    rodadas = [("campo=openai", ["-p", "plug_campo"], {}),
               ("campo=dsh", ["-p", "plug_campo_dsh"], {}),
               ("env TTS_CHAT_BACKEND(_LIVE)=openai", [],
                {"TTS_CHAT_BACKEND": "openai", "TTS_CHAT_BACKEND_LIVE": "openai"})]
    for nome, extra, env_extra in rodadas:
        env = {**os.environ, "PYTHONPATH": str(PLUGS), **env_extra}
        r = subprocess.run([PY, "-m", "pytest", "tests/", "-q", *extra],
                           cwd=str(RAIZ), env=env, capture_output=True, text=True)
        ultima = [l for l in r.stdout.splitlines() if "passed" in l][-1:]
        cobrar(r.returncode == 0, f"suíte verde com {nome}: {ultima}")


if __name__ == "__main__":
    prepara_plugins()
    repro()
    mordida()
    if "--suite" in sys.argv:
        controle_familia()
    print("\n✖ FALHAS:" if falhas else "\n✔ #205 revisado: repro nos 2 vetores, mordida nos 3 ambientes")
    for f in falhas:
        print("  -", f)
    sys.exit(1 if falhas else 0)