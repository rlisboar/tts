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
import subprocess
import threading
import time
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
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
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
    """Diretório de mentira com o MESMO settings.json do dono (cópia)."""
    dono = tmp_path / "dono"
    dono.mkdir()
    (dono / "settings.json").write_text((AQUI.parent / "settings.json").read_text())
    return str(dono)


def test_runner_nomeia_a_suite_que_escreveu_no_estado_do_dono(tmp_path):
    """Sem atribuição, "o settings mudou" é fato sem autor — foi o que travou a
    investigação de 01:24. A sonda reescreve o `settings.json` do dono de mentira."""
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path)})
    assert "estado REAL do dono" in r.stdout, r.stdout
    assert "suja_o_dono.sh escreveu settings.json" in r.stdout, r.stdout
    # sem o knob é AVISO: a suíte em si passou
    assert r.returncode == 0, r.stdout


def test_knob_exige_dono_intocado_vira_falha(tmp_path):
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "suja_o_dono.sh"),
                                 "TTS_DONO_DIR": _dono(tmp_path),
                                 "TTS_EXIGE_DONO_INTOCADO": "1"})
    assert r.returncode == 1, r.stdout
    assert "TTS_EXIGE_DONO_INTOCADO=1" in r.stdout, r.stdout


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
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_import_app.sh"),
                                 "SONDA_MODO": "env", "SONDA_SAIDA": str(saida),
                                 "SONDA_ISO": str(tmp_path)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "estado lido do isolamento" in saida.read_text()


def test_o_defeito_do_atributo_ainda_morde(tmp_path):
    """Controle da célula acima: do jeito antigo (patch por atributo DEPOIS do
    import) o processo nasce com o estado do DONO na RAM — a sonda não passa por
    acidente. Desde o #601 o import não grava, então o sintoma é a LEITURA."""
    saida = tmp_path / "veredito.txt"
    r = _rodar("navegador", env={"SUITES": str(SONDAS / "sonda_import_app.sh"),
                                 "SONDA_MODO": "atributo", "SONDA_SAIDA": str(saida),
                                 "SONDA_ISO": str(tmp_path)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "estado do DONO entrou na RAM" in saida.read_text()



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
    mensagem. O vigia tem de imprimir o motivo e escalar até KILL."""
    r = _rodar("rapido", env={"SUITES": str(SONDAS / "trava_preso_em_filho.sh"),
                              "TETO_S": "2", "TTS_SERIAL_LOCK": str(tmp_path / "lock")})
    assert r.returncode != 0, r.stdout
    assert "estourado DEPOIS da trava" in r.stdout, r.stdout


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
