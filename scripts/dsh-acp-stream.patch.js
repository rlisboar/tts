/* DSH4A_PATCH_V1 — projeção incremental de `agent/assistant-stream` no bridge ACP.
 *
 * POR QUE: `@deepseek-ai/dsh-acp` assina só `session/event` → `assistant/message`
 * (comitado), então o cliente ACP recebe UM `agent_message_chunk` no FIM da
 * geração (medido: 74 s numa resposta de 1,7 k chars). Os deltas do provedor já
 * existem em `agent/assistant-stream` e o bridge simplesmente não os assina.
 *
 * O QUE FAZ: assina `agent/assistant-stream` e emite um `agent_message_chunk` por
 * `text-delta`, marcado `partial: true` (no topo e em `_meta`), e MARCA a mensagem
 * comitada com `partial: false` reusando o mesmo `messageId` sintético da
 * tentativa — é por essa marca que o cliente deduplica (`dsh_client._nao_duplicar`).
 *
 * SEM SUPRESSÃO (de propósito): o comitado continua indo, então um cliente que não
 * conheça a marca não perde texto (no pior caso mostra o texto 2×); quem conhece,
 * não duplica. Marcar é mais seguro que suprimir para o estado do stream.
 *
 * ÂNCORAS: este arquivo é lido por `scripts/dsh-acp-stream-patch.mjs`, que exige
 * cada âncora EXATAMENTE uma vez e falha alto se o pacote mudar de forma.
 *
 * Este texto é a fonte da verdade: o script não edita nada além do que está aqui.
 */

//===@MODULE===
//#region DSH-4a: projeção incremental do stream do assistente (patch local)
/* DSH4A_PATCH_V1 */
/**
* Trace opcional (DSH-4b), LIGADO POR ARQUIVO para não depender de env: se
* `/tmp/dsh4a-trace.on` existir, cada evento cru recebido e cada `session/update`
* emitido vira uma linha JSON em `/tmp/dsh4a-trace.jsonl`. Sem o arquivo, custo
* zero (um `existsSync` na primeira chamada). Usa `process.getBuiltinModule` para
* não mexer nos imports do arquivo (nada de âncora nova).
*/
let dsh4aFd = void 0;
let dsh4aTraceOn = void 0;
function dsh4aTrace(kind, dados) {
	try {
		const fs = process.getBuiltinModule("node:fs");
		if (dsh4aTraceOn === void 0) {
			dsh4aTraceOn = fs.existsSync("/tmp/dsh4a-trace.on");
			dsh4aFd = dsh4aTraceOn ? fs.openSync("/tmp/dsh4a-trace.jsonl", "a") : -1;
		}
		if (dsh4aFd === -1) return;
		fs.writeSync(dsh4aFd, JSON.stringify({
			t: Date.now(),
			pid: process.pid,
			k: kind,
			...dados
		}) + "\n");
	} catch { /* trace nunca derruba o turno */ }
}
/** Estado por sessão da tentativa em voo: `{msgId, turn, chars}`. */
const dsh4aStreams = /* @__PURE__ */ new Map();
/** Abre o registro de uma tentativa; sobrescreve a anterior da mesma sessão. */
function dsh4aIniciar(sessionId, frame) {
	dsh4aStreams.set(sessionId, {
		msgId: `dsh4a-stream:${frame.attemptId}`,
		turn: frame.turn,
		chars: 0
	});
}
/** Um `text-delta` → um `agent_message_chunk` marcado como parcial. */
function dsh4aDeltaUpdate(sessionId, frame) {
	const state = dsh4aStreams.get(sessionId);
	if (state === void 0) return void 0;
	const chunk = frame.chunk;
	if (chunk === void 0 || chunk.type !== "text-delta") return void 0;
	const text = chunk.text;
	if (typeof text !== "string" || text.length === 0) return void 0;
	state.chars += text.length;
	return {
		sessionUpdate: "agent_message_chunk",
		messageId: state.msgId,
		content: {
			type: "text",
			text
		},
		partial: true,
		_meta: { partial: true }
	};
}
/**
* Marca a mensagem comitada de um turno que streamou: mesmo `messageId` dos deltas
* e `partial: false`, para o cliente saber que ESTE é o texto completo.
*/
function dsh4aMarcarComitado(update, sessionId, turn) {
	if (update.sessionUpdate !== "agent_message_chunk") return update;
	const state = dsh4aStreams.get(sessionId);
	if (state === void 0 || state.turn !== turn) return update;
	dsh4aTrace("commit", { turn });
	return {
		...update,
		messageId: state.msgId,
		partial: false,
		_meta: {
			...update._meta,
			partial: false
		}
	};
}
//#endregion
//===@END===

//===@APPLY===
	// DSH-4a: deltas do assistente → `session/update` incremental (ver o patch).
	ctx.on("agent/assistant-stream", ({ agent: streamAgent, frame }) => {
		const record = ownedRecord(streamAgent);
		if (record === void 0) return;
		const sessionId = record.agent.session.id;
		if (frame.type === "start") {
			dsh4aIniciar(sessionId, frame);
			return;
		}
		if (frame.type !== "chunk") return;
		const chunk = frame.chunk;
		dsh4aTrace("frame", {
			idx: frame.index,
			tipo: chunk?.type,
			len: chunk?.text?.length
		});
		const update = dsh4aDeltaUpdate(sessionId, frame);
		if (update === void 0) return;
		dsh4aTrace("emit", {
			idx: frame.index
		});
		notify({
			sessionId,
			update
		});
	});
//===@END===