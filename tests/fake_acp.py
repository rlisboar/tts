#!/usr/bin/env python3
"""Servidor ACP falso para os testes do `dsh_client` (sem dsh, sem Node).

Fala o mesmo wire do `dsh --profile acp` (JSON-RPC 2.0 por linha no stdio) e é
configurável por ambiente, para os testes cobrirem handshake, set_config,
streaming, cancel (com chunk tardio), permissão, erro e morte do processo.

Uso: python tests/fake_acp.py --profile tts-studio
Nunca escreve log em stdout (só protocolo); diagnóstico vai para stderr.
"""

import json
import os
import sys
import threading
import time

CFG = {
    "chunks": json.loads(os.environ.get("FAKE_ACP_CHUNKS", '["Olá", ", ", "mundo!"]')),
    # comitado (default, = harness real hoje) | deltas | deltas-sem-marca |
    # deltas+comitado | deltas-vazios+comitado. Os modos mapeiam os cenários da
    # anti-duplicação (marcado, sem marca, vazio e comitado junto).
    "modo": os.environ.get("FAKE_ACP_MODO", "comitado"),
    # marca `partial` no chunk comitado (True = marca, ausente = sem marca)
    "marca": os.environ.get("FAKE_ACP_MARCA") == "1",
    # texto do chunk comitado quando ele difere dos deltas (drift de pontuação)
    "comitado": os.environ.get("FAKE_ACP_COMITADO") or None,
    "chunk_delay_s": float(os.environ.get("FAKE_ACP_CHUNK_DELAY_S", "0")),
    "thought": os.environ.get("FAKE_ACP_THOUGHT", ""),
    "permission": os.environ.get("FAKE_ACP_PERMISSION") == "1",
    "prompt_error": json.loads(os.environ.get("FAKE_ACP_PROMPT_ERROR", "null")),
    "die_after_prompts": int(os.environ.get("FAKE_ACP_DIE_AFTER_PROMPTS", "0")),
    "new_error": json.loads(os.environ.get("FAKE_ACP_NEW_ERROR", "null")),
    "late_chunk": os.environ.get("FAKE_ACP_LATE_CHUNK", ""),
    "cancel_settle_s": float(os.environ.get("FAKE_ACP_CANCEL_SETTLE_S", "0")),
    # cancel "ignorado": o prompt continua e só settleia depois de N s (testa o
    # caminho de cancel órfão: sessão reaberta, chunk tardio descartado)
    "cancel_ignora": float(os.environ.get("FAKE_ACP_CANCEL_IGNORA_S", "0")),
    "prewarm_ttft_s": float(os.environ.get("FAKE_ACP_PREWARM_TTFT_S", "0")),
    # o prewarm demora/some: ignora o `session/cancel` e settleia só no fim do
    # atraso (pior caso: o cliente precisa liberar o slot por força e o retry
    # cobre a recusa) — #172
    "prewarm_ignora_cancel": os.environ.get("FAKE_ACP_PREWARM_IGNORA_CANCEL") == "1",
    # o harness REAL recusa um 2º prompt na MESMA sessão com
    # `-32602 a prompt is already in flight`; o fake era frouxo e por isso a
    # corrida prewarm x 1º turno não aparecia na suíte (#162)
    "recusa_em_voo": os.environ.get("FAKE_ACP_RECUSA_EM_VOO", "1") != "0",
    # recusa os N primeiros prompts de cada sessão com -32602 (força a corrida
    # sem depender de timing: é o que o retry do cliente tem de cobrir)
    "recusa_primeira": int(os.environ.get("FAKE_ACP_RECUSA_PRIMEIRA", "0")),
    "sem_config": os.environ.get("FAKE_ACP_SEM_CONFIG") == "1",
}

# sessões com um prompt em voo (o harness libera o slot só no settle)
_em_voo: dict = {}
_em_voo_lock = threading.Lock()
_recusas: dict = {}

_write_lock = threading.Lock()
_id = 0
_prompts = 0
_sessoes = 0
_cancel = threading.Event()
_prompt_vivo = threading.Event()


def log(*partes) -> None:
    print("FAKE_ACP", *partes, file=sys.stderr, flush=True)


def enviar(obj) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def responder(rid, result) -> None:
    enviar({"jsonrpc": "2.0", "id": rid, "result": result})


def erro(rid, code, message, details=None) -> None:
    err = {"code": code, "message": message}
    if details:
        err["data"] = {"details": details}
    enviar({"jsonrpc": "2.0", "id": rid, "error": err})


def atualizar(session_id, update) -> None:
    enviar({"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": session_id, "update": update}})


CONFIG_OPTIONS = [
    {"id": "model", "category": "model", "type": "select",
     "currentValue": '["deepseek-official","deepseek-v4-flash"]',
     "options": [
         {"value": '["deepseek-official","deepseek-flash"]', "name": "DeepSeek Flash"},
         {"value": '["dsflash","deepseek-flash-41"]', "name": "DeepSeek Flash 4.1"},
         {"options": [
             {"value": '["openrouter","z-ai/glm-5.3-flash"]', "name": "GLM 5.3 Flash"},
         ]},
     ]},
    {"id": "reasoning_effort", "category": "thought_level", "type": "select",
     "currentValue": "high",
     "options": [{"value": v} for v in ("off", "low", "high", "max")]},
]
CONFIG_OPTIONS = [] if CFG["sem_config"] else CONFIG_OPTIONS

_model = None
_effort = None


def pede_permissao() -> None:
    """Request server→client: o cliente DEVE negar (reject_once)."""
    global _id
    _id += 1
    rid = _id
    enviar({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
            "params": {"sessionId": "s1", "options": [
                {"optionId": "allow_once", "kind": "allow_once", "name": "Allow"},
                {"optionId": "reject_once", "kind": "reject_once", "name": "Reject"}]}})
    _permission_wait[rid] = None
    limite = time.time() + 5
    while _permission_wait.get(rid) is None and time.time() < limite:
        time.sleep(0.01)
    log("PERMISSAO", _permission_wait.get(rid))


_permission_wait: dict = {}


def _chunk(session_id: str, text: str, parcial) -> None:
    update = {"sessionUpdate": "agent_message_chunk",
              "content": {"type": "text", "text": text}}
    if parcial is not None:
        update["partial"] = parcial
    atualizar(session_id, update)


def _libera_slot(session_id: str) -> None:
    with _em_voo_lock:
        _em_voo[session_id] = False


def turno(session_id: str, rid: int, texto: str) -> None:
    global _prompts
    _prompts += 1
    primeiro = _prompts == 1
    log("PROMPT", json.dumps(texto)[:400])
    _cancel.clear()
    _prompt_vivo.set()
    if CFG["prompt_error"]:
        _prompt_vivo.clear()
        _libera_slot(session_id)
        err = CFG["prompt_error"]
        erro(rid, err.get("code", -32603), err.get("message", "erro"))
        return
    if CFG["permission"]:
        pede_permissao()
    if CFG["thought"]:
        atualizar(session_id, {"sessionUpdate": "agent_thought_chunk",
                               "content": {"type": "text", "text": CFG["thought"]}})
    if primeiro and CFG["prewarm_ttft_s"]:
        # dorme OLHANDO o cancel (salvo quando o teste quer o pior caso), para o
        # `session/cancel` do cliente valer: é o que libera o slot do prewarm
        fim = time.time() + CFG["prewarm_ttft_s"]
        while time.time() < fim:
            if _cancel.is_set() and not CFG["prewarm_ignora_cancel"]:
                log("PREWARM_CANCELADO")
                break
            time.sleep(0.02)

    modo = CFG["modo"]
    if modo == "comitado":
        # comportamento do harness real hoje: UM chunk, no fim, sem marca de parcial
        if CFG["chunk_delay_s"]:
            time.sleep(CFG["chunk_delay_s"])
        if not _cancel.is_set():
            _chunk(session_id, "".join(CFG["chunks"]), None)
    else:
        # "deltas" (pós-patch, sem comitado) e "deltas+comitado" (patch que marca
        # em vez de suprimir, ou pacote revertido no meio)
        for i, parte in enumerate(CFG["chunks"]):
            if _cancel.is_set():
                break
            if CFG["chunk_delay_s"]:
                time.sleep(CFG["chunk_delay_s"])
            if _cancel.is_set():
                break
            # modo "...vazios": os deltas vêm VAZIOS (frame parcial sem texto) → o
            # comitado é a verdade
            enviar = "" if modo.startswith("deltas-vazios") else parte
            # "deltas-sem-marca": patch que projeta os deltas SEM marcar `partial`
            _chunk(session_id, enviar,
                   None if "sem-marca" in modo else True)
            log("DELTA", i)
        envia_comitado = modo.startswith("deltas+") or modo.startswith("deltas-vazios")
        if not _cancel.is_set() and envia_comitado:
            _chunk(session_id, CFG["comitado"] or "".join(CFG["chunks"]),
                   False if CFG["marca"] else None)

    cancelado = _cancel.is_set()
    if cancelado:
        if CFG["cancel_ignora"]:
            # não olha mais o cancel: continua o turno e settleia tarde
            _cancel.clear()
            time.sleep(CFG["cancel_ignora"])
            _chunk(session_id, "TARDIO-IGNORADO", None)
            cancelado = False
        if CFG["cancel_settle_s"]:
            time.sleep(CFG["cancel_settle_s"])
        if CFG["late_chunk"] and cancelado:
            _chunk(session_id, CFG["late_chunk"], None)
    _prompt_vivo.clear()
    _libera_slot(session_id)
    responder(rid, {"stopReason": "cancelled" if cancelado else "end_turn"})
    if CFG["die_after_prompts"] and _prompts >= CFG["die_after_prompts"]:
        time.sleep(0.05)
        log("SAINDO", _prompts)
        os._exit(0)


def tratar(msg: dict) -> None:
    global _model, _effort, _sessoes
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if rid is not None and method is None:          # resposta nossa a um request
        if rid in _permission_wait:
            resultado = msg.get("result") or {}
            escolha = ((resultado.get("outcome") or {}).get("optionId")
                       or ("ERRO" if msg.get("error") else "VAZIO"))
            _permission_wait[rid] = escolha
        return
    if method == "initialize":
        responder(rid, {"protocolVersion": 1,
                        "agentInfo": {"name": "fake-acp", "version": "0"},
                        "agentCapabilities": {"sessionCapabilities": {"resume": {}}}})
    elif method == "session/new":
        if CFG["new_error"]:
            err = CFG["new_error"]
            erro(rid, err.get("code", -32603), err.get("message", "falha no session/new"),
                 (err.get("data") or {}).get("details"))
            return
        _sessoes += 1
        log("SESSAO", f"s{_sessoes}")
        responder(rid, {"sessionId": f"s{_sessoes}", "configOptions": CONFIG_OPTIONS})
    elif method == "session/set_config_option":
        if params.get("configId") == "model":
            _model = params.get("value")
        if params.get("configId") == "reasoning_effort":
            _effort = params.get("value")
        log("CONFIG", params.get("configId"), params.get("value"))
        responder(rid, {"configOptions": CONFIG_OPTIONS})
    elif method == "session/prompt":
        sid = params.get("sessionId", "s1")
        if CFG["recusa_primeira"]:
            with _em_voo_lock:
                feitas = _recusas.get(sid, 0)
                if feitas < CFG["recusa_primeira"]:
                    _recusas[sid] = feitas + 1
            if feitas < CFG["recusa_primeira"]:
                log("RECUSA_FORCADA", sid, feitas + 1)
                erro(rid, -32602, "Invalid params: a prompt is already in flight "
                                  "for this session")
                return
        with _em_voo_lock:
            ocupado = _em_voo.get(sid, False)
            if not ocupado:
                _em_voo[sid] = True
        if ocupado and CFG["recusa_em_voo"]:
            # igual ao harness: erro de INVALID PARAMS, sem fila
            log("EM_VOO recuso prompt na sessao", sid)
            erro(rid, -32602, "Invalid params: a prompt is already in flight "
                              "for this session")
            return
        threading.Thread(target=turno,
                         args=(sid, rid,
                               (params.get("prompt") or [{}])[0].get("text", "")),
                         daemon=True).start()
    elif method == "session/cancel":
        log("CANCEL")
        _cancel.set()
    elif method == "session/close":
        responder(rid, {})
    elif rid is not None:
        responder(rid, {})


def main() -> None:
    for linha in sys.stdin:
        linha = linha.strip()
        if not linha:
            continue
        try:
            msg = json.loads(linha)
        except ValueError:
            log("FRAME INVÁLIDO", linha[:120])
            continue
        tratar(msg)


if __name__ == "__main__":
    log("INICIADO", " ".join(sys.argv[1:]))
    # invariante: o env do filho é MÍNIMO — as chaves do app não vão junto
    log("ENV_CHAVE_APP", "sim" if os.environ.get("TTS_CHAT_API_KEY") else "nao")
    log("ENV_HOME", "sim" if os.environ.get("HOME") else "nao")
    main()