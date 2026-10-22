"""GATE QA (task_c33f2073): pool da Conversa, teto de sessões, unload e caches.

Contrato do #359, item a item:

1. 9 mensagens CONCORRENTES na Conversa com provedor saudável não viram erro de
   timeout — a fila do pool é contada à parte (`na_fila`/`fila`), não como
   demora do provedor;
2. teto de sessões evicta a mais ANTIGA (não recusa), e a poda por tempo segue;
3. `models/unload` com geração segurando o `_gen_lock` devolve **409** em ~3 s
   em vez de pendurar;
4. os caches param de crescer (teto + evicção) e apagar a voz remove a entrada;
5. o teto de histórico do Live é dito e somado na MESMA unidade (chars).

Nada aqui usa o `:7860`: `TestClient` e as fixtures de tmp do conftest.
"""

import json
import sys
import threading
import time
from pathlib import Path

import app
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import _voz_fake, auth, chave_de_uso, client, voices_tmp  # noqa: E402,F401

# Fixtures consumidas pelos testes do arquivo; o `ruff` cobre com `noqa`, o
# `pyflakes` (que o ignora) precisa do `__all__`. Inerte em runtime.
__all__ = [
    "auth",
    "chave_de_uso",
    "client",
    "voices_tmp",
]


# --------------------------------------------------------------- Conversa (1 e 2)

@pytest.fixture
def chat_saudavel(monkeypatch):
    """Provedor que responde na hora; `_chat_llm` é o ponto único."""
    monkeypatch.setattr(app, "_chat_dsh_prewarm", lambda *a, **k: None)
    monkeypatch.setattr(app, "_chat_llm", lambda msgs: json.dumps({"reply": "ok", "final": False}))
    with app._chat_lock:
        app._chat_sessions.clear()
    yield
    with app._chat_lock:
        app._chat_sessions.clear()


def _abre(client, auth, texto="oi"):
    r = client.post("/api/chat/start", headers=auth, json={"objective": "teste"})
    assert r.status_code == 200, r.text
    return r.json()["session_id"] if "session_id" in r.json() else r.json()["sid"]


def test_nove_mensagens_concorrentes_sem_timeout(client, chat_saudavel, monkeypatch, auth):
    """O pool da Conversa já saturava (8) e a nona virava "provedor demorou"."""
    orig = app._chat_llm

    def _lento(msgs):
        time.sleep(0.6)          # segura a thread do pool o suficiente para enfileirar
        return orig(msgs)

    monkeypatch.setattr(app, "_chat_llm", _lento)
    sids = [_abre(client, auth) for _ in range(9)]
    # o `start` já dispara um turno: espera ele fechar antes de mandar (senão o
    # 409 "fala anterior" é legítimo e o teste mediria outra coisa)
    limite = time.time() + 20
    while time.time() < limite:
        with app._chat_lock:
            ocupadas = [s for s in app._chat_sessions.values() if s.get("thinking")]
        if not ocupadas:
            break
        time.sleep(0.05)

    erros = []
    def _manda(sid):
        r = client.post(f"/api/chat/{sid}", headers=auth, json={"message": "oi"})
        if r.status_code != 200:
            erros.append((sid, r.status_code, r.text[:80]))

    threads = [threading.Thread(target=_manda, args=(s,)) for s in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not erros, erros
    limite = time.time() + 20
    while time.time() < limite:
        with app._chat_lock:
            pendentes = [s for s in app._chat_sessions.values() if s.get("thinking")]
        if not pendentes:
            break
        time.sleep(0.1)
    for sid in sids:
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        assert not d.get("error"), f"{sid} virou erro: {d.get('error')}"
        assert d.get("reply"), f"{sid} não recebeu resposta"


def test_fila_estourada_marca_a_sessao_sem_erro(client, chat_saudavel, monkeypatch, auth):
    """Espera de FILA estourada não é erro: sinaliza `fila` e o turno conclui."""
    monkeypatch.setattr(app, "_chat_fila_espera_s" if hasattr(app, "_chat_fila_espera_s") else
                        "_CHAT_FILA_ESPERA_S", 0.05)
    libera = threading.Event()

    def _travado(msgs):
        libera.wait(10)
        return json.dumps({"reply": "soltou"})

    monkeypatch.setattr(app, "_chat_llm", _travado)
    monkeypatch.setattr(app, "_CHAT_FILA_ESPERA_S", 0.05)
    sid = _abre(client, auth)
    # deixa o turno de abertura terminar (senão o 409 da fala anterior vem antes)
    limite = time.time() + 10
    while time.time() < limite:
        with app._chat_lock:
            if not app._chat_sessions.get(sid, {}).get("thinking"):
                break
        time.sleep(0.05)
    # agora ocupa TODAS as threads do pool
    # 2x o tamanho do pool: um worker que libere por acaso não esvazia a fila antes
    # da marca (o turno tem de ficar parado para `fila` ser escrito)
    ocupados = [app._chat_executor.submit(_travado, [])
                for _ in range(2 * app._chat_executor._max_workers)]
    # 409 = "IA ainda processando a fala anterior" (#491): sob carga do runner o
    # turno de abertura pode ser remarcado ENTRE a espera acima e o POST, então
    # o POST é laço com prazo — tiro único reprovava no CI e não aqui.
    limite_post = time.time() + 8
    while True:
        r = client.post(f"/api/chat/{sid}", headers=auth, json={"message": "oi"})
        if r.status_code != 409 or time.time() > limite_post:
            break
        time.sleep(0.1)
    assert r.status_code == 200
    # o POST responde antes de a thread do turno começar: a marca aparece logo
    # depois, então polla em vez de ler uma vez
    limite_fila = time.time() + 8      # sob carga a thread do turno demora
    while time.time() < limite_fila:
        if client.get(f"/api/chat/{sid}", headers=auth).json().get("fila") is True:
            break
        time.sleep(0.05)
    assert client.get(f"/api/chat/{sid}", headers=auth).json().get("fila") is True
    libera.set()
    for f in ocupados:
        f.result(timeout=10)
    limite = time.time() + 10
    while time.time() < limite:
        d = client.get(f"/api/chat/{sid}", headers=auth).json()
        if d.get("reply"):
            break
        time.sleep(0.1)
    d = client.get(f"/api/chat/{sid}", headers=auth).json()
    assert not d.get("error") and d.get("reply") == "soltou"


def test_teto_de_sessoes_evicta_a_mais_antiga(client, chat_saudavel, monkeypatch, auth):
    monkeypatch.setattr(app, "_CHAT_MAX_SESSIONS", 3)
    antiga = _abre(client, auth)
    with app._chat_lock:
        app._chat_sessions[antiga]["updated"] = time.time() - 1  # a mais velha, mas viva
    for _ in range(3):
        _abre(client, auth)
    with app._chat_lock:
        assert antiga not in app._chat_sessions, "a sessão mais antiga sobreviveu ao teto"
        assert len(app._chat_sessions) == 3


def test_purga_por_tempo_some_com_a_sessao(client, chat_saudavel, auth):
    sid = _abre(client, auth)
    with app._chat_lock:
        app._chat_sessions[sid]["updated"] = 1.0
    app._chat_purge(time.time())
    with app._chat_lock:
        assert sid not in app._chat_sessions


# --------------------------------------------------------------- unload com job (3)

def test_unload_com_geracao_em_curso_devolve_409(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_GEN_ESPERA_UNLOAD_S", 0.2)
    tomado = threading.Event()
    solta = threading.Event()

    def _segura():
        with app._gen_lock:
            tomado.set()
            solta.wait(10)

    t = threading.Thread(target=_segura)
    t.start()
    assert tomado.wait(5)
    try:
        inicio = time.time()
        r = client.post("/api/models/unload", headers=auth)
        gasto = time.time() - inicio
        assert r.status_code == 409, r.text
        assert gasto < 5, f"pendurou {gasto:.1f}s em vez de estourar a espera"
        assert "job" in r.text.lower() or "geração" in r.json().get("detail", "").lower()
    finally:
        solta.set()
        t.join(timeout=5)


def test_unload_sem_geracao_responde_200(client, auth, monkeypatch):
    monkeypatch.setattr(app, "_GEN_ESPERA_UNLOAD_S", 0.2)
    r = client.post("/api/models/unload", headers=auth)
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------- caches (4)

def test_cache_de_voz_remota_tem_teto_e_evicta(monkeypatch):
    monkeypatch.setattr(app, "_REMOTE_VOICE_CACHE_MAX", 2)
    app._remote_voice_cache.clear()
    for vid in ("a", "b", "c"):
        app._remote_voice_cache_guarda(vid, (1, 2))
    assert len(app._remote_voice_cache) == 2
    assert "a" not in app._remote_voice_cache, "não evictou o mais antigo"


def test_apagar_a_voz_remove_a_entrada_do_cache(client, auth, voices_tmp):
    _voz_fake(app.VOICES_DIR, "v1")
    app._remote_voice_cache_guarda("v1", (9, 9))
    assert client.delete("/api/voices/v1", headers=auth).status_code == 200
    assert "v1" not in app._remote_voice_cache


def test_delecao_que_falha_nao_limpa_OUTRAS_entradas(client, auth, voices_tmp):
    """O `pop` é pelo id pedido; 404 não pode derrubar entrada de outra voz."""
    app._remote_voice_cache["outra"] = (1, 1)
    assert client.delete("/api/voices/fantasma", headers=auth).status_code == 404
    assert app._remote_voice_cache.get("outra") == (1, 1)


# ------------------------------------------------------- unidade do histórico (5)

def test_teto_de_historico_conta_em_chars_e_diz_isso():
    """`_LIVE_RESUME_MAX`/`_live_hist_tam` falavam em bytes somando chars."""
    fonte = Path(app.__file__).read_text()
    assert "_LIVE_RESUME_MAX_BYTES" not in fonte, "o nome em bytes voltou"
    assert "_LIVE_RESUME_MAX_CHARS" in fonte
    assert "unidade dita" not in ""  # (espaço p/ nota)
    doc = app._live_hist_tam.__doc__ or ""
    assert "char" in doc.lower(), doc


def test_live_hist_tam_soma_chars_e_inclui_system_e_resumo():
    registro = {"msgs": [{"role": "user", "text": "á" * 10},
                         {"role": "assistant", "text": "b" * 5}],
                "system": "s" * 3, "resumo": "r" * 2}
    assert app._live_hist_tam(registro) == 20
