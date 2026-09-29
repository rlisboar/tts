"""Serialização do Metal: turno do Live × geração da UI NÃO podem gerar juntos.

Contexto (smoke do dono): com o Live e a UI gerando ao mesmo tempo, o processo do
servidor MORREU com `Command buffer execution failed: GPU Timeout Error` (exit
134). O app já serializa a geração local no `_gen_lock` (o job da UI o segura
desde sempre); o que este teste fixa é que o caminho do LIVE entra no MESMO lock
(`live_pipeline._tts_app`: `trava = app._NO_LOCK if app._use_remote_tts() else app._gen_lock`).

Instrumento: um dublê do `app._generate_chunk` (a folha que de fato toca o Metal)
que dorme, conta entradas/saídas e detecta SOBREPOSIÇÃO. Sem MLX carregado.

O que cada caso cobre:
- Live × Live (duas sessões em paralelo) — o caso medido no gate do #102;
- Live × job da UI, NOS DOIS sentidos (quem começa primeiro não importa);
- contraprova: com TTS remoto o Live não usa o lock local (por desenho) e a
  sobreposição aparece — provando que o silêncio dos outros casos é mérito do lock.

Contraprova por fora (rodei: 3 dos 4 testes falham): copie a árvore e troque as
DUAS linhas `trava = app._NO_LOCK if app._use_remote_tts() else app._gen_lock` de
`live_pipeline.py` (521 e 677 — prewarm e turno) por `trava = app._NO_LOCK`.
Trocar só a primeira não basta: o caminho do turno continua travado.
"""

from __future__ import annotations

import threading
import time

import pytest

import app
import live_pipeline as lp


class _GeradorInstrumentado:
    """Dublê de `app._generate_chunk`: alarga a seção crítica e mede o paralelismo."""

    def __init__(self, dorme: float = 0.40) -> None:
        self._trava = threading.Lock()
        self.dorme = dorme
        self.chamadas = 0
        self.dentro = 0
        self.max_simultaneas = 0
        self.sobrepostas = 0

    def __call__(self, *a, **k):
        with self._trava:
            self.chamadas += 1
            self.dentro += 1
            self.max_simultaneas = max(self.max_simultaneas, self.dentro)
            if self.dentro > 1:
                self.sobrepostas += 1
        try:
            time.sleep(self.dorme)          # alarga a janela: falta de lock aparece
            return "audio"
        finally:
            with self._trava:
                self.dentro -= 1


class _ModeloFake:
    sample_rate = 24000


@pytest.fixture
def gerador(monkeypatch, tmp_path):
    """Troca a folha do Metal por um dublê e deixa `_tts_app` rodar sem MLX."""
    g = _GeradorInstrumentado()
    monkeypatch.setattr(app, "_generate_chunk", g)
    monkeypatch.setattr(app, "_get_model", lambda *a, **k: _ModeloFake())
    monkeypatch.setattr(app, "_current_backend",
                        lambda *a, **k: {"id": "fake", "family": "omnivoice", "meta": {}})
    monkeypatch.setattr(app, "VOICES_DIR", tmp_path)      # sem wav → sem clone prompt
    monkeypatch.setattr(app, "_use_remote_tts", lambda: False)
    return g


def _em_threads(*funcoes):
    """Dispara as funções no MESMO instante (barreira de partida).

    Sem a partida comum, o fio que faz preparação (o Live chama `_get_model` antes
    de entrar na seção crítica) podia entrar depois de o outro já ter saído — e aí
    o teste passava mesmo SEM lock (verificado na cópia de contraprova)."""
    partida = threading.Event()

    def envolver(f):
        partida.wait(5)
        f()

    threads = [threading.Thread(target=envolver, args=(f,)) for f in funcoes]
    for t in threads:
        t.start()
    time.sleep(0.05)                 # todos parados na barreira
    partida.set()
    for t in threads:
        t.join(15)


def _job_da_ui():
    """O miolo do job da UI: pega o MESMO `_gen_lock` e chama a folha (app.py:3591)."""
    with app._gen_lock:
        app._generate_chunk(None, "job da UI", "pt", None, None, {}, family="omnivoice",
                            meta={}, sr=24000)


def test_dois_turnos_live_nao_sobrepoem_a_geracao(gerador):
    """Duas sessões do Live sintetizando juntas: uma geração por vez."""
    _em_threads(lambda: lp._tts_app("primeiro turno", {}, "voz-a"),
                lambda: lp._tts_app("segundo turno", {}, "voz-b"))
    assert gerador.chamadas == 2, "as duas gerações precisam ter acontecido"
    assert gerador.max_simultaneas == 1, "houve geração simultânea (Metal em risco)"
    assert gerador.sobrepostas == 0


@pytest.mark.parametrize("primeiro", ["live", "job"])
def test_turno_live_e_job_da_ui_nao_sobrepoem(gerador, primeiro):
    """Live × geração da UI nos DOIS sentidos (quem abre a seção crítica é indiferente)."""
    live = lambda: lp._tts_app("turno do live", {}, "voz")
    if primeiro == "live":
        _em_threads(live, _job_da_ui)
    else:
        _em_threads(_job_da_ui, live)
    assert gerador.chamadas == 2
    assert gerador.max_simultaneas == 1, f"sobrepôs (primeiro={primeiro})"
    assert gerador.sobrepostas == 0


def test_contraprova_remoto_nao_usa_o_lock_local(monkeypatch, gerador):
    """Com TTS remoto o Live NÃO toma o lock local — e a sobreposição reaparece.

    É o que separa 'os casos acima passam' de 'os casos acima passam POR CAUSA do lock'."""
    monkeypatch.setattr(app, "_use_remote_tts", lambda: True)
    _em_threads(lambda: lp._tts_app("turno remoto", {}, "voz"), _job_da_ui)
    assert gerador.chamadas == 2
    assert gerador.sobrepostas >= 1, "sem lock a sobreposição tinha de aparecer"