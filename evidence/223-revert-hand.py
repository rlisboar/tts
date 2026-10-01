"""GATE #223 item 2 — o revert do script é MESMO a forma pré-#209?

O script (`tests/live_lock_freeze.sh`, modo MORDIDA) reverte por INDENTAÇÃO: sobe
4 espaços o bloco `if ocupado:` (o `await` volta para dentro do `with _live_lock`).
Isso preserva a estrutura nova (`ocupado = ...` calculado sob o lock, atribuição em
`if not ocupado`). Aqui a reversão é OUTRA: o bloco é substituído pelo texto
VERBATIM do commit `4729512^` (a condição inline e o `_live_sessions[sid] = sess`
depois do `return`), extraído do próprio git.

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/223-revert-hand.py revert|restore
"""
import pathlib
import subprocess
import sys

APP = pathlib.Path("app.py")


def _limpa_bytecode():
    """#238: `cp`/write com o MESMO tamanho e mtime na mesma segundo deixa o .pyc
    válido e a medição roda bytecode velho — a mordida vira falso PASSA."""
    for pyc in pathlib.Path("__pycache__").glob("app.cpython-*.pyc"):
        pyc.unlink()

# --- o bloco COMO ESTÁ (fix do #209) -----------------------------------------
ATUAL = """    with _live_lock:
        # teto atômico com a inserção (ver comentário no topo do handler)
        anterior = _live_sessions.get(sid)
        ocupado = (anterior is None and len(_live_sessions) >= _LIVE_MAX_SESSIONS)
        if not ocupado:
            if anterior is not None:
                # #222: o id do cliente JÁ está vivo (reconexão que não fechou o
                # socket antigo, ou dois clientes com o mesmo id). A entrada é
                # sobrescrita e a sessão antiga continuaria VIVA e FORA do
                # registro — fora do sweep (nunca vencida, só morria quando o
                # cliente lembrasse de fechar) e fora da contagem do teto, que
                # assim era contornável reusando o id. Política: quem nasce
                # depois MANDA (mesma regra do registro de retomada) e a antiga
                # sai pelo caminho do fechamento, com motivo PRÓPRIO — ela não
                # ficou ociosa, foi substituída.
                anterior["substituida"] = True
            _live_sessions[sid] = sess
    if anterior is not None and not ocupado:
        _live_log_kv("substitui", sess=sid, antiga_s=int(time.time() - anterior["criada"]),
                     turnos=anterior.get("turno", 0))
    if ocupado:
        # FORA do lock (#209): `_live_lock` é `threading.Lock` e o mesmo lock é
        # pego por código SÍNCRONO no handler (`_live_sweep`, `_live_hist_*`).
        # Com o `await` lá dentro, um send que suspende (peer lento) deixava o
        # lock preso; a outra corrotina bloqueava a THREAD do event loop — e a
        # primeira só retomava se o loop rodasse. O slot já foi decidido aqui,
        # nada mais depende do lock.
        await ws.send_json(_live_erro(
            "busy", f"máximo de {_LIVE_MAX_SESSIONS} sessões simultâneas"))
        await ws.close(code=1013)
        return
"""

# --- o bloco do commit 4729512^ (pré-#209), verbatim --------------------------
PRE = """    with _live_lock:
        # teto atômico com a inserção (ver comentário no topo do handler)
        if sid not in _live_sessions and len(_live_sessions) >= _LIVE_MAX_SESSIONS:
            await ws.send_json(_live_erro(
                "busy", f"máximo de {_LIVE_MAX_SESSIONS} sessões simultâneas"))
            await ws.close(code=1013)
            return
        _live_sessions[sid] = sess
"""


def do_git() -> str:
    """Confere que o texto `PRE` é mesmo o do commit pré-fix."""
    return subprocess.run(["git", "show", "4729512^:app.py"], capture_output=True,
                          text=True, check=True).stdout


def main() -> int:
    modo = sys.argv[1] if len(sys.argv) > 1 else "revert"
    s = APP.read_text()
    if modo == "revert":
        if s.count(ATUAL) != 1:
            sys.exit(f"bloco ATUAL não casou 1x (achei {s.count(ATUAL)})")
        APP.write_text(s.replace(ATUAL, PRE))
        _limpa_bytecode()
        print("  · revertido pelo texto VERBATIM do 4729512^ (await dentro do with)")
    else:
        if s.count(PRE) != 1:
            sys.exit(f"bloco PRE não casou 1x (achei {s.count(PRE)})")
        APP.write_text(s.replace(PRE, ATUAL))
        _limpa_bytecode()
        print("  · restaurado para a forma do fix")

    # prova de que o PRE bate com o git, linha a linha
    git = do_git()
    if modo == "revert":
        assert PRE in git, "o texto do revert NÃO é o do 4729512^"
        print("  ✔ o bloco revertido é byte a byte o do commit 4729512^")
    return 0


if __name__ == "__main__":
    sys.exit(main())