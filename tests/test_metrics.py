"""Histórico de operação (`metrics_hist` + `/api/metrics`) — #376.

POR QUE ESTE ARQUIVO EXISTE: o `/api/status` é o AGORA. Sem série temporal, "o
Live ficou lento ontem" e "quantos 429 de admissão" não se respondem. O contrato
caro aqui é triplo: (a) toda requisição de API vira linha, inclusive as recusadas
pela auth/limite; (b) o agregado responde p50/p95 e taxa de erro; (c) o arquivo
NÃO carrega texto, áudio nem chave (o histórico é metadado).

O diretório é sempre um tmp (fixture): a suíte não escreve no `state/` do repo.
"""

import json
import sys
import time
from pathlib import Path

import pytest

import metrics_hist

sys.path.insert(0, str(Path(__file__).resolve().parent))
# `client`/`auth`/`chave_de_uso`/`voices_tmp` moram no `test_api.py` (não no
# conftest): importar é o padrão do repo para quem precisa da API de verdade.
from test_api import auth, chave_de_uso, client, voices_tmp  # noqa: E402, F401

# Declarados como usados: são FIXTURES consumidas pelos testes do arquivo.
# O `ruff` já cobre isso com o `noqa` (política pós-#378); o `__all__` mantém
# o `pyflakes` (que ignora o `noqa`) também verde — inerte em runtime.
__all__ = [
    "auth",
    "chave_de_uso",
    "client",
    "voices_tmp",
]


@pytest.fixture(autouse=True)
def dir_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_hist, "DIR", tmp_path / "metrics")
    metrics_hist.apagar_tudo()
    return tmp_path


def _linhas(d: Path) -> list[dict]:
    fora = []
    for p in sorted((d / "metrics").glob("*.jsonl")):
        fora += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return fora


# ------------------------------------------------------------- gravação direta

def test_registrar_grava_metadados_e_nada_de_conteudo(dir_tmp):
    metrics_hist.registrar("/api/tts", 12.345, 200, "tts", "mlx")
    (linha,) = _linhas(dir_tmp)
    assert linha["rota"] == "/api/tts" and linha["cod"] == 200
    assert linha["ms"] == 12.3 and linha["est"] == "tts" and linha["bk"] == "mlx"
    assert set(linha) == {"ts", "rota", "ms", "cod", "est", "bk"}


def test_arquivo_por_dia_e_no_diretorio_de_estado(dir_tmp):
    metrics_hist.registrar("/api/status", 1.0, 200)
    nome = next((dir_tmp / "metrics").glob("*.jsonl")).name
    assert nome == time.strftime("%Y-%m-%d") + ".jsonl"


def test_rotacao_por_tamanho_abre_sufixo(dir_tmp):
    """Teto de 1 MB não serve para testar rápido: usa 0 (sempre estoura) na 2ª vez."""
    metrics_hist.registrar("/api/tts", 1.0, 200, teto_mb=1)
    alvo = next((dir_tmp / "metrics").glob("*.jsonl"))
    alvo.write_text(alvo.read_text() + "x" * (1024 * 1024))   # estoura o teto
    metrics_hist.registrar("/api/tts", 2.0, 200, teto_mb=1)
    nomes = [p.name for p in (dir_tmp / "metrics").glob("*.jsonl")]
    assert len(nomes) == 2 and any(n.endswith("-2.jsonl") for n in nomes), nomes


def test_poda_remove_arquivo_mais_velho_que_o_ttl(dir_tmp):
    import os

    metrics_hist.registrar("/api/tts", 1.0, 200)
    velho = next((dir_tmp / "metrics").glob("*.jsonl"))
    (dir_tmp / "metrics").mkdir(exist_ok=True)
    # mtime fixado DEPOIS de escrever: um append posterior o atualizaria
    antigo = time.time() - 40 * 86400
    os.utime(velho, (antigo, antigo))
    novo = dir_tmp / "metrics" / "recente.jsonl"
    novo.write_text("")
    assert metrics_hist.podar(30) == 1
    assert not velho.exists() and novo.exists()


def test_poda_com_ttl_zero_limpa_tudo(dir_tmp):
    metrics_hist.registrar("/api/tts", 1.0, 200)
    assert metrics_hist.podar(0) == 1
    assert _linhas(dir_tmp) == []


# ----------------------------------------------------------------- agregados

def test_agregado_da_p50_p95_e_taxa_de_erro(dir_tmp):
    for ms, cod in ((10.0, 200), (20.0, 200), (30.0, 200), (40.0, 500)):
        metrics_hist.registrar("/api/tts", ms, cod)
    d = metrics_hist.agregar(300)
    rota = d["rotas"][0]
    assert d["total"] == 4 and rota["n"] == 4
    assert rota["p50"] == 20.0 and rota["p95"] >= 30.0
    assert rota["erros"] == 1 and rota["erro_taxa"] == 0.25


def test_agregado_filtra_janela_e_rota(dir_tmp):
    metrics_hist.registrar("/api/tts", 10.0, 200)
    metrics_hist.registrar("/api/status", 5.0, 200)
    velho = next((dir_tmp / "metrics").glob("*.jsonl"))
    linhas = velho.read_text().splitlines()
    antigo = json.loads(linhas[0])
    antigo["ts"] = int(time.time()) - 7200          # 2 h atrás
    velho.write_text(json.dumps(antigo) + "\n" + "\n".join(linhas[1:]) + "\n")

    assert metrics_hist.agregar(60)["total"] == 1            # a de 2 h saiu da janela
    assert metrics_hist.agregar(10800)["total"] == 2          # janela maior pega
    assert [r["rota"] for r in metrics_hist.agregar(600, "/api/status")["rotas"]] == ["/api/status"]


def test_tamanho_por_dia_e_media_por_linha(dir_tmp):
    metrics_hist.registrar("/api/tts", 1.0, 200)
    assert metrics_hist.tamanho_por_dia()[0]["linhas"] == 1
    assert metrics_hist.media_por_linha() > 40


# ------------------------------------------------------- pela API (middleware)

def _faz(client, auth, rota="api/tts"):
    return client.get(f"/api/{rota}", headers=auth)


def test_requisicao_de_api_gera_linha(client, auth, dir_tmp):
    _faz(client, auth, "status")
    _faz(client, auth, "status")
    rota = {l["rota"] for l in _linhas(dir_tmp)}
    assert "/api/status" in rota


def test_recusa_de_auth_tambem_e_historiada(client, chave_de_uso, dir_tmp):
    """401 e 429 são justamente o que se quer contar — o middleware roda por fora."""
    client.get("/api/status")           # sem chave (host de teste não é loopback)
    assert any(l["cod"] == 401 for l in _linhas(dir_tmp)), _linhas(dir_tmp)


def test_rota_e_normalizada_no_arquivo(client, auth, dir_tmp, voices_tmp):
    client.post("/api/tts", headers=auth, json={"text": "oi"})
    rota = next(l["rota"] for l in _linhas(dir_tmp) if l["cod"] != 401)
    assert "*" not in rota and rota.startswith("/api/")     # nunca o caminho cru com id


def test_estatico_nao_entra_no_historico(client, dir_tmp):
    client.get("/health")
    assert _linhas(dir_tmp) == []


def test_endpoint_devolve_agregado_e_metadados(client, auth, dir_tmp):
    _faz(client, auth, "status")
    d = client.get("/api/metrics?janela_min=5", headers=auth).json()
    assert d["total"] >= 1 and d["retencao_dias"] == 14 and d["teto_mb"] == 8
    assert isinstance(d["arquivos"], list) and d["bytes_por_linha"] > 0


def test_endpoint_exige_admin(client, chave_de_uso, dir_tmp):
    """Diagnóstico é admin: a chave de uso não lê o histórico da instalação."""
    assert client.get("/api/metrics", headers=chave_de_uso).status_code == 403


def test_endpoint_limita_janela(client, auth, dir_tmp):
    d = client.get("/api/metrics?janela_min=999999", headers=auth).json()
    assert d["janela_s"] == 10080 * 60
    d = client.get("/api/metrics?janela_min=0", headers=auth).json()
    assert d["janela_s"] == 60


def test_nada_de_chave_nem_texto_no_arquivo(client, auth, dir_tmp, voices_tmp):
    """O histórico é metadado: nem a chave usada, nem o texto sintetizado."""
    texto = "frase marcante do historico"
    client.post("/api/tts", headers=auth, json={"text": texto})
    bruto = "".join(p.read_text() for p in (dir_tmp / "metrics").glob("*.jsonl"))
    assert bruto, "nada foi historiado"
    for segredo in list(auth.values()) + [texto]:
        if segredo:
            assert segredo not in bruto, segredo
