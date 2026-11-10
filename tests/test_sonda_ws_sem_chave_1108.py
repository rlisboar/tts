"""#1108 — clientes de sonda/smoke do Live não põem a chave na URL do WS.

A sonda do `remote/deploy_mini.sh` abria `/api/live/ws?key=<chave>` e era a fonte
das 80 linhas com a chave do dono em claro no log do mini (44 handshakes no access
log + 36 avisos `[live] auth por ?key=`). O `tests/live_ws.sh` fazia o mesmo contra
servidor externo (`BASE=…`). Os dois passam a pedir o ticket (`POST
/api/live/ticket`, chave só no header) e conectar com `?ticket=`.

O comportamento do `deploy_mini.sh` é provado por shim em `test_deploy_mini_sh.py`
(`test_sonda_do_ws_usa_ticket_*`). Aqui fica a guarda estática do `live_ws.sh`, que
só roda com modelo real: nenhuma URL com `key=` e o ticket pedido antes de conectar.
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LIVE_WS = RAIZ / "tests" / "live_ws.sh"
DEPLOY = RAIZ / "remote" / "deploy_mini.sh"

# `?key=` / `&key=` montado em URL (f-string, concatenação ou shell)
_KEY_NA_URL = re.compile(r"[?&]key=")


def _codigo(caminho: Path) -> list[str]:
    """Linhas que não são comentário (o histórico do `?key=` fica nos comentários)."""
    return [l for l in caminho.read_text().splitlines() if not l.lstrip().startswith("#")]


def test_live_ws_nao_monta_key_na_url():
    achados = [l for l in _codigo(LIVE_WS) if _KEY_NA_URL.search(l)]
    assert not achados, achados


def test_live_ws_conecta_com_ticket_pedido_por_post():
    codigo = "\n".join(_codigo(LIVE_WS))
    assert 'http_post("/api/live/ticket"' in codigo
    conecta = [l for l in _codigo(LIVE_WS) if "/api/live/ws" in l]
    assert conecta and all("?ticket={ticket}" in l for l in conecta), conecta
    # o pedido vem ANTES da URL (ticket de um uso, 60 s — não reaproveitado)
    assert codigo.index('http_post("/api/live/ticket"') < codigo.index("/api/live/ws?ticket=")


def test_deploy_mini_nao_monta_key_na_url():
    achados = [l for l in _codigo(DEPLOY) if _KEY_NA_URL.search(l)]
    assert not achados, achados
