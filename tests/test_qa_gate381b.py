"""GATE QA #G (`task_553e0e3e`): ganho ÚNICO no job — envelope e loudness.

Mede com a amostra da auditoria (#284, item 1): quatro trechos com os níveis crus
de lá, um deles de crista alta. O que tem de valer:

- espalhamento de RMS entre trechos ≤ 2 dB e o trecho de crista alta dentro de
  ±1 dB do alvo (antes ele era normalizado pelo PICO e saía o mais baixo);
- o cumprimento curto não pode ser o mais baixo dos quatro;
- loudness integrada do WAV final em −18 LUFS ±1 (medida com `ffmpeg ebur128`);
- caminho remoto e in-process com o MESMO ganho;
- `_fade_edges` e o limiter seguem atuando: bordas em zero e pico ≤ 1,0;
- mordida: voltar ao ganho acoplado ao pico reprova tudo isso.
"""

import math

import numpy as np
import soundfile as sf

pytest_plugins = ["test_api"]

import app  # noqa: E402
from common import LUFS_ALVO, loudness_lufs  # noqa: E402
from test_app import _job_de_render  # noqa: E402

__all__ = ["app"]

PARAGRAFO = ("Bom dia a todos. Hoje a gente vai falar de três coisas, na ordem: "
             "primeiro o cronograma, depois os custos, e por último as dúvidas de vocês.")
# níveis crus medidos na auditoria, do cumprimento ao fim
NIVEIS = (0.0532, 0.1267, 0.1059, 0.0970)


def _gerador(crista: bool = True):
    def _gera(model, texto, *a, **k):
        nivel = NIVEIS[getattr(_gera, "i", 0) % len(NIVEIS)]
        _gera.i = getattr(_gera, "i", 0) + 1
        t = np.arange(int(2.5 * 24000)) / 24000.0
        env = np.repeat(np.hanning(2400), 25)[:t.size]
        env = np.pad(env, (0, t.size - env.size), mode="edge")
        x = (nivel * env * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
        if crista:
            # transiente de amplitude ABSOLUTA fixa: a crista de cada trecho varia
            # com o nível dele — é assim que a cadeia velha castigava o mais baixo
            x[:60] *= 0.5 / max(1e-9, float(np.abs(x[:60]).max()))
        return x

    _gera.i = 0
    return _gera


def _roda(monkeypatch, tmp_path, *, remoto=False, crista=True):
    monkeypatch.setitem(app._settings, "chunk_max_chars", 60)
    jid = _job_de_render(monkeypatch, tmp_path, _gerador(crista),
                         texto=PARAGRAFO, remoto=remoto)
    app._run_tts_job(jid, PARAGRAFO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs.pop(jid)
    assert job["status"] == "done", job.get("error")
    return jid, job


def _rms_pecas(jid) -> list[float]:
    valores = []
    for i in range(4):
        y, _ = sf.read(str(app._piece_dir(jid) / f"{i}.wav"), dtype="float32")
        valores.append(float(np.sqrt(np.mean(y ** 2))))
    return valores


def test_trechos_no_mesmo_nivel_e_cumprimento_nao_e_o_mais_baixo(monkeypatch, tmp_path):
    jid, _job = _roda(monkeypatch, tmp_path)
    rms = _rms_pecas(jid)
    assert len(rms) == 4
    espalhamento = 20 * math.log10(max(rms) / min(rms))
    assert espalhamento <= 2.0, f"espalhamento {espalhamento:.1f} dB em {rms}"
    alvo = 0.15
    for r in rms:
        assert abs(20 * math.log10(r / alvo)) <= 1.0, f"trecho fora de ±1 dB: {r:.4f}"
    # o cumprimento (1º) não pode DESABAR: na cadeia velha ele saía 3,7 dB abaixo
    assert 20 * math.log10(rms[0] / max(rms)) >= -1.0, rms


def test_loudness_final_no_alvo(monkeypatch, tmp_path):
    _jid, job = _roda(monkeypatch, tmp_path)
    final = tmp_path / f"{job['output']['id']}.wav"
    lufs = loudness_lufs(final)
    assert lufs is not None and abs(lufs - LUFS_ALVO) <= 1.0, lufs
    assert isinstance(job["output"]["lufs_ganho_db"], float)


def test_bordas_rampeadas_e_pico_limitado(monkeypatch, tmp_path):
    jid, _job = _roda(monkeypatch, tmp_path)
    for i in range(4):
        y, _ = sf.read(str(app._piece_dir(jid) / f"{i}.wav"), dtype="float32")
        assert abs(float(y[0])) < 1e-6 and abs(float(y[-1])) < 1e-6, f"borda crua no trecho {i}"
        assert float(np.abs(y).max()) <= 1.0 + 1e-6


def test_remoto_e_in_process_com_o_mesmo_ganho(monkeypatch, tmp_path):
    """Mesmo conteúdo pelos dois motores: o ganho do job não pode depender dele."""
    jid_local, _ = _roda(monkeypatch, tmp_path, remoto=False)
    jid_remoto, _ = _roda(monkeypatch, tmp_path, remoto=True)
    r_local = _rms_pecas(jid_local)
    r_remoto = _rms_pecas(jid_remoto)
    dif_db = 20 * math.log10(float(np.mean(r_local)) / float(np.mean(r_remoto)))
    assert abs(dif_db) <= 1.0, (round(dif_db, 2), r_local, r_remoto)
