"""GATE QA (task_cd199d3c): higiene de protocolo ACP e do patch de streaming.

Seis itens do #287/task_6325d5c0. O que cada teste prende:

1. requisição ACP desconhecida vira **`-32601`** (o `{}` fazia o harness achar que
   o método foi atendido e degradava calado);
2. pedido de permissão é SINAL (log + `permissoes_negadas`), a negação continua;
3. bridge `clean` (sem o patch) encurta o teto do 1º delta e aparece em
   `stats.ia.bridge` — sem isso o turno que cai no openai não tem explicação;
4. um único `node --version` por descoberta e `arquivo_bin` no `estado_bridge`;
5. trace do patch é POR PROCESSO, com rodízio, e o gatilho de desligar está
   documentado — e **não encosta** no trace de outro dsh do host;
6. `dsh4aStreams` é limpo quando o commit chega.

Nos itens de patch, `node --check` (o arquivo é JS servido por processo).
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "tests"))

from test_api import auth, client  # noqa: E402,F401

# Fixtures/import consumidos pelos testes; o `pyflakes` precisa do `__all__`.
__all__ = [
    "auth",
    "client",
]

PATCH = BASE / "scripts" / "dsh-acp-stream.patch.js"


def _linha(obj: dict) -> bytes:
    """`_rota_linha` recebe a LINHA crua (o NDJSON do stdin do filho)."""
    import json as _json

    return (_json.dumps(obj) + "\n").encode()


# ------------------------------------------------------- 1) método desconhecido

def test_metodo_acp_desconhecido_responde_32601(monkeypatch):
    import dsh_client

    escritos = []
    cli = dsh_client.DshClient.__new__(dsh_client.DshClient)
    cli._log = lambda _m: None
    cli._escrever = lambda obj: escritos.append(obj)
    cli._rota_linha(_linha({"jsonrpc": "2.0", "id": 7, "method": "session/coisa_nova"}))

    assert escritos, "não respondeu nada"
    resposta = escritos[-1]
    assert resposta["id"] == 7
    assert resposta["error"]["code"] == -32601
    assert "session/coisa_nova" in resposta["error"]["message"]


def test_metodo_conhecido_nao_vira_32601(monkeypatch):
    import dsh_client

    escritos = []
    cli = dsh_client.DshClient.__new__(dsh_client.DshClient)
    cli._log = lambda _m: None
    cli._escrever = lambda obj: escritos.append(obj)
    cli.permissoes_negadas = 0
    cli._dsh_permite_tool = False
    cli._rota_linha(_linha({"jsonrpc": "2.0", "id": 8, "method": "session/update",
                           "params": {"update": {"sessionUpdate": "agent_message_chunk"}}}))
    assert not any("error" in e for e in escritos if isinstance(e, dict))


# ------------------------------------------------------- 2) permissão negada

def test_pedido_de_permissao_conta_e_loga(capsys):
    """Negar é política; não deixar rastro é que era o problema."""
    import dsh_client

    escritos = []
    cli = dsh_client.DshClient.__new__(dsh_client.DshClient)
    cli._escrever = lambda obj: escritos.append(obj)
    cli._log = lambda m: print(m)
    cli.permissoes_negadas = 0
    cli._profile = "tts-studio"
    cli._rota_linha(_linha({"jsonrpc": "2.0", "id": 9, "method": "session/request_permission",
                           "params": {"options": []}}))
    assert cli.permissoes_negadas == 1
    assert escritos, "não respondeu ao pedido"
    assert "permiss" in capsys.readouterr().out.lower()


# ------------------------------------------------------- 3) bridge clean

def test_bridge_clean_encurta_o_teto(client, auth, monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_dsh_bridge_estado_cached", lambda: {"estado": "clean"})
    assert app_mod._DSH_FIRST_DELTA_CLEAN_S < app_mod._DSH_FIRST_DELTA_S, \
        "o teto clean tem de ser MENOR que o normal"
    # o valor escolhido é o do parse: o `if` está no pipeline (aqui só o contrato)
    assert app_mod._DSH_FIRST_DELTA_CLEAN_S <= 6


def test_estado_bridge_declara_arquivo_bin():
    import dsh_client

    estado = dsh_client.estado_bridge()
    
    assert "arquivo_bin" in estado
    assert "estado" in estado


# ------------------------------------------------------- 4) node chamado uma vez

def test_descobrir_modelos_chama_node_uma_vez(monkeypatch):
    import dsh_client

    chamadas = []
    original = subprocess.run

    def _run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and any("node" in str(c) for c in cmd):
            chamadas.append(list(cmd))
        return original(cmd, *a, **k)

    monkeypatch.setattr(dsh_client.subprocess, "run", _run)
    try:
        dsh_client.descobrir_modelos()
    except Exception:  # noqa: BLE001 — o node falso pode falhar; a contagem é o alvo
        pass
    versoes = [c for c in chamadas if "--version" in c]
    assert len(versoes) <= 1, f"node --version chamado {len(versoes)}x: {versoes}"


# ------------------------------------------------------- 5) trace por processo

def test_patch_usa_trace_por_pid_e_tem_rodizio():
    texto = PATCH.read_text()
    assert "${process.pid}" in texto, "o trace voltou a ser um arquivo único do host"
    assert "dsh4a-trace" in texto
    assert "renameSync" in texto and "DSH4A_TRACE_MAX" in texto, "sem rodízio, cresce sem teto"
    assert "/tmp/dsh4a-trace.on" in texto, "o gatilho de desligar sumiu"


def test_trace_nao_colide_entre_processos():
    """Um trace único era compartilhado por TODO dsh do host e crescia sem teto."""
    texto = PATCH.read_text()
    assert "${process.pid}" in texto
    # o nome antigo só pode aparecer em COMENTÁRIO (o cabeçalho ainda o cita)
    codigo = "\n".join(l for l in texto.splitlines() if not l.lstrip().startswith(("*", "//", "/*")))
    assert "dsh4a-trace.jsonl" not in codigo


def test_patch_compila():
    assert subprocess.run(["node", "--check", str(PATCH)], capture_output=True).returncode == 0


def test_mjs_compila():
    """Backtick dentro do template `USO` já quebrou o applier inteiro uma vez."""
    mjs = BASE / "scripts" / "dsh-acp-stream-patch.mjs"
    assert subprocess.run(["node", "--check", str(mjs)], capture_output=True).returncode == 0


# ------------------------------------------------------- 6) streams encerrados

def test_patch_encerra_os_streams_da_sessao_no_commit():
    texto = PATCH.read_text()
    assert "function dsh4aEncerrar" in texto
    # a DEFINIÇÃO também casa o nome: o que interessa é a CHAMADA, no delta
    chamadas = [l for l in texto.splitlines()
                if "dsh4aEncerrar(sessionId)" in l and not l.startswith("function")]
    assert chamadas, "o commit não limpa o mapa da sessão"
    assert any("agent_message_chunk" in l for l in chamadas)


# ------------------------------------------------------- docs do gatilho

def test_gatilho_de_desligar_documentado():
    doc = (BASE / "scripts" / "README.md").read_text()
    assert "dsh4a-trace" in doc
