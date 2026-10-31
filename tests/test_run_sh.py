"""Contrato do runner das suítes `.sh` (#387).

O runner só vale enquanto a lista estiver certa: suíte renomeada, apagada ou
esquecida fora dos estágios não pode passar despercebida. Este arquivo cobra o
contrato ESTÁTICO (não executa as suítes — quem executa é o `run_sh.sh`).

Cresceu junto do `tests/SHELL-SUITES.md`, que é a explicação; se um dos dois
divergir, o teste cai.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
import threading
import time
import pathlib
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RUNNER = AQUI / "run_sh.sh"
DOC = AQUI / "SHELL-SUITES.md"

# `serial.sh` é o caso de teste em si, invocado por `serial_lock.sh` (a suíte é o
# wrapper com relógio). `run_sh.sh` é o próprio runner.
AJUDANTES = {"serial.sh", "run_sh.sh"}


def _estagios() -> dict[str, list[str]]:
    """Lê os arrays do runner. O runner é DADO aqui: nada de duplicar a lista."""
    fonte = RUNNER.read_text()
    estagios: dict[str, list[str]] = {}
    for nome in ("RAPIDO", "NAVEGADOR", "INSTANCIA"):
        m = re.search(rf"^{nome}=\((.*?)\)$", fonte, re.M | re.S)
        assert m, f"array {nome} sumiu do run_sh.sh"
        estagios[nome] = m.group(1).split()
    return estagios


def test_todo_nome_da_lista_existe_e_e_executavel():
    for estagio, nomes in _estagios().items():
        assert nomes, f"estágio {estagio} vazio"
        for nome in nomes:
            alvo = AQUI / f"{nome}.sh"
            assert alvo.is_file(), f"{estagio}: {nome}.sh não existe"
            assert alvo.stat().st_mode & 0o111, f"{estagio}: {nome}.sh sem bit de execução"


def test_suite_nova_nao_fica_fora_dos_estagios():
    """Toda `.sh` de `tests/` está em UM estágio (ou é ajudante declarado)."""
    dentro = {f"{n}.sh" for nomes in _estagios().values() for n in nomes}
    no_disco = {p.name for p in AQUI.glob("*.sh")}
    faltando = no_disco - dentro - AJUDANTES
    assert not faltando, f"suíte fora de qualquer estágio: {sorted(faltando)}"


def test_nenhuma_suite_esta_em_dois_estagios():
    vistos: dict[str, str] = {}
    for estagio, nomes in _estagios().items():
        for nome in nomes:
            assert nome not in vistos, f"{nome} está em {vistos.get(nome)} e em {estagio}"
            vistos[nome] = estagio


def test_ajudante_declarado_e_usado_e_nao_e_suite():
    for nome in AJUDANTES - {"run_sh.sh"}:
        assert (AQUI / nome).is_file(), f"ajudante {nome} sumiu"
        quem_chama = [p.name for p in AQUI.glob("*.sh")
                      if nome in p.read_text() and p.name != nome]
        assert quem_chama, f"{nome} está como ajudante mas ninguém o invoca"


def test_doc_explica_os_tres_estagios():
    doc = DOC.read_text()
    for estagio in ("rapido", "navegador", "instancia"):
        assert estagio in doc, f"o doc não fala do estágio {estagio}"


def test_runner_isola_log_e_tem_teto_portatil():
    fonte = RUNNER.read_text()
    # log por execução: duas rodadas simultâneas não truncam uma a outra (#243/#224)
    assert "mktemp -d" in fonte
    # `timeout` do coreutils NÃO existe no macOS: o teto é do perl
    assert re.search(r"perl -e 'alarm shift", fonte)
    assert re.search(r"^\s*(?:TETO|_com_teto)", fonte, re.M)


# ───────────────────────────── execução: BASE e teto (#535) ──────────────────
# Os testes acima são estáticos. Os de baixo RODAM o runner com suítes-sonda de
# `tests/fixtures/run_sh/` (via `SUITES=`), sem tocar nas listas reais. Precisam de
# bash + perl (a mesma dependência do `rapido`).

SONDAS = AQUI / "fixtures" / "run_sh"


def _rodar(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
            # as sondas sobem app: sem isso elas escrevem no `state/metrics` da
            # árvore (o app herda o ambiente mínimo e o guard do conftest acusa)
            "TTS_METRICS_DIR": os.environ.get("TTS_METRICS_DIR", "")}
    base.update(env or {})
    return subprocess.run(["bash", str(RUNNER), *args], cwd=str(AQUI.parent),
                          capture_output=True, text=True, timeout=120,
                          env={**base, "TTS_SERIAL_ESPERA": "60"})


def _sonda_saida(tmp_path: Path) -> str:
    p = tmp_path / "sonda.txt"
    return str(p)


def test_navegador_nao_exporta_base_nem_os_escapes_do_dono(tmp_path):
    """#535: o estágio `navegador` é auto-contido e NÃO pode encostar no app do
    dono. Com BASE exportado, `admin_ui_flow`/`cfg_sujo_e_erros` deixavam de subir
    a própria instância e escreviam no `settings.json` dele."""
    saida = _sonda_saida(tmp_path)
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_env.sh"),
                                 "SONDA_SAIDA": saida,
                                 "BASE": "http://127.0.0.1:7860",
                                 "ADMIN_UI_NO_DONO": "", "NO_DONO": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    texto = Path(saida).read_text()
    assert "BASE=[<vazio>]" in texto, texto
    assert "ADMIN_UI_NO_DONO=[<vazio>]" in texto and "NO_DONO=[<vazio>]" in texto, texto


def test_instancia_exporta_base_e_liga_os_escapes_no_7860(tmp_path):
    """O outro lado: em `instancia` o alvo É o app de pé — BASE e escapes valem
    (o `TTS_RUN_SH_PULA_PRECHECK` evita precisar do app só para o teste)."""
    saida = _sonda_saida(tmp_path)
    r = _rodar("instancia", env={"SUITES": str(SONDAS / "sonda_env.sh"),
                                 "SONDA_SAIDA": saida,
                                 "BASE": "http://127.0.0.1:7860",
                                 "ADMIN_UI_NO_DONO": "", "NO_DONO": "",
                                 "TTS_RUN_SH_PULA_PRECHECK": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    texto = Path(saida).read_text()
    assert "BASE=[http://127.0.0.1:7860]" in texto, texto
    assert "ADMIN_UI_NO_DONO=[1]" in texto and "NO_DONO=[1]" in texto, texto


def test_suite_que_dorme_alem_do_teto_reprova_pelo_alarme(tmp_path):
    """Mordida do teto de TRABALHO: 6 s de sono com teto de 2 s tem de reprovar."""
    r = _rodar("rapido", env={"SUITES": str(SONDAS / "trava_lenta.sh"),
                              "TETO_S": "2", "SONDA_SONO": "6",
                              "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FALHA" in r.stdout and "trava_lenta" in r.stdout
    assert "estourado DEPOIS da trava" in r.stdout, r.stdout


def test_tempo_esperando_a_trava_nao_come_o_teto(tmp_path):
    """O defeito medido: com o teto contado da largada, a fila da trava matava a
    suíte antes de ela trabalhar. Presa a trava por 3 s, 0,5 s de trabalho com
    teto de 3 s tem de passar (com 2 s era fino sob carga: o próprio teste segura
    um `sleep 30` na trava)."""
    trava = tmp_path / "lock"
    trava.mkdir()
    dono = subprocess.Popen(["sleep", "30"])
    (trava / "pid").write_text(str(dono.pid))
    (trava / "qual").write_text("dona-de-teste")
    threading.Timer(3.0, lambda: subprocess.run(["rm", "-rf", str(trava)])).start()
    try:
        r = _rodar("rapido", env={"SUITES": str(SONDAS / "trava_curta.sh"),
                                  "TETO_S": "3",
                                  "TTS_SERIAL_LOCK": str(trava)})
    finally:
        dono.kill()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok" in r.stdout and "trava_curta" in r.stdout


# --- #535 (achado do PM): o runner NOMEIA quem escreveu no estado do dono ────
# O "dono" é uma CÓPIA (`TTS_DONO_DIR`): a rodada do pytest não encosta mais no
# `settings.json` real (ele ficou com 17 linhas em branco no gate do #535).
def _dono(tmp_path: Path) -> str:
    """Diretório de mentira com um `settings.json` SINTÉTICO.

    Sintético e não cópia do arquivo do dono: o worktree não tem o arquivo dele
    (#659) e o conteúdo aqui só precisa existir para a sonda mexer — o que se mede
    é o runner ver a mudança, não os valores."""
    dono = tmp_path / "dono"
    dono.mkdir()
    (dono / "settings.json").write_text('{"pre_prompt": "", "model": "sintetico"}')
    return str(dono)


def test_runner_nomeia_a_suite_que_escreveu_no_estado_do_dono(tmp_path):
    """Sem atribuição, "o settings mudou" é fato sem autor — foi o que travou a
    investigação de 01:24. A sonda reescreve o `settings.json` do dono de mentira."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path)})
    assert "estado REAL do dono" in r.stdout, r.stdout
    assert "suja_o_dono.sh escreveu settings.json" in r.stdout, r.stdout
    # e a sonda SOZINHA, sem a env, tem de RECUSAR (nada de fallback para a raiz)
    env = {k: v for k, v in os.environ.items() if k != "TTS_DONO_DIR"}
    d = subprocess.run(["bash", str(SONDAS / "suja_o_dono.sh")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert d.returncode == 2, (d.returncode, d.stdout, d.stderr)
    assert "TTS_DONO_DIR" in d.stderr, d.stderr
    # sem o knob é AVISO: a suíte em si passou
    assert r.returncode == 0, r.stdout


def test_knob_exige_dono_intocado_vira_falha(tmp_path):
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 1, r.stdout
    assert "TTS_EXIGE_DONO_INTOCADO=1" in r.stdout, r.stdout


def test_metrics_do_dono_vira_nota_e_nao_reprova_o_knob(tmp_path):
    """`state/metrics` é histórico ANEXADO por requisição: o app do PRÓPRIO dono
    engorda o arquivo pelo polling da tela (medido: +171 B em 40 s com o dono
    intocado). No veredito do knob isso acusava suíte inocente (#635)."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_metrics.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 0, r.stdout
    assert "state/metrics` mudou" in r.stdout or "state/metrics mudou" in r.stdout, r.stdout
    assert "não reprova" in r.stdout, r.stdout
    assert "escreveu state/metrics/" not in r.stdout.split("nota:")[-1].replace("MUDOU", ""), r.stdout


def test_settings_do_dono_continua_reprovando_o_knob(tmp_path):
    """Controle do anterior: os três arquivos de leitura-escrita seguem valendo."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 1, r.stdout
    assert "settings.json escreveu" in r.stdout or "escreveu settings.json" in r.stdout, r.stdout


def test_reescrita_do_mesmo_conteudo_nao_reprova_o_knob(tmp_path):
    """#635 (decisão do PM): o app do dono, num rev antigo, regrava o
    `settings.json` com o MESMO conteúdo — o mtime muda e o sha256 não. Reescrita
    idêntica não é vazamento: vira nota, não reprovação."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "reescreve_o_dono.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 0, r.stdout
    assert "MESMO conteúdo" in r.stdout, r.stdout
    assert "não reprova" in r.stdout, r.stdout


def test_suite_que_nao_escreve_nao_aparece(tmp_path):
    """Controle: suíte limpa não gera linha nenhuma."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_env.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path),
                                 "SONDA_SAIDA": str(tmp_path / "s.txt")})
    assert "estado REAL do dono" not in r.stdout, r.stdout


def test_atribuicao_sob_j3_nomeia_a_certa_e_nao_as_vizinhas(tmp_path):
    """#591: com `-j 3` a janela de uma suíte longa engole a escrita de outra e a
    acusação por janela (3 rodadas do gate, 4 conjuntos de culpados) mentia. O
    veredito é global; a dica tem de apontar a suíte que escreveu e declarar as
    co-rodantes como tais."""
    r = _rodar("navegador", "-j", "3",
               env={"SUITES": f"{SONDAS}/zz_parada.sh {SONDAS}/zz_parada.sh "
                              f"{SONDAS}/suja_o_dono.sh",
                    "SONDA_SONO": "2",
                    "TTS_DONO_DIR": _dono(tmp_path),
                    "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "suja_o_dono.sh escreveu settings.json" in r.stdout, r.stdout
    # e a sonda SOZINHA, sem a env, tem de RECUSAR (nada de fallback para a raiz)
    env = {k: v for k, v in os.environ.items() if k != "TTS_DONO_DIR"}
    d = subprocess.run(["bash", str(SONDAS / "suja_o_dono.sh")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert d.returncode == 2, (d.returncode, d.stdout, d.stderr)
    assert "TTS_DONO_DIR" in d.stderr, d.stderr
    # as duas paradas não escreveram: no máximo aparecem como co-rodantes
    escritores = [ln.strip() for ln in r.stdout.splitlines()
                  if " escreveu " in ln and "co-rodavam" not in ln]
    assert escritores and all("suja_o_dono.sh" in ln for ln in escritores), escritores
    assert "co-rodavam no instante:" in r.stdout, r.stdout
    assert "zz_parada.sh" in r.stdout.split("co-rodavam no instante:")[1]


# --- #591: o caminho do estado vai por ENV, ANTES do `import app` ────────────
LAUNCHERS = ["admin_ui_flow.sh", "cfg_sujo_e_erros.sh", "live_barge_rep.sh"]


def test_launchers_passam_os_caminhos_por_env_e_nao_por_atributo():
    """Patch de ATRIBUTO depois do `import app` é tarde: o import já rodou
    `_save_settings()` (materializa chave default) no arquivo do DONO."""
    for nome in LAUNCHERS:
        fonte = (AQUI / nome).read_text()
        for var in ("TTS_SETTINGS_PATH", "TTS_APIKEYS_PATH", "TTS_LEGACY_APIKEY_PATH"):
            # `VAR=… cmd` no shell ou `"VAR": …` no `env` do Popen: tanto faz, o
            # que se cobra é o caminho ir pelo AMBIENTE, antes do import.
            assert re.search(rf'{var}["\s]*[:=]', fonte), f"{nome} não passa {var} pelo ambiente"
        for atributo in ("app.SETTINGS_PATH =", "app.APIKEYS_PATH =",
                         "app.LEGACY_APIKEY_PATH ="):
            assert atributo not in fonte, f"{nome} ainda faz `{atributo}` depois do import"


def test_import_com_os_caminhos_no_ambiente_nao_toca_o_settings_do_dono(tmp_path):
    """Mordida do defeito: o `import app` LÊ o estado do dono quando os caminhos só
    são trocados depois dele. Com o env ANTES, a leitura vem do isolamento, as
    escritas seguintes caem lá e os arquivos do dono ficam intactos."""
    saida = tmp_path / "veredito.txt"
    # `TTS_EXIGE_DONO_INTOCADO=1`: se a sonda encostar no estado da árvore, o
    # próprio runner reprova — a célula prova a intocabilidade, não confia nela
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_import_app.sh"),
                                 "SONDA_MODO": "env", "SONDA_SAIDA": str(saida),
                                 "SONDA_ISO": str(tmp_path),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "estado lido do isolamento" in saida.read_text()


def test_o_defeito_do_atributo_ainda_morde(tmp_path):
    """Controle da célula acima: do jeito antigo (patch por atributo DEPOIS do
    import) o processo nasce com o estado do DONO na RAM — a sonda não passa por
    acidente. Desde o #601 o import não grava, então o sintoma é a LEITURA."""
    saida = tmp_path / "veredito.txt"
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_import_app.sh"),
                                 "SONDA_MODO": "atributo", "SONDA_SAIDA": str(saida),
                                 "SONDA_ISO": str(tmp_path),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "estado do DONO entrou na RAM" in saida.read_text()



# --- #621: suíte que MUTA arquivo versionado tem de mutar uma CÓPIA -------------
def _arvore_descartavel(tmp_path: Path) -> Path:
    """Worktree descartável com venv, pesos e `voices` ligados por symlink.

    Mesma montagem do `tools/worktrees.sh`, sem depender dele: a célula não pode
    depender do script que ela vigia."""
    wt = tmp_path / "arvore"
    subprocess.run(["git", "-C", str(AQUI.parent), "worktree", "add", "-q", "--detach",
                    str(wt), "HEAD"], check=True, capture_output=True)
    # o `.venv-mlx` é entrada RASTREADA (symlink): num worktree o
    # checkout já a materializa — recriar dá FileExistsError. Só liga quando falta.
    if not (wt / ".venv-mlx").exists() and not (wt / ".venv-mlx").is_symlink():
        (wt / ".venv-mlx").symlink_to(AQUI.parent / ".venv-mlx")
    if (AQUI.parent / "voices").is_dir() and not (wt / "voices").is_symlink() \
            and not (wt / "voices").exists():
        (wt / "voices").symlink_to(AQUI.parent / "voices")
    for linha in (AQUI.parent / "models" / "sha256sums.txt").read_text().splitlines():
        partes = linha.split("  ", 1)
        if len(partes) != 2:
            continue
        origem = AQUI.parent / partes[1]
        if origem.is_file() and not (wt / partes[1]).exists():
            (wt / partes[1]).parent.mkdir(parents=True, exist_ok=True)
            (wt / partes[1]).symlink_to(origem)
    return wt


def test_live_lock_freeze_muta_uma_copia_e_deixa_a_arvore_intacta(tmp_path):
    """A suíte reverte o `app.py` para medir a mordida. Feita na árvore
    COMPARTILHADA, a reversão fica visível por ~10 s e um commit de terceiro a
    commitaria (suspeita nº 1 dos "códigos que sumiram", #573).

    A célula roda o cenário numa CÓPIA descartável: a guarda não pode cometer o
    defeito que vigia (o `#670` pegou a versão anterior escrevendo no `app.py` da
    raiz). Na cópia, dois fatos objetivos: o SENTINELA no fim do arquivo sobrevive
    (prova que o `cp` de foto velha não sobrescreveu) e o bloco do `busy` volta à
    forma do FIX (prova que a reversão não ficou para trás). A raiz, por fora, tem
    de ficar com sha256 E mtime idênticos e sem o sentinela."""
    raiz_app = AQUI.parent / "app.py"

    def _impressao(arq: Path) -> tuple[str, int]:
        import hashlib
        return (hashlib.sha256(arq.read_bytes()).hexdigest(), arq.stat().st_mtime_ns)

    antes = _impressao(raiz_app)
    wt = _arvore_descartavel(tmp_path)
    try:
        copia_app = wt / "app.py"
        sentinela = f"# sentinela-isolamento-{os.getpid()}-{int(time.time())}"
        copia_app.write_text(copia_app.read_text() + sentinela + "\n")
        copia_txt = tmp_path / "copia.txt"
        r = subprocess.run(
            ["bash", str(wt / "tests" / "run_sh.sh"), "rapido"], cwd=str(wt),
            capture_output=True, text=True, timeout=900,
            env={**os.environ, "PATH": os.environ.get("PATH", ""),
                 "HOME": os.environ.get("HOME", "/tmp"), "TTS_SERIAL_ESPERA": "60",
                 "SUITES": str(wt / "tests" / "live_lock_freeze.sh"), "MORDIDA": "1",
                 "FREEZE_SONO": "0.15", "TETO_S": "300",
                 "SONDA_SAIDA": str(copia_txt), "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
        assert r.returncode == 0, r.stdout + r.stderr
        texto = copia_app.read_text()
        assert sentinela in texto, "a cópia foi sobrescrita (restore por `cp` de foto velha)"
        # fix = `await ws.send_json(...)` a 8 espaços, DENTRO do `if ocupado:` a 4
        assert re.search(r"^    if ocupado:\n(?:.*\n)*?        await ws\.send_json",
                         texto, re.M), "o bloco do `busy` ficou revertido na cópia"
        assert copia_txt.is_file(), "a suíte não registrou a cópia de trabalho"
        assert not copia_txt.read_text().strip().startswith(str(AQUI.parent)), \
            "a suíte mutou a árvore principal em vez da cópia"
        assert not Path(copia_txt.read_text().strip()).exists(), \
            "a cópia interna da suíte ficou para trás"
    finally:
        subprocess.run(["git", "-C", str(AQUI.parent), "worktree", "remove", "--force",
                        str(wt)], capture_output=True)

    # a árvore descartável tem de sumir — inclusive do registro do git (P3 do gate
    # #681: sem esta linha, desligar o `remove` deixava worktree vivo e a célula verde)
    assert not wt.exists(), f"a árvore descartável ficou em {wt}"
    registro = subprocess.run(["git", "-C", str(AQUI.parent), "worktree", "list",
                               "--porcelain"], capture_output=True, text=True).stdout
    assert f"worktree {wt}\n" not in registro, "o worktree ficou registrado no `.git`"
    assert _impressao(raiz_app) == antes, (
        "o `app.py` da ÁRVORE mudou durante a célula (sha/mtime) — se outro agente"
        " não estava editando, a guarda sujou a raiz")
    assert "sentinela-isolamento-" not in raiz_app.read_text(), \
        "o sentinela vazou para o `app.py` da árvore"


# --- #592: o teto pós-trava precisa VALER, dizer a verdade e não vazar ---------
def test_rotulo_do_estouro_e_trabalho_e_nao_fila(tmp_path):
    """O rótulo era decidido por "sou eu o pid da trava?"; com a trava mexida por
    concorrência invertia e a suíte que TRABALHAVA era acusada de estouro de fila.
    Aqui o dono da trava é a própria suíte (sonda imprime o `qual`)."""
    r = _rodar("rapido", env={"SUITES": str(SONDAS / "trava_lenta_rotulo.sh"),
                              "TETO_S": "2", "SONDA_SONO": "30",
                              "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
    assert r.returncode != 0, r.stdout
    assert "estourado DEPOIS da trava" in r.stdout, r.stdout
    assert "teto de FILA" not in r.stdout, r.stdout


def test_filho_surdo_a_term_ainda_morre_e_deixa_o_motivo(tmp_path):
    """O caso medido (828 s com teto de 90 s, log sem UMA linha): o bash adia o
    trap enquanto o filho em foreground vive, e o ALRM sozinho nunca vira
    mensagem. O vigia tem de imprimir o motivo E escalar até KILL — a asserção do
    PID morto é o que cobra a escada; sem ela a célula passaria pelo teto externo
    do runner mesmo com o filho vivo pendurado."""
    pid_txt = tmp_path / "filho.pid"
    t0 = time.time()
    r = _rodar("rapido", env={"SUITES": str(SONDAS / "trava_preso_em_filho.sh"),
                              "SONDA_SAIDA": str(pid_txt),
                              "TETO_S": "2", "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
    dur = time.time() - t0
    assert r.returncode != 0, r.stdout
    assert "estourado DEPOIS da trava" in r.stdout, r.stdout
    assert pid_txt.is_file(), "a sonda não registrou o PID do filho"
    pid = int(pid_txt.read_text().strip())
    for _ in range(20):
        if subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0:
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"o filho surdo {pid} sobreviveu à suíte")
    # teto de 2 s + escada (~6 s): muito abaixo do teto externo (90 s), então a
    # morte não veio de fora
    assert dur < 40, f"a rodada levou {dur:.1f}s — quem matou foi o teto externo"


def test_suite_que_sobrescreve_o_trap_exit_limpa_o_vigia(tmp_path):
    """Trap de EXIT sobrescrito sem `_teto_matar` deixa o VIGIA vivo depois do
    shell: ele manda ALRM num PID que pode ter sido reusado (outra suíte morreria
    por teto alheio). A sonda arma o vigia, sobrescreve o trap, chama o limpa e
    devolve o PID — que tem de estar morto depois."""
    saida = tmp_path / "vigia.txt"
    r = _rodar("rapido", env={"SUITES": str(SONDAS / "vigia_limpo.sh"),
                              "SONDA_SAIDA": str(saida),
                              "TETO_S": "30", "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
    assert r.returncode == 0, r.stdout + r.stderr
    pid = int(saida.read_text().strip().split("=")[1])
    for _ in range(15):
        if subprocess.run(["kill", "-0", str(pid)],
                          capture_output=True).returncode != 0:
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"vigia {pid} sobreviveu à suíte")


def test_toda_suite_com_trava_que_troca_o_trap_exit_chama_o_limpa_vigia():
    # `serial_lock.sh` fica de fora: é o teste da PRÓPRIA trava, o `source` e o
    # `serial_pega` dele vivem no worker (heredoc) — o shell da suíte nem arma vigia.
    suites = [p.name for p in AQUI.glob("*.sh")
              if p.name not in AJUDANTES | {"serial_lock.sh"}]
    for nome in suites:
        fonte = (AQUI / nome).read_text()
        # só quem ARMA o vigia: quem apenas escreve um worker que usa a trava
        # (o `serial_lock.sh`) não tem `_teto_matar` no próprio shell.
        if not re.search(r"^\s*source .*serial\.sh", fonte, re.M):
            continue
        if not re.search(r"^trap .*EXIT", fonte, re.M):
            continue
        assert "_teto_matar" in fonte, f"{nome} troca o trap EXIT e não chama `_teto_matar`"


def test_teto_da_suite_casa_pelo_NOME_e_o_operador_vence():
    """90 s é o default do HOOK: suíte de UI/Live passa disso trabalhando.

    A tabela já veio indexada com `.sh` enquanto o chamador passa o nome SEM
    extensão — ficou INERTA e `live_ui` morria aos 90 s com 300 na tabela. A célula
    cobra o teto EFETIVO (subcomando `teto`), não o texto da tabela."""
    r = _rodar("teto", "live_ui", "admin_ui_flow", "live_barge_rep", "trava_lenta")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "live_ui 300" in r.stdout, r.stdout
    assert "admin_ui_flow 600" in r.stdout and "live_barge_rep 600" in r.stdout, r.stdout
    assert "trava_lenta 90" in r.stdout, "nome fora da tabela tem de cair no default"
    # caminho absoluto COM extensão também casa: o nome é normalizado
    r = _rodar("teto", "/qualquer/live_ws.sh")
    assert "live_ws.sh 300" in r.stdout, r.stdout
    # `TETO_S=…` do operador vence a tabela
    r = _rodar("teto", "live_ui", env={"TETO_S": "7"})
    assert "live_ui 7" in r.stdout, r.stdout


def test_suite_verde_sem_teto_sai_zero(tmp_path):
    """`_teto_matar` tem de terminar em `return 0`: sem o vigia armado as guardas
    `[ -n "" ] && …` voltavam 1 e o trap de EXIT devolvia 1 — suíte verde com
    "✔ OK" saía rc=1 (`./tests/voz_check_ui.sh` à mão, medido pelo PM)."""
    r = subprocess.run(["bash", str(SONDAS / "verde_sem_teto.sh")],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
                            "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "trabalhei e passei" in r.stdout
    # e pelo runner (com o teto exportado) também
    r = _rodar("rapido", env={"SUITES": str(SONDAS / "verde_sem_teto.sh"),
                              "TTS_SERIAL_LOCK": str(tmp_path / "lock2")})
    assert r.returncode == 0, r.stdout + r.stderr


def test_doc_lista_exatamente_o_que_o_runner_tem():
    """O doc já ficou para trás (mandava `admin_ui_flow`/`cfg_sujo_e_erros` para
    `instancia`, onde elas nem rodam mais, e falava de "11 vermelhos" com 9 nas
    listas). As listas do doc passam a ser DADO do runner."""
    doc = DOC.read_text()
    secao = doc.split("## O que cada estágio tem hoje", 1)[1].split("\n## ", 1)[0]
    no_doc: dict[str, set[str]] = {}
    atual = None
    for linha in secao.splitlines():
        m = re.match(r"- `([a-z]+)`:\s*(.*)", linha)
        if m:
            atual = m.group(1)
            no_doc[atual] = set(re.findall(r"`([a-z0-9_]+)`", m.group(2)))
        elif atual and linha.startswith("  "):
            no_doc[atual] |= set(re.findall(r"`([a-z0-9_]+)`", linha))
        elif not linha.strip():
            atual = None
    # só nomes de SUÍTE contam: o texto do bullet cita símbolo à vontade
    suites = {n for nomes in _estagios().values() for n in nomes}
    for estagio, nomes in _estagios().items():
        chave = estagio.lower()
        no_doc[chave] &= suites
        assert chave in no_doc, f"o doc não lista o estágio {chave}"
        assert no_doc[chave] == set(nomes), (
            f"doc x runner em {chave}: só no doc {sorted(no_doc[chave] - set(nomes))}; "
            f"só no runner {sorted(set(nomes) - no_doc[chave])}")
    # a contagem do texto tem de bater com o tamanho do estágio de app de pé
    m = re.search(r"em vez de produzir (\w+) vermelhos", doc)
    assert m, "o texto perdeu a frase dos vermelhos mentirosos"
    palavras = {"sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11}
    assert palavras.get(m.group(1)) == len(_estagios()["INSTANCIA"]), (
        f"o texto fala de {m.group(1)} vermelhos e `instancia` tem "
        f"{len(_estagios()['INSTANCIA'])} suítes")


def test_sigint_no_harness_nao_deixa_servidor(tmp_path):
    """Interromper o smoke do Live não pode deixar o uvicorn dele para trás (#688).

    O harness sobe o servidor com grupo próprio; sem trap, um `timeout` (SIGTERM)
    matava só o script e o servidor ficava órfão — 32 achados com ppid 1 e 808 MB.
    SIGTERM é o caso que vazou: o SIGINT já virá como KeyboardInterrupt e passa
    pela limpeza normal, o SIGTERM não levanta nada. O teste interrompe com TERM e
    cobra o grupo morto e o pidfile removido."""
    import signal as _signal

    # o pidfile é nomeado pelo PID do PYTHON filho, não do bash: o jeito imune a
    # rodada vizinha é a DIFERENÇA de conjunto (quais existiam antes × depois)
    antes_pidfiles = set(Path("/tmp").glob("live_ws_servidor_*.pgid"))
    # `TTS_SERIAL=0`: a célula só quer o trap. Com a trava, o harness pode ficar
    # esperando outra suíte (espera padrão 1800 s) e não chega a subir o servidor
    # dentro da janela daqui — foi assim que falhou na suíte cheia.
    proc = subprocess.Popen([str(AQUI / "live_ws.sh")], cwd=str(AQUI.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            start_new_session=True,
                            env={**os.environ, "TTS_SERIAL": "0"})
    pidfile = None
    try:
        # pidfile pelo PID DO HARNESS que subimos (`_{pid}.pgid`), não pelo mais
        # recente de /tmp: com outra rodada viva ao lado, o "mais novo" é do outro
        for _ in range(240):                  # até 60 s para o servidor subir
            achados = list(set(Path("/tmp").glob("live_ws_servidor_*.pgid")) - antes_pidfiles)
            if achados:
                pidfile = achados[0]
                break
            assert proc.poll() is None, "o harness saiu antes de subir o servidor"
            time.sleep(0.25)
        assert pidfile is not None, "o harness não registrou o grupo do servidor"
        pgid = int(pidfile.read_text().strip())
        proc.send_signal(_signal.SIGTERM)
        proc.wait(timeout=90)
        for _ in range(120):
            try:
                os.killpg(pgid, 0)
            except OSError:
                break
            time.sleep(0.25)
        else:
            assert False, f"o servidor do harness sobreviveu ao SIGTERM (grupo {pgid})"
        assert not pidfile.exists(), "o pidfile do grupo ficou para trás"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        if pidfile is not None and pidfile.exists():
            try:
                os.killpg(int(pidfile.read_text().strip()), _signal.SIGKILL)
            except Exception:                 # noqa: BLE001
                pass
            pidfile.unlink(missing_ok=True)


def test_mata_orfaos_do_harness_sob_demanda(tmp_path):
    """Um SIGKILL no harness não roda trap: a sobra tem de morrer sob demanda (#688).

    O harness deixa o pidfile do grupo em `/tmp`; sem um modo avulso, um órfão
    sobrevive até alguém rodar a suíte de novo (40 h medidos)."""
    proc = subprocess.Popen(["sleep", "120"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    # o DONO no nome do pidfile tem de estar MORTO: é o que distingue órfão de
    # rodada vizinha (o filtro do 26b6d1a). PID alto e inexistente serve.
    pidfile = Path("/tmp") / "live_ws_servidor_99999_999999.pgid"
    pidfile.write_text(str(pgid))
    try:
        r = subprocess.run([str(AQUI / "live_ws.sh"), "--mata-orfaos"],
                           cwd=str(AQUI.parent), capture_output=True, text=True, timeout=120,
                           env={**os.environ, "TTS_SERIAL": "0"})
        assert r.returncode == 0, r.stdout + r.stderr
        assert "1 servidor(es) órfão(s)" in r.stdout, r.stdout
        for _ in range(40):
            try:
                os.killpg(pgid, 0)
            except OSError:
                break
            time.sleep(0.25)
        else:
            assert False, "o grupo órfão sobreviveu"
        assert not pidfile.exists(), "o pidfile do órfão ficou"
    finally:
        if proc.poll() is None:
            proc.kill()
        pidfile.unlink(missing_ok=True)


def test_varredura_de_orfao_nao_mata_rodada_viva(tmp_path):
    """A varredura de servidores órfãos não pode derrubar quem está rodando (#688).

    O harness varre `/tmp/live_ws_servidor_*.pgid` ao subir. Sem filtro, quem começa
    mata o servidor de outra rodada — e numa máquina com vários worktrees isso é o
    caso comum. O nome carrega o PID do dono: dono VIVO é rodada concorrente, dono
    morto é órfão de verdade."""
    import signal as _signal

    fonte = (AQUI / "live_ws.sh").read_text()
    ini = fonte.index("def mata_servidores_antigos")
    fim = fonte.index("def instala_traps")
    ns = {"os": os, "signal": _signal, "pathlib": pathlib}

    def _grupo(com_dono_vivo: bool) -> tuple[int, int]:
        dono = subprocess.Popen(["sleep", "60"], start_new_session=True)
        pgid = os.getpgid(dono.pid)
        pidfile = Path("/tmp") / f"live_ws_servidor_9_{dono.pid}.pgid"
        pidfile.write_text(str(pgid))
        ns.setdefault("_limpar", []).append(pidfile)
        if not com_dono_vivo:
            dono.kill()
            dono.wait()
        return dono.pid, pgid

    import atexit
    atexit.register(lambda: [p.unlink(missing_ok=True) for p in ns.get("_limpar", [])])

    _, vivo = _grupo(True)
    _, morto = _grupo(False)
    try:
        exec(fonte[ini:fim], ns)
        ns["mata_servidores_antigos"]()
        assert subprocess.run(["kill", "-0", str(vivo)]).returncode == 0, \
            "a varredura matou o servidor de uma rodada VIVA"
        assert subprocess.run(["kill", "-0", str(morto)]).returncode != 0, \
            "a varredura não matou o órfão"
    finally:
        for sinal in (vivo, morto):
            try:
                os.killpg(sinal, _signal.SIGTERM)
            except OSError:
                pass


def test_freeze_nao_liga_o_voices_real_e_duas_rodadas_seguidas(tmp_path):
    """`voices` é estado do DONO: a cópia do freeze não pode ligá-lo (#703).

    Com o link, o servidor da medida lia o diretório real e — pior — o `ln -s` sem
    `-n` seguia um `voices` que já fosse link e criava `voices/voices` DENTRO do
    dado do dono (aconteceu duas vezes). Aqui: duas rodadas seguidas, nenhum
    symlink dentro do `voices` da cópia e a listagem real intacta por fora."""
    real = AQUI.parent / "voices"
    antes = sorted(p.name for p in real.iterdir()) if real.is_dir() else []
    for rodada in (1, 2):
        copia_txt = tmp_path / f"copia{rodada}.txt"
        r = _rodar("rapido", env={
            "SUITES": str(AQUI / "live_lock_freeze.sh"), "MORDIDA": "1",
            "FREEZE_SONO": "0.15", "TETO_S": "300", "SONDA_SAIDA": str(copia_txt),
            "TTS_SERIAL_LOCK": str(tmp_path / "lock"), "TTS_SERIAL_ESPERA": "60",
            "FREEZE_MANTER": "1"})
        assert r.returncode == 0, r.stdout + r.stderr
        copia = Path(copia_txt.read_text().strip())
        tmp_path.joinpath(f"copia{rodada}.dir").write_text(str(copia))
        voz = copia / "voices"
        assert voz.is_dir() and not voz.is_symlink(), f"rodada {rodada}: voices é LINK"
        assert (voz / "voz.wav").is_file(), f"rodada {rodada}: a fixture não foi para a cópia"
        ligados = [str(p) for p in voz.rglob("*") if p.is_symlink()]
        assert not ligados, f"rodada {rodada}: symlink dentro do voices da cópia: {ligados}"
        depois = sorted(p.name for p in real.iterdir()) if real.is_dir() else []
        assert depois == antes, f"rodada {rodada}: o voices REAL mudou ({set(depois) ^ set(antes)})"
        shutil.rmtree(copia, ignore_errors=True)   # FREEZE_MANTER: a limpeza é minha


@pytest.fixture
def settings_na_raiz():
    """Garante um `settings.json` na raiz — worktree limpo não tem o do dono (#710).

    Devolve o caminho e o estado inicial. Se o arquivo já existir (árvore principal,
    ou worktree com estado) ele NÃO é tocado e não é removido no fim; se faltar, é
    criado sintético e apagado depois — a célula nunca depende da máquina."""
    real = AQUI.parent / "settings.json"
    criado = not real.is_file()
    if criado:
        real.write_text('{"model": "sintetico", "pre_prompt": ""}')
    try:
        yield real, (real.read_bytes(), real.stat().st_mtime_ns)
    finally:
        if criado:
            real.unlink(missing_ok=True)


def test_sonda_sem_dono_dir_nao_escreve_na_raiz(tmp_path, settings_na_raiz):
    """Sem `TTS_DONO_DIR` o runner dá uma CÓPIA à sonda; a raiz fica intocada (#710).

    O fallback da sonda era `$AQUI/../../..` = a raiz do repo — e na ÁRVORE
    PRINCIPAL a raiz é o `settings.json` do DONO: a sonda gravava `{}` por cima
    (3 bytes, 3 vezes em 31/10). Aqui: rodada sem a env, raiz com sha E mtime
    iguais, e o runner ainda tem de NOMEAR a escrita (na cópia)."""
    real, antes = settings_na_raiz
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh"),
                                 "TTS_DONO_DIR": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    depois = (real.read_bytes(), real.stat().st_mtime_ns)
    assert depois == antes, "a sonda escreveu no settings.json da RAIZ"
    assert real.read_bytes() != b"{}\n", "a raiz ficou com o conteúdo da sonda"
    assert "suja_o_dono.sh escreveu settings.json" in r.stdout, r.stdout
    # e a sonda SOZINHA, sem a env, tem de RECUSAR (nada de fallback para a raiz)
    env = {k: v for k, v in os.environ.items() if k != "TTS_DONO_DIR"}
    d = subprocess.run(["bash", str(SONDAS / "suja_o_dono.sh")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert d.returncode == 2, (d.returncode, d.stdout, d.stderr)
    assert "TTS_DONO_DIR" in d.stderr, d.stderr


def test_segredo_do_dono_deixado_em_tmp_derruba_a_rodada(tmp_path):
    """Suíte que deixa a chave do dono em `/tmp` tem de ser acusada (#723).

    O app semeia a chave do ambiente (`TTS_ROD_API_KEY`) e a PERSISTE no arquivo de
    chaves da rodada, então o segredo real ia parar em `/tmp` — 191 arquivos, e as
    suítes não limpavam. A guarda roda no fim do estágio e nomeia o arquivo."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "vaza_segredo.sh"),
                                 "TTS_ROD_API_KEY": "segredo-de-teste-123456"})
    # a guarda tem de NOMEAR o arquivo (é o núcleo); a agregação em falha é do
    # runner e está coberta pelos vizinhos
    assert "segredo do dono em" in r.stdout, r.stdout
    assert "vaza_segredo_" in r.stdout, r.stdout
    assert "segredo-em-tmp" in r.stdout, r.stdout


def test_runner_nao_abre_arquivo_do_dono_para_a_guarda_de_segredo(tmp_path):
    """A agulha da guarda é SINTÉTICA: sem chave no ambiente, o runner inventa o canário.

    Ler `.apikey`/`.apikeys.json` da raiz puxava o segredo para o shell e fazia a
    guarda depender de estado do dono (#723/task_57cef75c). Rodada sem
    `TTS_ROD_API_KEY`: o que circula tem de ser o canário, e o valor não pode sair
    impresso. O `.apikey` plantado em `tmp_path` é INERTE de propósito (o runner roda
    do repo, não daquela árvore): ele marca que nada da raiz entrou no arquivo, e a
    asserção de ausência prova isso."""
    arvore = tmp_path / "arvore"
    (arvore / "tests" / "fixtures" / "run_sh").mkdir(parents=True)
    # nome por `mktemp`: o PID do pytest pode se repetir entre rodadas e o arquivo
    # já existiria no retrato do início (a guarda compara conjuntos)
    (arvore / "tests" / "fixtures" / "run_sh" / "sonda.sh").write_text(
        '#!/usr/bin/env bash\n'
        'A="$(mktemp /tmp/canario_sonda_XXXXXXXX)"\n'
        'printf \'{"k":"%s"}\' "$TTS_ROD_API_KEY" > "$A"\n'
        'printf %s "$A" > "$TMP_OUT"\n')
    # isca INERTE: nada lê este arquivo; serve só para provar que ele não vaza
    dono_antigo = tmp_path / "dono" / ".apikey"
    dono_antigo.parent.mkdir()
    dono_antigo.write_text("chave-velha-do-dono-nao-circula")
    ponteiro = tmp_path / "caminho.txt"
    saida = None
    try:
        r = subprocess.run(
            ["bash", str(RUNNER), "navegador"],
            cwd=str(AQUI.parent), capture_output=True, text=True, timeout=120,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
                 "TTS_METRICS_DIR": "", "TMP_OUT": str(ponteiro), "TTS_SERIAL_ESPERA": "60",
                 "SUITES": str(arvore / "tests" / "fixtures" / "run_sh" / "sonda.sh")})
        assert ponteiro.is_file(), r.stdout + r.stderr
        saida = Path(ponteiro.read_text().strip())
        assert saida.is_file(), r.stdout + r.stderr
        conteudo = saida.read_text()
        assert "canario-da-rodada-" in conteudo, conteudo
        assert "chave-velha-do-dono" not in conteudo, conteudo
        # canário é sintético: informa (nota), não reprova
        assert "segredo SINTÉTICO" in r.stdout, r.stdout
        assert r.returncode == 0, r.stdout
        assert (saida.stat().st_mode & 0o777) == 0o600, oct(saida.stat().st_mode)
    finally:
        if saida is not None:
            saida.unlink(missing_ok=True)


def test_cp_p_por_cima_de_homonimo_e_acusado(tmp_path):
    """O caso que o P2 apontou: `cp -p` sobre arquivo que JÁ estava no retrato (#723).

    mtime preservado e nome conhecido — a comparação por conjunto ou por mtime não
    vê nada. Só o retrato por (caminho, HASH) acusa."""
    alvo = Path("/tmp") / f"homonimo_{os.getpid()}.json"
    fonte = tmp_path / "origem.json"
    alvo.write_text('{"k":"velho"}\n')
    try:
        r = _rodar("navegador", env={
            "SUITES": str(SONDAS / "cp_p_segredo.sh"),
            "TTS_ROD_API_KEY": "segredo-de-teste-123456",
            "ALVO": str(alvo), "FONTE": str(fonte)})
        assert r.returncode == 1, r.stdout + r.stderr
        assert "segredo do dono em" in r.stdout, r.stdout
        assert alvo.name in r.stdout, r.stdout
        assert "segredo-de-teste-123456" not in r.stdout, "imprimiu o valor"
    finally:
        alvo.unlink(missing_ok=True)
