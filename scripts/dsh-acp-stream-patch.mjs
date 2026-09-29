#!/usr/bin/env node
// DSH-4a — aplicador do patch do bridge ACP (`@deepseek-ai/dsh-acp`).
//
// Idempotente, reversível e falha-ALTO: não usa `sed` cego. Cada âncora do
// arquivo tem de casar EXATAMENTE uma vez; se o pacote mudar de forma, o script
// sai com código 2 e não escreve nada (em vez de editar errado).
//
// Uso (o MESMO texto sai em `--help`; detalhes em `scripts/README.md`):
//   --status    só relata: rc=0 PATCHADO · rc=1 LIMPO (não escreve)
//   --dry-run   mostra as âncoras e o diff de linhas; não escreve
//   (nenhuma)   aplica. Se já estiver patchado: no-op idempotente
//   --revert    ROLLBACK: restaura o backup (precisa do backup existir)
//   --reapply   garante PATCHADO: reverte (se houver backup) e aplica de novo.
//               É o remédio depois de `npm install -g @deepseek-ai/dsh`, que
//               recria o node_modules e apaga patch E backup.
//
//   --dir <caminho>   força o diretório do pacote (default: descoberta automática)
//   --force           aceita versão fora da lista verificada (mesmas âncoras)
//   --help            este texto
//
// ATENÇÃO (o bug que a doc antiga escondia): `--reapply` com o patch JÁ aplicado
// tem de continuar PATCHADO — não é "revert + desiste". Há teste de regressão.

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const USO = `aplicador do patch DSH-4a no bridge ACP (@deepseek-ai/dsh-acp)

  node scripts/dsh-acp-stream-patch.mjs [flags]

  --status    só relata: rc=0 PATCHADO · rc=1 LIMPO (não escreve)
  --dry-run   mostra as âncoras e o diff de linhas (não escreve)
  (nenhuma)   aplica; se já patchado, no-op idempotente
  --revert    ROLLBACK: restaura o backup ao lugar
  --reapply   garante PATCHADO (reverte se houver backup, aplica de novo);
              use depois de \`npm install -g @deepseek-ai/dsh\`
  --dir <dir> força o diretório do pacote
  --force     aceita versão fora da lista verificada
  --help      este texto

Trace (opcional, ligado por ARQUIVO): se /tmp/dsh4a-trace.on existir, o bridge
grava /tmp/dsh4a-trace.jsonl (um JSONL por evento cru recebido e por
session/update emitido). Desligado, o custo é um existsSync na 1ª chamada.`;

const AQUI = dirname(fileURLToPath(import.meta.url));
const PATCH = join(AQUI, "dsh-acp-stream.patch.js");
const MARKER = "DSH4A_PATCH_V1";
/** Versões verificadas com estes anchors. `--force` libera outras. */
const SUPORTADAS = ["0.1.5-rc.3"];

const argv = process.argv.slice(2);
const flag = (n) => argv.includes(n);
const valor = (n, d) => {
	const i = argv.indexOf(n);
	return i >= 0 ? argv[i + 1] : d;
};

const falha = (msg, extra = []) => {
	process.stderr.write(`FALHA: ${msg}\n`);
	for (const l of extra) process.stderr.write(`  ${l}\n`);
	process.exit(2);
};

if (flag("--help") || flag("-h")) {
	process.stdout.write(`${USO}\n`);
	process.exit(0);
}

// -------------------------------------------------------------------- alvo
function acharPacote() {
	const forcado = valor("--dir", "");
	if (forcado) return forcado;
	const raiz = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
	const candidatos = [
		join(raiz, "@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-acp"), // caso real (dep aninhada do `dsh`)
		join(raiz, "@deepseek-ai/dsh-acp")                                 // caso hoisted
	];
	const achado = candidatos.find((p) => existsSync(join(p, "lib/index.js")));
	if (!achado) {
		falha("não achei @deepseek-ai/dsh-acp instalado", candidatos.map((p) => `tentei: ${p}`));
	}
	return achado;
}

const dir = acharPacote();
const alvo = join(dir, "lib/index.js");
const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
const versao = pkg.version;
const backup = join(dir, `lib/index.js.orig-${versao}`);

const versaoVerificada = SUPORTADAS.includes(versao);
/** `--status` sempre relata; quem aplica é que recusa versão não verificada. */
if (!versaoVerificada && !flag("--force") && !flag("--status")) {
	falha(`versão ${versao} fora da lista verificada (${SUPORTADAS.join(", ")})`,
		["confira se `assistantUpdates()`/`ctx.on(\"session/event\")` continuam com a mesma forma",
		 "se continuarem: rode de novo com --force"]);
}

const sha = (p) => createHash("sha256").update(readFileSync(p)).digest("hex").slice(0, 12);
/** Lido a CADA passo: `--reapply` reverte antes de aplicar, então não pode cachear. */
const ler = () => readFileSync(alvo, "utf8");
const estaPatchado = () => ler().includes(MARKER);

if (flag("--status")) {
	const onde = estaPatchado() ? "PATCHADO" : "LIMPO (sem o patch)";
	process.stdout.write(`dsh-acp ${versao}${versaoVerificada ? "" : " (não verificada)"} · ${alvo}\n${onde} · sha256 ${sha(alvo)}\n`);
	process.stdout.write(`backup: ${existsSync(backup) ? `existe (${backup})` : "não existe"}\n`);
	process.exit(estaPatchado() ? 0 : 1);
}

const secoes = readFileSync(PATCH, "utf8").split(/^\/\/===@(\w+)===$/m);
const secao = (nome) => {
	const i = secoes.indexOf(nome);
	if (i < 0) falha(`seção ${nome} não existe em ${PATCH}`);
	return secoes[i + 1].trimEnd();
};

// -------------------------------------------------------------------- revert
function reverter() {
	if (!existsSync(backup)) falha(`não há backup para reverter (${backup})`);
	copyFileSync(backup, alvo);
	process.stdout.write(`revertido de ${backup}\n`);
}

// -------------------------------------------------------------------- aplicar
function aplicar() {
	if (estaPatchado()) {
		process.stdout.write(`já patchado (${MARKER}, v${versao}) — idempotente, nada a fazer\n`);
		// Backup é o ORIGINAL: se o arquivo já está patchado, não dá para
		// reconstruí-lo — avisa porque o `--revert` depende dele.
		if (!existsSync(backup)) {
			process.stdout.write(`  ATENÇÃO: sem ${backup} — \`--revert\` vai falhar (o arquivo fica como está)\n`);
		}
		return;
	}
	// 1) âncoras: cada uma tem de casar exatamente uma vez (por linha, ignorando
	//    indentação — o pacote é reindentado entre builds, o texto não).
	const conteudo = ler();
	const linhas = conteudo.split("\n");
	const idxUnico = (rotulo, texto) => {
		const achados = [];
		linhas.forEach((l, i) => {
			if (l.trim() === texto) achados.push(i);
		});
		if (achados.length !== 1) {
			falha(`âncora "${rotulo}" casou ${achados.length}× (esperado 1)`,
				[`texto procurado: ${texto}`, `linhas: ${JSON.stringify(achados)}`]);
		}
		return achados[0];
	};

	const iAntesDe = idxUnico("assistantUpdates", "async function assistantUpdates(ctx, session, event) {");
	const iEvento = idxUnico("ctx.on(session/event)", 'ctx.on("session/event", (session, event) => {');
	const iFor = idxUnico("for do assistantUpdates", "for (const update of await assistantUpdates(this.ctx, session, event)) await this.notify({");
	if (linhas[iFor + 1].trim() !== "sessionId: this.agent.session.id," || linhas[iFor + 2].trim() !== "update") {
		falha("o corpo do `for` do assistantUpdates mudou de forma",
			[`linha ${iFor + 2}: ${JSON.stringify(linhas[iFor + 1])}`, `linha ${iFor + 3}: ${JSON.stringify(linhas[iFor + 2])}`]);
	}
	// `});` aparece muitas vezes: procuro o primeiro depois do session/event cujo
	// bloco fecha na indentação de 1 tab.
	const indentEvento = linhas[iEvento].slice(0, linhas[iEvento].length - linhas[iEvento].trimStart().length);
	const iFechaEvento = linhas.findIndex((l, i) => i > iEvento && l === `${indentEvento}});`);
	if (iFechaEvento < 0) falha("não achei o fim do bloco ctx.on(\"session/event\")", [`linha ${iEvento + 1}`]);

	const saida = [];
	for (let i = 0; i < linhas.length; i += 1) {
		if (i === iAntesDe) saida.push(secao("MODULE"), "");
		if (i === iFechaEvento + 1) saida.push(secao("APPLY"), "");
		if (i === iFor + 2) {
			saida.push(`${linhas[i].slice(0, linhas[i].length - linhas[i].trimStart().length)}update: dsh4aMarcarComitado(update, this.agent.session.id, event.data.turn)`);
			continue;
		}
		saida.push(linhas[i]);
	}
	const novo = saida.join("\n");

	if (flag("--dry-run")) {
		process.stdout.write(`dry-run: ${alvo}\n  âncoras em ${[iAntesDe, iFechaEvento, iFor + 2].map((n) => n + 1).join(", ")}\n  ${linhas.length} → ${novo.split("\n").length} linhas\n`);
		return;
	}

	if (!existsSync(backup)) copyFileSync(alvo, backup);
	writeFileSync(alvo, novo);
	// verificação: o arquivo tem de continuar importável (deps resolvem no lugar).
	try {
		execFileSync("node", ["--check", alvo], { stdio: "pipe" });
	} catch (e) {
		copyFileSync(backup, alvo);
		falha(`o arquivo patchado não compila — revertido do backup`, [`${e.stderr ?? e}`]);
	}
	process.stdout.write(`patch aplicado · dsh-acp ${versao} · ${alvo}\n`);
	process.stdout.write(`  backup: ${backup} (${sha(backup)})\n  patchado: sha256 ${sha(alvo)} · marcador ${MARKER}\n`);
}

if (flag("--revert")) reverter();
else if (flag("--reapply")) {
	// `--reapply` garante PATCHADO. Reverter só faz sentido com backup no lugar:
	// depois de `npm install -g` o node_modules (e o backup) são recriados, então
	// o caminho normal lá é aplicar direto sobre o arquivo limpo.
	if (existsSync(backup)) reverter();
	else process.stdout.write(`sem backup (node_modules recriado) — aplicando direto\n`);
	aplicar();
} else {
	aplicar();
}