"""DSH-2: o Live pelo backend dsh (harness ACP), com cliente falso.

O que o alvo depende e é provado aqui: chunk de token → TTS (mesmo caminho do
turno), `cancel(turno_id)` no barge-in com descarte de chunk tardio ANTES do
chunker, `prewarm` no connect (boot de 17-18 s fora do turno), erro do dsh virando
`error{pipeline}` com a sessão VIVA, e o teto de contexto reabrindo sessão nova com
o contexto renderizado. Sem `dsh` o pipeline fica byte a byte como era (o caminho
openai é coberto por tests/test_live_pipeline.py).

O cliente `DshClient` de verdade tem os testes dele em tests/test_dsh_client.py
(fake ACP); aqui o dublê prova só o CONSUMO do contrato pelo pipeline.
"""

import queue
import threading
import time

import numpy as np
import pytest

import app
import live_pipeline as lp
from test_api import dsh_fake_bin, dsh_limpo  # fixtures de test_api (fora do conftest)

# os imports de fixture são usados só como PARÂMETRO de teste, uso que o pyflakes
# não enxerga (ele ignora `noqa`): `__all__` marca como usados e mata F401/F811.
__all__ = ["dsh_fake_bin", "dsh_limpo"]


# ---------------------------------------------------------------------------
# Dublês
# ---------------------------------------------------------------------------

class Fala:
    def __init__(self):
        self.eventos = []
        self.audios = []

    def json(self, d):
        self.eventos.append(dict(d))

    def audio(self, b):
        self.audios.append(b)

    def tipos(self):
        return [e["type"] for e in self.eventos]

    def texto(self):
        return "".join(e.get("delta", "") for e in self.eventos
                       if e["type"] == "assistant_text")


class DshFalso:
    """Cliente dsh mínimo: registra chamadas e serve deltas roteirizados.

    `erros` é consumido por turno (None = ok): permite provar "o erro do turno 1
    não mata a sessão, o turno 2 responde"."""

    def __init__(self, deltas=("Bom ", "dia! "), erros=None):
        self.chamadas = []            # (msgs, turno_id) na ordem — msgs str OU list
        self.cancelados = []
        self.prewarm_n = 0
        self.fechado = False
        self.deltas = deltas
        self.erros = list(erros) if erros else []
        self.pipe = None              # gancho p/ o teste disparar barge-in
        self._trava = threading.Lock()

    # contrato consumido pelo pipeline
    def prewarm(self):
        self.prewarm_n += 1

    def close(self):
        self.fechado = True

    def cancel(self, turno_id):
        self.cancelados.append(turno_id)

    def stream(self, msgs, turno_id):
        with self._trava:
            self.chamadas.append((msgs, turno_id))
        erro = self.erros.pop(0) if self.erros else None
        if erro is not None:
            raise erro
        if self.pipe is not None and self.pipe.cancelado:
            return                            # turno já cancelado antes do 1º token
        for d in self.deltas:
            yield d


def _pipeline(fala, dsh, *, texto="Bom dia", deltas=("bom dia",), tts_s=0.0,
              llm=None, **kw):
    """Pipeline com dublês rápidos e o cliente dsh falso já plugado."""
    def stt(pcm, language):
        return texto

    def tts(chunk, omni):
        if tts_s:
            time.sleep(tts_s)
        return np.zeros(240, dtype=np.float32)

    dsh.deltas = deltas
    dsh.pipe = None
    p = lp.LivePipeline(fala.json, fala.audio, stt=stt, llm=llm, tts=tts,
                        prewarm=lambda **_: None, dsh=dsh, **kw)
    dsh.pipe = p
    p.push_pcm(b"\x00\x00" * 1600)
    return p


def _fala_nova():
    return b"\x00\x00" * 1600


# ---------------------------------------------------------------------------
# Modo sessão: contexto só no 1º turno, depois só o texto novo
# ---------------------------------------------------------------------------

def test_primeiro_turno_manda_contexto_e_os_seguintes_so_o_texto():
    fala, dsh = Fala(), DshFalso()
    p = _pipeline(fala, dsh, system="Você é o TTS-STUDIO.",
                  history=[{"role": "user", "content": "combinamos ontem"}])

    p.end_of_speech(inicia_thread=False)

    ctx, turno_id = dsh.chamadas[0]
    assert isinstance(ctx, list), "abrir sessão ACP exige o contexto renderizado"
    assert "1 a 3 frases curtas" in ctx[0]["content"], \
        "PERSONA obrigatória no 1º prompt (decisão do PM: fala curta)"
    assert "Você é o TTS-STUDIO." in ctx[0]["content"], "system da sessão vem junto"
    assert ctx[-1]["content"] == "Bom dia"
    assert any("combinamos ontem" in m.get("content", "") for m in ctx)
    assert turno_id.startswith("lt"), "identidade de turno vai para o harness"

    p.push_pcm(_fala_nova())
    p.end_of_speech(inicia_thread=False)

    assert dsh.chamadas[1][0] == "Bom dia", \
        "turno seguinte manda SÓ o texto novo (o contexto vive no harness)"
    assert fala.tipos().count("turn_complete") == 2


def test_tokens_viram_audio_por_chunk_na_ordem():
    fala, dsh = Fala(), DshFalso()
    p = _pipeline(fala, dsh, deltas=("Primeira frase. ", "Segunda frase. "))

    p.end_of_speech(inicia_thread=False)

    assert fala.texto() == "Primeira frase. Segunda frase. "
    assert len(fala.audios) == 2, "um áudio por chunk de sentença"


# ---------------------------------------------------------------------------
# Barge-in: cancel(turno_id) + descarte do chunk tardio
# ---------------------------------------------------------------------------

def test_barge_in_cancela_o_turno_no_harness_e_corta_o_chunk_tardio():
    """Cliente que IGNORA o cancel e continua entregando token: o chunk tardio não
    pode virar áudio do turno nem entrar no `assistant_text`. O stream falso fica
    PRESO até o cancel (senão o turno terminaria antes do barge-in e o teste
    dependeria de sorte de escala)."""
    fala, dsh = Fala(), DshFalso()
    p = _pipeline(fala, dsh)

    def stream(msgs, turno_id):
        dsh.chamadas.append((msgs, turno_id))
        yield "ja saiu "
        while not p.cancelado:
            time.sleep(0.01)
        yield "tardio"

    dsh.stream = stream
    p.end_of_speech(inicia_thread=True)
    assert _espera(lambda: dsh.chamadas)

    p.cancel()                              # barge-in do handler
    assert _espera(lambda: dsh.cancelados, 2.0)
    assert dsh.cancelados == [dsh.chamadas[0][1]], \
        "o cancel casa por IDENTIDADE do turno em voo"

    assert _espera(lambda: "interrupted" in fala.tipos(), 2.0)
    assert fala.texto() == "ja saiu ", "o chunk tardio não entrou no texto"


def test_delta_de_turno_abandonado_nao_entra_no_chunker():
    """Guarda de identidade do lado do pipeline (defesa em profundidade): se um
    delta escapar mesmo com a checagem de cancel, ele morre ANTES do chunker."""
    fala, dsh = Fala(), DshFalso()
    p = _pipeline(fala, dsh)

    def stream(msgs, turno_id):
        yield "Antes. "
        p._dsh_turno = "outro"              # turno trocou no meio do stream
        yield "Depois."

    dsh.stream = stream
    p.end_of_speech(inicia_thread=False)

    assert fala.texto() == "Antes. ", "delta de turno abandonado ficou fora"


# ---------------------------------------------------------------------------
# Erro do dsh: error{pipeline}, sessão VIVA e turno seguinte funciona
# ---------------------------------------------------------------------------

def test_erro_do_dsh_vira_error_pipeline_sem_matar_a_sessao():
    """Sem fallback explícito (o `llm` do app): o erro do dsh ANTES do 1º token
    ainda entrega o turno (pelo openai) e a sessão segue viva para o turno 2."""
    import dsh_client

    fala, dsh = Fala(), DshFalso(erros=[dsh_client.DshError("processo morreu")])

    def llm_openai(msgs):
        yield "resposta"

    p = _pipeline(fala, dsh, llm=llm_openai)

    p.end_of_speech(inicia_thread=False)
    assert [e for e in fala.eventos if e["type"] == "dsh_indisponivel"]
    assert "error" not in fala.tipos()

    p.push_pcm(_fala_nova())                # a sessão segue viva
    p.end_of_speech(inicia_thread=False)
    assert fala.tipos().count("turn_complete") == 2


# ---------------------------------------------------------------------------
# Ciclo de vida: prewarm no start, close fecha o cliente, teto de contexto
# ---------------------------------------------------------------------------

def test_start_prewarm_e_close_do_cliente_dsh():
    p = _pipeline(Fala(), DshFalso())
    p.start()
    assert p._dsh.prewarm_n == 1, "boot de 17-18 s tem de sair no prewarm"
    p.close()
    assert p._dsh.fechado is True, "cliente por sessão fecha com ela"


def test_teto_de_contexto_reabre_sessao_com_contexto(monkeypatch):
    monkeypatch.setattr(lp, "_DSH_CTX_MAX_CHARS", 5)
    fala, dsh = Fala(), DshFalso()
    p = _pipeline(fala, dsh)

    p.end_of_speech(inicia_thread=False)
    assert isinstance(dsh.chamadas[0][0], list)

    p.push_pcm(_fala_nova())                # estourou o teto no fim do turno
    p.end_of_speech(inicia_thread=False)

    assert isinstance(dsh.chamadas[1][0], list), \
        "passou do teto: o próximo turno reabre a sessão com o contexto"
    assert "1 a 3 frases curtas" in dsh.chamadas[1][0][0]["content"], \
        "sessão reaberta pelo teto também leva a persona"


def test_sem_dsh_o_caminho_openai_nao_muda():
    fala = Fala()
    vistos = []

    def llm(msgs):
        vistos.append(msgs)
        yield "ok "

    p = lp.LivePipeline(fala.json, fala.audio,
                        stt=lambda pcm, lang: "oi", llm=llm,
                        tts=lambda c, o: np.zeros(240, dtype=np.float32),
                        prewarm=lambda **_: None)
    p.push_pcm(_fala_nova())
    p.end_of_speech(inicia_thread=False)

    assert vistos and isinstance(vistos[0], list), "llm injetado segue recebendo msgs"
    assert fala.texto() == "ok "


def test_prewarm_do_dsh_que_falha_marca_indisponivel_e_segue():
    """#146: quem morre de verdade é o handshake, no pre-warm. A sessão tem de
    ficar MARCADA ali mesmo (nada de pagar backoff no turno) e já responder pelo
    openai, sem tentar o dsh de novo."""
    fala, dsh = Fala(), DshFalso()

    def prewarm_ruim():
        raise RuntimeError("dsh fora do ar")

    dsh.prewarm = prewarm_ruim

    def llm_openai(msgs):
        yield "bom dia"

    p = _pipeline(fala, dsh, llm=llm_openai)
    p.start()                               # pre-warm que falha não pode levantar

    assert p._dsh_indisponivel is True
    assert "dsh fora do ar" in p._dsh_motivo, "motivo guardado (vai para o stats)"
    assert [e for e in fala.eventos if e["type"] == "dsh_indisponivel"], "avisa o cliente"
    assert dsh.fechado is True, "processo órfão liberado"

    p.end_of_speech(inicia_thread=False)
    assert fala.texto() == "bom dia", "turno responde pelo openai"
    assert dsh.chamadas == [], "marcado: não volta a tentar o dsh"


def test_stats_ia_reflete_o_fallback(monkeypatch, dsh_limpo):
    """`stats.ia` não pode mentir no painel: com o dsh caído, o backend EFETIVO é
    openai e `fallback` fica true, mesmo com o settings pedindo dsh.

    `dsh_limpo` (#205): o `TTS_CHAT_BACKEND` do teste sozinho não basta — o Live lê
    `TTS_CHAT_BACKEND_LIVE`/`chat_backend_live` ANTES do global, então com o knob
    exportado (ou o dono escolhendo openai na tela) o `pedido` virava openai e este
    teste media outra rota."""
    class PipeFalso:
        _dsh_indisponivel = True
        _dsh_motivo = "DshError: rc=1"

    monkeypatch.setenv("TTS_CHAT_BACKEND", "dsh")
    caiu = app._live_stats_ia({"pipe": PipeFalso()})
    assert caiu == {"pedido": "dsh", "backend": "openai", "fallback": True,
                    "motivo": "DshError: rc=1"}

    class PipeOk:
        _dsh_indisponivel = False
        _dsh_motivo = ""

    assert app._live_stats_ia({"pipe": PipeOk()})["backend"] == "dsh"
    assert app._live_stats_ia({"pipe": PipeOk()})["fallback"] is False


# ---------------------------------------------------------------------------
# Wire no app: cliente POR sessão Live, criado no connect e fechado no fim
# ---------------------------------------------------------------------------

class DshClienteFalso(DshFalso):
    criados = []

    def __init__(self, **cfg):
        super().__init__()
        self.cfg = cfg
        DshClienteFalso.criados.append(self)


@pytest.fixture()
def sessao_min(monkeypatch, dsh_limpo):
    # `dsh_limpo` (#205): sem neutralizar `TTS_CHAT_BACKEND_LIVE`/`chat_backend_live`,
    # o `_live_pipe_novo` abaixo não criava cliente nenhum quando o Live estava em
    # openai — o teste acusava 0 cliente e media a rota errada.
    monkeypatch.setenv("TTS_CHAT_BACKEND", "dsh")
    monkeypatch.setattr(app.dsh_client, "DshClient", DshClienteFalso)
    DshClienteFalso.criados.clear()
    return {"id": "s1", "voice_id": None, "system": None, "history": [],
            "fila": queue.Queue()}


def test_app_cria_cliente_por_sessao_e_fecha_com_ela(sessao_min):
    sess = sessao_min
    pipe = app._live_pipe_novo(sess)

    assert len(DshClienteFalso.criados) == 1, "um cliente por sessão Live"
    cli = DshClienteFalso.criados[0]
    assert sess["dsh"] is cli and pipe._dsh is cli
    # O Live SEGUE o campo do dono — `off` é o DEFAULT, não um valor forçado (a UI
    # diz o mesmo: "o Live usa a MESMA config do dsh da Conversa", e `effort ≠ off`
    # pode estourar o orçamento de latência, com aviso). Cravar "off" aqui deixava
    # a suíte refém do settings.json do dono (#246): com o campo em `low`, o filho
    # hostil do #206 acusava. Comparar com o cfg RESOLVIDO segue pinando o que
    # importa (o cliente nasce do cfg do app, não de um literal) e fica verde em
    # qualquer estado do dono.
    assert cli.cfg["effort"] == app._chat_dsh_cfg()["effort"], \
        "o cliente do Live tem de nascer do cfg resolvido (effort do dono)"

    pipe.close()
    assert cli.fechado is True, "fechar a sessão fecha o processo do dsh"


def _sessao(nome="sx"):
    return {"id": nome, "voice_id": None, "system": None, "history": [],
            "fila": queue.Queue()}


# ---------------------------------------------------------------------------
# #207: `start()` é chamado numa THREAD e o `finally` do WS pode fechar a sessão
# antes (ou no meio) dele. O `_saiu` era escrito e nunca lido: a sessão morta
# subia worker e processo `dsh` NOVOS, com 2 threads leitoras, e ninguém mais
# chamava `close()` — um órfão por conexão curta.
# ---------------------------------------------------------------------------

@pytest.fixture()
def pipe_dsh_real(dsh_limpo, dsh_fake_bin, monkeypatch):
    """Pipeline REAL com o processo do `fake_acp` (o ciclo de vida é o do alvo).

    `_prewarm` falso: carregar os modelos aqui não muda o que se mede e custa
    segundos. O `_dsh.prewarm()` do `start()` continua REAL — é ele que subia o
    processo órfão."""
    monkeypatch.setenv("TTS_CHAT_BACKEND_LIVE", "dsh")
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: False)
    pipe = app._live_pipe_novo(_sessao("orfa"))
    assert pipe._dsh is not None, "o teste precisa do processo de verdade"
    monkeypatch.setattr(pipe, "_prewarm", lambda **kw: None)
    try:
        yield pipe
    finally:
        try:
            pipe.close()
        except Exception:                # noqa: BLE001 — limpeza best-effort
            pass


def test_start_sobe_o_dsh_da_sessao_viva(pipe_dsh_real):
    """Controle: sem `close()` antes, o `start()` segue subindo tudo."""
    pipe = pipe_dsh_real
    pipe.start()
    assert pipe._dsh.alive is True, "sessão viva tem de pre-warmar o dsh"


def test_close_antes_do_start_nao_ressuscita_o_dsh(pipe_dsh_real):
    """O caso do #207: o cliente cai logo após conectar e o `finally` do WS fecha
    a sessão antes de a thread do `_live_pipe_start` entrar no `start()`."""
    pipe = pipe_dsh_real
    pipe.close()
    pipe.start()
    assert pipe._dsh.alive is False, "sessão morta não pode subir processo novo"


def test_close_no_meio_do_prewarm_nao_sobe_o_dsh(pipe_dsh_real, monkeypatch):
    """`close()` caindo DURANTE o pre-warm (segundos de modelo em produção): o
    boot do dsh não precisa nem começar depois dele."""
    pipe = pipe_dsh_real
    entrou = threading.Event()

    def prewarm_lento(**kw):
        entrou.set()
        time.sleep(0.3)

    monkeypatch.setattr(pipe, "_prewarm", prewarm_lento)
    th = threading.Thread(target=pipe.start, daemon=True)
    th.start()
    assert entrou.wait(2), "o start() tinha de estar dentro do pre-warm"
    pipe.close()
    th.join(5)
    assert not th.is_alive(), "start() não pode ficar preso"
    assert pipe._dsh.alive is False


def test_close_durante_o_boot_do_dsh_nao_deixa_processo_orfo(pipe_dsh_real, monkeypatch):
    """`close()` no MEIO do boot: o processo que ele mata não é o último — o
    backoff do `_garantir` (dsh_client) sobe OUTRO para terminar a chamada."""
    pipe = pipe_dsh_real
    abrir = pipe._dsh._abrir_sessao
    subir = pipe._dsh._subir
    subidas = []
    fechou = []

    def subir_contando():
        subidas.append(True)
        return subir()

    def fechar_e_abrir():
        if not fechou:                   # o close() cai ANTES do `session/new`...
            fechou.append(True)
            pipe.close()                 # ...e o handshake morre no meio do `_subir`
        abrir()

    monkeypatch.setattr(pipe._dsh, "_subir", subir_contando)
    monkeypatch.setattr(pipe._dsh, "_abrir_sessao", fechar_e_abrir)
    pipe.start()
    assert len(subidas) == 2, "o backoff tinha de ter subido um processo NOVO"
    assert pipe._dsh.alive is False, "o processo do retry não pode ficar sem dono"


def test_close_na_criacao_do_worker_nao_deixa_filho_orfao(dsh_limpo, monkeypatch):
    """Mesma janela no worker persistente: ele nasce depois do `close()` e não
    pode ficar com o filho vivo (nem estourar `None.fecha()` no caminho)."""
    monkeypatch.setenv("TTS_CHAT_BACKEND", "openai")   # isola do dsh
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: True)
    criados = []

    class WorkerFalso:
        def __init__(self, voice_id=None):
            self.fechado = False
            criados.append(self)
            pipe.close()                 # o cliente cai NA CRIAÇÃO do worker

        def start(self):
            self.iniciado = True

        def fecha(self):
            self.fechado = True

    monkeypatch.setattr(lp, "_LiveWorker", WorkerFalso)
    pipe = app._live_pipe_novo(_sessao("worker-orfao"))
    pipe._prewarm = lambda **kw: None
    pipe.start()
    assert len(criados) == 1, "o worker tinha de ter sido criado"
    assert criados[0].fechado is True, "worker criado após o close() fica órfão"
    assert pipe._worker is None


def test_close_dentro_do_start_do_worker_nao_deixa_filho_orfao(dsh_limpo, monkeypatch):
    """#228 (residual do #207): a janela seguinte, e mais larga — o `close()` cai
    DEPOIS de `self._worker = w` e DURANTE o `w.start()`.

    Nessa ordem o `close()` vê a referência, chama `fecha()` sem nada para matar (o
    filho ainda não existe) e solta a referência; o filho nasce depois e o
    `if self._saiu` do `start()` já não acha ninguém para fechar."""
    monkeypatch.setenv("TTS_CHAT_BACKEND", "openai")   # isola do dsh
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: True)
    criados = []

    class WorkerFalso:
        def __init__(self, voice_id=None):
            self.fechado = False
            self.vivo = False
            criados.append(self)

        def start(self):
            pipe.close()                 # já passou a atribuição; o filho não existe
            self.vivo = True             # o filho "nasce" DEPOIS do close()

        def fecha(self):
            self.fechado = True
            self.vivo = False

    monkeypatch.setattr(lp, "_LiveWorker", WorkerFalso)
    pipe = app._live_pipe_novo(_sessao("worker-mid-start"))
    pipe._prewarm = lambda **kw: None
    pipe.start()
    assert len(criados) == 1, "o worker tinha de ter sido criado"
    assert criados[0].fechado is True, "o filho nasceu depois do close() e ficou órfão"
    assert criados[0].vivo is False
    assert pipe._worker is None


def test_tts_live_com_close_entre_as_duas_leituras_nao_estoura(dsh_limpo, monkeypatch):
    """#241 (família #207/#228): `_tts_live` lê `self._worker` DUAS vezes — a
    atribuição e, na linha seguinte, `self._worker.ativo`. Um `close()` da thread
    do WS entre as duas zerava a referência e o turno estourava
    `AttributeError: 'NoneType' object has no attribute 'ativo'` no meio, em vez
    de cair no in-process.

    A corrida é forçada de fora: o SETTER do atributo chama `close()` logo DEPOIS
    de guardar o valor — é exatamente a ordem "atribuiu → close() → leu"."""
    monkeypatch.setenv("TTS_CHAT_BACKEND", "openai")   # isola do dsh
    monkeypatch.setattr(lp, "_worker_habilitado", lambda: True)
    monkeypatch.setattr(lp, "_tts_app", lambda texto, omni, voice_id=None: "in-process")
    criados = []

    class WorkerFalso:
        def __init__(self, voice_id=None):
            self._ativo = True
            self.fechado = False
            criados.append(self)

        @property
        def ativo(self):
            return self._ativo

        def fecha(self):
            self.fechado = True
            self._ativo = False

        def gerar(self, texto, omni):
            return "pelo-worker"

    monkeypatch.setattr(lp, "_LiveWorker", WorkerFalso)
    pipe = app._live_pipe_novo(_sessao("worker-leitura-dupla"))

    def _get(self):
        return self.__dict__.get("_worker")

    def _set(self, v):
        self.__dict__["_worker"] = v
        if v is not None and self.__dict__.pop("_arme", False):
            self.close()                 # cai entre a atribuição e a 2ª leitura

    monkeypatch.setattr(type(pipe), "_worker", property(_get, _set), raising=False)
    pipe.__dict__["_arme"] = True

    assert pipe._tts_live("teste", {}) == "in-process"
    assert len(criados) == 1, "o worker tinha de ter sido criado"
    assert criados[0].fechado is True, "o worker criado no meio tem de ser fechado"
    assert pipe._worker is None


def test_backend_do_live_e_do_live_a_conversa_segue_no_global(monkeypatch):
    """#176: `chat_backend_live=dsh` + `chat_backend=openai` — o Live usa o harness e
    a Conversa continua no endpoint do dono (era o que faltava para a recomendação do
    #175 ser acionável)."""
    monkeypatch.setenv("TTS_CHAT_BACKEND", "openai")
    monkeypatch.setenv("TTS_CHAT_BACKEND_LIVE", "dsh")
    monkeypatch.setattr(app.dsh_client, "DshClient", DshClienteFalso)
    DshClienteFalso.criados.clear()
    sess = _sessao("live-dsh")
    pipe = app._live_pipe_novo(sess)
    try:
        assert len(DshClienteFalso.criados) == 1, "o Live tinha de usar o dsh"
        assert sess["dsh"] is pipe._dsh is DshClienteFalso.criados[0]
    finally:
        pipe.close()
    # a CONVERSA não foi arrastada junto
    monkeypatch.setattr(app, "_chat_llm_openai", lambda _m: "via-openai")
    monkeypatch.setattr(app, "_chat_llm_dsh", lambda _m: "via-dsh")
    assert app._chat_llm([]) == "via-openai"


def test_conversa_no_dsh_e_live_no_global(monkeypatch):
    """O inverso: quem quer o dsh na Conversa e o provedor no Live."""
    monkeypatch.setenv("TTS_CHAT_BACKEND", "dsh")
    monkeypatch.setenv("TTS_CHAT_BACKEND_LIVE", "openai")
    monkeypatch.setattr(app.dsh_client, "DshClient", DshClienteFalso)
    DshClienteFalso.criados.clear()
    pipe = app._live_pipe_novo(_sessao("live-openai"))
    assert pipe._dsh is None and not DshClienteFalso.criados
    monkeypatch.setattr(app, "_chat_llm_openai", lambda _m: "via-openai")
    monkeypatch.setattr(app, "_chat_llm_dsh", lambda _m: "via-dsh")
    assert app._chat_llm([]) == "via-dsh"


def test_backend_do_live_vazio_herda_o_global(monkeypatch, dsh_limpo):
    # `dsh_limpo` também neutraliza o CAMPO `chat_backend_live` (o dono pode tê-lo
    # deixado em dsh na tela) — sem ele o vazio do teste não é o vazio de produção.
    monkeypatch.delenv("TTS_CHAT_BACKEND_LIVE", raising=False)
    monkeypatch.setenv("TTS_CHAT_BACKEND", "dsh")
    assert app._chat_backend_live() == "dsh"
    monkeypatch.setenv("TTS_CHAT_BACKEND", "openai")
    assert app._chat_backend_live() == "openai"
    # e um valor inválido cai no global em vez de virar openai seco
    monkeypatch.setenv("TTS_CHAT_BACKEND_LIVE", "lixo")
    monkeypatch.setenv("TTS_CHAT_BACKEND", "dsh")
    assert app._chat_backend_live() == "dsh"


def test_stats_ia_anuncia_o_backend_do_live(monkeypatch):
    monkeypatch.setenv("TTS_CHAT_BACKEND", "openai")
    monkeypatch.setenv("TTS_CHAT_BACKEND_LIVE", "dsh")
    ia = app._live_stats_ia({"pipe": None})
    assert ia["pedido"] == "dsh" and ia["backend"] == "dsh"
    monkeypatch.setenv("TTS_CHAT_BACKEND_LIVE", "openai")
    ia = app._live_stats_ia({"pipe": None})
    assert ia["pedido"] == "openai" and ia["backend"] == "openai"


def test_app_sem_backend_dsh_nao_cria_cliente(monkeypatch, dsh_limpo):
    # `dsh_limpo`: sem ele um `TTS_CHAT_BACKEND_LIVE=dsh` herdado do dono faz o
    # Live subir o dsh e o teste falha medindo outra rota (achado do gate #177).
    monkeypatch.setenv("TTS_CHAT_BACKEND", "openai")
    sess = {"id": "s2", "voice_id": None, "system": None, "history": [],
            "fila": queue.Queue()}
    pipe = app._live_pipe_novo(sess)
    assert pipe._dsh is None and sess["dsh"] is None


def test_dsh_que_nao_sobe_nao_deixa_a_sessao_muda(monkeypatch):
    """#146: se o dsh morre ANTES do 1º token (boot/handshake), o turno é refeito no
    openai em vez de morrer em erro — a sessão não fica muda — e o dsh fica marcado
    para os turnos seguintes. Avisa UMA vez com `dsh_indisponivel`."""
    import dsh_client

    fala, dsh = Fala(), DshFalso(erros=[dsh_client.DshError("binário ausente")])
    falados = []

    def llm_openai(msgs):
        falados.append(msgs)
        yield "resposta do openai"

    p = _pipeline(fala, dsh, llm=llm_openai)

    p.end_of_speech(inicia_thread=False)
    assert fala.texto() == "resposta do openai", "turno refeito no openai"
    assert "error" not in fala.tipos(), "dsh fora não é erro do turno"
    avisos = [e for e in fala.eventos if e["type"] == "dsh_indisponivel"]
    assert len(avisos) == 1 and avisos[0]["fallback"] == "openai"
    assert dsh.fechado is True, "processo sem uso é liberado"

    p.push_pcm(_fala_nova())                # turno seguinte já nasce no openai
    p.end_of_speech(inicia_thread=False)
    assert fala.tipos().count("turn_complete") == 2
    assert len(dsh.chamadas) == 1, "não tenta o dsh de novo"
    assert len(falados) == 2


def test_falha_no_meio_do_stream_fecha_com_error_e_marca_o_dsh():
    """Erro DEPOIS de já ter saído texto: não dá para refazer sem repetir — fecha
    com `error{pipeline}` (sessão viva) e o próximo turno vai para o openai."""
    fala, dsh = Fala(), DshFalso()
    falados = []

    def llm_openai(msgs):
        falados.append(msgs)
        yield "pelo openai"

    p = _pipeline(fala, dsh, llm=llm_openai)

    def stream_meio(msgs, turno_id):
        yield "ja falei isso "
        raise RuntimeError("caiu no meio")

    dsh.stream = stream_meio
    p.end_of_speech(inicia_thread=False)

    erros = [e for e in fala.eventos if e["type"] == "error"]
    assert erros and erros[0]["code"] == "pipeline"
    assert p._dsh_indisponivel is True

    dsh.stream = DshFalso.stream.__get__(dsh)      # dsh "voltaria", mas está marcado
    p.push_pcm(_fala_nova())
    p.end_of_speech(inicia_thread=False)
    assert fala.tipos().count("turn_complete") == 1
    assert falados, "próximo turno foi para o openai"


def _espera(cond, timeout=5.0):
    prazo = time.monotonic() + timeout
    while time.monotonic() < prazo:
        if cond():
            return True
        time.sleep(0.01)
    return False