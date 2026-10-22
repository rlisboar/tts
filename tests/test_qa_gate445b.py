"""Gate do #376 (`task_3bd68b6d`): histórico de operação — série, agregados, poda.

O arquivo é diagnóstico: serve para responder "o Live ficou lento ontem" e "quantos
429 de admissão". O que NUNCA pode entrar nele é texto, áudio ou chave.
"""

import json

import pytest

import metrics_hist

pytest_plugins = ["test_api"]

from test_api import auth, chave_de_uso, client  # noqa: E402, F401

__all__ = ["auth", "chave_de_uso", "client"]


@pytest.fixture(autouse=True)
def hist_tmp(tmp_path, monkeypatch):
    pasta = tmp_path / "metrics"
    pasta.mkdir()
    monkeypatch.setattr(metrics_hist, "DIR", pasta)
    monkeypatch.setattr(metrics_hist, "_contador", 0)
    return pasta


def _linhas(pasta) -> list[dict]:
    return [json.loads(ln) for p in pasta.glob("*.jsonl") for ln in p.read_text().splitlines()]


def test_uma_linha_por_requisicao(client, auth, hist_tmp):
    for _ in range(3):
        client.get("/api/status", headers=auth)
    achados = [ln for ln in _linhas(hist_tmp) if ln["rota"].endswith("/api/status")]
    assert len(achados) == 3
    for ln in achados:
        assert isinstance(ln["ms"], float) and ln["ms"] >= 0
        assert ln["cod"] == 200


def test_rota_normalizada_nao_guardando_id(hist_tmp):
    # normaliza quem chama (o app); o módulo grava o que recebe
    import app

    metrics_hist.registrar(app._rate_path_normalizado("/api/tts/jobs/abc123/pieces/2"),
                           12.0, 200)
    (ln,) = _linhas(hist_tmp)
    assert ln["rota"] == "/api/tts/jobs/*/pieces/*"


def test_agregado_tem_p50_p95_e_taxa_de_erro(hist_tmp):
    for ms in (10, 20, 30, 40):
        metrics_hist.registrar("/api/tts", ms, 200)
    for _ in range(2):
        metrics_hist.registrar("/api/tts", 500, 500)
    dados = metrics_hist.agregar(3600)
    rota = next(r for r in dados["rotas"] if r["rota"] == "/api/tts")
    assert rota["p50"] == 30.0            # nearest-rank: ceil(0,5*6) = 3º
    assert rota["p95"] == 500.0
    assert rota["n"] == 6 and round(rota["erro_taxa"], 3) == round(2 / 6, 3)


def test_janela_exclui_linha_velha(hist_tmp):
    metrics_hist.registrar("/api/tts", 5.0, 200)
    arquivo = next(hist_tmp.glob("*.jsonl"))
    velha = json.loads(arquivo.read_text().splitlines()[0])
    velha["ts"] -= 3 * 3600
    arquivo.write_text(json.dumps(velha) + "\n")
    assert metrics_hist.agregar(3600)["rotas"] == []
    assert metrics_hist.agregar(4 * 3600)["rotas"]


def test_rotacao_por_teto_e_poda_por_ttl(hist_tmp):
    # o teto é em MB (mínimo 1): enche o arquivo do dia e a próxima gravação rotaciona
    import time as _t

    dia = _t.strftime("%Y-%m-%d")
    (hist_tmp / f"{dia}.jsonl").write_text("x" * (1024 * 1024 + 10) + "\n")
    metrics_hist.registrar("/api/tts", 1.0, 200, teto_mb=0)
    assert len(list(hist_tmp.glob("*.jsonl"))) >= 2, "não rotacionou por tamanho"
    import os

    velho = hist_tmp / "2020-01-01.jsonl"
    velho.write_text("")
    os.utime(velho, (1_600_000_000, 1_600_000_000))   # a poda olha mtime, não o nome
    antes = len(list(hist_tmp.glob("*.jsonl")))
    assert metrics_hist.podar(ttl_dias=1) == 1
    assert len(list(hist_tmp.glob("*.jsonl"))) == antes - 1


def test_tamanho_por_dia_e_listado(hist_tmp):
    metrics_hist.registrar("/api/tts", 1.0, 200)
    linhas = metrics_hist.tamanho_por_dia()
    assert linhas and linhas[0]["bytes"] > 0 and linhas[0]["arquivo"]


def test_sem_texto_sem_audio_sem_chave(client, auth, hist_tmp):
    frase = "zebra amarela cantarolava"
    client.post("/api/tts", headers=auth,
                json={"text": frase, "voice_id": "", "remoto": False})
    bruto = "".join(p.read_text() for p in hist_tmp.glob("*.jsonl"))
    assert "zebra" not in bruto and "amarela" not in bruto
    chave = auth["X-API-Key"]
    assert chave not in bruto
    assert bruto.strip(), "nada foi gravado"


def test_metrics_exige_admin(client, auth, chave_de_uso):
    assert client.get("/api/metrics", headers=chave_de_uso).status_code == 403
    r = client.get("/api/metrics", headers=auth)
    assert r.status_code == 200
    corpo = r.json()
    for campo in ("rotas", "arquivos", "retencao_dias", "teto_mb", "bytes_por_linha"):
        assert campo in corpo
    assert client.get("/api/metrics?janela_min=99999", headers=auth).status_code == 200
