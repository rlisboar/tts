"""Política de autenticação dos servidores remotos (RTX) — um lugar só.

`uvicorn server:app --host 0.0.0.0` publica a porta em toda a rede. Sem chave, o
middleware liberava TODOS os endpoints e o servidor ficava aberto — para a LAN e
para quem alcançasse a porta. O modo aberto era silencioso: `/health` respondia
e nada avisava, então um deploy que esqueceu a variável passava despercebido.

Regra explícita (fail closed):
  • `<PREFIXO>_API_KEY` preenchida → exige `Authorization: Bearer <chave>` ou
    `X-API-Key: <chave>` em tudo, menos `/health` e `OPTIONS`;
  • chave AUSENTE → o processo NÃO sobe, a menos que
    `<PREFIXO>_ALLOW_NO_AUTH=1` declare "aqui é atrás de firewall/VPN"
    (comportamento legado, agora por escolha e não por esquecimento).

O check roda no import, antes de carregar os modelos na VRAM: unidade que
esqueceu a variável morre na hora, com a mensagem no journal do systemd.

`/health` publica o modo em `auth` ("required" | "open") — é como se confere a
política do servidor no ar sem ssh e sem imprimir chave.

Módulo stdlib de propósito (dá para testar sem torch/transformers/onnx): o deploy
copia este arquivo ao lado do `server.py`.
"""
from __future__ import annotations

import os
import secrets

EXEMPT_PATH = "/health"

MODO_CHAVE = "required"
MODO_ABERTO = "open"


def load_key(var: str, *, allow_no_auth_var: str) -> tuple[str, str]:
    """Lê a chave do ambiente e devolve `(chave, modo)`.

    `modo` é `"required"` (chave existe, será exigida) ou `"open"` (sem chave, só
    com a escape hatch). Sem chave e sem escape hatch, imprime o motivo e
    levanta `SystemExit(1)` — fail closed antes de subir os modelos.
    """
    key = os.environ.get(var, "").strip()
    if key:
        print(f"[auth] {var} definida — tudo menos {EXEMPT_PATH} e OPTIONS exige chave",
              flush=True)
        return key, MODO_CHAVE
    if os.environ.get(allow_no_auth_var, "0").strip() == "1":
        print(f"[auth] {var} vazia + {allow_no_auth_var}=1 — servidor ABERTO "
              f"em 0.0.0.0: use só atrás de firewall/VPN", flush=True)
        return "", MODO_ABERTO
    print(
        f"[auth] {var} vazia e o uvicorn sobe em 0.0.0.0: o servidor ficaria ABERTO na rede.\n"
        f"  • defina {var}=<chave> no Environment do unit (recomendado), ou\n"
        f"  • exporte {allow_no_auth_var}=1 para assumir o modo legado sem auth.",
        flush=True)
    raise SystemExit(1)


def precisa_chave(method: str, path: str) -> bool:
    """`/health` e `OPTIONS` ficam fora (health serve para monitorar sem chave)."""
    return path != EXEMPT_PATH and method.upper() != "OPTIONS"


def apresentada(request) -> str:
    """Chave que o cliente mandou, em `Authorization: Bearer` ou `X-API-Key`."""
    auth = (request.headers.get("authorization", "") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key", "") or "").strip()


def autorizado(request, expected: str) -> bool:
    """Compara em tempo constante; em bytes, porque `compare_digest` de str só
    aceita ASCII e uma chave acentuada viraria 500 em vez de 401."""
    supplied = apresentada(request)
    return bool(supplied) and secrets.compare_digest(supplied.encode(), expected.encode())
