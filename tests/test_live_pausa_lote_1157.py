"""#1157: o lote (jobs de TTS e `/v1/audio/speech`) cede a vez ao Live.

Enquanto há sessão Live ATIVA o lote não começa trecho novo — o em curso termina —
e o `_gen_lock` fica livre entre trechos para o Live. Retoma quando nenhuma sessão
teve atividade há `live_pausa_lote_ociosa_s` ou quando a sessão fecha. Tudo com o
job REAL (`_run_tts_job`); só o modelo é um dublê que devolve áudio sintético.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import app  # noqa: E402
from test_api import (_isolamento_disco, _setup_ok, auth, client,  # noqa: E402,F401,F811
                      live_limpo, ws_client)

TEXTO = "Primeira frase do lote. Segunda frase do lote. Terceira frase do lote. Quarta frase do lote."


class _Modelo:
    sample_rate = 24000


@pytest.fixture()
def lote(monkeypatch, tmp_path):
    """Job real com `_generate_chunk` falso. Devolve o controle da bancada."""
    ctl = types_ns()
    ctl.gerados = []
    ctl.segura = {}               # índice do trecho -> Event que o libera
    ctl.entrou = {}               # índice do trecho -> Event "começou a gerar"

    def gera(model, chunk, language, conds, ref_text, omni, **kw):
        i = len(ctl.gerados)
        ctl.gerados.append(chunk)
        ctl.entrou.setdefault(i, threading.Event()).set()
        ev = ctl.segura.get(i)
        if ev is not None:
            assert ev.wait(20), "o teste não liberou o trecho"
        return (0.1 * np.ones(12000, dtype=np.float32))

    monkeypatch.setitem(app._settings, "tts_marca_dagua", False)
    monkeypatch.setitem(app._settings, "speech_queue", False)
    monkeypatch.setitem(app._settings, "chunk_max_chars", 30)
    monkeypatch.setitem(app._settings, "live_pausa_lote", True)
    monkeypatch.setitem(app._settings, "live_pausa_lote_ociosa_s", 60)
    monkeypatch.setattr(app, "_current_backend",
                        lambda *a, **k: {"id": "fake", "family": "fake", "meta": {}})
    monkeypatch.setattr(app, "_get_model", lambda *a, **k: _Modelo())
    monkeypatch.setattr(app, "_generate_chunk", gera)
    monkeypatch.setattr(app, "_motivo_anomalia", lambda *a, **k: "")
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(app, "_schedule_job_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(app, "_jobs_hist_salva", lambda *a, **k: None)
    monkeypatch.setattr(app, "_normaliza_loudness", lambda *a, **k: 0.0)
    monkeypatch.setattr(app, "_piece_dir", lambda jid: tmp_path / jid)
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path / "outputs")
    (tmp_path / "outputs").mkdir()
    with app._live_lock:
        app._live_sessions.clear()
    yield ctl
    with app._live_lock:
        app._live_sessions.clear()


def types_ns():
    import types
    return types.SimpleNamespace()


def _sessao(sid="s1", atras_s=0.0, **extra):
    """Sessão Live mínima: só os campos que o gate lê. `atividade` = carimbo do
    #1157; `visto` fica FRESCO de propósito (o cliente manda frame de mic o tempo
    todo) e não pode segurar o lote."""
    sess = {"id": sid, "visto": time.monotonic(),
            "atividade": time.monotonic() - atras_s, **extra}
    with app._live_lock:
        app._live_sessions[sid] = sess
    return sess


def _fecha(sid="s1"):
    with app._live_lock:
        app._live_sessions.pop(sid, None)


def _dispara(job_id="j1"):
    app._jobs[job_id] = {"status": "queued"}
    t = threading.Thread(target=app._run_tts_job,
                         args=(job_id, TEXTO, "v", Path("/nao/existe.wav"), "pt", {}),
                         kwargs={"use_queue": False}, daemon=True)
    t.start()
    return t, app._jobs[job_id]


def _espera(cond, prazo=8.0):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_sem_live_o_job_e_identico_ao_de_hoje(lote):
    t, job = _dispara()
    t.join(20)
    assert not t.is_alive()
    assert job["status"] == "done", job.get("error")
    assert job["total"] == len(lote.gerados) >= 3
    assert not job.get("pausado_pelo_live") and not job.get("pausado_s")


def test_com_live_ativo_nenhum_trecho_comeca_e_o_gen_lock_fica_livre(lote):
    _sessao()
    t, job = _dispara()
    assert _espera(lambda: job.get("pausado_pelo_live")), "o job não pausou"
    time.sleep(0.5)
    assert lote.gerados == [], "um trecho começou com o Live ativo"
    assert job["status"] in ("queued", "running")   # pausa antes do lock: ainda na fila
    assert job["progress"]["stage"] == "pausado pelo Live"
    # o Live precisa do `_gen_lock`: com o lote pausado ele está livre
    assert app._gen_lock.acquire(timeout=2), "o lote pausado segurou o `_gen_lock`"
    app._gen_lock.release()
    _fecha()
    t.join(20)
    assert job["status"] == "done", job.get("error")
    assert len(lote.gerados) == job["total"]
    assert job["pausado_pelo_live"] is False and job["pausado_s"] >= 0.5


def test_o_trecho_em_curso_termina_e_o_proximo_espera(lote):
    lote.segura[0] = threading.Event()
    t, job = _dispara()
    assert lote.entrou.setdefault(0, threading.Event()).wait(8)
    _sessao()                               # o Live abre com o trecho 0 no meio
    lote.segura[0].set()                    # o trecho 0 termina
    assert _espera(lambda: job.get("pausado_pelo_live")), "não pausou antes do trecho 1"
    time.sleep(0.4)
    assert len(lote.gerados) == 1, "começou trecho novo com o Live ativo"
    _fecha()
    t.join(20)
    assert job["status"] == "done" and len(lote.gerados) == job["total"]


def test_sessao_aberta_em_silencio_por_ociosa_s_o_lote_retoma(lote, monkeypatch):
    """O caso que o `visto` errava: o cliente manda frame de mic o tempo todo, em
    silêncio também. A sessão segue ABERTA e com `visto` fresco, mas sem fala, turno
    nem áudio há `ociosa_s` — o lote tem de voltar sem a sessão fechar."""
    monkeypatch.setitem(app._settings, "live_pausa_lote_ociosa_s", 5)
    sess = _sessao(atras_s=4.6)             # a janela de atividade fecha em ~0,4 s
    parar = threading.Event()

    def frames_de_mic():
        while not parar.is_set():
            sess["visto"] = time.monotonic()
            sess["st_ultimo_frame"] = time.monotonic()
            time.sleep(0.02)

    threading.Thread(target=frames_de_mic, daemon=True).start()
    try:
        t, job = _dispara()
        assert _espera(lambda: job.get("pausado_pelo_live")), "nem pausou com a sessão nova"
        t.join(20)
        assert job["status"] == "done", job.get("error")
        assert 0.2 <= job["pausado_s"] < 3.0
        assert "s1" in app._live_sessions        # retomou com a sessão ainda aberta
    finally:
        parar.set()


def test_sinais_de_live_ativo(lote, monkeypatch):
    monkeypatch.setitem(app._settings, "live_pausa_lote_ociosa_s", 5)
    agora = time.monotonic()
    # silêncio: nada recente, mesmo com `visto` e frame frescos
    _sessao(atras_s=100, st_ultimo_frame=agora)
    assert app._live_ativo_para_lote() is False
    # 1) turno em curso
    _sessao(atras_s=100, audio_pendente=True)
    assert app._live_ativo_para_lote() is True
    # 2) áudio do agente ainda tocando no cliente
    _sessao(atras_s=100, pb_fim_est=agora + 30)
    assert app._live_ativo_para_lote() is True
    _sessao(atras_s=100, pb_fim_est=agora - 1)
    assert app._live_ativo_para_lote() is False
    # 3) atividade recente
    _sessao(atras_s=1)
    assert app._live_ativo_para_lote() is True
    # sessão fechando/vencida/substituída nunca segura o lote
    for marca in ("fechar", "vencida", "substituida"):
        _sessao(atras_s=0, audio_pendente=True, **{marca: True})
        assert app._live_ativo_para_lote() is False, marca


def test_os_pontos_do_live_carimbam_atividade(ws_client, live_limpo):  # noqa: F811
    """`atividade` nasce com a sessão e renova em speech_start/end, fim de turno e
    envio de áudio — e NÃO em frame de mic."""
    with ws_client.websocket_connect("/api/live/ws") as ws:
        assert _setup_ok(ws)["type"] == "ready"
        with app._live_lock:
            sess = next(iter(app._live_sessions.values()))
        assert time.monotonic() - sess["atividade"] < 5, "sessão nova sem carimbo"

        def velha():
            sess["atividade"] = time.monotonic() - 1000

        for tipo in ("speech_start", "speech_end", "turn_complete", "interrupted"):
            velha()
            app._live_envia_json(sess, {"type": tipo, "turn": 1})
            assert time.monotonic() - sess["atividade"] < 5, tipo
        velha()
        app._live_envia_audio(sess, b"\x01\x00" * 240, marcar=False)
        assert time.monotonic() - sess["atividade"] < 5, "envio de áudio"
        velha()
        ws.send_bytes(b"\x00\x00" * 320)      # frame de mic em silêncio
        time.sleep(0.3)
        assert time.monotonic() - sess["atividade"] > 900, "frame de mic renovou a atividade"


def test_com_live_ativo_o_job_nem_carrega_o_modelo(lote, monkeypatch):
    carregou = []
    monkeypatch.setattr(app, "_get_model", lambda *a, **k: carregou.append(1) or _Modelo())
    _sessao()
    t, job = _dispara()
    assert _espera(lambda: job.get("pausado_pelo_live"))
    time.sleep(0.4)
    assert carregou == [], "o modelo foi carregado com o Live ativo"
    _fecha()
    t.join(20)
    assert job["status"] == "done" and carregou


def test_setting_desligado_volta_ao_comportamento_antigo(lote, monkeypatch):
    monkeypatch.setitem(app._settings, "live_pausa_lote", False)
    _sessao()
    t, job = _dispara()
    t.join(20)
    assert job["status"] == "done", job.get("error")
    assert not job.get("pausado_s") and len(lote.gerados) == job["total"]


def test_cancelar_durante_a_pausa_sai_na_hora_e_solta_a_trava(lote):
    _sessao()
    t, job = _dispara()
    assert _espera(lambda: job.get("pausado_pelo_live"))
    job["cancelado"] = True
    t.join(10)
    assert not t.is_alive(), "o cancelamento não furou a pausa"
    assert job["status"] == "error" and "cancelado" in job["error"]
    assert lote.gerados == []
    assert not app._gen_lock._is_owned()


def test_v1_audio_speech_respeita_a_pausa_e_conclui_quando_o_live_sai(lote):
    from fastapi.testclient import TestClient

    _sessao()
    cli = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50001))
    saida = {}

    def pede():
        saida["r"] = cli.post("/v1/audio/speech", json={
            "input": TEXTO, "voice": "alloy", "response_format": "wav"})

    th = threading.Thread(target=pede, daemon=True)
    th.start()
    assert _espera(lambda: any(j.get("pausado_pelo_live") for j in app._jobs_snapshot())), \
        "o /v1/audio/speech não pausou com o Live ativo"
    assert lote.gerados == []
    _fecha()
    th.join(30)
    assert not th.is_alive()
    assert saida["r"].status_code == 200, saida["r"].text[:300]
    assert len(lote.gerados) >= 3


def test_a_pausa_de_FRONTEIRA_de_trecho_nao_segura_o_gen_lock(lote):
    """O GANHO do card, na pausa que se REPETE (gate #1161, achado 1).

    A célula de cima só olha a pausa de ENTRADA, antes do `with _gen_lock` — ali o
    lock nunca foi tomado, então segurá-lo não muda nada. A pausa que importa é a de
    fronteira de trecho, DENTRO do `with`: aqui o trecho 0 termina com o Live já
    ativo e o `_gen_lock` tem de estar LIVRE para o Live gerar."""
    lote.segura[0] = threading.Event()
    t, job = _dispara()
    assert lote.entrou.setdefault(0, threading.Event()).wait(8)
    _sessao()                               # Live ativo com o trecho 0 no meio
    lote.segura[0].set()                    # fronteira: o trecho 1 tem de esperar
    assert _espera(lambda: job.get("pausado_pelo_live")), "não pausou na fronteira"
    assert app._gen_lock.acquire(timeout=2), \
        "o lote segurou o _gen_lock na pausa de FRONTEIRA — o Live espera o job inteiro"
    app._gen_lock.release()
    assert len(lote.gerados) == 1, "começou trecho novo durante a pausa"
    _fecha()
    t.join(20)
    assert job["status"] == "done", job.get("error")
    assert len(lote.gerados) == job["total"]


def test_sem_live_o_laco_nao_solta_o_gen_lock(lote, monkeypatch):
    """Gate #1161, decisão 4: sem Live o lock NÃO é solto entre trechos.

    Soltar sempre deixava dois jobs de LOTE se intercalarem — dois modelos residentes
    e o `_model_state` de um pisando no outro — e fazia o P5 virar "quase hoje"."""
    chamadas = []
    real = app._corre_sem_gen_lock
    monkeypatch.setattr(app, "_corre_sem_gen_lock",
                        lambda fn: (chamadas.append(1), real(fn))[1])
    t, job = _dispara()
    t.join(20)
    assert job["status"] == "done", job.get("error")
    assert chamadas == [], "soltou o _gen_lock sem Live ativo"


def test_turno_travado_nao_segura_o_lote_alem_do_teto(lote, monkeypatch):
    """Gate #1161, decisão 5: turno preso tem teto.

    `_live_turno_em_curso` sozinho não expira; sem o teto uma sessão com turno
    travado (nada de `speech_end`/`turn_complete`) segurava o lote para sempre — o
    TTL só a marca `vencida`, e vencida é pulada por `_live_ativo_para_lote`."""
    monkeypatch.setattr(app, "_LOTE_TURNO_PRESO_S", 0.5)
    _sessao(audio_pendente=True)            # turno aberto e NADA mais recente
    t, job = _dispara()
    assert _espera(lambda: job.get("pausado_pelo_live")), "nem pausou"
    assert lote.gerados == []
    t.join(15)                              # o teto solta o lote sozinho
    assert not t.is_alive(), "o turno travado segurou o lote além do teto"
    assert job["status"] == "done", job.get("error")
    assert len(lote.gerados) >= 3


def test_cancelar_na_pausa_de_FRONTEIRA_nao_gera_o_trecho_seguinte(lote):
    """Gate #1161, achado 3: o cancelamento tem de furar TAMBÉM a pausa de
    fronteira (a célula antiga cancelava na pausa de entrada, antes do lock)."""
    lote.segura[0] = threading.Event()
    t, job = _dispara()
    assert lote.entrou.setdefault(0, threading.Event()).wait(8)
    _sessao()
    lote.segura[0].set()
    assert _espera(lambda: job.get("pausado_pelo_live")), "não pausou na fronteira"
    job["cancelado"] = True
    t.join(10)
    assert not t.is_alive(), "o cancelamento não furou a pausa de fronteira"
    assert job["status"] == "error" and "cancelado" in job["error"]
    assert len(lote.gerados) == 1, f"gerou depois do cancelamento: {len(lote.gerados)}"
    assert not app._gen_lock._is_owned()


def test_v1_audio_speech_nao_conta_o_tempo_pausado_no_teto(lote, monkeypatch):
    """Pausa não é síntese lenta: com o teto efetivo em 1,5 s, o pedido fica pausado
    por ~3 s (o dobro do teto) e ainda assim termina em 200, não em 504."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app, "_SPEECH_SINC_TETO_S", 1.5)
    _sessao()
    cli = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50002))
    saida = {}

    def pede():
        saida["r"] = cli.post("/v1/audio/speech", json={
            "input": TEXTO, "voice": "alloy", "response_format": "wav"})

    th = threading.Thread(target=pede, daemon=True)
    th.start()
    assert _espera(lambda: any(j.get("pausado_pelo_live") for j in app._jobs_snapshot()))
    time.sleep(3.0)
    assert th.is_alive(), "o pedido desistiu durante a pausa"
    _fecha()
    th.join(30)
    assert saida["r"].status_code == 200, saida["r"].text[:300]


def test_v1_audio_speech_estoura_o_teto_pelo_TRABALHO_mesmo_com_pausa(lote, monkeypatch):
    """Gate #1161, achado 2: o teto é consultado de verdade.

    Teto efetivo de 0,3 s e geração de 0,5 s por trecho (trabalho acima do teto); a
    pausa de 2 s NÃO conta. Tem de dar 504. Com `join(_SPEECH_SINC_TETO_S)` o
    relógio real (2,5 s) caberia no valor monkeypatched e sairia 200."""
    from fastapi.testclient import TestClient

    original = app._generate_chunk

    def gera_lento(*a, **k):
        time.sleep(0.5)
        return original(*a, **k)

    monkeypatch.setattr(app, "_generate_chunk", gera_lento)
    monkeypatch.setattr(app, "_SPEECH_SINC_TETO_S", 0.3)
    _sessao()
    cli = TestClient(app.app, raise_server_exceptions=False, client=("127.0.0.1", 50003))
    saida = {}

    def pede():
        saida["r"] = cli.post("/v1/audio/speech", json={
            "input": TEXTO, "voice": "alloy", "response_format": "wav"})

    th = threading.Thread(target=pede, daemon=True)
    th.start()
    assert _espera(lambda: any(j.get("pausado_pelo_live") for j in app._jobs_snapshot()))
    time.sleep(2.0)                          # pausa longa: não pode virar trabalho
    _fecha()
    th.join(30)
    assert saida["r"].status_code == 504, saida["r"].status_code


def test_settings_roundtrip_e_clamp(client, auth):  # noqa: F811
    r = client.get("/api/settings", headers=auth).json()
    assert r["live_pausa_lote"] is True and r["live_pausa_lote_ociosa_s"] == 60
    ok = client.post("/api/settings", headers=auth,
                     json={"live_pausa_lote": False, "live_pausa_lote_ociosa_s": 1})
    assert ok.status_code == 200, ok.text
    r = client.get("/api/settings", headers=auth).json()
    assert r["live_pausa_lote"] is False and r["live_pausa_lote_ociosa_s"] == 5
    client.post("/api/settings", headers=auth,
                json={"live_pausa_lote": True, "live_pausa_lote_ociosa_s": 99999})
    r = client.get("/api/settings", headers=auth).json()
    assert r["live_pausa_lote"] is True and r["live_pausa_lote_ociosa_s"] == 3600
    client.post("/api/settings", headers=auth,
                json={"live_pausa_lote_ociosa_s": 60})
