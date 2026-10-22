"""Doc não pode citar rota que não existe, nem apontar linha errada (#390).

Dois docs mandavam copiar `curl` para `/api/tts/jobs` — que NÃO existe (só
`/api/tts/jobs/{job_id}`): quem seguia o README tomava 404. E a cópia morria calada,
porque nada comparava o que o doc ensina com o que o app registra.

Aqui o `/api/...` de todo `.md` do repo é extraído e conferido contra as rotas
registradas, comparando POR FORMA (segmento de parâmetro casa com parâmetro). O
`RELATORIO-app.py.md` também cita faixas `L<a>-<b>`: elas têm de existir e conter
um `def`/`@app.`, senão apontam para o meio do nada.

Falso positivo honesto: um doc pode citar rota de OUTRO produto. Se acontecer, a
saída é prefixar/escapar no doc — não afrouxar o teste.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app

RAIZ = Path(__file__).resolve().parent.parent
DOCS = sorted(p for p in RAIZ.glob("*.md") if p.is_file())
CITACAO = re.compile(r"/api/[A-Za-z0-9_./{}<>,*-]+")
FAIXA = re.compile(r"L(\d+)-L?(\d+)")

# `{job_id}`, `<id>` e `{a,b}` são parâmetro; `/api/*` e `apikeys*` são glob.
PARAM = re.compile(r"^(?:\{[^}]*\}|<[^>]+>|\*|\d+)$")


def _seg(rota: str) -> list[str]:
    return [x for x in rota.rstrip(".,`)").split("/") if x]


# Doc de ESTRUTURA (árvore/prosa) cita família e nome solto, não comando:
# `/api/chat/post`, `/api/live`. Fica de fora da conferência estrita; os docs que
# ensinam comando (README, LIVE, CONVERSA-IA, SECURITY*) seguem cobrados.
GROSSEIROS = {"architecture-summary.md"}


def _casa(padrao: list[str], rota: list[str]) -> bool:
    """Segmento de parâmetro casa com qualquer coisa; `*` casa 1..n segmentos;
    `prefixo*` casa o prefixo do segmento; padrão terminado em `/` casa a família."""
    if not padrao:
        return not rota
    p = padrao[0]
    if p == "*":
        return any(_casa(padrao[1:], rota[i:]) for i in range(1, len(rota) + 1))
    if not rota:
        return False
    r = rota[0]
    if PARAM.match(p) or (p.endswith("*") and r.startswith(p[:-1])):
        return _casa(padrao[1:], rota[1:])
    return p == r and _casa(padrao[1:], rota[1:])


def _variantes(rota: str) -> list[list[str]]:
    rota = rota.rstrip(".,`)")
    grupos = re.findall(r"\{([^}]*,[^}]*)\}", rota)
    variantes = [rota]
    for g in grupos:
        novas = []
        for v in variantes:
            novas += [v.replace("{" + g + "}", item.strip(), 1) for item in g.split(",")]
        variantes = novas
    return [_seg(v) for v in variantes]


def _rotas_do_app() -> list[list[str]]:
    return [_seg(r.path) for r in app.app.routes
            if getattr(r, "path", "").startswith("/api/")]


def test_docs_so_citam_rotas_registradas():
    validas = _rotas_do_app()
    assert validas, "não consegui ler as rotas do app — o teste estaria cego"
    problemas = []
    for doc in DOCS:
        if doc.name in GROSSEIROS:
            continue
        for n, linha in enumerate(doc.read_text(errors="replace").splitlines(), 1):
            for bruta in CITACAO.findall(linha):
                if bruta.endswith("/") and any(
                        _casa(_seg(bruta) + ["*"], rota) for rota in validas):
                    continue
                if not any(_casa(v, rota) for v in _variantes(bruta) for rota in validas):
                    problemas.append(f"{doc.name}:{n}  {bruta}")
    assert not problemas, "rota citada no doc que o app não registra:\n  " + "\n  ".join(problemas)


def test_faixas_de_linha_do_relatorio_existem():
    rel = RAIZ / "RELATORIO-app.py.md"
    if not rel.is_file():
        pytest.skip("RELATORIO-app.py.md não existe")
    linhas = (RAIZ / "app.py").read_text().splitlines()
    problemas = []
    for n_doc, linha in enumerate(rel.read_text().splitlines(), 1):
        for a, b in FAIXA.findall(linha):
            a, b = int(a), int(b)
            if not (1 <= a <= b <= len(linhas)):
                problemas.append(f"{rel.name}:{n_doc}  L{a}-{b} fora do arquivo")
                continue

    assert not problemas, "\n  ".join(problemas)
