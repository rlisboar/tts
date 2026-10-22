"""GATE QA #N (`task_c6935f21`): pontuação do corte preservada e pausa por pontuação.

Antes: todo corte por vírgula/dois-pontos recebia um ponto (contorno de fim de
frase no meio da frase) e o vão entre trechos era fixo em 550 ms, com 300 ms de
cauda no último.

Aqui, no WAV: nenhum `:.`/`,.` produzido, vão curto onde havia vírgula, vão longo
em fim de frase, cauda final curta, e — o que separa prosódia de mutilação — o
texto falado não perde palavra no corte.
"""

import numpy as np
import soundfile as sf

pytest_plugins = ["test_api"]

import app  # noqa: E402
from common import pontua_corte, sanitize_text, split_text  # noqa: E402
from test_app import _job_de_render  # noqa: E402

__all__ = ["app"]

PARAGRAFO = ("Bom dia a todos. Hoje a gente vai falar de três coisas, na ordem: "
             "primeiro o cronograma, depois os custos, e por último as dúvidas de vocês.")
LIM_VAO_CURTO = int(0.25 * 24000) + 5
LIM_VAO_LONGO = int(0.50 * 24000) + 5
LIM_CAUDA = int(0.10 * 24000) + 5


def _gerador(model, texto, *a, **k):
    """2 s de fala com 0,3 s de silêncio no fim: é a sobra que o trim corta."""
    t = np.arange(int(2.0 * 24000)) / 24000.0
    env = np.hanning(t.size)
    x = (0.2 * env * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    return np.concatenate([x, np.zeros(7200, dtype=np.float32)])


def _render(monkeypatch, tmp_path):
    monkeypatch.setitem(app._settings, "chunk_max_chars", 60)
    jid = _job_de_render(monkeypatch, tmp_path, _gerador, texto=PARAGRAFO)
    app._run_tts_job(jid, PARAGRAFO, "voz-x", tmp_path / "voz-x.wav",
                     "pt", {"num_steps": 4}, use_queue=False)
    job = app._jobs.pop(jid)
    assert job["status"] == "done", job.get("error")
    return jid, job


def _vaos_e_cauda(wav: np.ndarray):
    quieto = np.abs(wav) <= 1e-4
    vaos, i = [], 0
    while i < quieto.size:
        if quieto[i]:
            j = i
            while j < quieto.size and quieto[j]:
                j += 1
            if i > 0 and j < quieto.size and (j - i) >= int(0.05 * 24000):
                vaos.append(j - i)
            i = j
        else:
            i += 1
    cauda = 0
    for v in wav[::-1]:
        if abs(float(v)) > 1e-4:
            break
        cauda += 1
    return vaos, cauda


def test_pontuacao_do_corte_preservada():
    chunks = split_text(sanitize_text(PARAGRAFO), max_chars=60)
    finais = [pontua_corte(c, ultimo=(i == len(chunks) - 1))
              for i, c in enumerate(chunks)]
    assert all(not f.endswith(":.") and not f.endswith(",.") for f in finais), finais
    assert finais[1].endswith(":") and finais[2].endswith(",") and finais[3].endswith(".")


def test_pausa_do_wav_segue_a_pontuacao(monkeypatch, tmp_path):
    jid, job = _render(monkeypatch, tmp_path)
    wav, _ = sf.read(str(tmp_path / f"{job['output']['id']}.wav"), dtype="float32")
    vaos, cauda = _vaos_e_cauda(wav)
    assert vaos, "não achou vão nenhum"
    assert min(vaos) <= LIM_VAO_CURTO, f"vão de vírgula longo demais: {vaos}"
    assert max(vaos) <= LIM_VAO_LONGO, f"vão de fim de frase longo demais: {vaos}"
    assert cauda <= LIM_CAUDA, f"cauda final {cauda / 24000 * 1000:.0f} ms"
    assert not np.any(np.isnan(wav))


def test_nenhuma_palavra_se_perde_no_corte():
    """Corte não pode comer palavra: a soma dos trechos tem o texto inteiro."""
    chunks = split_text(sanitize_text(PARAGRAFO), max_chars=60)
    falado = " ".join(pontua_corte(c, ultimo=(i == len(chunks) - 1))
                      for i, c in enumerate(chunks))
    palavras = [p for p in sanitize_text(PARAGRAFO).lower().replace(".", " ").split() if p]
    for p in palavras:
        assert p in falado.lower(), f"palavra sumiu no corte: {p}"
