"""DSH-4a — o aplicador do patch do bridge ACP (`scripts/dsh-acp-stream-patch.mjs`).

Não depende do pacote global instalado: monta um `@deepseek-ai/dsh-acp` falso em
tmp_path com as MESMAS âncoras do 0.1.5-rc.3 e exercita o script pela CLI
(idempotência, backup, rollback, fail-loud na versão e na forma).

O alvo real é `node_modules` global (`$(npm root -g)/@deepseek-ai/dsh/...`), por
isso o script é testado por subprocesso — é assim que ele roda na máquina.
"""
import json
import shutil
import subprocess

from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "dsh-acp-stream-patch.mjs"
PATCH = RAIZ / "scripts" / "dsh-acp-stream.patch.js"
VERSAO = "0.1.5-rc.3"

# Fixture com as três âncoras exatas do pacote real, no formato que o script usa.
FIXTURE = """const sessions = new Map();
const ownedRecord = (agent) => agent;

async function assistantUpdates(ctx, session, event) {
\tconst updates = [];
\treturn updates;
}

class Record {
\tasync onSessionEvent(session, event) {
\t\tif (event.type === "assistant/message") {
\t\t\tconst delivery = this.outputTail.then(async () => {
\t\t\t\tfor (const update of await assistantUpdates(this.ctx, session, event)) await this.notify({
\t\t\t\t\tsessionId: this.agent.session.id,
\t\t\t\t\tupdate
\t\t\t\t});
\t\t\t});
\t\t\tvoid delivery;
\t\t}
\t}
}

export function apply(ctx) {
\tctx.on("session/event", (session, event) => {
\t\tconst record = sessions.get(session.header.id);
\t\tif (record?.ownsSession(session) === true) record.onSessionEvent(session, event);
\t});
}

export const name = "dsh-acp";
"""


def precisa_node():
    if shutil.which("node") is None:
        pytest.skip("node ausente")


def pacote_falso(tmp_path: Path, *, versao: str = VERSAO, fonte: str = FIXTURE):
    raiz = tmp_path / "dsh-acp"
    (raiz / "lib").mkdir(parents=True)
    (raiz / "package.json").write_text(json.dumps({"name": "@deepseek-ai/dsh-acp",
                                                   "version": versao, "type": "module"}))
    (raiz / "lib/index.js").write_text(fonte)
    return raiz


def roda(raiz: Path, *args: str):
    return subprocess.run(["node", str(SCRIPT), "--dir", str(raiz), *args],
                          capture_output=True, text=True)


def test_status_limpo_aplica_idempotente_reverte(tmp_path):
    precisa_node()
    raiz = pacote_falso(tmp_path)
    alvo = raiz / "lib/index.js"
    original = alvo.read_bytes()

    r = roda(raiz, "--status")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "LIMPO" in r.stdout

    r = roda(raiz)
    assert r.returncode == 0, r.stdout + r.stderr
    patchado = alvo.read_text()
    assert "DSH4A_PATCH_V1" in patchado
    assert 'ctx.on("agent/assistant-stream"' in patchado
    assert "partial: true" in patchado and "partial: false" in patchado
    assert "dsh4aMarcarComitado(update, this.agent.session.id, event.data.turn)" in patchado
    # o backup é byte a byte o original e fica ao lado, com a versão no nome.
    assert (raiz / f"lib/index.js.orig-{VERSAO}").read_bytes() == original

    r = roda(raiz, "--status")
    assert r.returncode == 0 and "PATCHADO" in r.stdout

    # idempotente: nada muda
    antes = alvo.read_bytes()
    r = roda(raiz)
    assert r.returncode == 0 and "já patchado" in r.stdout
    assert alvo.read_bytes() == antes

    # rollback num comando
    r = roda(raiz, "--revert")
    assert r.returncode == 0, r.stdout + r.stderr
    assert alvo.read_bytes() == original
    assert roda(raiz, "--status").returncode == 1


def test_reapply_com_o_patch_ja_aplicado_nao_desfaz(tmp_path):
    """Regressão (achada na #160): `--reapply` revertia, dizia "já patchado" e
    deixava o arquivo LIMPO — o cache do conteúdo era lido antes do revert."""
    precisa_node()
    raiz = pacote_falso(tmp_path)
    assert roda(raiz).returncode == 0
    alvo = raiz / "lib/index.js"
    patchado = alvo.read_text()
    r = roda(raiz, "--reapply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "patch aplicado" in r.stdout
    assert alvo.read_text() == patchado
    assert roda(raiz, "--status").returncode == 0


def test_reapply_apos_reinstalacao(tmp_path):
    """`npm install -g` recria o node_modules: --reapply tem de repor o patch."""
    precisa_node()
    raiz = pacote_falso(tmp_path)
    assert roda(raiz).returncode == 0
    alvo = raiz / "lib/index.js"
    patchado = alvo.read_text()
    alvo.write_text(FIXTURE)          # simula o pacote recriado do zero
    r = roda(raiz, "--reapply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert alvo.read_text() == patchado


def test_dry_run_nao_escreve(tmp_path):
    precisa_node()
    raiz = pacote_falso(tmp_path)
    alvo = raiz / "lib/index.js"
    original = alvo.read_bytes()
    r = roda(raiz, "--dry-run")
    assert r.returncode == 0 and "âncoras em" in r.stdout
    assert alvo.read_bytes() == original
    assert not (raiz / f"lib/index.js.orig-{VERSAO}").exists()


def test_versao_nao_verificada_falha_alto_sem_force(tmp_path):
    precisa_node()
    raiz = pacote_falso(tmp_path, versao="9.9.9")
    alvo = raiz / "lib/index.js"
    original = alvo.read_bytes()
    r = roda(raiz)
    assert r.returncode == 2
    assert "9.9.9" in r.stderr and "fora da lista verificada" in r.stderr
    assert alvo.read_bytes() == original
    # --status relata mesmo em versão não verificada; --force aplica
    assert "não verificada" in roda(raiz, "--status").stdout
    assert roda(raiz, "--force").returncode == 0


def test_forma_do_pacote_mudou_falha_alto(tmp_path):
    precisa_node()
    fonte = FIXTURE.replace("async function assistantUpdates(ctx, session, event) {",
                            "async function assistantUpdatesRenomeado(ctx, session, event) {")
    raiz = pacote_falso(tmp_path, fonte=fonte)
    alvo = raiz / "lib/index.js"
    original = alvo.read_bytes()
    r = roda(raiz, "--force")
    assert r.returncode == 2, r.stdout + r.stderr
    assert 'âncora "assistantUpdates" casou 0×' in r.stderr
    assert alvo.read_bytes() == original          # não edita errado
    assert not (raiz / f"lib/index.js.orig-{VERSAO}").exists()


def test_fora_do_padrao_do_for_do_assistantUpdates(tmp_path):
    """A terceira âncora: se o corpo do `for` mudar, também falha em vez de adivinhar."""
    precisa_node()
    fonte = FIXTURE.replace("sessionId: this.agent.session.id,\n\t\t\t\t\tupdate",
                            "sessionId: this.agent.session.id,\n\t\t\t\t\tupdateAntigo")
    raiz = pacote_falso(tmp_path, fonte=fonte)
    alvo = raiz / "lib/index.js"
    r = roda(raiz, "--force")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "corpo do `for`" in r.stderr
    assert alvo.read_text() == fonte


def test_help_sai_com_zero_e_lista_as_flags_sem_tocar_em_pacote():
    """`--help` não pode exigir o pacote instalado nem descobrir `npm root -g`."""
    precisa_node()
    r = subprocess.run(["node", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    for f in ("--status", "--dry-run", "--revert", "--reapply", "--help", "--force"):
        assert f in r.stdout, f"{f} ausente no --help"
    # o texto tem de dizer o que o --reapply FAZ (reverte se houver backup, aplica)
    assert "garante PATCHADO" in r.stdout
    # e o trace tem de estar documentado no próprio --help
    assert "/tmp/dsh4a-trace.on" in r.stdout and "existsSync" in r.stdout


def test_reapply_sem_backup_aplica_direto(tmp_path):
    """Depois de `npm install -g` o patch E o backup somem: --reapply aplica."""
    precisa_node()
    raiz = pacote_falso(tmp_path)
    assert roda(raiz).returncode == 0
    alvo = raiz / "lib/index.js"
    patchado = alvo.read_text()
    alvo.write_text(FIXTURE)                            # node_modules recriado
    (raiz / f"lib/index.js.orig-{VERSAO}").unlink()
    r = roda(raiz, "--reapply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sem backup" in r.stdout and "patch aplicado" in r.stdout
    assert alvo.read_text() == patchado                 # mesmo patch, byte a byte
    assert (raiz / f"lib/index.js.orig-{VERSAO}").exists()   # backup do original


def test_reapply_ja_patchado_sem_backup_avisa_que_revert_vai_falhar(tmp_path):
    """Já patchado e sem backup: no-op, mas o aviso tem de sair (o original se perdeu)."""
    precisa_node()
    raiz = pacote_falso(tmp_path)
    assert roda(raiz).returncode == 0
    (raiz / f"lib/index.js.orig-{VERSAO}").unlink()
    r = roda(raiz, "--reapply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "já patchado" in r.stdout and "ATENÇÃO" in r.stdout
    assert roda(raiz, "--revert").returncode == 2


def test_revert_sem_backup_falha_alto(tmp_path):
    """`--revert` é o único que EXIGE backup; sem ele, rc=2 e nada escrito."""
    precisa_node()
    raiz = pacote_falso(tmp_path)
    r = roda(raiz, "--revert")
    assert r.returncode == 2
    assert "não há backup" in r.stderr


def test_o_readme_do_scripts_documenta_as_flags():
    texto = (RAIZ / "scripts/README.md").read_text()
    for f in ("--status", "--dry-run", "--revert", "--reapply"):
        assert f in texto
    assert "/tmp/dsh4a-trace.on" in texto


def test_o_patch_textual_tem_as_duas_secoes():
    texto = PATCH.read_text()
    assert texto.count("//===@MODULE===") == 1
    assert texto.count("//===@APPLY===") == 1
    assert "DSH4A_PATCH_V1" in texto