"""Testes das funções puras do app.py (import leve — sem carregar modelos)."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import app


# ---------------------------------------------------------------------------
# Idioma / instruct
# ---------------------------------------------------------------------------

def test_omni_language():
    assert app._omni_language("auto") == "None"
    assert app._omni_language("") == "None"
    assert app._omni_language("português") == "pt"
    assert app._omni_language("Portuguese") == "pt"
    assert app._omni_language("en") == "en"


def test_sanitize_instruct_filtragem():
    ok = "male, middle-aged, low pitch"
    assert app._sanitize_instruct(ok) == ok
    # texto livre (emoção) é descartado; tags válidas ficam
    assert app._sanitize_instruct("happy, excited, female") == "female"
    assert app._sanitize_instruct("female, female, male") == "female, male"
    assert app._sanitize_instruct("") == ""


# ---------------------------------------------------------------------------
# STT: filtros anti-ruído
# ---------------------------------------------------------------------------

def _r(nsp=0.1, alp=-0.3, cr=1.2):
    return {"segments": [{"no_speech_prob": nsp, "avg_logprob": alp,
                          "compression_ratio": cr}]}


def test_stt_ok_aceita_fala_real():
    ok, motivo = app._stt_ok(_r(), "Olá, tudo bem com você?")
    assert ok, motivo


def test_stt_ok_rejeita_curto_e_blacklist(monkeypatch):
    monkeypatch.setitem(app._settings, "stt_anti_ruido", True)   # não depende do settings.json real
    assert not app._stt_ok(_r(), "a")[0]              # 1 char < stt_min_chars
    ok, motivo = app._stt_ok(_r(), "obrigado")
    assert not ok and motivo == "alucinação comum"


def test_stt_ok_rejeita_metricas_ruins(monkeypatch):
    monkeypatch.setitem(app._settings, "stt_anti_ruido", True)
    # confiança baixa e repetição derrubam sozinhas
    assert not app._stt_ok(_r(alp=-2.0), "uma frase qualquer aqui")[0]
    assert not app._stt_ok(_r(cr=4.0), "uma frase qualquer aqui")[0]
    # no_speech_prob SOZINHO não derruba mais: medido no app, era ele que fazia
    # fala curta e boa ("Sim.") voltar sem texto. Com confiança boa, passa.
    assert app._stt_ok(_r(nsp=0.9), "uma frase qualquer aqui")[0]
    # aí sim, sem-fala + confiança razoável-por-pouco rejeitam em conjunto
    assert not app._stt_ok(_r(nsp=0.9, alp=-0.8), "uma frase qualquer aqui")[0]


def test_stt_ok_motivo_quando_nao_ouviu_fala(monkeypatch):
    monkeypatch.setitem(app._settings, "stt_anti_ruido", True)
    # antes, texto vazio caía em "curto demais" e sugeria filtro de tamanho
    ok, motivo = app._stt_ok(_r(), "")
    assert not ok and motivo == "não ouviu fala"


def test_stt_ok_anti_ruido_desligado_passa_tudo(monkeypatch):
    monkeypatch.setitem(app._settings, "stt_anti_ruido", False)
    assert app._stt_ok(_r(alp=-4.0, cr=9.0), "obrigado")[0]
    assert app._stt_ok(_r(), "")[0]      # até vazio: quem decide é o chamador


# ---------------------------------------------------------------------------
# YouTube: erro retryable
# ---------------------------------------------------------------------------

def test_yt_retryable():
    assert app._yt_retryable(RuntimeError("HTTP Error 403: Forbidden"))
    assert app._yt_retryable(RuntimeError("Unable to download video data"))
    assert app._yt_retryable(RuntimeError("Sign in to confirm you're not a bot"))
    assert not app._yt_retryable(RuntimeError("Video privado"))
    assert not app._yt_retryable(RuntimeError("tres vazia"))


# ---------------------------------------------------------------------------
# Fila de falas (_SpeechGate)
# ---------------------------------------------------------------------------

@pytest.fixture()
def gate_rapido(monkeypatch):
    monkeypatch.setitem(app._settings, "speech_queue_gap_s", 0.1)
    return app._SpeechGate()


def test_gate_fifo_ordem_e_espera(gate_rapido):
    t1 = gate_rapido.begin("job-a")
    t2 = gate_rapido.begin("job-b")
    tempos = {}

    def entrega(tok, nome, dur):
        gate_rapido.deliver(tok, duration_s=dur)
        tempos[nome] = time.time()

    th1 = threading.Thread(target=entrega, args=(t1, "a", 0.0))
    th2 = threading.Thread(target=entrega, args=(t2, "b", 0.0))
    th1.start()
    time.sleep(0.05)                     # garante que "a" chega primeiro no gate
    th2.start()
    th1.join(timeout=5)
    th2.join(timeout=5)
    assert set(tempos) == {"a", "b"}
    assert tempos["a"] < tempos["b"]                    # FIFO
    assert tempos["b"] - tempos["a"] >= 0.08            # esperou folga da fala "a"


def test_gate_abort_avanca_ticket(gate_rapido):
    t1 = gate_rapido.begin("job-a")
    t2 = gate_rapido.begin("job-b")
    gate_rapido.abort(t1)                # erro: não reserva tempo de fala
    t0 = time.time()
    gate_rapido.deliver(t2, duration_s=0.0)
    assert time.time() - t0 < 0.5        # entrega imediata


def test_gate_duracao_estimada_por_texto(gate_rapido):
    t = gate_rapido.begin("job-x")
    app._jobs["job-x"] = {"text": "x" * 140, "status": "running"}
    try:
        gate_rapido.deliver(t)           # sem duration_s → estima do texto
        assert app._jobs["job-x"]["speech_duration_s"] > 0
    finally:
        app._jobs.pop("job-x", None)


# ---------------------------------------------------------------------------
# Evict de jobs
# ---------------------------------------------------------------------------

def test_anomalo_considera_speed():
    sr = 24000
    # 3s de áudio, texto de 100 chars → limiar base 100/45 ≈ 2.22s
    audio = np.full(3 * sr, 0.2, dtype=np.float32)
    assert not app._anomalo(audio, sr, "x" * 100)              # ok a speed 1
    assert app._anomalo(np.zeros(1, dtype=np.float32), sr, "x" * 100)  # inaudível
    # speed 2 encurta o áudio pela metade (1.5s < limiar 2.22): NÃO é truncamento
    curto = audio[:int(1.5 * sr)]
    assert app._anomalo(curto, sr, "x" * 100, speed=1.0)        # falso-positivo antigo
    assert not app._anomalo(curto, sr, "x" * 100, speed=2.0)    # corrigido


def test_evict_jobs_preserva_running(monkeypatch):
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_MAX", 3)
        for i in range(5):
            status = "running" if i < 2 else "done"
            app._jobs[f"job-{i}"] = {"status": status}
        app._evict_jobs()
        assert len(app._jobs) == 3
        ids = list(app._jobs)
        assert "job-0" in ids and "job-1" in ids        # running sobrevive
        assert ids[-1] == "job-4"                        # mais novo sempre fica
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


def test_evict_jobs_nao_descarta_ativo_mesmo_sem_terminado(monkeypatch):
    """Todos em voo = nenhum terminado para descartar.

    Antes o evict caía num "último recurso" e pegava o job MAIS ANTIGO, mesmo
    running: o cliente perdia status e trechos enquanto a thread seguia gerando.
    Agora ele passa do teto e volta a ele conforme os jobs terminam."""
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_MAX", 3)
        for i in range(5):
            app._jobs[f"job-{i}"] = {"status": "running"}
        app._evict_jobs()
        assert list(app._jobs) == [f"job-{i}" for i in range(5)]
        # um terminou: o teto volta a valer e sai justamente o terminado
        app._jobs["job-0"]["status"] = "done"
        app._evict_jobs()
        assert "job-0" not in app._jobs and len(app._jobs) == 4
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


def test_jobs_ativos_conta_running_e_queued(monkeypatch):
    """Job parado na fila de falas ainda é ativo (o cliente está esperando)."""
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        app._jobs.update(a={"status": "running"}, b={"status": "queued"},
                         c={"status": "done"}, d={"status": "error"})
        assert app._jobs_ativos() == 2
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


def test_admissao_recusa_com_429_e_retem_os_ativos(monkeypatch):
    """N > teto de ativos: o N+1º leva 429 no lugar de derrubar um ativo."""
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_ACTIVE_MAX", 2)
        monkeypatch.setattr(app, "_JOBS_MAX", 2)
        ids = [app._jobs_admit({"text": "x"}) for _ in range(2)]
        with pytest.raises(app.HTTPException) as e:
            app._jobs_admit({"text": "x"})
        assert e.value.status_code == 429
        assert "2/2" in e.value.detail and e.value.headers.get("Retry-After") == "5"
        assert list(app._jobs) == ids              # nenhum ativo foi descartado
        assert all(j["status"] == "running" for j in app._jobs.values())

        # um terminou: cabe outro, e o que sai do histórico é o terminado
        app._jobs[ids[0]]["status"] = "done"
        novo = app._jobs_admit({"text": "x"})
        assert novo in app._jobs and ids[0] not in app._jobs
        assert app._jobs[ids[1]]["status"] == "running"
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


def test_evict_nao_apaga_trechos_do_job_ativo(tmp_path, monkeypatch):
    """O .job-* é o que o cliente está streamando — só sai com o job terminado."""
    orig = dict(app._jobs)
    app._jobs.clear()
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path)
    try:
        monkeypatch.setattr(app, "_JOBS_MAX", 1)
        a = app._jobs_admit({"text": "a"})
        pdir = app._piece_dir(a)
        pdir.mkdir()
        (pdir / "0.wav").write_bytes(b"RIFF")
        b = app._jobs_admit({"text": "b"})          # teto 1, "a" ainda ativo
        assert set(app._jobs) == {a, b} and pdir.exists()

        app._jobs[a]["status"] = "done"
        app._jobs_admit({"text": "c"})
        assert a not in app._jobs and not pdir.exists()
        assert b in app._jobs
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


def test_admissao_e_atomica_sob_concorrencia(monkeypatch):
    """Teto de ativos exato mesmo com admissões simultâneas.

    Endpoint `def` roda no threadpool do FastAPI: sem lock, todos passavam
    juntos pela checagem e o teto era furado (medido no gate da #22: teto 2 com
    10 simultâneas → 4 aceitos). O invariante do P1 não mudava — o que furava
    era o teto como número."""
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_ACTIVE_MAX", 2)
        largada = threading.Barrier(10)
        aceitos, recusados = [], []

        def tentar():
            largada.wait(5)
            try:
                aceitos.append(app._jobs_admit({"text": "x"}))
            except app.HTTPException as exc:
                recusados.append(exc.status_code)

        threads = [threading.Thread(target=tentar) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert len(aceitos) == 2, aceitos
        assert recusados == [429] * 8
        assert app._jobs_ativos() == 2 == len(app._jobs)
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


def test_snapshot_nao_estoura_com_admissao_concorrente(monkeypatch):
    """Iterar `_jobs.values()` enquanto outra thread admite/evicta estoura
    "OrderedDict mutated during iteration" (medido: 2 estouros num stress de
    2000 admissões). `_jobs_snapshot()` é a cópia sob lock para quem itera."""
    orig = dict(app._jobs)
    app._jobs.clear()
    try:
        monkeypatch.setattr(app, "_JOBS_ACTIVE_MAX", 8)
        parar = threading.Event()
        erros = []

        def leitor():
            try:
                while not parar.is_set():
                    assert isinstance(app._jobs_snapshot(), list)
                    any(j.get("status") == "running" for j in app._jobs_snapshot())
                    app._jobs_ativos()
            except Exception as exc:                     # noqa: BLE001 (flake8; o pyflakes do hook ignora)
                erros.append(repr(exc))

        def trabalhador():
            for _ in range(150):
                try:
                    jid = app._jobs_admit({"text": "x"})
                except app.HTTPException:
                    continue
                app._jobs[jid]["status"] = "done"
                app._evict_jobs()

        leitores = [threading.Thread(target=leitor) for _ in range(3)]
        escritores = [threading.Thread(target=trabalhador) for _ in range(6)]
        for t in leitores + escritores:
            t.start()
        for t in escritores:
            t.join(20)
        parar.set()
        for t in leitores:
            t.join(5)
        assert not erros, erros
        assert len(app._jobs) <= app._JOBS_MAX
        assert app._jobs_ativos() == 0
    finally:
        app._jobs.clear()
        app._jobs.update(orig)


# ---------------------------------------------------------------------------
# Limpeza de boot de outputs/.job-* (#25) — outro processo importando app
# ---------------------------------------------------------------------------

def _rodar_boot_isolado(base):
    """Importa uma CÓPIA do app num subprocesso — a limpeza de boot de verdade.

    A cópia isola BASE/OUTPUTS_DIR: o subprocesso roda o import de app sem mexer
    no `outputs/` do repo. `backends`, `common` e `static` vêm do repo (o mount
    do StaticFiles exige o diretório); a cópia fica em sys.path[0] via cwd."""
    shutil.copy(app.__file__, base / "app.py")
    if not (base / "static").exists():
        (base / "static").symlink_to(Path(app.__file__).resolve().parent / "static")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(base), str(Path(app.__file__).resolve().parent)])}
    r = subprocess.run([sys.executable, "-c", "import app"], cwd=str(base), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]


def _processo_fake_worker():
    """Processo python cuja cmdline contém 'tts_worker.py' (grupo próprio)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "tts_worker.py"],
        start_new_session=True)


def test_boot_preserva_job_de_instancia_viva_e_remove_orfao(tmp_path):
    """Aceite da #25: importar app (pytest/smoke) não pode apagar o `.job-*` de
    um job EM VOO de outra instância — era 404 no status e `System error` no
    .wav — nem matar o worker dela. Órfão (dono morto) continua saindo."""
    base = tmp_path / "app-sob-teste"
    out = base / "outputs"
    out.mkdir(parents=True)

    vivo = out / ".job-vivo"                      # dono = ESTE processo (vivo)
    vivo.mkdir()
    (vivo / "owner.pid").write_text(str(os.getpid()))
    (vivo / "status.json").write_text('{"pieces": 3}')
    worker_vivo = _processo_fake_worker()
    (vivo / "worker.pid").write_text(str(worker_vivo.pid))

    morto = out / ".job-morto"                    # dono morto; worker órfão vivo
    morto.mkdir()
    (morto / "owner.pid").write_text("999999")    # pid que não existe mais
    worker_morto = _processo_fake_worker()
    (morto / "worker.pid").write_text(str(worker_morto.pid))

    legado = out / ".job-legado"                  # versão anterior: sem owner.pid
    legado.mkdir()
    (legado / "status.json").write_text("{}")     # ...mas gerando AGORA
    worker_legado = _processo_fake_worker()       # P3 do gate: dir e worker andam juntos
    (legado / "worker.pid").write_text(str(worker_legado.pid))

    recente = out / ".job-recente"                # dir de versão anterior: sem dono
    recente.mkdir()
    (recente / "status.json").write_text("{}")

    velho = out / ".job-velho"                    # sem dono e sem atividade
    velho.mkdir()
    (velho / "0.wav").write_bytes(b"RIFF")
    antigo = time.time() - (app._JOB_ORFAO_JANELA_S + 600)
    for alvo in (velho, velho / "0.wav"):
        os.utime(alvo, (antigo, antigo))

    arquivo = out / ".job-arquivo"                # arquivo solto, não diretório
    arquivo.write_text("lixo")
    os.utime(arquivo, (antigo, antigo))

    try:
        _rodar_boot_isolado(base)

        assert vivo.exists() and (vivo / "status.json").exists()   # em voo intacto
        assert worker_vivo.poll() is None                          # worker vivo segue
        assert not morto.exists()                                  # órfão saiu
        assert worker_morto.poll() is not None                     # ...e o worker dele
        assert recente.exists()                                    # sem dono, recente
        assert not velho.exists()                                  # sem dono, velho
        assert not arquivo.exists()                                # arquivo solto velho
        assert legado.exists() and worker_legado.poll() is None     # dir e worker juntos
    finally:
        worker_vivo.kill()
        worker_morto.kill()
        worker_legado.kill()
        worker_vivo.wait(10)
        worker_morto.wait(10)
        worker_legado.wait(10)


def test_job_dir_ativo_por_dono_e_por_atividade(tmp_path):
    """Unidade da regra: com dono registrado é ele que decide (vivo fica, morto
    sai NA HORA, mesmo recente); sem dono vale atividade recente; pid reciclado
    por programa que não é python não conta (quando o `ps` responde — em sandbox
    ele pode estar negado, aí vale só a vida do pid)."""
    d = tmp_path / ".job-x"
    d.mkdir()
    assert app._job_dir_ativo(d)                     # recém-criado, sem dono: segura

    (d / "owner.pid").write_text(str(os.getpid()))
    assert app._job_owner_vivo(d) and app._job_dir_ativo(d)

    (d / "owner.pid").write_text("999999")           # dono morto: órfão de crash
    assert not app._job_dir_ativo(d)                 # ...sai mesmo sendo recente
    (d / "status.json").write_text("{}")
    antigo = time.time() - (app._JOB_ORFAO_JANELA_S + 60)
    for alvo in (d, d / "status.json"):
        os.utime(alvo, (antigo, antigo))
    assert not app._job_dir_ativo(d)

    (d / "owner.pid").unlink()                       # versão anterior: sem dono
    assert app._job_dir_ativo(d)                     # velho sem dono: atividade manda
    (d / "status.json").unlink()
    for alvo in (d,):
        os.utime(alvo, (antigo, antigo))
    assert not app._job_dir_ativo(d)                 # sem dono e parado: órfão

    (d / "owner.pid").write_text("1")                # pid 1 (launchd) não é python
    if app._pid_cmd(1):                              # ps respondeu: reciclagem pega
        assert not app._job_owner_vivo(d)
        assert not app._job_dir_ativo(d)
    else:                                            # sem ps: vale a vida do pid
        assert app._job_owner_vivo(d)


# ---------------------------------------------------------------------------
# Resolução de parâmetros
# ---------------------------------------------------------------------------

def test_resolve_omni_payload_sobrepoe_settings():
    o = app._resolve_omni({"num_steps": 24, "speed": 1.5}, family="omnivoice")
    assert o["num_steps"] == 24
    assert o["speed"] == 1.5
    o2 = app._resolve_omni({}, family="omnivoice")
    assert o2["num_steps"] == app._settings["omni_num_steps"]
    assert o2["speed"] == app._settings["speed"]
    assert o2["seed"] == app._settings["omni_seed"]


def test_resolve_duration():
    assert app._resolve_duration_s(None, 5.0) is None
    assert app._resolve_duration_s("0", 5.0) is None
    assert app._resolve_duration_s("3", 5.0) == 3.0
    assert app._resolve_duration_s(999, 5.0) == 60.0


# ---------------------------------------------------------------------------
# VAD do Silero: o caminho ONNX tem de ser o escolhido
# ---------------------------------------------------------------------------

def test_vad_pede_onnx_explicito(monkeypatch):
    """Regressão 2026-09-24: `load_silero_vad()` sem argumento NÃO garante ONNX.

    No silero-vad 6.x o default é onnx=False (no 5.x era True), então o app
    carregava o jit do torch mesmo com onnxruntime instalado — e o `except`
    nunca disparava, porque os dois ramos chamavam o mesmo modelo. A ordem das
    chamadas ([True, False]) é o que prova a preferência pelo ONNX."""
    import silero_vad
    chamadas = []

    def espiao(onnx=False, opset_version=16):
        chamadas.append(onnx)
        if onnx:
            raise ModuleNotFoundError("No module named 'onnxruntime'")
        return "modelo-jit-simulado"

    monkeypatch.setattr(silero_vad, "load_silero_vad", espiao)
    monkeypatch.setattr(app, "_vad_model", None)
    monkeypatch.setattr(app, "_vad_backend", "")

    assert app._vad_load() == "modelo-jit-simulado"    # caiu no fallback
    assert chamadas == [True, False]                   # ...mas só após pedir ONNX
    assert app._vad_backend == "torch-jit"


def test_vad_usa_onnx_quando_onnxruntime_existe(monkeypatch):
    """Com onnxruntime instalado o modelo escolhido é o ONNX (não o jit)."""
    import importlib.util
    monkeypatch.setattr(app, "_vad_model", None)
    monkeypatch.setattr(app, "_vad_backend", "")

    modelo = app._vad_load()
    if importlib.util.find_spec("onnxruntime") is None:
        assert app._vad_backend == "torch-jit"
    else:
        assert app._vad_backend == "onnx"
        assert type(modelo).__name__ == "OnnxWrapper"


def test_vad_load_nao_usa_importlib_resources_path_deprecado(monkeypatch):
    """Regressão 2026-09-24 (task #30): o `silero_vad/model.py` tem dois ramos.

    Com o backport `importlib_resources` instalado ele usa `files()` (moderno);
    sem ele, cai no `importlib.resources.path()`, deprecado desde o Python 3.11
    e que largava 1 DeprecationWarning por load (4 no gate, um em cada teste que
    carrega o VAD real). O pin no requirements.txt existe para escolher o ramo
    moderno — se ele sumir do venv, este teste acusa, em vez de um filtro de -W
    esconder um warning de terceiro e, junto, os nossos."""
    import importlib.util
    import warnings

    if importlib.util.find_spec("onnxruntime") is None:
        pytest.skip("onnxruntime ausente")
    assert importlib.util.find_spec("importlib_resources") is not None, (
        "backport importlib_resources ausente no venv: silero-vad volta a usar "
        "importlib.resources.path() deprecado (instale -r requirements.txt)")

    import importlib_resources
    import silero_vad
    # Importados de propósito FORA do bloco: o alvo do teste é só o load.
    # E eles provam o ramo moderno — `.files()` acha o peso de verdade.
    assert callable(silero_vad.load_silero_vad)
    assert importlib_resources.files("silero_vad.data").joinpath(
        "silero_vad.onnx").is_file(), "ramo moderno do silero não achou o ONNX"

    monkeypatch.setattr(app, "_vad_model", None)
    monkeypatch.setattr(app, "_vad_backend", "")

    with warnings.catch_warnings(record=True) as capturados:
        warnings.simplefilter("always")        # vence o registry de deduplicação
        app._vad_load()

    assert app._vad_backend == "onnx"          # o caminho exercitado é o real
    # Só o que sai do próprio silero (a API deprecada) e do nosso app: o
    # `catch_warnings` é global do processo e um thread de outro teste pode pingar
    # um warning de terceiro no meio da janela (flake visto pelo speech-pipeline).
    deprecacoes = []
    for x in capturados:
        if not issubclass(x.category, DeprecationWarning):
            continue
        origem = Path(x.filename)
        if "silero_vad" in origem.parts or origem.name == "app.py":
            deprecacoes.append(f"{origem.name}:{x.lineno}: {x.message}")
    assert not deprecacoes, deprecacoes


def test_vad_tem_fala_silencio_puro_e_falso(tmp_path, monkeypatch):
    """Sessão ONNX rodando de verdade: silêncio não passa como fala (o
    fail-open do `except` devolveria True e o guarda anti-alucinação sumiria)."""
    import importlib.util
    import soundfile as sf
    if importlib.util.find_spec("onnxruntime") is None:
        pytest.skip("onnxruntime ausente")
    monkeypatch.setattr(app, "_vad_model", None)
    monkeypatch.setattr(app, "_vad_backend", "")

    arq = tmp_path / "silencio.wav"
    sf.write(str(arq), np.zeros(16000 * 3, dtype="float32"), 16000)

    assert app._vad_tem_fala(arq) is False
    assert app._vad_backend == "onnx"


def test_vad_reamostra_24k_antes_do_modelo(tmp_path, monkeypatch):
    """O navegador envia WAV a 24 kHz (TARGET_SR do index.html) e o Silero só
    aceita 8/16 kHz. Sem reamostrar, o modelo estourava e o `except` devolvia
    True — ruído puro vindo de arquivo passava como fala."""
    import importlib.util
    import soundfile as sf
    if importlib.util.find_spec("onnxruntime") is None:
        pytest.skip("onnxruntime ausente")
    monkeypatch.setattr(app, "_vad_model", None)
    monkeypatch.setattr(app, "_vad_backend", "")

    rng = np.random.default_rng(11)
    arq = tmp_path / "ruido24k.wav"
    sf.write(str(arq), rng.normal(0, 0.05, 24000 * 3).astype("float32"), 24000)

    assert app._vad_tem_fala(arq) is False


@pytest.mark.parametrize("preset, esperado", [(None, "1"), ("0", "0"), ("1", "1")])
@pytest.mark.parametrize("backend", ["openai", "dsh"])
def test_ort_telemetry_default_sem_sobrescrever_usuario(preset, esperado, backend, tmp_path):
    """onnxruntime sem $HOME gravável larga um ":memory:.ses" na raiz do repo (e
    um warning no stderr) a cada import — o app.py corta isso com `setdefault`.
    Como é `setdefault` (igual ao `${VAR:-1}` do run.sh), valor explícito do
    usuário MANDA: o teste roda em subprocess com env controlado, senão um
    `ORT_DISABLE_TELEMETRY=0` no ambiente do pytest faria o gate mentir.

    Dois pontos para o teste não passar/falhar por CORRIDA nem por estado do dono
    (task_6db0e2cc): (1) o backend do chat vai EXPLÍCITO por env, nos dois
    sentidos, com o dsh FALSO (sem depender do dsh instalado nem do
    `settings.json` real — sem o pino, trocar a Conversa para dsh na tela pintava
    o gate de vermelho); (2) o filho ESPERA o pre-warm terminar quando ele existe
    — o log do dsh sai por stderr e o stdout tem de sair limpo COM o pre-warm
    rodando, que é o que a igualdade exata abaixo cobra."""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items()
           if k not in ("ORT_DISABLE_TELEMETRY", "TTS_CHAT_BACKEND", "TTS_CHAT_DSH_BIN")}
    if preset is not None:
        env["ORT_DISABLE_TELEMETRY"] = preset
    env["TTS_CHAT_BACKEND"] = backend
    if backend == "dsh":
        # wrapper para o servidor ACP falso, como o `dsh_fake_bin` do test_api: o
        # nome não começa com `dsh` de propósito (pula o check de Node).
        fake = Path(__file__).resolve().parent / "fake_acp.py"
        wrapper = tmp_path / "fake-acp"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
        wrapper.chmod(0o755)
        env["TTS_CHAT_DSH_BIN"] = str(wrapper)
    r = subprocess.run([sys.executable, "-c",
                        "import os, app\n"
                        "th = getattr(app, '_chat_dsh_prewarm_thread', None)\n"
                        "if th is not None:\n"
                        "    th.join(60)\n"
                        "print(os.environ['ORT_DISABLE_TELEMETRY'])"],
                       capture_output=True, text=True, env=env, cwd=app.BASE)
    assert r.returncode == 0, r.stderr[-300:]
    assert r.stdout.strip() == esperado, f"stdout do import saiu sujo: {r.stdout!r}"
    if backend == "dsh":
        assert "[chat-dsh]" in r.stderr, "o pre-warm não logou — o caminho dsh não correu"


# ---------------------------------------------------------------------------
# Áudio que o libsndfile não lê (webm/opus do MediaRecorder)
# ---------------------------------------------------------------------------

def _gerar_webm(tmp_path, sinal):
    """Codifica um sinal de 16 kHz em webm/opus com o FFMPEG do app."""
    import subprocess
    import soundfile as sf
    origem = tmp_path / "origem.wav"
    sf.write(str(origem), sinal, 16000)
    webm = tmp_path / "clip.webm"
    p = subprocess.run([app.FFMPEG, "-y", "-v", "error", "-i", str(origem),
                        "-c:a", "libopus", str(webm)], capture_output=True, timeout=60)
    if p.returncode != 0 or not webm.exists():
        pytest.skip("ffmpeg indisponível para gerar webm")
    return webm


def test_wav_to_mono16k_decodifica_webm(tmp_path):
    """libsndfile não lê matroska. Sem o fallback ffmpeg em `_wav_to_mono16k`,
    quem passa o blob cru leva LibsndfileError (era o caminho interno do
    `_transcribe`, que só não quebrava porque todo endpoint converte antes)."""
    import soundfile as sf
    rng = np.random.default_rng(3)
    webm = _gerar_webm(tmp_path, rng.normal(0, 0.05, 16000 * 2).astype("float32"))

    with pytest.raises(Exception):
        sf.read(str(webm))                       # sozinho o libsndfile não dá conta

    a = app._wav_to_mono16k(webm)
    assert a.dtype == np.float32 and a.ndim == 1
    assert abs(len(a) / 16000 - 2.0) < 0.2       # 2 s preservados


def test_vad_em_webm_nao_cai_no_fail_open(tmp_path, monkeypatch):
    """webm com ruído puro: antes o `except` devolvia True e o guarda
    anti-alucinação virava no-op nesse formato."""
    import importlib.util
    if importlib.util.find_spec("onnxruntime") is None:
        pytest.skip("onnxruntime ausente")
    rng = np.random.default_rng(5)
    webm = _gerar_webm(tmp_path, rng.normal(0, 0.05, 16000 * 3).astype("float32"))
    monkeypatch.setattr(app, "_vad_model", None)
    monkeypatch.setattr(app, "_vad_backend", "")

    assert app._vad_tem_fala(webm) is False


# ---------------------------------------------------------------------------
# #303 — NaN não pode virar WAV mudo com status done
# ---------------------------------------------------------------------------

class _ModeloFalso:
    sample_rate = 24000


def _job_de_render(monkeypatch, tmp_path, gerador, texto="x", remoto=False):
    """Monta o mínimo para rodar `_run_tts_job` sem modelo nem Metal."""
    monkeypatch.setattr(app, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(app, "_use_remote_tts", lambda: remoto)
    monkeypatch.setattr(app, "_current_backend", lambda *a, **k: {
        "id": "omnivoice", "family": "omnivoice", "is_shortcut": True,
        "path": "omnivoice", "meta": {"label": "Omni"}})
    monkeypatch.setattr(app, "_get_model", lambda *a, **k: _ModeloFalso())
    monkeypatch.setattr(app, "_cond_for", lambda *a, **k: {})
    monkeypatch.setattr(app, "_voice_ref_text", lambda *a, **k: None)
    monkeypatch.setattr(app, "_generate_chunk", gerador)
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(app, "_schedule_job_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(app, "_touch_use", lambda *a, **k: None)
    app._jobs.clear()
    return app._jobs_admit({"text": texto})


_TEXTO_1_TRECHO = ("Hoje a gente vai falar de tres coisas na ordem primeiro o cronograma "
                   "depois os custos e por ultimo as duvidas de voces")


def _bom(segundos: float = 3.0) -> np.ndarray:
    """Fala sintética audível, longa o bastante para não ser 'truncada'."""
    n = int(segundos * 24000)
    t = np.arange(n) / 24000.0
    return (0.15 * np.sin(2 * np.pi * 180 * t)
            + 0.08 * np.sin(2 * np.pi * 700 * t)).astype(np.float32)


def test_anomalo_reprova_nao_finito():
    bom = _bom()
    assert app._anomalo(np.full(24000, np.nan, dtype=np.float32), 24000, "x" * 100)
    assert app._anomalo(np.full(24000, np.inf, dtype=np.float32), 24000, "x" * 100)
    misto = bom.copy()
    misto[100] = np.nan
    assert app._anomalo(misto, 24000, "x" * 100)
    assert "finito" in app._motivo_anomalia(misto, 24000, "x" * 100)
    assert not app._anomalo(bom, 24000, _TEXTO_1_TRECHO)


def test_render_com_nan_regera_e_nao_grava_mudo(monkeypatch, tmp_path, capsys):
    """1ª tentativa NaN: retry, áudio audível, sinal no log E no meta."""
    chamadas = []

    def _gerador(model, texto, *a, **k):
        chamadas.append(texto)
        if len(chamadas) == 1:
            return np.full(24000, np.nan, dtype=np.float32)
        return _bom()

    job_id = _job_de_render(monkeypatch, tmp_path, _gerador)
    app._run_tts_job(job_id, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    saida = capsys.readouterr().out
    try:
        assert job["status"] == "done", job
        assert len(chamadas) == 2, "o trecho NaN não foi regerado"
        assert job.get("retries") == 1
        meta = job["output"]
        assert meta["anomalias"] == 1
        assert "finito" in meta["anomalias_motivos"][0]
        assert meta["rms"] > 0.05, "gravou mudo"
        assert "[tts]" in saida and "NaN" in saida, "sem sinal no log"
        import soundfile as sf
        wav, _ = sf.read(str(tmp_path / f"{meta['id']}.wav"), dtype="float32")
        assert float(np.sqrt(np.mean(wav ** 2))) > 0.05
    finally:
        app._jobs.pop(job_id, None)


def test_render_anomalo_nas_duas_tentativas_avisa(monkeypatch, tmp_path):
    """2ª tentativa também NaN: o trecho entra, mas o job fica AVISADO."""
    def _gerador(model, texto, *a, **k):
        return np.full(24000, np.nan, dtype=np.float32)

    job_id = _job_de_render(monkeypatch, tmp_path, _gerador)
    app._run_tts_job(job_id, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    try:
        assert job["status"] == "done"
        assert job.get("retries") == 2
        assert "anômalos" in (job.get("warning") or "")
        # `anomalias` conta EVENTOS (as duas tentativas); o aviso é do não resolvido
        assert job["output"]["anomalias"] == 2
        assert job["warning"] in job["output"]["warning"]
        assert job["output"]["rms"] == 0.0
    finally:
        app._jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# #302 — um ganho só: trechos no mesmo nível e arquivo final no alvo de loudness
# ---------------------------------------------------------------------------

_PARAGRAFO = ("Bom dia a todos. Hoje a gente vai falar de três coisas, na ordem: "
              "primeiro o cronograma, depois os custos, e por último as dúvidas de vocês.")
# níveis crus medidos na auditoria (#284, item 1) para os 4 trechos do parágrafo
_NIVEIS = (0.0532, 0.1267, 0.1059, 0.0970)


def test_render_iguala_trechos_e_fecha_no_alvo_de_loudness(monkeypatch, tmp_path):
    import soundfile as sf

    from common import LUFS_ALVO, loudness_lufs

    monkeypatch.setitem(app._settings, "chunk_max_chars", 60)
    n = {"i": 0}
    niveis_vistos = []

    def _gerador(model, texto, *a, **k):
        nivel = _NIVEIS[n["i"] % len(_NIVEIS)]
        n["i"] += 1
        niveis_vistos.append(nivel)
        t = np.arange(int(2.5 * 24000)) / 24000.0
        # modulado como sílabas (mede ~ -16 LUFS, fora do alvo) + transiente de
        # crista alta em todo trecho (o caso que invertia o envelope)
        env = np.repeat(np.hanning(2400), 25)[:t.size]
        env = np.pad(env, (0, t.size - env.size), mode="edge")
        x = (nivel * env * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
        x[:60] *= 6.0
        return x

    job_id = _job_de_render(monkeypatch, tmp_path, _gerador, texto=_PARAGRAFO)
    app._run_tts_job(job_id, _PARAGRAFO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    try:
        assert job["status"] == "done", job
        assert len(niveis_vistos) == 4, "o parágrafo não virou 4 trechos"
        # 1) os trechos saem no MESMO nível (o envelope não inverte mais)
        import math
        rms = []
        for i in range(4):
            y, _ = sf.read(str(app._piece_dir(job_id) / f"{i}.wav"), dtype="float32")
            rms.append(float(np.sqrt(np.mean(y ** 2))))
        espalhamento = 20 * math.log10(max(rms) / min(rms))
        assert espalhamento <= 2.0, f"níveis desiguais: {rms} ({espalhamento:.1f} dB)"
        # 2) o arquivo final é ancorado no alvo de loudness, com UM ganho registrado
        final = tmp_path / f"{job['output']['id']}.wav"
        assert abs(loudness_lufs(final) - LUFS_ALVO) <= 1.0
        assert isinstance(job["output"]["lufs_ganho_db"], float)
        assert job["output"]["lufs_ganho_db"] != 0.0
    finally:
        app._jobs.pop(job_id, None)


def test_render_avisa_quando_nao_da_para_medir_loudness(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_normaliza_loudness", lambda *a, **k: None)
    job_id = _job_de_render(
        monkeypatch, tmp_path, lambda *a, **k: _bom(), texto=_TEXTO_1_TRECHO)
    app._run_tts_job(job_id, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    try:
        assert "loudness" in (job.get("warning") or "")
        assert "loudness" in job["output"]["warning"]
        assert job["output"]["lufs_ganho_db"] is None
    finally:
        app._jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# #304 — remoto: anomalia detectada e fallback local (nunca job error)
# ---------------------------------------------------------------------------

def _so_local(monkeypatch):
    """Caminho local mínimo para o fallback: modelo falso, sem voz em disco."""
    monkeypatch.setattr(app, "_ensure_remote_voice", lambda *a, **k: None)
    monkeypatch.setitem(app._settings, "remote_tts_voice", "")


def test_remoto_com_silencio_regera_local(monkeypatch, tmp_path):
    """200 com silêncio não passa como bom: 2 tentativas remotas e vira LOCAL."""
    import soundfile as sf

    _so_local(monkeypatch)
    remotos = {"n": 0}

    def _remoto(*a, **k):
        remotos["n"] += 1
        return np.zeros(24000, dtype=np.float32)          # 200 com mudo

    locais = {"n": 0}

    def _local(*a, **k):
        locais["n"] += 1
        return _bom()

    monkeypatch.setattr(app, "_tts_remote_chunk", _remoto)
    job_id = _job_de_render(monkeypatch, tmp_path, _local,
                            texto=_TEXTO_1_TRECHO, remoto=True)
    app._run_tts_job(job_id, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    try:
        assert job["status"] == "done", job
        assert remotos["n"] == 2, "o silêncio remoto não foi tentado 2x"
        assert locais["n"] >= 1, "o trecho não foi regerado local"
        assert job.get("remote_fallback") is True
        assert job["output"]["remote_fallback"] is True
        assert "LOCAL" in (job.get("warning") or "")
        assert job["output"]["rms"] > 0.05, "gravássemos mudo"
        wav, _ = sf.read(str(tmp_path / f"{job['output']['id']}.wav"), dtype="float32")
        assert float(np.sqrt(np.mean(wav ** 2))) > 0.05
    finally:
        app._jobs.pop(job_id, None)


def test_remoto_que_estoura_cai_para_local_sem_error(monkeypatch, tmp_path):
    """500 persistente: job termina `done` marcado, não `error`."""
    _so_local(monkeypatch)
    chamadas = {"n": 0}

    def _remoto(*a, **k):
        chamadas["n"] += 1
        raise RuntimeError("TTS remoto falhou (500)")

    def _local(*a, **k):
        return _bom()

    monkeypatch.setattr(app, "_tts_remote_chunk", _remoto)
    job_id = _job_de_render(monkeypatch, tmp_path, _local,
                            texto=_TEXTO_1_TRECHO, remoto=True)
    app._run_tts_job(job_id, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    try:
        assert job["status"] == "done", job
        assert chamadas["n"] == 2, "não insistiu 2x antes de desistir"
        assert job["output"]["remote_fallback"] is True
        assert job["output"]["rms"] > 0.05
    finally:
        app._jobs.pop(job_id, None)


def test_remoto_saudavel_nao_marca_fallback(monkeypatch, tmp_path):
    """Remoto bom: nenhum aviso, nenhum fallback, e o local não é tocado."""
    _so_local(monkeypatch)
    locais = {"n": 0}

    def _local(*a, **k):
        locais["n"] += 1
        return _bom()

    monkeypatch.setattr(app, "_tts_remote_chunk", lambda *a, **k: _bom())
    job_id = _job_de_render(monkeypatch, tmp_path, _local,
                            texto=_TEXTO_1_TRECHO, remoto=True)
    app._run_tts_job(job_id, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    try:
        assert job["status"] == "done"
        assert locais["n"] == 0, "gerou local com o remoto saudável"
        assert job["output"]["remote_fallback"] is False
        assert not job.get("warning")
    finally:
        app._jobs.pop(job_id, None)


def test_fallback_desligado_volta_a_dar_error(monkeypatch, tmp_path):
    """`TTS_REMOTE_TTS_FALLBACK=0`: falha do remoto derruba o job (comportamento antigo)."""
    _so_local(monkeypatch)
    monkeypatch.setenv("TTS_REMOTE_TTS_FALLBACK", "0")

    def _remoto(*a, **k):
        raise RuntimeError("TTS remoto falhou (500)")

    monkeypatch.setattr(app, "_tts_remote_chunk", _remoto)
    job_id = _job_de_render(monkeypatch, tmp_path, lambda *a, **k: _bom(),
                            texto=_TEXTO_1_TRECHO, remoto=True)
    app._run_tts_job(job_id, _TEXTO_1_TRECHO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    try:
        assert job["status"] == "error"
        assert "remoto" in (job.get("error") or "").lower()
    finally:
        app._jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# #319 — pausas medidas no WAV final (vírgula curta, fim de frase longa)
# ---------------------------------------------------------------------------

def _maior_vale(wav: np.ndarray, sr: int, limiar: float = 1e-4) -> int:
    """Maior sequência de amostras em silêncio, em amostras."""
    quieto = np.abs(wav) <= limiar
    maior = atual = 0
    for q in quieto:
        atual = atual + 1 if q else 0
        maior = max(maior, atual)
    return maior


def _cauda(wav: np.ndarray, limiar: float = 1e-4) -> int:
    """Só a sequência de silêncio que FECHA o arquivo (do fim para o começo)."""
    n = 0
    for v in wav[::-1]:
        if abs(float(v)) > limiar:
            break
        n += 1
    return n


def test_pausas_do_render_seguem_a_pontuacao(monkeypatch, tmp_path):
    """Vírgula respira curto e ponto respira fundo; a cauda final é curta.

    Antes era um vão FIXO de 550 ms (300 de pad + 250 fixos) mesmo onde havia
    vírgula, e 300 ms de cauda no último trecho (#319).
    """
    import soundfile as sf

    monkeypatch.setitem(app._settings, "chunk_max_chars", 60)

    def _gerador(model, texto, *a, **k):
        # 1 s de fala + 0,3 s de silêncio no fim: é a sobra que o trim tem de
        # cortar (sem isso a cauda medida seria a do próprio gerador)
        fala = np.arange(int(1.0 * 24000)) / 24000.0
        env = np.hanning(fala.size)
        x = (0.2 * env * np.sin(2 * np.pi * 180 * fala)).astype(np.float32)
        return np.concatenate([x, np.zeros(7200, dtype=np.float32)])

    job_id = _job_de_render(monkeypatch, tmp_path, _gerador, texto=_PARAGRAFO)
    app._run_tts_job(job_id, _PARAGRAFO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs[job_id]
    sr = 24000
    try:
        assert job["status"] == "done", job
        wav, _ = sf.read(str(tmp_path / f"{job['output']['id']}.wav"), dtype="float32")
        # cauda final: só o pad do fresco, sem pausa inserida
        final = _cauda(wav)
        assert final <= int(0.10 * sr) + 5, f"cauda final {final / sr * 1000:.0f} ms"
        # vãos internos: cada um respeita o limite da SUA pontuação
        maiores = []
        quieto = np.abs(wav) <= 1e-4
        i = 0
        while i < quieto.size:
            if quieto[i]:
                j = i
                while j < quieto.size and quieto[j]:
                    j += 1
                # só vãos de verdade (>= 50 ms): as passagens pelo zero do
                # próprio sinal também contam como silêncio e poluiriam o mínimo
                if i > 0 and j < quieto.size and (j - i) >= int(0.05 * sr):
                    maiores.append(j - i)
                i = j
            else:
                i += 1
        assert maiores, "não achou vão nenhum"
        gaps = [round(m / sr * 1000) for m in maiores]
        # limites ABSOLUTOS do aceite (uma pausa única em todos não passa)
        assert max(maiores) <= int(0.50 * sr) + 5, f"vão longo demais: {gaps} ms"
        assert min(maiores) <= int(0.25 * sr) + 5, f"nenhum vão curto de vírgula: {gaps} ms"
    finally:
        app._jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# #324 — integridade da referência e dos perfis de locutor
# ---------------------------------------------------------------------------

def _fala_suja(segundos: float = 2.0, sr: int = 24000) -> np.ndarray:
    t = np.arange(int(segundos * sr)) / sr
    rng = np.random.default_rng(11)
    fala = 0.15 * np.sin(2 * np.pi * 200 * t) * np.hanning(t.size)
    return (fala + 0.05 * rng.standard_normal(t.size)).astype(np.float32)


def test_denoise_preserva_comprimento_e_nivel():
    """`istft` devolvia amostras a mais e o gating tirava nível (referência muda)."""
    x = _fala_suja()
    for forca in (0.3, 0.7, 1.0):
        y = app._denoise_audio(x, 24000, forca)
        assert y.size == x.size, f"comprimento mudou com strength {forca}"
        r_in = float(np.sqrt(np.mean(x ** 2)))
        r_out = float(np.sqrt(np.mean(y ** 2)))
        assert abs(20 * np.log10(r_out / r_in)) <= 3.0, (forca, r_in, r_out)


def test_denoise_estavel_em_chamadas_repetidas():
    """Limpar de novo não pode fazer o vetor crescer (era +64 amostras por vez)."""
    x = _fala_suja(0.5)
    y = x
    for _ in range(3):
        y = app._denoise_audio(y, 24000, 0.7)
    assert y.size == x.size


def test_denoise_para_referencia_recusa_quando_o_nivel_desaba(monkeypatch):
    x = _fala_suja(1.0)
    monkeypatch.setattr(app, "_denoise_audio",
                        lambda a, sr, s: np.asarray(a, dtype=np.float32) * 0.01)
    y, recusado = app._denoise_para_referencia(x, 24000, 1.0)
    assert recusado is True
    assert np.allclose(y, x), "recusado tem de devolver a amostra ORIGINAL"


def test_referencia_pronta_normaliza_e_rampeia():
    x = _fala_suja(1.0) * 0.05          # bem baixa, como gravação ruim
    y = app._referencia_pronta(x, 24000)
    assert float(np.sqrt(np.mean(y ** 2))) == pytest.approx(0.15, rel=0.06)
    assert y[0] == pytest.approx(0.0, abs=1e-6) and y[-1] == pytest.approx(0.0, abs=1e-6)


def test_perfis_de_locutor_gravados_atomicos_e_com_modo(tmp_path, monkeypatch):
    alvo = tmp_path / ".speaker-profiles.json"
    monkeypatch.setattr(app, "SPEAKER_PATH", alvo)
    app._speaker_save({"ana": {"vecs": [[0.1, 0.2]], "updated": 0}})
    assert alvo.exists()
    assert json.loads(alvo.read_text())["ana"]["vecs"] == [[0.1, 0.2]]
    assert (alvo.stat().st_mode & 0o777) == 0o600
    # escrita atômica não deixa tmp para trás
    assert [p.name for p in tmp_path.iterdir()] == [alvo.name]


def test_perfis_corrompidos_nao_viram_gate_inerte(tmp_path, monkeypatch):
    """Arquivo ilegível: `enforce` FALHA FECHADO e avisa; ausente continua inerte."""
    alvo = tmp_path / ".speaker-profiles.json"
    monkeypatch.setattr(app, "SPEAKER_PATH", alvo)
    monkeypatch.setattr(app, "_speaker_embed", lambda p: [1.0, 0.0])
    app._settings["speaker_gate"] = "enforce"
    try:
        # ausente = "ninguém cadastrado": inerte, sem aviso
        assert app._speaker_gate_ok(Path("x.wav")) == {}
        # corrompido = ilegível: rejeita e diz o motivo
        alvo.write_text('{"ana": {"vecs": [[0.1,')
        out = app._speaker_gate_ok(Path("x.wav"))
        assert out.get("rejected") is True and "ilegíveis" in out.get("reason", "")
        # modo etiqueta: não bloqueia, mas avisa
        app._settings["speaker_gate"] = "label"
        out = app._speaker_gate_ok(Path("x.wav"))
        assert "speaker_aviso" in out and "rejected" not in out
    finally:
        app._settings["speaker_gate"] = "off"


def test_enrolls_concorrentes_nao_perdem_vetor(tmp_path, monkeypatch):
    alvo = tmp_path / ".speaker-profiles.json"
    monkeypatch.setattr(app, "SPEAKER_PATH", alvo)

    def enroll(vec):
        # mesma seção crítica do endpoint /api/speaker/enroll
        with app._speaker_lock:
            perfis = app._speaker_load()
            p = perfis.get("ana", {"vecs": [], "updated": 0})
            p["vecs"] = (p.get("vecs") or []) + [vec]
            p["updated"] = 1
            perfis["ana"] = p
            app._speaker_save(perfis)

    ts = [threading.Thread(target=enroll, args=([float(i), 0.0],)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(json.loads(alvo.read_text())["ana"]["vecs"]) == 8
