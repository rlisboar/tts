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
* zero (um `statSync` na primeira chamada). Usa `process.getBuiltinModule` para
* não mexer nos imports do arquivo (nada de âncora nova).
*
* ARQUIVO POR PROCESSO + RODÍZIO (#287): o `.jsonl` fixo crescia sem teto e o
* fd nunca fechava; e como o gatilho é um arquivo GLOBAL, um `touch` ligava o
* trace de TODO processo dsh do host. Agora cada processo escreve no seu
* (`...jsonl.<pid>`, até `DSH4A_TRACE_MAX` bytes, girando para `.1`) e o
* desligamento é `rm /tmp/dsh4a-trace.on` (os fd abertos seguem no arquivo do
* processo até ele sair).
*/
const DSH4A_TRACE_MAX = 2 * 1024 * 1024;
let dsh4aFd = void 0;
let dsh4aEscritos = 0;
let dsh4aTraceOn = void 0;
let dsh4aArquivo = void 0;
function dsh4aTraceAbre(fs) {
	dsh4aArquivo = `/tmp/dsh4a-trace.${process.pid}.jsonl`;
	try {
		dsh4aEscritos = fs.statSync(dsh4aArquivo).size;
		if (dsh4aEscritos > DSH4A_TRACE_MAX) {
			fs.renameSync(dsh4aArquivo, `${dsh4aArquivo}.1`);
			dsh4aEscritos = 0;
		}
	} catch { dsh4aEscritos = 0; }
	dsh4aFd = fs.openSync(dsh4aArquivo, "a");
}
function dsh4aTrace(kind, dados) {
	try {
		const fs = process.getBuiltinModule("node:fs");
		if (dsh4aTraceOn === void 0) {
			dsh4aTraceOn = fs.existsSync("/tmp/dsh4a-trace.on");
			if (dsh4aTraceOn) dsh4aTraceAbre(fs);
		}
		if (dsh4aFd === void 0 || dsh4aFd === -1) return;
		const linha = JSON.stringify({
			t: Date.now(),
			pid: process.pid,
			k: kind,
			...dados
		}) + "\n";
		fs.writeSync(dsh4aFd, linha);
		dsh4aEscritos += linha.length;
		if (dsh4aEscritos > DSH4A_TRACE_MAX) {          // rodízio por tamanho
			fs.closeSync(dsh4aFd);
			dsh4aFd = -1;
			dsh4aTraceOn = void 0;                       // reabre (e gira) na próxima
		}
	} catch { /* trace nunca derruba o turno */ }
}
/** Estado por sessão da tentativa em voo: `{msgId, turn, chars}`. */
const dsh4aStreams = /* @__PURE__ */ new Map();
/** Fecha o registro do turno: turno que morre no meio não deixa `msgId` vivo e um
 * `commit` tardio não é marcado com a tentativa abandonada (#287). */
function dsh4aEncerrar(sessionId) {
	dsh4aStreams.delete(sessionId);
}
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
	// o comitado FECHA o turno daquela sessão: nada depois dele pertence a esta
	// tentativa, então o mapa não precisa guardar nada
	if (update.sessionUpdate === "agent_message_chunk") dsh4aEncerrar(sessionId);
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