"""O boot do mini mata SÓ a produção — não o vizinho de porta nem a instância de teste.

O `start-server.sh` fazia `pkill -f "uvicorn app:app"`: qualquer uvicorn do
usuário morria junto (suítes, bancada, instância em 7861-7865) e o laço do
`run.sh` ressuscitava o uvicorn no meio do restart, disputando a porta com o
processo novo. Agora a limpeza é por PIDFILE e por PORTA.

O teste sobe dois dublês com a assinatura `uvicorn app:app` — um na porta de
produção, outro numa porta vizinha — roda o boot e cobra: o da porta morre, o
vizinho sobrevive, e o `run.sh` novo fica no lugar (pidfile apontando para ele).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BOOT = RAIZ / "remote" / "launchd" / "start-server.sh"


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _duble(tmp_path: Path, porta: int, nome: str) -> subprocess.Popen:
    """`uvicorn app:app` escutando na porta E um filho que não escuta.

    O filho é o ponto: worker de verdade (`intencao_worker`, 3,8 GB) não abre
    porta, então limpeza que mata só o listener o deixa órfão. O `ps` mostra o
    caminho do dublê seguido de `app:app`; o `exec` mantém o pid."""
    binario = tmp_path / "bin" / nome
    binario.parent.mkdir(parents=True, exist_ok=True)
    binario.write_text(
        "#!/bin/sh\n"
        "sleep 200 &\n"
        "exec python3 -c 'import http.server,sys;"
        'http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])),'
        "http.server.SimpleHTTPRequestHandler).serve_forever()' \"$1\" app:app\n")
    binario.chmod(0o755)
    return subprocess.Popen([str(binario), str(porta)])


def _vivo(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _vivo_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _filhos(pid: int) -> list[int]:
    saida = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(x) for x in saida.split()]


def _espera(cond, teto: float = 8.0) -> bool:
    limite = time.monotonic() + teto
    while time.monotonic() < limite:
        if cond():
            return True
        time.sleep(0.05)
    return bool(cond())


def _cmd(pid: int) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True).stdout


def test_boot_mata_so_a_producao(tmp_path):
    porta = _porta_livre()
    vizinha = _porta_livre()
    while vizinha == porta:
        vizinha = _porta_livre()

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.sh").write_text("#!/bin/zsh\nsleep 60\n")
    (repo / "run.sh").chmod(0o755)
    casa = tmp_path / "home"
    casa.mkdir()

    antigo = _duble(tmp_path, porta, "uvicorn")
    vizinho = _duble(tmp_path, vizinha, "uvicorn-vizinho")
    # worker do app: sessão PRÓPRIA, como o `intencao_worker` de 3,8 GB — ele
    # sobrevive ao pai e o boot tem de achá-lo pelo caminho do repo
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)",
         str(repo / "intencao_worker.py")], start_new_session=True)
    assert _espera(lambda: "app:app" in _cmd(antigo.pid)), "dublê não subiu"
    assert _espera(lambda: _filhos(antigo.pid)), "o dublê não tem filho para morrer"
    filho = _filhos(antigo.pid)[0]
    assert _espera(lambda: "app:app" in _cmd(vizinho.pid)), "dublê vizinho não subiu"
    assert _espera(lambda: "intencao_worker.py" in _cmd(worker.pid)), "worker não subiu"

    env = {**os.environ, "HOME": str(casa), "TTS_SERVER_PORTA": str(porta),
           "TTS_SERVER_REPO": str(repo)}
    boot = subprocess.Popen(["/bin/zsh", str(BOOT)], env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pidfile = casa / ".tts-studio" / "server.pid"
        assert _espera(lambda: pidfile.exists()), "o boot não gravou o pidfile"
        novo = int(pidfile.read_text().strip())
        assert _espera(lambda: "run.sh" in _cmd(novo)), \
            "o pidfile não aponta para o run.sh novo"

        assert _espera(lambda: not _vivo(antigo)), \
            "o processo na porta de produção sobreviveu ao boot"
        assert _espera(lambda: not _vivo_pid(filho)), (
            "o filho que não escuta (worker) ficou órfão segurando GPU/RAM")
        assert _espera(lambda: not _vivo(worker)), (
            "o worker de sessão própria sobreviveu e fica segurando GPU/RAM")
        assert _vivo(vizinho), (
            "o boot matou o vizinho de porta — o `pkill -f` largo voltou?")
    finally:
        vizinho.terminate()
        worker.terminate()
        try:
            os.killpg(boot.pid, 15)          # leva o `sleep` filho junto
        except OSError:
            boot.terminate()
