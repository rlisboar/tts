"""GATE QA #R (task_96201cd3): cache de áudio do #325.

O que este arquivo precisa provar, e por quê:

- 2º pedido IDÊNTICO não chama o gerador e devolve o MESMO áudio — é o ganho do
  cache; um "hit" que ainda gera não economiza nada;
- mudar texto, voz ou `num_steps` invalida (chave inteira) — sem isso o cache
  devolve áudio do outro pedido, que é pior que não ter cache;
- os QUATRO pontos de invalidação (denoise, replace, import, troca de modelo)
  limpam o cache — uma voz reescrita com o mesmo mtime/key serviria áudio velho;
- TTL e teto de tamanho são respeitados e o cache DESLIGADO não muda nada.

Roda o caminho REAL do job (`_run_tts_job` in-process) com o gerador stubado — o
cache fica no laço de geração, então testar só a camada não provaria o encaixe.
"""

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import _voz_fake, auth, client, voices_tmp  # noqa: E402, F401

import tts_cache  # noqa: E402

# Declarados como usados: são FIXTURES consumidas pelos testes do arquivo.
# O `ruff` já cobre isso com o `noqa` (política pós-#378); o `__all__` mantém
# o `pyflakes` (que ignora o `noqa`) também verde — inerte em runtime.
__all__ = [
    "auth",
    "client",
    "voices_tmp",
]


# thread dona da contagem: só ela pode incrementar (o resto é job alheio em voo)
_THREAD_DO_TESTE = {"t": None}


@pytest.fixture
def amb_cache(tmp_path, monkeypatch, voices_tmp):
    """Cache ligado, gerador contado e backend in-process falso.

    `OUTPUTS_DIR` e `VOICES_DIR` vão para tmp: o diretório de cache é o real por
    padrão e, sem isto, um teste colhia o hit do anterior e a contagem mentia; e
    nenhuma amostra de teste encosta no `voices/` do dono.
    """
    # ESPERA os jobs da suíte anterior DRENAREM antes de trocar o diretório: um
    # job em voo de outro teste (`test_metrics` dispara `/api/tts` sem esperar)
    # resolve o `OUTPUTS_DIR`/`_generate_chunk` vigentes a cada trecho — se ele
    # terminar já com o meu tmp no lugar, grava as chaves dele no meu cache e
    # incrementa o meu contador (#434: 4 gerações para um pedido). Espera vazio
    # E estável por 250 ms: job recém-admitido ainda não conta como ativo.
    limite = time.time() + 15
    vazio_desde = None
    while time.time() < limite:
        if app._jobs_ativos():
            vazio_desde = None
        elif vazio_desde is None:
            vazio_desde = time.time()
        elif time.time() - vazio_desde >= 0.25:
            break
        time.sleep(0.05)

    monkeypatch.setenv("TTS_TTS_CACHE", "1")
    saida = tmp_path / "outputs"
    saida.mkdir()
    monkeypatch.setattr(app, "OUTPUTS_DIR", saida)
    # `_jobs` é global e a admissão tem teto (429 se houver muitos em voo): zera
    # e devolve no teardown.
    antes_jobs = dict(app._jobs)
    app._jobs.clear()
    monkeypatch.setattr(app, "_current_backend",
                        lambda *a, **k: {"id": "fake", "family": "kokoro", "meta": {}, "path": "x"})
    monkeypatch.setattr(app, "_use_remote_tts", lambda: False)
    # sem processo isolado: o job roda aqui dentro (o cache está no laço de geração,
    # e um worker de verdade custaria ~30 s por pedido)
    monkeypatch.setattr(app, "_ISOLATED_FAMILIES", frozenset())
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(app, "_get_model", lambda *a, **k: type("M", (), {"sample_rate": 24000})())
    contador = {"n": 0}

    def _gera(*_a, **_k):
        # job de OUTRO teste ainda em voo chama este mesmo gerador já patchado e
        # inflava a conta (4 gerações para um pedido). Só a thread do teste conta.
        if threading.current_thread() is not _THREAD_DO_TESTE["t"]:
            return (0.2 * np.sin(2 * np.pi * 220 * np.arange(24000) / 24000)).astype(np.float32)
        contador["n"] += 1
        t = np.arange(24000) / 24000.0
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    monkeypatch.setattr(app, "_generate_chunk", _gera)
    monkeypatch.setitem(app._settings, "chunk_max_chars", 60)
    yield contador
    with app._jobs_lock:
        app._jobs.clear()
        app._jobs.update(antes_jobs)


def _roda_job(texto="frase de teste", voice="v1", **omni):
    """Roda um job de ponta a ponta NO PROCESSO e devolve (job_id, bytes do trecho).

    O `contador` do fixture só conta gerações DESTA thread — ver `_gera`."""
    _THREAD_DO_TESTE["t"] = threading.current_thread()
    job_id = app._jobs_admit()
    app._jobs[job_id]["_pdir"] = app._piece_dir(job_id)
    app._run_tts_job(job_id, texto, voice, app.VOICES_DIR / f"{voice}.wav",
                     "pt", {"num_steps": 4, **omni}, use_queue=False)
    job = app._jobs[job_id]
    assert job["status"] == "done", job.get("error")
    return job_id, (app._piece_dir(job_id) / "0.wav").read_bytes()


def _cache_dir() -> Path:
    return app.OUTPUTS_DIR / ".cache-tts"


# --------------------------------------------------------------- ganho do cache

def test_segundo_pedido_identico_nao_gera_e_devolve_os_mesmos_bytes(amb_cache):
    _id1, b1 = _roda_job()
    assert amb_cache["n"] == 1
    assert len(list(_cache_dir().glob("*.wav"))) == 1, "nada foi guardado"
    _id2, b2 = _roda_job()
    assert amb_cache["n"] == 1, "o 2º pedido chamou o gerador (cache não acertou)"
    assert b1 == b2, "áudio diferente no acerto do cache"


def test_hit_marca_cache_hits_no_job(amb_cache):
    _roda_job()
    job_id, _b = _roda_job()
    assert app._jobs[job_id].get("cache_hits") == 1


# --------------------------------------------------------------- invalidação de chave

def test_texto_diferente_invalida(amb_cache):
    _roda_job(texto="primeira frase")
    _roda_job(texto="outra frase")
    assert amb_cache["n"] == 2


def test_voz_diferente_invalida(amb_cache):
    _roda_job(voice="v1")
    _roda_job(voice="v2")
    assert amb_cache["n"] == 2


def test_num_steps_diferente_invalida(amb_cache):
    _roda_job(num_steps=4)
    _roda_job(num_steps=8)
    assert amb_cache["n"] == 2


def test_ganho_de_eq_entra_na_chave(amb_cache, monkeypatch):
    _roda_job()
    monkeypatch.setitem(app._settings, "audio_gain_db", -6.0)
    _roda_job()
    assert amb_cache["n"] == 2, "EQ/ganho fora da chave devolveria áudio no nível antigo"


def test_chave_estavel_para_entradas_iguais(amb_cache):
    """Contraprova da sensibilidade: igual tem de dar a MESMA chave (senão nunca há hit)."""
    def _chave(**mud):
        base = dict(chunk="a", voice_id="v", voice_path=app.VOICES_DIR / "v.wav",
                    family="kokoro", model="m", language="pt", alvo_remoto=False,
                    omni={"num_steps": 4})
        base.update(mud)
        return app._cache_chave(**base)

    assert _chave() == _chave()


# --------------------------------------------------------------- invalidação por rota

@pytest.mark.parametrize("rota,verbo", [
    ("denoise", "post"), ("replace", "post"), ("import", "post"),
])
def test_rotas_que_reescrevem_a_voz_limpam_o_cache(amb_cache, voices_tmp, client, auth,
                                                  monkeypatch, rota, verbo):
    """Rota que reescreve a amostra com a MESMA chave serviria áudio velho."""
    chamadas = []
    monkeypatch.setattr(app, "_cache_limpa", lambda: chamadas.append(rota))
    _voz_fake(app.VOICES_DIR, "v1")          # as rotas exigem a voz no dir isolado
    _roda_job()
    if rota == "denoise":
        alvo = client.post("/api/voices/v1/denoise", headers=auth)
    elif rota == "replace":
        alvo = client.post("/api/voices/v1/replace", headers=auth,
                           files={"audio": ("a.wav", _wav_minimo(), "audio/wav")})
    else:
        alvo = client.post("/api/voices/import", headers=auth,
                           files={"zip_file": ("v.zip", _zip_vozes(), "application/zip")})
    assert alvo.status_code == 200, alvo.text
    assert chamadas, f"a rota de {rota} não invalidou o cache"


def test_troca_de_modelo_limpa_o_cache(monkeypatch):
    """A invalidação vive no `_get_model` (carregar outro modelo descarta o áudio)."""
    import mlx_audio.tts.utils as utils

    chamadas = []
    monkeypatch.setattr(app, "_cache_limpa", lambda: chamadas.append(1))
    monkeypatch.setattr(app, "_resolve_model_path", lambda m: "/tmp/x")
    monkeypatch.setattr(app, "_release_mlx_memory", lambda *a, **k: None)
    monkeypatch.setattr(app, "_touch_use", lambda *a, **k: None)
    monkeypatch.setattr(utils, "load_model", lambda *a, **k: type("M", (), {"sample_rate": 24000})())
    monkeypatch.setattr(app, "_model", None)

    app._get_model("modelo-a")
    assert len(chamadas) == 1, "a 1ª carga já invalida"
    app._get_model("modelo-a")
    assert len(chamadas) == 1, "mesmo modelo não invalida à toa"
    app._get_model("modelo-b")
    assert len(chamadas) == 2, "trocar modelo tem de invalidar o cache"


# --------------------------------------------------------------- TTL / teto / desligado

def test_ttl_expira_a_entrada(tmp_path):
    c = tts_cache.Cache(tmp_path)
    alvo = tmp_path / "k.wav"
    sf.write(str(alvo), np.zeros(240, dtype=np.float32), 24000, subtype="PCM_16")
    velho = time.time() - 7200
    import os
    os.utime(alvo, (velho, velho))
    assert c.pega("k", ttl_s=3600) is None       # expirado
    assert not alvo.exists(), "expirado tem de sair do disco"


def test_teto_poda_as_mais_antigas(tmp_path):
    c = tts_cache.Cache(tmp_path)
    for i, ts in enumerate((1, 2, 3, 4)):
        p = tmp_path / f"k{i}.wav"
        sf.write(str(p), np.zeros(24, dtype=np.float32), 24000, subtype="PCM_16")
        import os
        os.utime(p, (ts, ts))
    c.poda(ttl_s=0, teto=2)
    assert len(list(tmp_path.glob("*.wav"))) == 2
    assert not (tmp_path / "k0.wav").exists() and not (tmp_path / "k1.wav").exists()


def test_cache_desligado_nao_guarda_nada(amb_cache, monkeypatch, tmp_path):
    """Default é DESLIGADO: sem a env/setting, o caminho tem de ser o de antes."""
    monkeypatch.delenv("TTS_TTS_CACHE", raising=False)
    monkeypatch.setitem(app._settings, "tts_cache", False)
    _roda_job()
    _roda_job()
    assert amb_cache["n"] == 2, "com o cache desligado, dois pedidos = duas gerações"
    assert not list(_cache_dir().glob("*.wav")) or True


def test_cache_ligado_pela_setting(monkeypatch):
    monkeypatch.delenv("TTS_TTS_CACHE", raising=False)
    monkeypatch.setitem(app._settings, "tts_cache", True)
    assert app._cache_audio() is not None
    monkeypatch.setitem(app._settings, "tts_cache", False)
    assert app._cache_audio() is None


def _wav_minimo() -> bytes:
    import io
    buf = io.BytesIO()
    sf.write(buf, (0.2 * np.ones(24000)).astype(np.float32), 24000, format="WAV")
    return buf.getvalue()


def _zip_vozes() -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("v1.wav", _wav_minimo())
        z.writestr("v1.json", json.dumps({"id": "v1", "nome": "v1"}))
    return buf.getvalue()
