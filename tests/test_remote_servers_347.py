"""Servidores remotos (#347): event loop livre, 503 sem ASR, pool por tradutor e rev.

POR QUE ESTE ARQUIVO EXISTE: quatro achados P2 da auditoria #286 sobre os
servidores da RTX. O contrato caro deles é invisível na leitura feliz —
`/health` travado durante uma transcrição, 500 em vez de 503 com o Whisper
desligado, o tradutor "rápido" enfileirado atrás do lento e `/health` sem dizer
qual código está no ar.

Usa o mesmo harness de `test_remote_auth_policy.py` (stubs de torch/transformers/
librosa), então nada carrega modelo nem toca CUDA.
"""

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(BASE / "tests"))
from test_remote_auth_policy import carrega_servidor  # noqa: E402, F401 (fixture)

# `noqa` no import (#378: o ruff respeita; o pyflakes, que saiu, exigia `__all__`).


def _client(mod, chave="k1"):
    """TestClient com a chave: o host do TestClient não é loopback, então o
    middleware exigiria chave em toda rota — e um 401 não exercita o handler."""
    return TestClient(mod.app, headers={"X-API-Key": chave})


# ------------------------------------------------- 1) event loop não bloqueia

class _Upload:
    """Só o que `read_upload_limited` usa. `UploadFile` de verdade exige o
    multipart inteiro; aqui o alvo é o escalonamento do loop, não o parsing."""

    filename = "a.wav"

    def __init__(self, dados=b"RIFF"):
        self._dados = dados

    async def read(self, n=-1):
        return self._dados


def test_health_responde_durante_a_transcricao(carrega_servidor, monkeypatch):
    """Uma transcrição em curso NÃO pode segurar o loop (o smoke e o app usam
    `/health` como liveness). Medido no LOOP, sem TestClient: com o handler
    síncrono, qualquer timer atrasa o tempo da inferência inteira."""
    import asyncio

    mod = carrega_servidor("voxtral_server.py", chave="k1")
    monkeypatch.setattr(mod, "MIN_SPEECH", 0.0)
    monkeypatch.setattr(mod, "_load16", lambda _p: object())
    monkeypatch.setattr(mod, "_speech_stats", lambda _w: (9.0, 0.9, 9.0))
    monkeypatch.setattr(mod, "_compression_ratio", lambda _t: 1.0)
    def _run_lento(_wav, _lang):
        time.sleep(0.8)
        return "texto", -0.2

    monkeypatch.setattr(mod, "_run", _run_lento)

    async def cenario():
        # args explícitos: chamando o handler direto não há o `Form()` do FastAPI
        tarefa = asyncio.create_task(mod.transcriptions(
            _Upload(), model=None, language="pt", response_format="json",
            beam_size=5, temperature=0.0, prompt=None))
        # Timer de 50 ms logo em seguida: se o handler bloquear o loop, o timer só
        # volta DEPOIS da inferência (não adianta esperar um flag — o flag é posto
        # no próprio thread do loop e só é visto quando ele volta).
        t0 = time.monotonic()
        await asyncio.sleep(0.05)
        atraso_ms = (time.monotonic() - t0 - 0.05) * 1000
        await tarefa
        return atraso_ms

    atraso = asyncio.run(cenario())
    assert atraso < 300, f"loop preso na inferência (timer atrasou {atraso:.0f} ms)"


def test_gpu_serializa_mas_duas_transcricoes_concluem(carrega_servidor, monkeypatch):
    """Fora do loop != paralelo na GPU: o pool tem 1 worker (o Voxtral não é
    thread-safe), então duas chamadas enfileiram e as duas terminam."""
    mod = carrega_servidor("voxtral_server.py", chave="k1")
    monkeypatch.setattr(mod, "MIN_SPEECH", 0.0)
    monkeypatch.setattr(mod, "_load16", lambda _p: object())
    monkeypatch.setattr(mod, "_speech_stats", lambda _w: (9.0, 0.9, 9.0))
    monkeypatch.setattr(mod, "_compression_ratio", lambda _t: 1.0)
    dentro = {"agora": 0, "pico": 0}

    def _run_contando(_wav, _lang):
        dentro["agora"] += 1
        dentro["pico"] = max(dentro["pico"], dentro["agora"])
        time.sleep(0.2)
        dentro["agora"] -= 1
        return "ok", -0.1

    monkeypatch.setattr(mod, "_run", _run_contando)
    cliente = _client(mod)
    resposta = []

    def chama():
        resposta.append(cliente.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"RIFF", "audio/wav")}, data={"language": "pt"}))

    threads = [threading.Thread(target=chama) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert [r.status_code for r in resposta] == [200, 200]
    assert dentro["pico"] == 1, "duas inferências ao mesmo tempo na GPU"


def test_translations_nao_roda_inferencia_no_loop(carrega_servidor, monkeypatch):
    mod = carrega_servidor("voxtral_server.py", chave="k1")
    monkeypatch.setattr(mod, "_load16", lambda _p: object())
    monkeypatch.setattr(mod, "_run", lambda _w, _l: ("en texto", -0.1))
    cliente = _client(mod)
    chamou_em = {}

    def espia(*a, **k):
        chamou_em["thread"] = threading.current_thread().name
        return "en texto", -0.1

    monkeypatch.setattr(mod, "_run", espia)
    r = cliente.post("/v1/audio/translations",
                     files={"file": ("a.wav", b"RIFF", "audio/wav")})
    assert r.status_code == 200
    assert "gpu" in chamou_em["thread"], chamou_em


# ------------------------------------------------- 2) Whisper desligado = 503

def test_sem_engine_de_asr_e_503_nao_500(carrega_servidor, monkeypatch):
    mod = carrega_servidor("omni_server.py", chave="k1")
    monkeypatch.setattr(mod, "asr", None)
    monkeypatch.setattr(mod, "asr_turbo", None)
    r = _client(mod).post("/v1/audio/transcriptions",
                          files={"file": ("a.wav", b"RIFF", "audio/wav")},
                          data={"language": "pt"})
    assert r.status_code == 503
    assert "OMNI_LOAD_WHISPER=0" in r.json()["detail"]


def test_health_diz_que_o_asr_esta_desligado(carrega_servidor, monkeypatch):
    """`asr_model` fixo em "large-v3" mentia com o Whisper desligado."""
    mod = carrega_servidor("omni_server.py", chave="k1")
    monkeypatch.setattr(mod, "asr", None)
    corpo = _client(mod).get("/health").json()
    assert corpo["asr"] is None and corpo["asr_model"] is None


def test_health_normal_publica_o_asr(carrega_servidor):
    mod = carrega_servidor("omni_server.py", chave="k1")
    corpo = _client(mod).get("/health").json()
    assert corpo["asr"] == "large-v3"


# ------------------------------------------------- 3) pool separado por tradutor

def test_7b_nao_espera_o_14b(monkeypatch):
    """Com um pool só, o "rápido" enfileirava atrás do lento — o nome mentia.
    Aqui o 14B segura o pool dele e o 7B conclui antes."""
    import asyncio
    import importlib.util

    import test_remote_auth_policy as base

    carregados: list = []
    base._instala_stubs(monkeypatch, carregados)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)
    monkeypatch.syspath_prepend(str(BASE / "remote"))
    monkeypatch.setenv("OMNI_API_KEY", "k1")
    spec = importlib.util.spec_from_file_location("omni_pool", BASE / "remote" / "omni_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["omni_pool"] = mod
    spec.loader.exec_module(mod)

    assert mod.MT_POOL is not mod.MT_FAST_POOL, "os dois tradutores dividem o mesmo pool"
    assert mod.MT_POOL._max_workers == 1 and mod.MT_FAST_POOL._max_workers == 1

    # ocupar o 14B e provar que o 7B passa pelo pool DELE
    solta = threading.Event()

    async def exercita():
        preso = mod.MT_POOL.submit(lambda: solta.wait(2))
        fast = await asyncio.get_running_loop().run_in_executor(
            mod.MT_FAST_POOL, lambda: "7b rodou")
        solta.set()
        preso.result(2)
        return fast

    assert asyncio.run(exercita()) == "7b rodou"


# ------------------------------------------------- 4) rev no /health

@pytest.mark.parametrize("arquivo", ["omni_server.py", "voxtral_server.py"])
def test_health_publica_o_rev_do_env(carrega_servidor, monkeypatch, arquivo):
    monkeypatch.setenv("GIT_REV", "abc1234-dirty")
    mod = carrega_servidor(arquivo, chave="k1")
    assert _client(mod).get("/health").json()["rev"] == "abc1234-dirty"


@pytest.mark.parametrize("arquivo", ["omni_server.py", "voxtral_server.py"])
def test_rev_sem_env_vem_do_arquivo_ao_lado_do_server(carrega_servidor, monkeypatch, tmp_path, arquivo):
    """O deploy grava `.rev` ao lado do `server.py` — o servidor lê dali no boot."""
    monkeypatch.delenv("GIT_REV", raising=False)
    mod = carrega_servidor(arquivo, chave="k1")
    alvo = tmp_path / ".rev"
    alvo.write_text("deadbee\n")
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "server.py"))
    assert _client(mod).get("/health").json()["rev"] == "deadbee"
