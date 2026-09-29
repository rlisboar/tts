"""Testes do `dsh_client` com um servidor ACP falso (sem dsh, sem Node, sem MLX).

Cobre o contrato publicado na task_129d2183: handshake, set_config, streaming por
iterador, cancel com descarte de chunk tardio, permissão negada, processo morto →
erro claro + reinício, erro explicativo e parsing das configOptions.
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

import dsh_client
from dsh_client import DshClient, DshError

FAKE = str(Path(__file__).resolve().parent / "fake_acp.py")


def cliente(monkeypatch, env=None, **kw):
    """Cliente apontando para o fake_acp (python puro: o check de node é pulado).

    Os knobs do fake vão por `env_extra`: o env do filho é MÍNIMO por desenho,
    então `monkeypatch.setenv` no processo de teste NÃO chega nele."""
    cfg = dict(bin=sys.executable, extra_args=[FAKE], profile="test",
               prewarm=False, on_log=lambda _m: None, env_extra=dict(env or {}),
               restart_delay=0.02)
    cfg.update(kw)
    return DshClient(**cfg)


def _log_prompt(logs):
    """Decodifica o PROMPT logado pelo fake (o log é JSON, com escapes)."""
    return json.loads(next(l for l in logs if "PROMPT " in l).split("PROMPT ", 1)[1])


# ---------------------------------------------------------------- helpers puros
def test_par_opaco_rota_e_validade():
    assert dsh_client.dsh_model_route('["dsflash","deepseek-flash-41"]') == "dsflash"
    assert dsh_client.dsh_model_route("lixo") is None
    assert dsh_client.dsh_model_valido('["a","b"]')
    assert not dsh_client.dsh_model_valido('["a"]')
    assert not dsh_client.dsh_model_valido("deepseek-flash-41")


def test_rota_sem_chave_cai_no_default_seguro():
    assert dsh_client.dsh_model_for_turn("") == dsh_client.DSH_DEFAULT_MODEL
    assert dsh_client.dsh_model_for_turn('["deepseek-official","deepseek-v4-pro"]') \
        == dsh_client.DSH_DEFAULT_MODEL
    com_chave = '["openrouter","z-ai/glm-5.3-flash"]'
    assert dsh_client.dsh_model_for_turn(com_chave) == com_chave


def test_effort_normaliza_alias_e_invalido():
    assert dsh_client.dsh_effort_valido("none") == "off"
    assert dsh_client.dsh_effort_valido("HIGH") == "high"
    assert dsh_client.dsh_effort_valido("xhigh") == "max"
    assert dsh_client.dsh_effort_valido("nada") == "off"


def test_parse_config_options_achata_grupos_e_marca_default():
    opts = [{"id": "model", "currentValue": '["dsflash","deepseek-flash-41"]',
             "options": [{"value": '["a","m1"]', "name": "M1"},
                         {"options": [{"value": '["b","m2"]', "name": "M2"}]}]},
            {"id": "reasoning_effort",
             "options": [{"value": v} for v in ("off", "low", "high", "max")]}]
    modelos = dsh_client.parse_config_options(opts)
    assert [m["id"] for m in modelos] == ['["a","m1"]', '["b","m2"]']
    assert modelos[0]["provider"] == "a" and modelos[0]["modelo"] == "m1"
    assert modelos[1]["efforts"] == ["off", "low", "high", "max"]
    assert not modelos[0]["isDefault"]


def test_resolver_bin_ausente():
    with pytest.raises(DshError, match="não existe"):
        dsh_client.resolver_bin("/nada/aqui/dsh", checa_node=False)


# ------------------------------------------------------------------- protocolo
def test_handshake_streaming_e_set_config(monkeypatch):
    logs = []
    c = cliente(monkeypatch, on_log=logs.append,
                env={"FAKE_ACP_THOUGHT": "pensando"})
    try:
        texto = "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    assert texto == "Olá, mundo!"
    assert c.ultimas_thought_chars == len("pensando")
    assert any("CONFIG model" in l for l in logs)
    assert any("CONFIG reasoning_effort" in l for l in logs)
    # model sem chave no catálogo → o cliente manda o default seguro
    assert any(dsh_client.dsh_model_for_turn("") in l and "CONFIG model" in l for l in logs)


def test_prompt_de_lista_renderiza_historico(monkeypatch):
    logs = []
    c = cliente(monkeypatch, on_log=logs.append)
    try:
        c.collect([{"role": "system", "content": "Seja breve."},
                   {"role": "user", "content": "bom dia"},
                   {"role": "assistant", "content": "bom dia!"},
                   {"role": "user", "content": "e agora?"}])
    finally:
        c.close()
    prompt = _log_prompt(logs)
    assert "Seja breve." in prompt
    assert "Usuário: bom dia" in prompt
    assert "Assistente: bom dia!" in prompt


def test_string_nao_renderiza(monkeypatch):
    logs = []
    c = cliente(monkeypatch, on_log=logs.append)
    try:
        c.collect("só o turno novo")
    finally:
        c.close()
    assert _log_prompt(logs) == "só o turno novo"


def test_env_do_filho_e_minimo_sem_as_nossas_chaves(monkeypatch):
    """As credenciais do app não vão para o dsh (ele lê ~/.dsh/.credentials.yaml)."""
    monkeypatch.setenv("TTS_CHAT_API_KEY", "chave-do-app")
    monkeypatch.setenv("TTS_ROD_ADMIN_KEY", "admin")
    logs = []
    c = cliente(monkeypatch, on_log=logs.append)
    try:
        c.collect("oi")
    finally:
        c.close()
    assert any("ENV_CHAVE_APP nao" in l for l in logs)
    assert any("ENV_HOME sim" in l for l in logs)


def test_permissao_sempre_negada(monkeypatch):
    logs = []
    c = cliente(monkeypatch, on_log=logs.append, env={"FAKE_ACP_PERMISSION": "1"})
    try:
        assert "".join(c.stream("oi", "t1")) == "Olá, mundo!"
    finally:
        c.close()
    assert any("PERMISSAO reject_once" in l for l in logs)


def test_cancel_descarta_chunk_tardio(monkeypatch):
    """Cancela com o turno EM CURSO e o chunk tardio não entra.

    A espera é pelo 1º chunk de verdade (evento), não por `sleep` fixo: o boot do
    fake (handshake + sessão + prompt) estoura um orçamento de 0,25 s quando a
    máquina está carregada e o cancel chegava ANTES de qualquer chunk — aí a
    última asserção virava flake sob carga (task_6db0e2cc)."""
    c = cliente(monkeypatch, env={
        "FAKE_ACP_MODO": "deltas",
        "FAKE_ACP_CHUNKS": json.dumps(["A", "B", "C", "D", "E"]),
        "FAKE_ACP_CHUNK_DELAY_S": "0.15",
        "FAKE_ACP_CANCEL_SETTLE_S": "0.2",
        "FAKE_ACP_LATE_CHUNK": "TARDIO",
    })
    caixa = {}
    primeiro = threading.Event()

    def correr():
        try:
            partes = []
            for pedaco in c.stream("conte", "t1"):
                partes.append(pedaco)
                primeiro.set()
            caixa["texto"] = "".join(partes)
        except Exception as exc:  # noqa: BLE001
            caixa["erro"] = exc

    th = threading.Thread(target=correr)
    th.start()
    assert primeiro.wait(15), "nenhum chunk em 15 s — o fake travou no boot"
    c.cancel("t1")
    th.join(10)
    c.close()
    assert not th.is_alive()
    assert "erro" not in caixa
    assert "TARDIO" not in caixa["texto"]
    assert caixa["texto"] != "", "algum chunk antes do cancel deveria ter passado"


def test_cancel_idempotente_e_turno_velho_nao_mata_o_novo(monkeypatch):
    c = cliente(monkeypatch)
    try:
        c.stream("oi", "t1").__next__()
        c.cancel("t-antigo")          # sem sessão/ação: não levanta
        c.cancel("t1")
        c.cancel("t1")
    finally:
        c.close()
    assert c.cancelado("t1") and c.cancelado("t-antigo")


def test_cancel_orfao_reabre_sessao_e_nao_vaza_chunk_tardio(monkeypatch):
    """dsh que IGNORA o session/cancel: o iterador sai no prazo, a sessão é
    reaberta e o chunk tardio da sessão abandonada não entra no turno novo."""
    logs = []
    c = cliente(monkeypatch, on_log=logs.append, cancel_espera=0.3, env={
        "FAKE_ACP_MODO": "deltas",
        # 6 deltas × 0,08 s = 0,48 s de janela. O fake decide se o turno foi
        # cancelado SÓ quando termina de emitir os deltas, e o `session/cancel`
        # chega por IPC: com 3 × 0,05 s (0,15 s) a mensagem do cancel podia
        # chegar DEPOIS dessa decisão, o turno settleava sozinho e a sessão não
        # era reaberta — o teste caía por corrida, não por comportamento
        # (medido: falhava com a máquina carregada, passava sozinho).
        "FAKE_ACP_CHUNKS": json.dumps(["A", "B", "C", "D", "E", "F"]),
        "FAKE_ACP_CHUNK_DELAY_S": "0.08",
        "FAKE_ACP_CANCEL_IGNORA_S": "1.5",
    })
    caixa = {}
    try:
        def correr():
            caixa["texto"] = "".join(c.stream("primeiro", "t1"))
        th = threading.Thread(target=correr)
        th.start()
        time.sleep(0.12)
        t0 = time.perf_counter()
        c.cancel("t1")
        th.join(10)
        assert not th.is_alive()
        assert time.perf_counter() - t0 < 1.0, "não pode esperar o prompt órfão settlear"
        assert "TARDIO-IGNORADO" not in caixa["texto"]
        time.sleep(1.8)                       # o chunk tardio da sessão velha chega aqui
        assert "TARDIO-IGNORADO" not in "".join(c.stream("segundo", "t2"))
        assert any("sessão será reaberta" in l for l in logs)
        assert len([l for l in logs if "SESSAO" in l]) >= 2
    finally:
        c.close()


def test_processo_morto_vira_erro_claro_e_reinicia(monkeypatch):
    c = cliente(monkeypatch, env={"FAKE_ACP_DIE_AFTER_PROMPTS": "1"})
    try:
        assert "".join(c.stream("primeira", "t1")) == "Olá, mundo!"
        limite = time.time() + 5
        while c.alive and time.time() < limite:   # o fake sai logo após o turno
            time.sleep(0.05)
        assert not c.alive
        assert "".join(c.stream("segunda", "t2")) == "Olá, mundo!"
    finally:
        c.close()


def test_erro_de_prompt_e_explicativo(monkeypatch):
    c = cliente(monkeypatch, env={"FAKE_ACP_PROMPT_ERROR": json.dumps(
        {"code": -32603, "message": 'no API key for provider route "x"'})})
    try:
        with pytest.raises(DshError) as e:
            "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    assert "-32603" in str(e.value)
    assert "sem chave" in str(e.value)


def test_erro_no_session_new(monkeypatch):
    c = cliente(monkeypatch, env={"FAKE_ACP_NEW_ERROR": json.dumps(
        {"code": -32603, "message": "falhou", "data": {"details": "mcp-client(z)"}})})
    try:
        with pytest.raises(DshError) as e:
            "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    assert "mcp-client(z)" in str(e.value)


def test_backoff_para_apos_cinco_falhas(monkeypatch):
    """Binário que morre na hora: 5 tentativas e erro claro, sem loop cego."""
    c = DshClient(bin="/usr/bin/true", profile="test", prewarm=False, restart_delay=0.02)
    try:
        with pytest.raises(DshError, match="PARADO"):
            "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    assert c._handshake_fails == dsh_client._RESTART_MAX_TRIES + 1


def test_resultado_dos_modos_de_chunk(monkeypatch):
    """#147: N deltas, comitado no fim, ou só comitado — o texto final é o MESMO
    e nada se repete. O consumidor não pode depender do modo."""
    for modo in ("comitado", "deltas", "deltas+comitado", "deltas-vazios+comitado"):
        env = {"FAKE_ACP_MODO": modo, "FAKE_ACP_CHUNKS": json.dumps(["Olá", ", ", "mundo!"])}
        if modo == "deltas+comitado":
            env["FAKE_ACP_MARCA"] = "1"
        c = cliente(monkeypatch, env=env)
        try:
            texto = "".join(c.stream("oi", f"t-{modo}"))
        finally:
            c.close()
        assert texto == "Olá, mundo!", f"modo {modo}"
        if modo == "deltas+comitado":
            assert c.chunks_duplicados == 1, "o comitado inteiro foi absorvido"


def test_modo_deltas_entrega_incremental(monkeypatch):
    """(a) pós-patch: cada delta sai na hora — é o que derruba o 1º token."""
    c = cliente(monkeypatch, env={"FAKE_ACP_MODO": "deltas",
                                  "FAKE_ACP_CHUNK_DELAY_S": "0.02"})
    try:
        itens = list(c.stream("oi", "t1"))
    finally:
        c.close()
    assert itens == ["Olá", ", ", "mundo!"]
    assert c.chunks_duplicados == 0


def test_comitado_sozinho_nao_regride(monkeypatch):
    """(c) o harness de HOJE (1 chunk no fim) segue igual: 1 emissão, sem dup."""
    c = cliente(monkeypatch)                      # modo default = comitado
    try:
        itens = list(c.stream("oi", "t1"))
    finally:
        c.close()
    assert itens == ["Olá, mundo!"]
    assert c.chunks_emitidos == 1 and c.chunks_duplicados == 0


def test_comitado_sem_marca_com_diferenca_de_pontuacao(monkeypatch):
    """Patch que não marca: o comitado difere do acumulado por pontuação.
    Não pode sair o parágrafo inteiro de novo."""
    c = cliente(monkeypatch, env={
        "FAKE_ACP_MODO": "deltas+comitado",
        "FAKE_ACP_CHUNKS": json.dumps(["Olá", " mundo"]),
        "FAKE_ACP_COMITADO": "Olá, mundo!",     # drift de pontuação no comitado
    })
    try:
        texto = "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    assert texto == "Olá mundo!"          # deltas "Olá"+" mundo" + só o "!" do comitado
    assert c.chunks_duplicados == 1
    # Semântica: pontuação que o comitado introduz DENTRO do trecho já emitido não
    # pode ser reinserida (o que já saiu não volta) — o que dá é não repetir o resto.


def test_guarda_de_identidade_reavaliada_a_cada_delta(monkeypatch):
    """A guarda vale CHUNK A CHUNK: cancelar no meio de N deltas tem que parar ali,
    sem deixar passar o resto."""
    c = cliente(monkeypatch, env={
        "FAKE_ACP_MODO": "deltas",
        "FAKE_ACP_CHUNKS": json.dumps(["1", "2", "3", "4", "5", "6"]),
        "FAKE_ACP_CHUNK_DELAY_S": "0.1",
    })
    caixa = {}

    def correr():
        caixa["itens"] = list(c.stream("conte", "t1"))

    th = threading.Thread(target=correr)
    th.start()
    time.sleep(0.25)
    c.cancel("t1")
    th.join(10)
    c.close()
    assert not th.is_alive()
    assert caixa["itens"], "algum delta antes do cancel deveria ter saído"
    assert "".join(caixa["itens"]) in ("1", "12")


def test_delta_repetido_legitimo_nao_some(monkeypatch):
    """Regressão do achado do gate #138: no modo deltas, "ha","ha","ha" sumia.
    O delta repetido NÃO pode ser confundido com a mensagem comitada."""
    for chunks, esperado in ((["ha", "ha", "ha"], "hahaha"),
                             (["sim, ", "sim, ", "claro"], "sim, sim, claro"),
                             (["muito", " ", "muito", " bom"], "muito muito bom")):
        c = cliente(monkeypatch, env={"FAKE_ACP_MODO": "deltas",
                                      "FAKE_ACP_CHUNKS": json.dumps(chunks)})
        try:
            texto = "".join(c.stream("oi", "t1"))
        finally:
            c.close()
        assert texto == esperado, f"chunks {chunks}"
        assert c.chunks_duplicados == 0, f"chunks {chunks} não têm comitado"


def test_delta_repetido_sem_marca_tambem_sobrevive(monkeypatch):
    """Patch que projeta deltas SEM marcar `partial`: delta repetido é igual ao
    último chunk bruto e menor que o acumulado — é delta, não comitado."""
    c = cliente(monkeypatch, env={
        "FAKE_ACP_MODO": "deltas-sem-marca",
        "FAKE_ACP_CHUNKS": json.dumps(["Olá", "Olá", " mundo"]),
    })
    try:
        texto = "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    assert texto == "OláOlá mundo"


def test_par_deltas_mais_comitado_com_texto_repetido(monkeypatch):
    """O par (deltas → comitado idêntico NO FIM) tem que suprimir, sem perder a
    repetição legítima do MEIO. Mesmo texto repetido: `deltas` preserva, o par
    absorve o comitado."""
    for chunks, comitado, esperado in (
            (["ha", "ha"], "haha", "haha"),
            (["ha", "ha", "ha"], "hahaha", "hahaha"),
            (["sim, ", "sim, ", "claro"], "sim, sim, claro", "sim, sim, claro")):
        for marca in ("0", "1"):
            c = cliente(monkeypatch, env={
                "FAKE_ACP_MODO": "deltas+comitado",
                "FAKE_ACP_CHUNKS": json.dumps(chunks),
                "FAKE_ACP_COMITADO": comitado,
                "FAKE_ACP_MARCA": marca,
            })
            try:
                texto = "".join(c.stream("oi", "t1"))
            finally:
                c.close()
            assert texto == esperado, f"{chunks} marca={marca}"
            assert c.chunks_duplicados == 1, f"{chunks} marca={marca}"
        # contraprova sem comitado: nada de supressão no meio do turno
        c = cliente(monkeypatch, env={"FAKE_ACP_MODO": "deltas",
                                      "FAKE_ACP_CHUNKS": json.dumps(chunks)})
        try:
            assert "".join(c.stream("oi", "t1")) == comitado, f"{chunks}"
        finally:
            c.close()
        assert c.chunks_duplicados == 0


def test_nao_duplicar_regras():
    """Função pura: cada regra da anti-duplicação, incluindo a marca do patch."""
    f = dsh_client._nao_duplicar
    assert dsh_client._lcp("Olá mundo!", "Olá mundo") == 9
    assert f("abc", None, "") == "abc"                 # 1º chunk entra inteiro
    assert f("abc", None, "abc") == ""                 # comitado idêntico → descarta
    assert f("abcd", None, "abc") == "d"               # snapshot cumulativo → só o resto
    assert f(", ", None, "Olá") == ", "                # delta puro no meio
    assert f("delta", True, "acumulado") == "delta"    # marca de delta: entra como veio
    assert f("Olá mundo!", False, "Olá mundo") == "!"  # marca de comitado: só o que falta
    assert f("Olá mundo!", None, "Olá mundo") == "!"   # sem marca, mesmo porte → heurística
    assert f("", None, "abc") == ""
    assert f("bem mais longo que o acumulado", None, "curto") \
        == "bem mais longo que o acumulado"
    # marca de DELTA vence a igualdade (regressão do gate #138)
    assert f("ha", True, "ha") == "ha"
    assert f("ha", True, "ha", "ha") == "ha"
    # snapshot cumulativo MARCADO (estritamente maior) ainda vira sufixo
    assert f("Olá, ", True, "Olá") == ", "
    # sem marca, o delta repetido é reconhecido pelo último chunk bruto
    assert f("ha", None, "ha", "ha") == "ha"
    assert f("ha", None, "ha") == ""                   # sem histórico: parece comitado


def _pacote_falso(tmp_path, marcador=True, versao="0.1.5-rc.3"):
    """Layout de host: bin/dsh + node_modules/@deepseek-ai/dsh-acp/lib/index.js."""
    raiz = tmp_path / "host"
    pacote = raiz / "node_modules" / "@deepseek-ai" / "dsh-acp"
    (pacote / "lib").mkdir(parents=True, exist_ok=True)
    corpo = "export const x = 1;\n"
    if marcador:
        corpo += f"/* {dsh_client.DSH_PATCH_MARKER} */\n"
    (pacote / "lib" / "index.js").write_text(corpo)
    (pacote / "package.json").write_text(json.dumps({"version": versao}))
    exe = raiz / "bin" / "dsh"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return str(exe)


def test_estado_bridge_patchado_e_limpo(tmp_path, monkeypatch):
    # NÃO pode shell out no caminho bom: a busca por caminho resolve sozinha
    monkeypatch.setattr(dsh_client.subprocess, "run",
                        lambda *a, **k: pytest.fail("não deveria rodar subprocess"))
    assert dsh_client.estado_bridge(_pacote_falso(tmp_path, marcador=True))["estado"] \
        == "patched"
    assert dsh_client.estado_bridge(_pacote_falso(tmp_path, marcador=False))["estado"] \
        == "clean"
    info = dsh_client.estado_bridge(_pacote_falso(tmp_path, marcador=False))
    assert info["versao"] == "0.1.5-rc.3"
    assert "npm install -g" in info["motivo"]


def test_estado_bridge_unknown_nunca_levanta(tmp_path, monkeypatch):
    """Hermético de propósito (#163): tudo em tmp_path, sem tocar o host, sem
    patch global de `Path.read_text` (que valia para o processo inteiro e podia
    ser lido por outra thread)."""
    # binário ausente: nem tenta o npm
    monkeypatch.setattr(dsh_client, "_pacote_acp_por_npm",
                        lambda: pytest.fail("não deveria chamar o npm sem binário"))
    info = dsh_client.estado_bridge("/nao/existe/dsh")
    assert info["estado"] == "unknown" and "não existe" in info["motivo"]
    # binário existe mas o pacote não está em nenhum parent (e o npm falha)
    exe = tmp_path / "sozinho" / "dsh"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(dsh_client, "_pacote_acp_por_npm", lambda: None)
    info = dsh_client.estado_bridge(str(exe))
    assert info["estado"] == "unknown" and "não encontrado no host" in info["motivo"]
    # pacote ILEGÍVEL: existe mas não dá para ler (sem monkeypatch e sem depender
    # de thread: `Path.read_text` global era o suspeito do flake sob carga)
    exe2 = _pacote_falso(tmp_path)
    idx = Path(exe2).parent.parent / "node_modules/@deepseek-ai/dsh-acp/lib/index.js"
    idx.chmod(0o000)
    try:
        info = dsh_client.estado_bridge(exe2)
    finally:
        idx.chmod(0o644)
    assert info["estado"] == "unknown"
    assert info["motivo"], "unknown tem de explicar o motivo"


def _binario_que_falha(tmp_path, rc=3, stderr="boom: subi e morri"):
    """Script que EXISTE, executa e morre com rc — o caso do smoke de fallback."""
    p = tmp_path / "binario-que-falha"
    p.write_text(f'#!/bin/sh\necho "{stderr}" >&2\nexit {rc}\n')
    p.chmod(0o755)
    return str(p)


def test_binario_existe_e_falha_nao_diz_nao_encontrado(tmp_path):
    """#163: `/bin/false` (ou qualquer binário que sobe e morre) tem de dizer que
    existe e falhou — não "não encontrado no PATH"."""
    exe = _binario_que_falha(tmp_path)
    c = DshClient(bin=exe, profile="test", prewarm=False, restart_delay=0.01)
    try:
        with pytest.raises(DshError) as e:
            "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    msg = str(e.value)
    assert "não encontrado" not in msg
    assert "existe e saiu com rc=3" in msg
    assert "boot/handshake, não o PATH" in msg
    assert "boom: subi e morri" in msg            # stderr capturado
    assert exe in msg                             # nomeia o binário e o comando


def test_resolver_bin_separa_os_tres_casos(tmp_path):
    with pytest.raises(DshError, match="não encontrado no PATH"):
        dsh_client.resolver_bin("nome-que-nao-existe-em-lugar-nenhum",
                                checa_node=False)
    with pytest.raises(DshError, match="não existe"):
        dsh_client.resolver_bin(str(tmp_path / "nao-existe"), checa_node=False)
    naoexec = tmp_path / "naoexec"
    naoexec.write_text("x")
    naoexec.chmod(0o644)
    with pytest.raises(DshError, match="não é executável"):
        dsh_client.resolver_bin(str(naoexec), checa_node=False)


def test_prewarm_em_thread_nao_colide_com_o_primeiro_turno(monkeypatch):
    """Regressão do #162: prewarm em outra thread (é o que o Live faz no connect)
    + 1º turno chegando logo depois. O harness aceita UM prompt por sessão."""
    logs = []
    c = cliente(monkeypatch, prewarm=True, on_log=logs.append,
                env={"FAKE_ACP_PREWARM_TTFT_S": "0.6"})
    th = threading.Thread(target=c.prewarm)
    try:
        th.start()
        time.sleep(0.2)                       # boot feito, prompt do prewarm EM VOO
        texto = "".join(c.stream("pergunta real", "t1"))
        th.join(20)
    finally:
        c.close()
    assert texto == "Olá, mundo!"
    assert not th.is_alive()
    # o turno ESPEROU o slot em vez de bater no harness
    assert not any("EM_VOO" in l for l in logs)
    assert c._prompt_livre.is_set(), "o slot não pode ficar travado"


def test_erro_de_prompt_em_voo_e_tratado_como_transitorio(monkeypatch):
    """Cinto: se o harness recusar mesmo assim, o turno repete UMA vez e segue
    (antes o Live marcava o dsh como morto pela sessão inteira)."""
    logs = []
    c = cliente(monkeypatch, on_log=logs.append,
                env={"FAKE_ACP_RECUSA_PRIMEIRA": "1"})
    try:
        texto = "".join(c.stream("oi", "t1"))
    finally:
        c.close()
    assert texto == "Olá, mundo!"
    assert any("RECUSA_FORCADA" in l for l in logs), "o fake tinha de ter recusado"
    assert any("recusado em voo" in l for l in logs), "o cliente tinha de ter repetido"


def test_prewarm_tambem_regete_erro_em_voo(monkeypatch):
    logs = []
    c = cliente(monkeypatch, prewarm=True, on_log=logs.append,
                env={"FAKE_ACP_RECUSA_PRIMEIRA": "1"})
    try:
        c.prewarm()                            # o 1º prompt (o prewarm) é recusado
        assert c._prompt_livre.is_set()
        assert "".join(c.stream("oi", "t1")) == "Olá, mundo!"
    finally:
        c.close()
    assert any("RECUSA_FORCADA" in l for l in logs)


def test_corrida_prewarm_turno_e_determinista_em_15_rodadas(monkeypatch):
    """O fake é ESTRITO (recusa prompts cruzados como o harness). Aqui a corrida é
    FORÇADA: o turno só começa depois que o fake RECEBEU o prompt do prewarm (que
    demora 0,25 s), então sem a espera de slot o harness recusaria. 15/15."""
    for i in range(15):
        logs = []
        c = cliente(monkeypatch, prewarm=True, on_log=logs.append,
                    env={"FAKE_ACP_PREWARM_TTFT_S": "0.25"})
        th = threading.Thread(target=c.prewarm)
        try:
            th.start()
            limite = time.time() + 20
            while not any("PROMPT" in l for l in logs) and time.time() < limite:
                time.sleep(0.01)
            assert any("PROMPT" in l for l in logs), "prewarm nem chegou no harness"
            texto = "".join(c.stream("pergunta", f"t{i}"))
            th.join(20)
            assert texto == "Olá, mundo!", f"rodada {i}"
            assert not th.is_alive()
            assert not any("EM_VOO" in l for l in logs), \
                f"o harness recusou na rodada {i} — o turno não esperou o slot"
            assert c._prompt_livre.is_set(), f"slot travado na rodada {i}"
        finally:
            c.close()


def test_retry_esgotado_sobe_o_erro_e_o_146_segue_valendo(monkeypatch):
    """Recusa em DOBRO: o retry (1×) não basta → o erro SOBE como DshError, que é
    o que dispara o `dsh_indisponivel`/fallback do Live (#146)."""
    with pytest.raises(DshError, match="already in flight"):
        c = cliente(monkeypatch, env={"FAKE_ACP_RECUSA_PRIMEIRA": "5"})
        try:
            "".join(c.stream("oi", "t1"))
        finally:
            c.close()


def test_turno_cancela_o_prewarm_em_vez_de_esperar_por_ele(monkeypatch):
    """#172: o prewarm não pode ADIAR o 1º turno. Turno que chega com o prewarm em
    voo cancela o prewarm (barato) e manda o turno — sem esperar os 5 s dele."""
    logs = []
    c = cliente(monkeypatch, prewarm=True, on_log=logs.append,
                env={"FAKE_ACP_PREWARM_TTFT_S": "5"})
    th = threading.Thread(target=c.prewarm)
    try:
        th.start()
        limite = time.time() + 20
        while not any("PROMPT" in l for l in logs) and time.time() < limite:
            time.sleep(0.01)
        t0 = time.perf_counter()
        texto = "".join(c.stream("pergunta real", "t1"))
        dt = time.perf_counter() - t0
        th.join(20)
    finally:
        c.close()
    assert texto == "Olá, mundo!"
    assert dt < 3.0, f"o turno esperou o prewarm: {dt:.2f}s (o prewarm ia até 5s)"
    assert any("cancelando o pre-warm" in l for l in logs)
    assert any("PREWARM_CANCELADO" in l for l in logs), "o fake tinha de ter visto o cancel"
    assert not any("EM_VOO" in l for l in logs), "o harness não foi pego ocupado"
    assert c._prompt_livre.is_set()


def test_prewarm_que_estoura_o_prazo_nao_mente_sobre_o_slot(monkeypatch):
    """#170: timeout do prewarm NÃO é 'sessão livre'. O slot continua tomado, o
    log diz que segue em voo, e o turno seguinte ainda sai (cancela e segue)."""
    logs = []
    c = cliente(monkeypatch, prewarm=True, on_log=logs.append, prewarm_timeout=0.3,
                env={"FAKE_ACP_PREWARM_TTFT_S": "2"})
    try:
        c.prewarm()
        assert any("ainda em voo" in l for l in logs), logs
        assert not c._prompt_livre.is_set(), "o slot NÃO podia ser declarado livre"
        assert c._prewarm_em_voo()
        texto = "".join(c.stream("agora o turno", "t1"))
    finally:
        c.close()
    assert texto == "Olá, mundo!"
    assert c._prompt_livre.is_set()


def test_prewarm_que_ignora_o_cancel_ainda_libera_e_o_turno_sai(monkeypatch):
    """Pior caso: o prewarm não obedece o cancel. O turno espera um pouco, libera o
    slot à força e o RETRY (cinto) cobre a recusa — o turno não pode morrer."""
    logs = []
    c = cliente(monkeypatch, prewarm=True, on_log=logs.append, prewarm_cancel_espera=0.3,
                env={"FAKE_ACP_PREWARM_TTFT_S": "1.5",
                     "FAKE_ACP_PREWARM_IGNORA_CANCEL": "1"})
    th = threading.Thread(target=c.prewarm)
    try:
        th.start()
        limite = time.time() + 20
        while not any("PROMPT" in l for l in logs) and time.time() < limite:
            time.sleep(0.01)
        texto = "".join(c.stream("pergunta real", "t1"))
        th.join(20)
    finally:
        c.close()
    assert texto == "Olá, mundo!"
    assert any("liberando o slot à força" in l for l in logs)
    assert c._prompt_livre.is_set()


def test_prewarm_que_NUNCA_responde_o_turno_sai_por_sessao_nova(monkeypatch):
    """Pior caso absoluto (#172): o prewarm não settleia (dorme 60 s e ignora o
    cancel). O turno cancela, espera, libera à força, insiste e — quando o harness
    segue recusando — REABRE a sessão (o prompt preso morre com a antiga)."""
    logs = []
    c = cliente(monkeypatch, prewarm=True, on_log=logs.append, prewarm_cancel_espera=0.2,
                env={"FAKE_ACP_PREWARM_TTFT_S": "60",
                     "FAKE_ACP_PREWARM_IGNORA_CANCEL": "1"})
    th = threading.Thread(target=c.prewarm)
    try:
        th.start()
        limite = time.time() + 20
        while not any("PROMPT" in l for l in logs) and time.time() < limite:
            time.sleep(0.01)
        texto = "".join(c.stream("pergunta real", "t1"))
    finally:
        c.close()
    assert texto == "Olá, mundo!"
    assert any("reabrindo sessão" in l for l in logs), logs[-8:]
    assert c._prompt_livre.is_set()


def test_prewarm_lento_nao_levanta_e_nao_marca_indisponivel(monkeypatch):
    """Critério do #172: prewarm lento/falho é otimização — `prewarm()` não levanta,
    então o Live não tem o que marcar como `dsh_indisponivel`."""
    c = cliente(monkeypatch, prewarm=True, prewarm_timeout=0.2,
                env={"FAKE_ACP_PREWARM_TTFT_S": "1"})
    try:
        c.prewarm()                       # não levanta
        assert c.alive and c.session_id
    finally:
        c.close()


def test_prompt_em_voo_regra():
    assert dsh_client._prompt_em_voo(dsh_client.DshError(
        "session/prompt: acp -32602: Invalid params: a prompt is already in "
        "flight for this session"))
    assert not dsh_client._prompt_em_voo(dsh_client.DshError("acp -32603: no key"))
    assert not dsh_client._prompt_em_voo(dsh_client.DshError("-32602: outra coisa"))


def test_close_e_idempotente(monkeypatch):
    c = cliente(monkeypatch)
    c.collect("oi")
    c.close()
    c.close()
    assert not c.alive